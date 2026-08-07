"""AI-OS Slice 4.4 — Install-prep projection (Case facts → operator view).

Projection-only. Does **not** invent a second Source of Truth:
reads `active_facts` (already filtered of superseded) and emits a bounded
install-prep readiness card for Daszek / feed consumers.

Owned keys are HVAC install-prep signals already present on Case facts —
not a parallel store.
"""

from __future__ import annotations

from typing import Any

# Fact keys that contribute to install-prep readiness (bounded; extend via tests).
INSTALL_PREP_FACT_KEYS: frozenset[str] = frozenset(
    {
        "heated_area_m2",
        "building_type",
        "city",
        "address",
        "heat_source_current",
        "ozc_kw",
        "dhw_persons",
        "install_date_preferred",
        "access_notes",
        "electrical_capacity",
    }
)

REQUIRED_FOR_READY: frozenset[str] = frozenset(
    {
        "heated_area_m2",
        "city",
        "building_type",
    }
)


def _fact_key(row: dict[str, Any]) -> str:
    return str(row.get("fact_key") or row.get("key") or "").strip().lower()


def _fact_value(row: dict[str, Any]) -> str:
    for key in ("normalized_value", "raw_value", "value"):
        text = str(row.get(key) or "").strip()
        if text:
            return text
    return ""


def project_install_prep(
    *,
    active_facts: list[dict[str, Any]] | None,
    case_id: str = "",
) -> dict[str, Any]:
    """Build install-prep projection from active Case facts only.

    Returns a stable dict shape for feed/UI. Missing required keys → not ready.
    Superseded rows must already be excluded by the caller (`active_facts`).
    """
    rows = [dict(item) for item in (active_facts or []) if isinstance(item, dict)]
    # Defense: never treat superseded as current even if a caller leaks them.
    rows = [r for r in rows if str(r.get("status") or "active").lower() != "superseded"]

    by_key: dict[str, str] = {}
    for row in rows:
        key = _fact_key(row)
        if key not in INSTALL_PREP_FACT_KEYS:
            continue
        value = _fact_value(row)
        if value and key not in by_key:
            by_key[key] = value

    present = sorted(by_key.keys())
    missing_required = sorted(k for k in REQUIRED_FOR_READY if k not in by_key)
    ready = not missing_required

    return {
        "schema": "install_prep_projection.v1",
        "case_id": str(case_id or ""),
        "ready": ready,
        "status": "ready" if ready else "incomplete",
        "present_keys": present,
        "missing_required_keys": missing_required,
        "values": {k: by_key[k] for k in present},
        "source": "case_active_facts",
        "second_sot": False,
    }


__all__ = [
    "INSTALL_PREP_FACT_KEYS",
    "REQUIRED_FOR_READY",
    "project_install_prep",
]
