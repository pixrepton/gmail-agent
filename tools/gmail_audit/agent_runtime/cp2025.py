"""Deterministic CP2025 eligibility check (PR-C) — not LLM."""

from __future__ import annotations

from llm_contracts.engagement_snapshot_v2 import HvacProfile


def check_cp2025_eligibility(profile: HvacProfile) -> tuple[bool | None, str]:
    """
    Return (eligible, summary_pl).
    None eligible = insufficient data (not a hard false).
    """
    area = profile.heated_area_m2
    if area is None or area <= 0:
        return None, "Brak metrażu — nie można ocenić CP2025."
    if area < 40:
        return False, f"Metraż {area} m² poniżej typowego progu programu (wstępna ocena)."
    building = (profile.building_type or "").strip().lower()
    if building and building not in {"single_family", "single_family_house", "dom", "dom jednorodzinny", "dom_jednorodzinny", "dom jednorodzinny wolnostojacy"}:
        return None, "Typ budynku wymaga weryfikacji operatora pod CP2025."
    return True, f"Wstępnie kwalifikuje się do CP2025 (metraż {area} m², dom jednorodzinny)."
