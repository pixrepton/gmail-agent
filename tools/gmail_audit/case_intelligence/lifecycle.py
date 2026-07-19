"""Lifecycle revision, feedback memory, and merge/split suggestions for case intelligence."""
from __future__ import annotations
from typing import Any

from v2_semantics import is_case_only_transition, normalize_lifecycle_intent

from .constants import INTELLIGENCE_PRESENCE_MODES, INTELLIGENCE_SURFACE_ZONES
from .validators import _bounded_float, _coerce_int, _presence_rank, _stable_id, _string_or_default


def build_merge_split_suggestions(
    *,
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
) -> dict[str, Any]:
    merge_candidates: list[dict[str, Any]] = []
    raw_candidates = list(case_link_result.get("candidates") or [])
    selected_case_key = str(case_link_result.get("selected_case_key") or "").strip()
    if selected_case_key and not any(str((candidate or {}).get("case_key") or "").strip() == selected_case_key for candidate in raw_candidates):
        raw_candidates.insert(0, {
            "case_key": selected_case_key,
            "match_confidence": case_link_result.get("confidence") or intake_result.get("confidence", {}).get("case_link_confidence") or 0.0,
        })

    for candidate in raw_candidates[:3]:
        if not isinstance(candidate, dict):
            continue
        case_key = str(candidate.get("case_key") or "").strip()
        confidence = _bounded_float(candidate.get("match_confidence") or candidate.get("score"), default=0.0)
        if not case_key or confidence < 0.45:
            continue
        merge_candidates.append({
            "candidate_case_id": _stable_id("case", case_key),
            "candidate_case_key": case_key, "suggestion_type": "merge", "confidence": confidence,
            "reason_pl": "Nowy sygnal wyglada podobnie do istniejacej sprawy i moze wymagac polaczenia zamiast tworzenia rownoleglego watku.",
            "review_required": True,
        })

    split_suspicions: list[dict[str, Any]] = []
    review_flags = set((intake_result.get("review") or {}).get("flags") or [])
    secondary_signals = list(intake_result.get("secondary_signals") or [])
    references = ((intake_result.get("extracted_data") or {}).get("references") or {})
    reference_groups = sum(1 for key in ("invoice_numbers", "shipment_numbers", "order_numbers", "transaction_numbers", "case_ids") if references.get(key))
    if "multiple_competing_signals" in review_flags or (secondary_signals and reference_groups >= 2):
        split_suspicions.append({
            "candidate_case_id": "", "suggestion_type": "split",
            "confidence": 0.68 if secondary_signals else 0.55,
            "reason_pl": "Sygnal moze mieszac dwa niezalezne watki i warto to zweryfikowac przed dalszym prowadzeniem sprawy.",
            "review_required": True,
        })

    summary_pl = ""
    if merge_candidates:
        summary_pl = "System widzi mozliwe powiazanie z istniejaca sprawa."
    elif split_suspicions:
        summary_pl = "System podejrzewa, ze temat moze zawierac wiecej niz jeden watek."
    else:
        summary_pl = "Brak silnych przeslanek do merge lub split."

    _ = snapshot
    return {"summary_pl": summary_pl, "merge_candidates": merge_candidates, "split_suspicions": split_suspicions}


def build_feedback_learning_memory(feedback_memory_seed: dict[str, Any] | None) -> dict[str, Any]:
    counts = feedback_memory_seed if isinstance(feedback_memory_seed, dict) else {}
    explicit_signals: list[str] = []
    preference_biases: list[str] = []
    suppression_hints: list[str] = []

    too_strong = _coerce_int(counts.get("za_mocne"), default=0)
    too_weak = _coerce_int(counts.get("za_slabe"), default=0)
    case_only = _coerce_int(counts.get("tylko_w_sprawie"), default=0)
    hide_kind = _coerce_int(counts.get("nie_pokazuj_takich"), default=0)
    helpful = _coerce_int(counts.get("trafne"), default=0)

    if helpful:
        explicit_signals.append("helpful")
    if too_strong:
        explicit_signals.append("too_strong")
        preference_biases.append("prefer_lower_presence")
    if too_weak:
        explicit_signals.append("too_weak")
        preference_biases.append("allow_stronger_presence")
    if case_only:
        explicit_signals.append("case_only")
        suppression_hints.append("prefer_case_only_for_repeated_updates")
    if hide_kind:
        explicit_signals.append("hide_this_kind")
        suppression_hints.append("suppress_similar_signals")

    emphasis_hint = "Neutralne ustawienie ekspozycji."
    if too_strong > too_weak:
        emphasis_hint = "Lekko oszczedzaj uwage na podobnych tematach."
    elif too_weak > too_strong:
        emphasis_hint = "Mozna odrobine mocniej podbijac podobne tematy."

    tone_hint = "Krotko, rzeczowo i po polsku."
    if hide_kind or case_only:
        tone_hint = "Jeszcze krocej i bez nadmiernego wzmacniania."

    return {
        "explicit_signals": explicit_signals, "implicit_signals": [],
        "preference_biases": preference_biases, "suppression_hints": suppression_hints,
        "tone_hint_pl": tone_hint, "emphasis_hint_pl": emphasis_hint,
    }


def build_lifecycle_revision(
    *,
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
    case_understanding: dict[str, Any],
    desk_composition: dict[str, Any],
    current_note_state: dict[str, Any],
) -> dict[str, Any]:
    current_presence = str(current_note_state.get("presence_mode") or "")
    target_presence = str(desk_composition.get("presence_mode") or "silent")
    target_zone = str(desk_composition.get("surface_zone") or "silent")
    action = str((intake_result.get("decision") or {}).get("action") or "")

    lifecycle_intent = normalize_lifecycle_intent(str(desk_composition.get("lifecycle_intent") or "noop"), target_zone)
    if current_presence:
        if is_case_only_transition(lifecycle_intent, target_zone):
            lifecycle_intent = "move_to_case_only"
        elif not bool(desk_composition.get("should_surface")):
            lifecycle_intent = "suppress" if action in {"ignore", "mark_reference"} else "move_to_case_only"
        elif _presence_rank(target_presence) > _presence_rank(current_presence):
            lifecycle_intent = "escalate_presence"
        elif _presence_rank(target_presence) < _presence_rank(current_presence):
            lifecycle_intent = "deescalate_presence"
        elif action in {"append_to_existing_case", "update_case_state"} or str(case_link_result.get("decision") or "") in {"linked", "weak_link"}:
            lifecycle_intent = "update"
        else:
            lifecycle_intent = "update"
    elif lifecycle_intent in {"create", "update", "suppress", "move_to_case_only"}:
        pass
    elif target_zone == "case_only":
        lifecycle_intent = "move_to_case_only"
    elif not bool(desk_composition.get("should_surface")):
        lifecycle_intent = "suppress"
    elif action in {"append_to_existing_case", "update_case_state"} or str(case_link_result.get("decision") or "") in {"linked", "weak_link"}:
        lifecycle_intent = "update"

    reason_pl = _string_or_default(case_understanding.get("attention_reason"), default="Lifecycle pozostaje zgodny z aktualnym zrozumieniem sprawy.")
    lifecycle_intent = normalize_lifecycle_intent(lifecycle_intent, target_zone)

    return {
        "lifecycle_intent": lifecycle_intent,
        "target_presence_mode": target_presence if target_presence in INTELLIGENCE_PRESENCE_MODES else "silent",
        "target_surface_zone": target_zone if target_zone in INTELLIGENCE_SURFACE_ZONES else "silent",
        "reason_pl": reason_pl,
        "should_create": lifecycle_intent == "create",
        "should_update": lifecycle_intent in {"update", "escalate_presence", "deescalate_presence", "move_to_case_only"},
    }
