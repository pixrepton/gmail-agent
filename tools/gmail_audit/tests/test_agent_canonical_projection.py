"""F3: agent path through canonical projection composer (build_operator_projection_snapshot)."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.feed_projection import (
    build_canonical_operator_snapshot,
    enrich_envelope_from_engagement,
    projection_canonical_enabled,
)
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import build_initial_snapshot


def _snapshot_with_draft():
    snap = build_initial_snapshot(case_id="c1", engagement_id="e1", trace_id="t1")
    return apply_snapshot_delta(
        snap,
        {
            "case_kind": "wycena_oferta",
            "operational_status": {"code": "pending_operator"},
            "actions": [
                {"id": "draft_reply", "enabled": True, "payload_pl": "Dzień dobry, dziękujemy za zapytanie...", "disabled_reason_pl": None}
            ],
            "gaps": [{"field": "heated_area_m2", "severity": "blocking", "ask_pl": "Podaj metraż ogrzewanego budynku (m²)."}],
        },
    )


def test_projection_canonical_flag(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_PROJECTION_CANONICAL", raising=False)
    assert projection_canonical_enabled() is False
    monkeypatch.setenv("AGENT_PROJECTION_CANONICAL", "1")
    assert projection_canonical_enabled() is True


def test_enrich_envelope_injects_operator_fields() -> None:
    snap = apply_snapshot_delta(
        build_initial_snapshot(case_id="c2", engagement_id="e2", trace_id="t2"),
        {
            "case_kind": "awaria_naprawa",
            "actions": [{"id": "draft_reply", "enabled": True, "payload_pl": "Draft serwisowy", "disabled_reason_pl": None}],
        },
    )
    base = {"projection_envelope": {"desk_cards": [{"card_type": "case_essence", "title": "Case", "summary": ""}]}}
    out = enrich_envelope_from_engagement(base, snap)
    env = out["projection_envelope"]
    assert env["case_kind"] == "awaria_naprawa"
    assert env["draft_reply_pl"] == "Draft serwisowy"
    assert env["hitl_action_id"] == "draft_reply"
    assert env["desk_cards"][0]["operator_essence_pl"] == "Draft serwisowy"
    assert out["draft_reply_pl"] == "Draft serwisowy"


def test_canonical_snapshot_has_envelope_and_operator_fields() -> None:
    snap = _snapshot_with_draft()
    warnings: list[str] = []
    out = build_canonical_operator_snapshot(
        engagement=snap,
        signal=SimpleNamespace(signal_id="sig1"),
        intake_output={"message": {"message_id": "m1", "subject": "Oferta"}, "decision": {"action": "review"}, "source": {"mailbox": "biuro@"}},
        run_id="r1",
        store=None,  # pusty pack -> composer deterministyczny, ale envelope musi powstać
        settings=None,
        warnings=warnings,
    )
    assert out["schema_version"] == "operator_projection_snapshot.v1"
    assert isinstance(out.get("projection_envelope"), dict)
    assert "daszek_routes" in out
    assert out["case_kind"] == "wycena_oferta"
    assert out["draft_reply_pl"].startswith("Dzień dobry")
    assert out["operator_questions_pl"]  # pytania z gaps[].ask_pl
