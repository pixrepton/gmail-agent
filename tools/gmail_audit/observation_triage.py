"""Formal cheap-triage layer over raw observations before canonical signals."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from preclassifier import preclassify_snapshot
from raw_observation_contract import RawObservation
from redaction import sanitize_for_storage


TRIAGE_SCHEMA_VERSION = "1.0"


@dataclass(slots=True, frozen=True)
class ObservationTriageResult:
    triage_id: str
    schema_version: str
    observation_id: str
    source_kind: str
    triage_class: str
    routing_decision: str
    reason_codes: list[str] = field(default_factory=list)
    reasoning_budget: dict[str, Any] = field(default_factory=dict)
    batching: dict[str, Any] = field(default_factory=dict)
    preclassification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def triage_gmail_observation(observation: RawObservation) -> dict[str, Any]:
    snapshot = dict((observation.payload or {}).get("snapshot") or {})
    preclassification = sanitize_for_storage(preclassify_snapshot(snapshot))
    lane = str(preclassification.get("lane") or "intake_llm")
    confidence = float(preclassification.get("confidence") or 0.0)

    if lane == "skip":
        triage_class = "ignore"
        routing_decision = "skip_heavy_reasoning"
        reason_codes = list(preclassification.get("reasons") or ["obvious_noise"])
        budget = _reasoning_budget("skip", max_context_messages=0, max_cold_fetches=0)
    elif lane == "reference_only":
        triage_class = "reference_only"
        routing_decision = "skip_heavy_reasoning"
        reason_codes = list(preclassification.get("reasons") or ["reference_only_signal"])
        budget = _reasoning_budget("skip", max_context_messages=1, max_cold_fetches=0)
    elif lane == "review_direct":
        triage_class = "needs_operator_review"
        routing_decision = "route_to_review"
        reason_codes = list(preclassification.get("reasons") or ["review_direct_signal"])
        budget = _reasoning_budget("skip", max_context_messages=1, max_cold_fetches=0)
    else:
        triage_class = "business_signal"
        routing_decision = "promote_to_reasoning"
        reason_codes = list(preclassification.get("reasons") or ["default_intake_lane"])
        budget = _reasoning_budget("standard", max_context_messages=2, max_cold_fetches=2)

    triage = ObservationTriageResult(
        triage_id=_stable_triage_id(observation.observation_id, triage_class, routing_decision, reason_codes),
        schema_version=TRIAGE_SCHEMA_VERSION,
        observation_id=observation.observation_id,
        source_kind=observation.source_kind,
        triage_class=triage_class,
        routing_decision=routing_decision,
        reason_codes=reason_codes,
        reasoning_budget=budget,
        batching={"enabled": False},
        preclassification={
            "lane": lane,
            "reasons": reason_codes,
            "confidence": confidence,
            "stage_name": str(preclassification.get("stage_name") or "preclassifier"),
        },
    )
    return triage.to_dict()


def triage_drive_observation(observation: RawObservation) -> dict[str, Any]:
    payload = dict(observation.payload or {})
    candidate = dict(payload.get("candidate") or {})
    removed = bool(payload.get("removed"))
    lane = str(candidate.get("lane") or "drive_signal")
    document_kind = str(candidate.get("document_kind") or "")
    scope = str(candidate.get("scope") or "")
    parent_drive_item_id = str(
        candidate.get("parent_drive_item_id")
        or observation.source_ref.get("parent_drive_item_id")
        or ""
    ).strip()
    folder_path = str(candidate.get("folder_path") or "").strip()
    probable_case_key = str(candidate.get("probable_case_key") or "").strip()

    batching = {"enabled": False}
    if removed:
        triage_class = "business_signal"
        routing_decision = "promote_to_reasoning"
        reason_codes = ["drive_removed_item"]
        budget = _reasoning_budget("thin", max_context_messages=0, max_cold_fetches=1)
        preclassification = {"lane": "drive_removed", "reasons": reason_codes, "confidence": 0.99}
    elif document_kind == "media_asset" and lane == "case_folder":
        batch_window_seconds = 180
        batch_group = _drive_media_batch_group(
            observed_at=observation.observed_at,
            parent_drive_item_id=parent_drive_item_id,
            folder_path=folder_path,
            probable_case_key=probable_case_key,
            window_seconds=batch_window_seconds,
        )
        triage_class = "business_signal"
        routing_decision = "promote_to_reasoning"
        reason_codes = ["drive_case_media_burst_candidate"]
        budget = _reasoning_budget("thin", max_context_messages=0, max_cold_fetches=1)
        batching = {
            "enabled": True,
            "window_seconds": batch_window_seconds,
            "group_key": batch_group,
            "signal_kind": "drive_media_batch_observed",
            "source_ref_override": {
                "batch_group": batch_group,
                "parent_drive_item_id": parent_drive_item_id,
                "folder_path": folder_path,
                "probable_case_key": probable_case_key,
            },
            "revision_marker": batch_group,
        }
        preclassification = {
            "lane": lane,
            "reasons": reason_codes,
            "confidence": float(candidate.get("classification_confidence") or 0.97),
        }
    elif scope == "company_reference" and document_kind not in {
        "contract",
        "order",
        "deposit_invoice",
        "invoice",
        "warranty_card",
        "service_protocol",
    }:
        triage_class = "reference_only"
        routing_decision = "skip_heavy_reasoning"
        reason_codes = ["drive_company_reference"]
        budget = _reasoning_budget("skip", max_context_messages=0, max_cold_fetches=0)
        preclassification = {
            "lane": lane or "reference_only",
            "reasons": reason_codes,
            "confidence": float(candidate.get("classification_confidence") or 0.8),
        }
    else:
        triage_class = "business_signal"
        routing_decision = "promote_to_reasoning"
        reason_codes = ["drive_business_document"]
        budget = _reasoning_budget("standard", max_context_messages=0, max_cold_fetches=2)
        preclassification = {
            "lane": lane or "drive_signal",
            "reasons": reason_codes,
            "confidence": float(candidate.get("classification_confidence") or 0.85),
        }

    triage = ObservationTriageResult(
        triage_id=_stable_triage_id(observation.observation_id, triage_class, routing_decision, reason_codes),
        schema_version=TRIAGE_SCHEMA_VERSION,
        observation_id=observation.observation_id,
        source_kind=observation.source_kind,
        triage_class=triage_class,
        routing_decision=routing_decision,
        reason_codes=reason_codes,
        reasoning_budget=budget,
        batching=batching,
        preclassification=preclassification,
    )
    return triage.to_dict()


def _reasoning_budget(reasoning_mode: str, *, max_context_messages: int, max_cold_fetches: int) -> dict[str, Any]:
    return {
        "reasoning_mode": reasoning_mode,
        "max_context_messages": max_context_messages,
        "max_cold_fetches": max_cold_fetches,
    }


def _drive_media_batch_group(
    *,
    observed_at: str,
    parent_drive_item_id: str,
    folder_path: str,
    probable_case_key: str,
    window_seconds: int,
) -> str:
    anchor = parent_drive_item_id or folder_path or probable_case_key or "drive-media"
    bucket = _time_bucket(observed_at, window_seconds)
    return f"drive-media:{anchor}:{bucket}"


def _time_bucket(observed_at: str, window_seconds: int) -> str:
    text = str(observed_at or "").strip()
    if not text:
        return "unknown"
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    bucket_epoch = int(dt.timestamp()) // max(1, int(window_seconds))
    return str(bucket_epoch)


def _stable_triage_id(observation_id: str, triage_class: str, routing_decision: str, reason_codes: list[str]) -> str:
    digest = hashlib.sha256(
        f"{observation_id}|{triage_class}|{routing_decision}|{'|'.join(reason_codes)}".encode("utf-8")
    ).hexdigest()
    return f"triage_{digest[:24]}"


__all__ = [
    "ObservationTriageResult",
    "TRIAGE_SCHEMA_VERSION",
    "triage_drive_observation",
    "triage_gmail_observation",
]
