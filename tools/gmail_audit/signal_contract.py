"""Canonical unified signal contract for Gmail/Drive ingress normalization."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from signal_types import SIGNAL_SCHEMA_VERSION


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(*parts: Any, prefix: str = "") -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(_stable_json(part).encode("utf-8"))
        digest.update(b"|")
    return f"{prefix}{digest.hexdigest()[:24]}"


def canonicalize_source_ref(source_ref: dict[str, Any]) -> dict[str, Any]:
    """Return a stable, JSON-safe source reference envelope."""
    if not isinstance(source_ref, dict):
        return {}
    return json.loads(_stable_json(source_ref))


def build_content_hash(payload: dict[str, Any], artifacts: dict[str, Any]) -> str:
    return _stable_hash(payload, artifacts, prefix="sha_")


def build_idempotency_key(
    *,
    source_kind: str,
    signal_kind: str,
    source_ref: dict[str, Any],
    revision_marker: str = "",
) -> str:
    return _stable_hash(
        {
            "source_kind": source_kind,
            "signal_kind": signal_kind,
            "source_ref": canonicalize_source_ref(source_ref),
            "revision_marker": revision_marker,
        },
        prefix="idem_",
    )


@dataclass(slots=True, frozen=True)
class SignalSourceRef:
    """Stable source identity envelope used across ingress adapters."""

    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return canonicalize_source_ref(self.fields)


@dataclass(slots=True, frozen=True)
class SignalIngestMetadata:
    """Operational metadata required for replay, audit, and ownership tracing."""

    observed_at: str
    effective_at: str | None = None
    replayable: bool = True
    created_by_runtime: str = ""
    processing_state: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class SignalProcessingStatus:
    """Processing envelope captured alongside the canonical signal."""

    state: str = "pending"
    note: str = ""
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class CanonicalSignal:
    """Canonical append-only signal shared by Gmail and Drive ingress paths."""

    signal_id: str
    schema_version: str
    signal_kind: str
    source_kind: str
    source_ref: dict[str, Any]
    observed_at: str
    effective_at: str | None
    case_key_hint: str | None
    thread_key_hint: str | None
    business_lane: str | None
    signal_summary_pl: str
    payload: dict[str, Any]
    artifacts: dict[str, Any]
    processing_state: str
    idempotency_key: str
    content_hash: str | None
    replayable: bool
    created_by_runtime: str
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _stable_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "CanonicalSignal":
        data = dict(payload or {})
        return cls(
            signal_id=str(data.get("signal_id") or ""),
            schema_version=str(data.get("schema_version") or SIGNAL_SCHEMA_VERSION),
            signal_kind=str(data.get("signal_kind") or ""),
            source_kind=str(data.get("source_kind") or ""),
            source_ref=canonicalize_source_ref(data.get("source_ref") or data.get("source_ref_json") or {}),
            observed_at=str(data.get("observed_at") or ""),
            effective_at=str(data.get("effective_at") or "") or None,
            case_key_hint=str(data.get("case_key_hint") or "") or None,
            thread_key_hint=str(data.get("thread_key_hint") or "") or None,
            business_lane=str(data.get("business_lane") or "") or None,
            signal_summary_pl=str(data.get("signal_summary_pl") or ""),
            payload=dict(data.get("payload") or data.get("payload_json") or {}),
            artifacts=dict(data.get("artifacts") or data.get("artifacts_json") or {}),
            processing_state=str(data.get("processing_state") or "pending"),
            idempotency_key=str(data.get("idempotency_key") or ""),
            content_hash=str(data.get("content_hash") or "") or None,
            replayable=bool(data.get("replayable", True)),
            created_by_runtime=str(data.get("created_by_runtime") or ""),
            created_at=str(data.get("created_at") or datetime.now().astimezone().isoformat()),
        )


def build_signal_id(
    *,
    schema_version: str,
    signal_kind: str,
    source_kind: str,
    idempotency_key: str,
) -> str:
    return _stable_hash(
        {
            "schema_version": schema_version,
            "signal_kind": signal_kind,
            "source_kind": source_kind,
            "idempotency_key": idempotency_key,
        },
        prefix="sig_",
    )


def build_canonical_signal(
    *,
    signal_kind: str,
    source_kind: str,
    source_ref: dict[str, Any],
    observed_at: str,
    signal_summary_pl: str,
    payload: dict[str, Any],
    artifacts: dict[str, Any] | None = None,
    effective_at: str | None = None,
    case_key_hint: str | None = None,
    thread_key_hint: str | None = None,
    business_lane: str | None = None,
    revision_marker: str = "",
    replayable: bool = True,
    created_by_runtime: str = "",
    processing_state: str = "pending",
    schema_version: str = SIGNAL_SCHEMA_VERSION,
) -> CanonicalSignal:
    normalized_source_ref = canonicalize_source_ref(source_ref)
    normalized_artifacts = dict(artifacts or {})
    idempotency_key = build_idempotency_key(
        source_kind=source_kind,
        signal_kind=signal_kind,
        source_ref=normalized_source_ref,
        revision_marker=revision_marker,
    )
    content_hash = build_content_hash(payload, normalized_artifacts)
    return CanonicalSignal(
        signal_id=build_signal_id(
            schema_version=schema_version,
            signal_kind=signal_kind,
            source_kind=source_kind,
            idempotency_key=idempotency_key,
        ),
        schema_version=schema_version,
        signal_kind=signal_kind,
        source_kind=source_kind,
        source_ref=normalized_source_ref,
        observed_at=observed_at,
        effective_at=effective_at,
        case_key_hint=case_key_hint,
        thread_key_hint=thread_key_hint,
        business_lane=business_lane,
        signal_summary_pl=signal_summary_pl,
        payload=dict(payload or {}),
        artifacts=normalized_artifacts,
        processing_state=processing_state,
        idempotency_key=idempotency_key,
        content_hash=content_hash,
        replayable=replayable,
        created_by_runtime=created_by_runtime,
    )


__all__ = [
    "CanonicalSignal",
    "SignalIngestMetadata",
    "SignalProcessingStatus",
    "SignalSourceRef",
    "build_canonical_signal",
    "build_content_hash",
    "build_idempotency_key",
    "build_signal_id",
    "canonicalize_source_ref",
]
