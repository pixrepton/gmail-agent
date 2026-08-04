"""Deterministic known-fact reask guard (PLANNER-EXEC-FIDELITY-01).

Blocks customer-facing questions / operator clarification that ask again for
facts already present in structured HVAC profile or Brain 1 gaps (absence list).
"""

from __future__ import annotations

import re
from typing import Any

# Canonical fact keys → Polish/English cue patterns in ask text.
_FACT_CUES: dict[str, tuple[str, ...]] = {
    "heated_area_m2": (
        r"\bmetr(a[zż]|azu|aż|ażu)\b",
        r"\bpowierzchni(a|i)?\b",
        r"\bm2\b",
        r"\bm²\b",
        r"\barea\b",
        r"\bheated[_\s]?area\b",
    ),
    "raw_geographic_signal": (
        r"\bmiasto\b",
        r"\blokali[sz]acj",
        r"\bkod\s*poczt",
        r"\bpostal\b",
        r"\bcity\b",
        r"\bwroc[lł]aw",
        r"\badres\b",
    ),
    "building_type": (
        r"\btyp\s+budynku\b",
        r"\bbuilding[_\s]?type\b",
        r"\bjednorodzin",
        r"\bbli[zź]niak",
    ),
    "current_heating_source": (
        r"\bobecne\s+ogrzew",
        r"\bźródł[oa]\s+ciepł",
        r"\bcurrent[_\s]?heating\b",
        r"\bkocio[łl]\b",
    ),
    "thermal_demand_kw": (
        r"\bozc\b",
        r"\bstrat[ay]\s+ciep[łl]",
        r"\bthermal[_\s]?demand\b",
        r"\bzapotrzebowan",
    ),
}

_SERVICE_KINDS = frozenset(
    {
        "awaria_naprawa",
        "przeglad_konserwacja",
        "reklamacja",
        "serwis",
    }
)


def known_facts_from_snapshot(snapshot: Any) -> dict[str, Any]:
    """Extract canonical known facts from HVAC profile + understanding."""
    profile = getattr(snapshot, "hvac_profile", None)
    known: dict[str, Any] = {}
    if profile is not None:
        area = getattr(profile, "heated_area_m2", None)
        if area is not None:
            known["heated_area_m2"] = area
        location = getattr(profile, "location", None)
        city = getattr(location, "city", None) if location is not None else None
        postal = getattr(location, "postal_code", None) if location is not None else None
        if city or postal:
            known["raw_geographic_signal"] = {
                "city": city,
                "postal_code": postal,
            }
        building = getattr(profile, "building_type", None)
        if building:
            known["building_type"] = building
        thermal = getattr(profile, "thermal_demand_kw", None)
        if thermal is not None:
            known["thermal_demand_kw"] = thermal

    understanding = getattr(snapshot, "case_understanding", None)
    if understanding is not None:
        # missing_critical_fields means NOT known — do not treat as known.
        pass
    return known


def ask_targets_known_fact(ask_pl: str, known: dict[str, Any]) -> list[str]:
    """Return fact keys already known that the ask text appears to request."""
    text = str(ask_pl or "").strip().lower()
    if not text:
        return []
    hits: list[str] = []
    for key, patterns in _FACT_CUES.items():
        if key not in known:
            continue
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                hits.append(key)
                break
    return hits


def guard_known_fact_reask(
    *,
    tool_name: str,
    arguments: dict[str, Any] | None,
    snapshot: Any,
) -> dict[str, Any] | None:
    """Return block payload when planner asks again for a known fact."""
    name = str(tool_name or "").strip()
    args = arguments if isinstance(arguments, dict) else {}
    ask = ""
    if name == "request_operator_clarification":
        ask = str(args.get("ask_pl") or "")
    elif name == "generate_draft_reply":
        # Draft body is deterministic; sanity gate handles content. Here we only
        # catch missing_info intent when the profile already has the facts.
        intent = str(args.get("intent") or "").strip()
        if intent != "missing_info":
            return None
        ask = "metraż OZC lokalizacja powierzchnia"
    elif name == "report_gaps_and_stop":
        ask = str(args.get("ask_pl") or "")
        gaps = args.get("gaps")
        if isinstance(gaps, list):
            ask = " ".join(
                str(g.get("ask_pl") or g.get("field") or "")
                for g in gaps
                if isinstance(g, dict)
            )
    else:
        return None

    known = known_facts_from_snapshot(snapshot)
    hits = ask_targets_known_fact(ask, known)
    if not hits:
        return None
    return {
        "blocked": True,
        "reason_code": "known_fact_reask_blocked",
        "failure_class": "PLANNER_KNOWN_FACT_REASK",
        "fact_keys": hits,
        "known_facts": {k: known[k] for k in hits},
    }


def is_service_case_kind(case_kind: str) -> bool:
    kind = str(case_kind or "").strip().lower()
    return kind in _SERVICE_KINDS or "awaria" in kind or "serwis" in kind or "reklam" in kind


__all__ = [
    "ask_targets_known_fact",
    "guard_known_fact_reask",
    "is_service_case_kind",
    "known_facts_from_snapshot",
]
