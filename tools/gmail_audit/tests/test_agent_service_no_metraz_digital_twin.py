"""F6 digital twin: a service ticket must NOT trigger the 'podaj metraż' question.

Reprodukuje bug ze screenshotu (mail 'Zgłoszenie awarii' -> agent pytał o metraż) i
dowodzi, że po naprawie agent klasyfikuje sprawę jako awaria_naprawa i nie zgłasza
blokującej luki heated_area_m2 — na pełnej pętli silnika agenta.
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import signal_extractor
from agent_runtime.constitution import load_constitution
from agent_runtime.graph import AgentGraphEngine
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tools_registry import AgentToolRegistry


class _FakeStore:
    def append_fact_rows(self, rows):  # noqa: ANN001, ANN201
        return None

    def fetch_facts_for_case(self, case_id):  # noqa: ANN001, ANN201
        return []


def test_service_ticket_does_not_ask_metraz(monkeypatch) -> None:
    monkeypatch.setattr(signal_extractor, "run_signal_extraction", lambda **kw: {"hvac_intent": "service"})

    snap = build_initial_snapshot(case_id="c_serv", engagement_id="e_serv", trace_id="t")
    snap = apply_snapshot_delta(snap, {"operational_status": {"steps_remaining": 1}})

    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text"]),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    )
    ctx = ToolExecutionContext(
        snapshot=snap,
        settings=object(),
        mailbox_store=_FakeStore(),
        signal_payload={"subject": "ODP: Zgłoszenie awarii", "body_text": "pompa nie grzeje, błąd E5"},
    )
    final = engine.run(snap, context=ctx).snapshot

    assert final.case_kind == "awaria_naprawa"
    assert not any(g.field == "heated_area_m2" for g in final.gaps), "serwis nie może pytać o metraż"


def test_sales_ticket_still_asks_metraz_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(signal_extractor, "run_signal_extraction", lambda **kw: {"hvac_intent": "quote"})

    snap = build_initial_snapshot(case_id="c_sales", engagement_id="e_sales", trace_id="t")
    snap = apply_snapshot_delta(snap, {"operational_status": {"steps_remaining": 1}})

    engine = AgentGraphEngine(
        planner=MockSequencePlanner(["extract_facts_from_text"]),
        constitution=load_constitution(),
        tool_registry=AgentToolRegistry(),
    )
    ctx = ToolExecutionContext(
        snapshot=snap,
        settings=object(),
        mailbox_store=_FakeStore(),
        signal_payload={"subject": "Proszę o ofertę", "body_text": "chcę pompę ciepła"},
    )
    final = engine.run(snap, context=ctx).snapshot

    assert final.case_kind == "wycena_oferta"
    assert any(g.field == "heated_area_m2" and g.severity == "blocking" for g in final.gaps)
