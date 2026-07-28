"""AI-OS-INTELLIGENCE-FINAL-HARDENING-01 / SLICE-3A-STRUCTURED-UNDERSTANDING-HANDOFF.

Brain 1 authors what a case MEANS. Brain 2 plans what to DO about it. Before this slice the
planner received that meaning only as a <=400-char Polish sentence folded into
`agent_memory.reasoning_trace` by `graph._ground_current_signal`, and `_compact_view` passed it on
as one of the last three `recent_steps` -- so `missing_critical_fields`, `risks` and
`recommended_next_step_pl` reached the planner as prose it would have to re-parse, or not at all.

`EngagementSnapshotV2.case_understanding` already carried all of it, structured, freshness-checked
and correlated. This slice consumes what exists rather than adding a third semantic model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent_runtime.agent_reconcile import (  # noqa: E402
    build_case_understanding_projection,
    build_case_understanding_provenance_projection,
)
from agent_runtime.constitution import AgentConstitution  # noqa: E402
from agent_runtime.tool_result import ToolCallPlan, ToolResult  # noqa: E402
from agent_runtime.graph import _apply_tool_result, _ground_current_signal  # noqa: E402
from agent_runtime.openai_agent_client import _brain1_context, _compact_view  # noqa: E402
from agent_runtime.store import InMemoryOperatorEngagementStore, build_initial_snapshot  # noqa: E402
from llm_contracts.engagement_snapshot_v2 import (  # noqa: E402
    CaseUnderstandingProvenance,
    EngagementSnapshotV2,
    FeedVisibility,
)
from understanding_output import build_case_understanding_provenance  # noqa: E402


# ── Brain 1 fixtures: real shapes, not invented ones ───────────────────────────────────────

_MESSAGE_ID = "msg_3a_1"


def _understanding_output(*, source_signal_id: str = _MESSAGE_ID) -> dict:
    """The `understanding_output` shape `build_understanding_output` emits."""
    return {
        "source_signal_id": source_signal_id,
        "created_at": "2026-07-27T09:00:00Z",
        "situation_summary_pl": "Klient prosi o wycene pompy ciepla dla domu 150 m2.",
        "operator_explanation": {
            "essence_pl": "Klient prosi o wycene pompy ciepla dla domu 150 m2.",
            "why_pl": "Zapytanie ofertowe bez podanego zapotrzebowania cieplnego.",
        },
        "thread_delta": {"operator_visible_delta_summary": "Pierwsza wiadomosc w watku."},
        "missing_critical_fields": ["thermal_demand_kw", "lokalizacja"],
        "risks": [
            {"risk_type": "underspecified_scope", "severity": "high", "summary_pl": "Brak danych o budynku."},
        ],
        "next_best_action_recommendation": {"title_pl": "Dopytaj o zapotrzebowanie cieplne."},
    }


def _br_meta(source_mode: str, *, notes: list | None = None) -> dict:
    """The `execution_metadata` shape `business_reasoner` emits (SLICE-2A)."""
    meta = {"stage_name": "business_reasoning", "source_mode": source_mode}
    if source_mode == "fallback":
        meta["reasoning_status"] = "unavailable"
    elif source_mode == "skipped_for_lane":
        meta["reasoning_status"] = "skipped"
    else:
        meta["reasoning_status"] = "ok"
    meta["normalization_notes"] = notes or []
    return meta


def _intel(*, provenance: dict | None, understanding: dict | None = None) -> dict:
    intel = {"understanding_output": understanding if understanding is not None else _understanding_output()}
    if provenance is not None:
        intel["execution_metadata"] = {"case_understanding_provenance": provenance}
    return intel


def _snapshot_with_understanding(intel: dict, *, message_id: str = _MESSAGE_ID) -> EngagementSnapshotV2:
    """Real projection -> real `_ground_current_signal` -> snapshot, exactly as production does."""
    base = build_initial_snapshot(case_id="case_3a", engagement_id="eng_3a", trace_id="t")
    return _ground_current_signal(
        base,
        {
            "subject": "Wycena pompy ciepla",
            "understanding_brief_pl": "Klient prosi o wycene pompy ciepla dla domu 150 m2.",
            "case_understanding_projection": build_case_understanding_projection(intel, message_id=message_id),
            "case_understanding_provenance": build_case_understanding_provenance_projection(
                intel, message_id=message_id
            ),
        },
    )


# ── B7.1-5: the provenance mapping ─────────────────────────────────────────────────────────


def test_clean_model_result_is_available_and_clean():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("model_result"),
        validation_errors=[],
    )
    assert p["availability"] == "available"
    assert p["source_mode"] == "model_result"
    assert p["validation_state"] == "clean"
    assert p["validation_error_count"] == 0


def test_normalized_result_is_corrected_and_never_labelled_degraded():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta(
            "normalized_model_result",
            notes=[{"field_name": "recommended_next_action", "reason_code": "synonym", "raw_value": "x"}],
        ),
        validation_errors=[],
    )
    assert p["availability"] == "available", "a repaired result is still a result"
    assert p["validation_state"] == "corrected"
    assert p["normalization_count"] == 1
    assert "normalized:recommended_next_action:synonym" in p["reason_codes"]
    # a harmless synonym rewrite and a real dictionary collision are indistinguishable today;
    # recording `corrected` states what happened, `degraded` would state more than is known
    assert "degraded" not in json.dumps(p)


def test_a_validation_correction_alone_makes_it_corrected():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("model_result"),
        validation_errors=["risks_severity_out_of_vocabulary"],
    )
    assert p["availability"] == "available"
    assert p["source_mode"] == "model_result", "the model result is not retro-labelled"
    assert p["validation_state"] == "corrected"
    assert p["validation_error_count"] == 1
    assert "validation:risks_severity_out_of_vocabulary" in p["reason_codes"]


def test_fallback_is_unavailable():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("fallback"),
        validation_errors=[],
    )
    assert p["availability"] == "unavailable"
    assert p["source_mode"] == "fallback"


def test_skipped_for_lane_is_not_required_and_raises_no_warning():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("skipped_for_lane"),
        validation_errors=[],
    )
    assert p["availability"] == "not_required"
    assert p["source_mode"] == "skipped_for_lane"
    # standing operator decision: a deliberately skipped lane is normal, not a degradation
    assert p["availability"] != "unavailable"
    assert p["reason_codes"] == []


# ── B7.6 / B7.11: no fabrication ───────────────────────────────────────────────────────────


def test_legacy_snapshot_gets_no_fabricated_status():
    snap = build_initial_snapshot(case_id="c", engagement_id="e", trace_id="t")
    assert snap.case_understanding_provenance is None
    assert _brain1_context(snap) is None
    assert "brain1_context" not in _compact_view(snap)


def test_absent_brain1_provenance_yields_none_not_an_empty_envelope():
    assert build_case_understanding_provenance_projection({"understanding_output": {}}, message_id=_MESSAGE_ID) is None
    assert build_case_understanding_provenance_projection(None, message_id=_MESSAGE_ID) is None


def test_unknown_source_mode_stays_empty_instead_of_being_guessed():
    p = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=None,
        validation_errors=None,
    )
    assert p["source_mode"] == "", "no reasoning metadata means unknown, not a default"
    assert p["validation_state"] == "", "the validator did not run -- distinguishable from 'ran, found nothing'"


def test_validator_ran_and_found_nothing_is_distinguishable_from_did_not_run():
    ran = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("model_result"),
        validation_errors=[],
    )
    did_not_run = build_case_understanding_provenance(
        understanding_output=_understanding_output(),
        business_execution_metadata=_br_meta("model_result"),
        validation_errors=None,
    )
    assert ran["validation_state"] == "clean"
    assert did_not_run["validation_state"] == ""


def test_missing_understanding_fields_get_no_placeholders():
    sparse = {
        "source_signal_id": _MESSAGE_ID,
        "created_at": "2026-07-27T09:00:00Z",
        "operator_explanation": {"essence_pl": "Krotka tresc."},
    }
    projected = build_case_understanding_projection({"understanding_output": sparse}, message_id=_MESSAGE_ID)
    assert projected["missing_critical_fields"] == []
    assert projected["risks"] == []
    assert projected["recommended_next_step_pl"] == ""
    assert projected["why_pl"] == ""
    assert "brak danych" not in json.dumps(projected, ensure_ascii=False).lower()


# ── B7.7 / B5: freshness ───────────────────────────────────────────────────────────────────


def test_provenance_from_a_different_signal_is_rejected():
    intel = _intel(
        provenance=build_case_understanding_provenance(
            understanding_output=_understanding_output(source_signal_id="OTHER_SIGNAL"),
            business_execution_metadata=_br_meta("model_result"),
            validation_errors=[],
        )
    )
    assert build_case_understanding_provenance_projection(intel, message_id=_MESSAGE_ID) is None


def test_a_turn_without_fresh_understanding_clears_both_fields_together():
    intel = _intel(
        provenance=build_case_understanding_provenance(
            understanding_output=_understanding_output(),
            business_execution_metadata=_br_meta("model_result"),
            validation_errors=[],
        )
    )
    grounded = _snapshot_with_understanding(intel)
    assert grounded.case_understanding is not None
    assert grounded.case_understanding_provenance is not None

    stale_turn = _ground_current_signal(grounded, {"subject": "Kolejna wiadomosc"})
    assert stale_turn.case_understanding is None
    assert stale_turn.case_understanding_provenance is None, "provenance must never outlive its Understanding"


def test_provenance_is_never_set_without_an_understanding():
    base = build_initial_snapshot(case_id="c", engagement_id="e", trace_id="t")
    only_provenance = _ground_current_signal(
        base,
        {
            "subject": "s",
            "case_understanding_projection": None,
            "case_understanding_provenance": build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("model_result"),
                validation_errors=[],
            ),
        },
    )
    assert only_provenance.case_understanding_provenance is None


def test_a_later_available_replaces_an_earlier_unavailable():
    first = _snapshot_with_understanding(
        _intel(
            provenance=build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("fallback"),
                validation_errors=[],
            )
        )
    )
    assert first.case_understanding_provenance.availability == "unavailable"
    second = _ground_current_signal(
        first,
        {
            "subject": "s2",
            "case_understanding_projection": build_case_understanding_projection(
                _intel(provenance=None), message_id=_MESSAGE_ID
            ),
            "case_understanding_provenance": build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("model_result"),
                validation_errors=[],
            ),
        },
    )
    assert second.case_understanding_provenance.availability == "available"


# ── B7.8: `unavailable` must not launder a fallback recommendation ─────────────────────────


def test_an_unavailable_recommendation_is_never_presented_as_canonical():
    """The recommendation may still be shown, but never without its `unavailable` marker."""
    snap = _snapshot_with_understanding(
        _intel(
            provenance=build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("fallback"),
                validation_errors=[],
            )
        )
    )
    context = _brain1_context(snap)
    assert context["understanding"]["recommended_next_step_pl"]
    assert context["provenance"]["availability"] == "unavailable"
    assert context["provenance"]["source_mode"] == "fallback"
    # the two travel together in one object -- a consumer cannot read the recommendation from the
    # planner payload without also reading that it was not authored by a real reasoning run
    payload = json.dumps(_compact_view(snap), ensure_ascii=False)
    assert '"availability": "unavailable"' in payload or '"availability":"unavailable"' in payload


# ── B7.9-10, B7.14: the planner actually receives structure ────────────────────────────────


def _grounded() -> EngagementSnapshotV2:
    return _snapshot_with_understanding(
        _intel(
            provenance=build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("model_result"),
                validation_errors=[],
            )
        )
    )


def test_structured_understanding_survives_planner_compaction():
    view = _compact_view(_grounded())
    assert "brain1_context" in view, "compaction must not drop Brain 1's output"
    understanding = view["brain1_context"]["understanding"]
    assert understanding["essence_pl"].startswith("Klient prosi o wycene")


def test_gaps_risks_and_next_step_stay_structural_not_prose():
    understanding = _compact_view(_grounded())["brain1_context"]["understanding"]
    assert understanding["missing_critical_fields"] == ["thermal_demand_kw", "lokalizacja"]
    assert isinstance(understanding["risks"], list)
    assert understanding["risks"][0]["severity"] == "high"
    assert understanding["risks"][0]["risk_type"] == "underspecified_scope"
    assert understanding["recommended_next_step_pl"] == "Dopytaj o zapotrzebowanie cieplne."


def test_evidence_provenance_travels_with_the_understanding():
    context = _compact_view(_grounded())["brain1_context"]
    assert context["provenance"]["source_signal_id"] == _MESSAGE_ID
    assert context["provenance"]["observed_at"] == "2026-07-27T09:00:00Z"
    assert context["provenance"]["schema_version"] == "v1"


def test_the_one_line_brief_still_exists_but_is_no_longer_the_only_input():
    view = _compact_view(_grounded())
    joined = " ".join(view["recent_steps"])
    assert "Zrozumienie sprawy:" in joined, "the backward-compatible brief must remain"
    # ...and the structured channel carries what the brief cannot
    assert view["brain1_context"]["understanding"]["missing_critical_fields"]
    assert "thermal_demand_kw" not in joined, "the brief never carried the structured gap list"


def test_brain1_context_is_ephemeral_and_not_a_second_stored_copy():
    snap = _grounded()
    stored = snap.model_dump(mode="python")
    assert "brain1_context" not in stored, "the planner view must not become a persisted field"
    assert stored["case_understanding"] is not None, "the single stored representation is unchanged"


# ── B7.15: full capture path to the final planner payload ──────────────────────────────────


def test_full_capture_path_from_brain1_fixture_to_final_planner_messages():
    """Brain 1 fixture -> projection -> store write/read -> planner messages, no external LLM."""
    from agent_runtime.openai_agent_client import OpenAIToolPlanner

    store = InMemoryOperatorEngagementStore()
    grounded = _grounded()
    store.insert_snapshot(grounded)
    reloaded = store.load_snapshot("eng_3a")
    assert reloaded.case_understanding is not None, "the projection survived serialisation"
    assert reloaded.case_understanding_provenance is not None

    class _Settings:
        openai_api_key = "test"
        openai_base_url = ""
        model = "test-model"

    planner = OpenAIToolPlanner(settings=_Settings(), client=object())
    constitution = AgentConstitution(
        hvac_rules="",
        company_context="",
        forbidden_actions=("wysylka_bez_zgody",),
        tool_allowlist=("generate_draft_reply",),
    )
    messages = planner._build_messages(
        snapshot=reloaded,
        constitution=constitution,
        available_tools=("generate_draft_reply",),
    )

    user_message = [m for m in messages if m["role"] == "user"][-1]["content"]
    payload = json.loads(user_message.split("\n\n", 1)[1])
    assert "brain1_context" in payload, "the final planner payload must carry the structured hand-off"
    assert payload["brain1_context"]["understanding"]["missing_critical_fields"] == [
        "thermal_demand_kw",
        "lokalizacja",
    ]
    assert payload["brain1_context"]["provenance"]["availability"] == "available"


# ── B7.12: authority guard ─────────────────────────────────────────────────────────────────


def test_a_tool_delta_cannot_overwrite_brain1_semantic_fields():
    snap = _grounded()
    original_essence = snap.case_understanding.essence_pl
    hostile = ToolResult(
        status="ok",
        turn_summary_pl="probowano nadpisac",
        snapshot_delta={
            "case_understanding": {"essence_pl": "PRZEJETE PRZEZ TOOL", "source_signal_id": "fake"},
            "case_understanding_provenance": {"availability": "available", "source_mode": "model_result"},
            "operational_status": {"code": "ready_for_quote"},
        },
    )
    updated = _apply_tool_result(snap, ToolCallPlan(tool_name="generate_draft_reply", arguments={}), hostile)
    assert updated.case_understanding.essence_pl == original_essence
    assert updated.case_understanding.source_signal_id == _MESSAGE_ID
    assert updated.case_understanding_provenance.source_mode == "model_result"
    # the legitimate part of the same delta still applies -- this is a field guard, not a veto
    assert updated.operational_status.code == "ready_for_quote"


def test_the_canonical_brain1_writer_is_still_allowed():
    """`_ground_current_signal` is the canonical writer and must not be caught by the guard."""
    grounded = _grounded()
    assert grounded.case_understanding is not None
    assert grounded.case_understanding.essence_pl.startswith("Klient prosi")


# ── B7.13: no visibility side effects ──────────────────────────────────────────────────────


def test_understanding_provenance_never_touches_feed_visibility():
    from feed_visibility import effective_visibility_mode, is_main_feed_member

    base = build_initial_snapshot(
        case_id="c", engagement_id="e", trace_id="t",
        feed_visibility=FeedVisibility(mode="hidden", reason_codes=["obvious_noise"]),
    )
    before_mode, before_reasons = effective_visibility_mode(base)
    after = _ground_current_signal(
        base,
        {
            "subject": "s",
            "case_understanding_projection": build_case_understanding_projection(
                _intel(provenance=None), message_id=_MESSAGE_ID
            ),
            "case_understanding_provenance": build_case_understanding_provenance(
                understanding_output=_understanding_output(),
                business_execution_metadata=_br_meta("fallback"),
                validation_errors=["some_error"],
            ),
        },
    )
    assert after.feed_visibility == base.feed_visibility
    assert effective_visibility_mode(after) == (before_mode, before_reasons)
    assert is_main_feed_member(after) is False, "even an `unavailable` understanding cannot reveal noise"


def test_provenance_model_rejects_an_invented_availability_value():
    import pydantic

    try:
        CaseUnderstandingProvenance(availability="degraded")
    except pydantic.ValidationError:
        return
    raise AssertionError("`degraded` must not be a valid availability -- no severity contract exists yet")
