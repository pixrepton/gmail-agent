"""F1: agent extract_facts_from_text via central LLM + case_kind classification."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import signal_extractor
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tools.handlers import extract_facts_from_text


class _FakeStore:
    def __init__(self, facts: list[dict] | None = None) -> None:
        self._facts = list(facts or [])
        self.appended: list[dict] = []

    def append_fact_rows(self, rows: list[dict]) -> None:
        self.appended.extend(rows)

    def fetch_facts_for_case(self, case_id: str) -> list[dict]:
        return list(self._facts)


def _ctx(signal_payload: dict, *, store: object | None = None) -> ToolExecutionContext:
    snap = build_initial_snapshot(case_id="", engagement_id="eng_x", trace_id="t")
    return ToolExecutionContext(
        snapshot=snap,
        settings=object(),  # sentinel — _run_llm_extraction nie woła load_settings
        mailbox_store=store or _FakeStore(),
        signal_payload=dict(signal_payload),
    )


def _stub(monkeypatch, result: dict) -> None:
    monkeypatch.setattr(signal_extractor, "run_signal_extraction", lambda **kw: dict(result))


def test_service_mail_no_metraz_gap(monkeypatch) -> None:
    _stub(monkeypatch, {"hvac_intent": "service"})
    ctx = _ctx({"subject": "Zgłoszenie awarii", "body_text": "pompa nie grzeje, błąd E5"})
    delta = extract_facts_from_text(None, ctx).snapshot_delta
    assert delta["case_kind"] == "awaria_naprawa"
    assert "gaps" not in delta  # serwis nigdy nie pyta o metraż


def test_sales_mail_fills_profile(monkeypatch) -> None:
    _stub(monkeypatch, {
        "hvac_intent": "quote",
        "heated_area_m2": 128.0,
        "raw_geographic_signal": "Kraków",
        "building_type": "single_family",
    })
    ctx = _ctx({"subject": "Proszę o ofertę", "body_text": "dom 128 m2 w Krakowie"})
    delta = extract_facts_from_text(None, ctx).snapshot_delta
    assert delta["case_kind"] == "wycena_oferta"
    assert delta["hvac_profile"]["heated_area_m2"] == 128
    assert delta["hvac_profile"]["location"]["city"] == "Kraków"
    assert "gaps" not in delta


def test_sales_mail_missing_metraz_sets_blocking_gap(monkeypatch) -> None:
    _stub(monkeypatch, {"hvac_intent": "quote"})
    ctx = _ctx({"subject": "Proszę o wycenę", "body_text": "chcę pompę ciepła"})
    delta = extract_facts_from_text(None, ctx).snapshot_delta
    assert delta["case_kind"] == "wycena_oferta"
    assert any(g["field"] == "heated_area_m2" and g["severity"] == "blocking" for g in delta["gaps"])


def test_finance_mail_is_ksiegowosc(monkeypatch) -> None:
    _stub(monkeypatch, {})
    ctx = _ctx({"subject": "Wyciąg bankowy", "body_text": "saldo konta", "business_area": "finance"})
    delta = extract_facts_from_text(None, ctx).snapshot_delta
    assert delta["case_kind"] == "ksiegowosc"
    assert "gaps" not in delta


def test_invoice_direction_sprzedaz_from_own_nip(monkeypatch) -> None:
    monkeypatch.setenv("TOPINSTAL_OWN_NIP", "1234567890")
    _stub(monkeypatch, {})
    store = _FakeStore(facts=[{"fact_key": "seller_nip", "raw_value": "123-456-78-90"}])
    ctx = _ctx({"subject": "Faktura FV/1/2026", "body_text": "do zapłaty", "business_area": "finance"}, store=store)
    assert extract_facts_from_text(None, ctx).snapshot_delta["case_kind"] == "faktura_sprzedaz"


def test_invoice_direction_zakup_from_own_nip(monkeypatch) -> None:
    monkeypatch.setenv("TOPINSTAL_OWN_NIP", "1234567890")
    _stub(monkeypatch, {})
    store = _FakeStore(facts=[
        {"fact_key": "seller_nip", "raw_value": "9999999999"},
        {"fact_key": "buyer_nip", "raw_value": "1234567890"},
    ])
    ctx = _ctx({"subject": "Faktura kosztowa", "body_text": "od dostawcy", "business_area": "finance"}, store=store)
    assert extract_facts_from_text(None, ctx).snapshot_delta["case_kind"] == "faktura_zakup"


def test_empty_text_warns_without_metraz_gap(monkeypatch) -> None:
    ctx = _ctx({"subject": "", "body_text": ""})
    gaps = extract_facts_from_text(None, ctx).snapshot_delta.get("gaps", [])
    assert gaps and gaps[0]["field"] == "source_text"
