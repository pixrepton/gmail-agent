"""Canonical raw-observation contract for append-only ingress journaling."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from signal_contract import canonicalize_source_ref


RAW_OBSERVATION_SCHEMA_VERSION = "1.0"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(*parts: Any, prefix: str = "") -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(_stable_json(part).encode("utf-8"))
        digest.update(b"|")
    return f"{prefix}{digest.hexdigest()[:24]}"


def build_source_fingerprint(
    *,
    source_kind: str,
    observation_kind: str,
    source_ref: dict[str, Any],
    source_marker: str = "",
) -> str:
    return _stable_hash(
        {
            "source_kind": source_kind,
            "observation_kind": observation_kind,
            "source_ref": canonicalize_source_ref(source_ref),
            "source_marker": str(source_marker or ""),
        },
        prefix="obsfp_",
    )


def build_payload_hash(payload: dict[str, Any]) -> str:
    return _stable_hash(payload, prefix="obspay_")


def build_observation_id(
    *,
    schema_version: str,
    source_kind: str,
    observation_kind: str,
    source_fingerprint: str,
) -> str:
    return _stable_hash(
        {
            "schema_version": schema_version,
            "source_kind": source_kind,
            "observation_kind": observation_kind,
            "source_fingerprint": source_fingerprint,
        },
        prefix="obs_",
    )


@dataclass(slots=True, frozen=True)
class RawObservation:
    """Append-only raw source event recorded before heavy reasoning."""

    observation_id: str
    schema_version: str
    observation_kind: str
    source_kind: str
    source_ref: dict[str, Any]
    occurred_at: str | None
    observed_at: str
    source_fingerprint: str
    payload_hash: str
    payload: dict[str, Any]
    created_by_runtime: str
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return _stable_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RawObservation":
        data = dict(payload or {})
        return cls(
            observation_id=str(data.get("observation_id") or ""),
            schema_version=str(data.get("schema_version") or RAW_OBSERVATION_SCHEMA_VERSION),
            observation_kind=str(data.get("observation_kind") or ""),
            source_kind=str(data.get("source_kind") or ""),
            source_ref=canonicalize_source_ref(data.get("source_ref") or data.get("source_ref_json") or {}),
            occurred_at=str(data.get("occurred_at") or "") or None,
            observed_at=str(data.get("observed_at") or ""),
            source_fingerprint=str(data.get("source_fingerprint") or ""),
            payload_hash=str(data.get("payload_hash") or ""),
            payload=dict(data.get("payload") or data.get("payload_json") or {}),
            created_by_runtime=str(data.get("created_by_runtime") or ""),
            created_at=str(data.get("created_at") or datetime.now().astimezone().isoformat()),
        )


def build_raw_observation(
    *,
    observation_kind: str,
    source_kind: str,
    source_ref: dict[str, Any],
    observed_at: str,
    payload: dict[str, Any],
    occurred_at: str | None = None,
    source_marker: str = "",
    created_by_runtime: str = "",
    schema_version: str = RAW_OBSERVATION_SCHEMA_VERSION,
) -> RawObservation:
    normalized_source_ref = canonicalize_source_ref(source_ref)
    source_fingerprint = build_source_fingerprint(
        source_kind=source_kind,
        observation_kind=observation_kind,
        source_ref=normalized_source_ref,
        source_marker=source_marker,
    )
    return RawObservation(
        observation_id=build_observation_id(
            schema_version=schema_version,
            source_kind=source_kind,
            observation_kind=observation_kind,
            source_fingerprint=source_fingerprint,
        ),
        schema_version=schema_version,
        observation_kind=observation_kind,
        source_kind=source_kind,
        source_ref=normalized_source_ref,
        occurred_at=occurred_at,
        observed_at=observed_at,
        source_fingerprint=source_fingerprint,
        payload_hash=build_payload_hash(payload),
        payload=dict(payload or {}),
        created_by_runtime=created_by_runtime,
    )


__all__ = [
    "RAW_OBSERVATION_SCHEMA_VERSION",
    "RawObservation",
    "build_observation_id",
    "build_payload_hash",
    "build_raw_observation",
    "build_source_fingerprint",
]
