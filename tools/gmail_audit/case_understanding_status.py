"""SLICE-2C — derive `case_understanding_status` from the Understanding provenance.

One derivation, one owner. Before this slice, "is our understanding of this case good enough?" was
re-answered by every consumer from `case_understanding_provenance` internals (`availability`,
`source_mode`, `validation_state`), with different rules each time.

Two properties this module protects:

* it derives, it does not invent. Absent or empty provenance yields ``None`` — never a fabricated
  `ok`, and never a fabricated `unavailable` either;
* the derived status has NO membership authority. `feed_visibility` neither imports nor reads it,
  and nothing here can hide a card or create one. See `CaseUnderstandingStatusV1`.

`validation_state="corrected"` is deliberately NOT `degraded`: `CaseUnderstandingProvenance`
records that a normalisation happened and explicitly refuses to rank how bad it was. Only a real
substitute (`source_mode="fallback"`) is reported as `degraded`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_REASONING_NOT_REQUIRED = "reasoning_not_required"

CASE_UNDERSTANDING_STATUSES = (
    STATUS_OK,
    STATUS_DEGRADED,
    STATUS_UNAVAILABLE,
    STATUS_REASONING_NOT_REQUIRED,
)

_REASONS_PL = {
    STATUS_OK: "Rozumienie sprawy aktualne",
    STATUS_DEGRADED: "Rozumienie zastępcze — traktuj ostrożnie",
    STATUS_UNAVAILABLE: "Rozumienie sprawy niedostępne",
    STATUS_REASONING_NOT_REQUIRED: "Rozumowanie niewymagane dla tej ścieżki",
}


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(observed_at: str, now_iso: str) -> int | None:
    observed = _parse_iso(observed_at)
    if observed is None:
        return None
    now = _parse_iso(now_iso) if now_iso else datetime.now(timezone.utc)
    if now is None:
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=now.tzinfo or timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=observed.tzinfo or timezone.utc)
    delta = int((now - observed).total_seconds())
    return delta if delta >= 0 else None


def build_case_understanding_status(
    provenance: dict[str, Any] | None,
    *,
    now_iso: str = "",
) -> dict[str, Any] | None:
    """Derive the SLICE-2C status projection, or ``None`` when provenance says nothing.

    Args:
        provenance: a `CaseUnderstandingProvenance`-shaped dict (or model dump).
        now_iso: reference timestamp for `age_seconds`; defaults to now. Passed explicitly by
            tests so the derivation stays deterministic.
    """
    prov = provenance if isinstance(provenance, dict) else {}
    availability = str(prov.get("availability") or "").strip().lower()
    if not availability:
        return None

    source_mode = str(prov.get("source_mode") or "").strip().lower()
    validation_state = str(prov.get("validation_state") or "").strip().lower()
    reason_codes = [str(code)[:80] for code in (prov.get("reason_codes") or [])][:6]

    if availability == "not_required" or source_mode == "skipped_for_lane":
        status = STATUS_REASONING_NOT_REQUIRED
    elif availability == "unavailable":
        status = STATUS_UNAVAILABLE
    elif availability == "available":
        status = STATUS_DEGRADED if source_mode == "fallback" else STATUS_OK
    else:
        # An unrecognised availability literal is reported honestly rather than guessed at.
        return None

    derived_codes = list(reason_codes)
    if validation_state == "corrected":
        # Recorded, deliberately not a downgrade.
        derived_codes.append("normalized:corrected")
    if int(prov.get("validation_error_count") or 0) > 0:
        derived_codes.append(f"validation_errors:{int(prov.get('validation_error_count') or 0)}")

    observed_at = str(prov.get("observed_at") or "").strip()
    return {
        "schema_version": "case_understanding_status.v1",
        "status": status,
        "source": source_mode,
        "reason": _REASONS_PL[status],
        "reason_codes": derived_codes[:8],
        "source_signal_id": str(prov.get("source_signal_id") or "").strip(),
        "observed_at": observed_at,
        "age_seconds": _age_seconds(observed_at, now_iso),
    }


__all__ = [
    "CASE_UNDERSTANDING_STATUSES",
    "STATUS_DEGRADED",
    "STATUS_OK",
    "STATUS_REASONING_NOT_REQUIRED",
    "STATUS_UNAVAILABLE",
    "build_case_understanding_status",
]
