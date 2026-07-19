from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parents[4] / "daszek" / "fixtures" / "v3"
TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_context_contract import build_case_context_pack_vnext
from mailbox_memory_models import CaseContextPack

FORBIDDEN_OPERATIONAL_KEYS = frozenset(
    {
        "email_body",
        "body",
        "snippet",
        "raw_llm",
        "raw_response",
        "prompt",
        "prompt_text",
        "subject",
    }
)

FIXTURE_NAMES = (
    "normal.json",
    "conflicts.json",
    "gaps.json",
    "service_signal.json",
    "marketing_signal.json",
    "sparse.json",
    "proposal_sparse.json",
    "proposed_only.json",
)


def _load(name: str) -> dict:
    path = FIXTURE_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def test_daszek_v3_fixture_files_are_valid_json() -> None:
    for name in FIXTURE_NAMES:
        data = _load(name)
        assert isinstance(data, dict)
        assert "evidence_cards" in data
        assert isinstance(data["evidence_cards"], list)
        for key in (
            "conflicting_facts",
            "completeness_gaps",
            "graph_hints",
            "service_signals",
            "marketing_signals",
            "execution_results",
        ):
            assert key in data, name
            assert isinstance(data[key], list), name
        if name == "proposed_only.json":
            assert "action_proposals" not in data, name
            assert "proposed_next_actions" in data, name
            assert isinstance(data["proposed_next_actions"], list), name
            assert data["proposed_next_actions"], name
        else:
            assert "action_proposals" in data, name
            assert isinstance(data["action_proposals"], list), name
            assert "proposed_next_actions" in data, name
            assert isinstance(data["proposed_next_actions"], list), name


def test_normal_fixture_action_proposals_and_duplicate_id() -> None:
    data = _load("normal.json")
    assert data["action_proposals"]
    prop = data["action_proposals"][0]
    assert prop.get("proposal_id")
    assert prop.get("recommended_operator_action") or prop.get("title")
    assert "proposed_next_actions" in data
    dup_ids = [x.get("proposal_id") for x in data["proposed_next_actions"] if x.get("proposal_id") == prop["proposal_id"]]
    assert dup_ids


def test_proposed_only_fixture_has_no_action_proposals_key() -> None:
    data = _load("proposed_only.json")
    assert "action_proposals" not in data
    rows = data["proposed_next_actions"]
    assert rows and rows[0].get("proposal_id")
    assert rows[0].get("recommended_operator_action") or rows[0].get("title")


def test_proposal_sparse_minimal_action_shape() -> None:
    data = _load("proposal_sparse.json")
    ap = data["action_proposals"]
    assert len(ap) == 1
    row = ap[0]
    assert "payload" not in row or row.get("payload") in (None, {})
    assert not row.get("evidence_refs")
    assert row.get("action_type") or row.get("recommended_operator_action")


def test_conflicts_fixture_includes_optional_vnext_feed_fields() -> None:
    data = _load("conflicts.json")
    assert data.get("context_pack_version")
    assert "has_blocking_conflicts" in data
    assert "has_blocking_gaps" in data
    assert isinstance(data.get("top_conflicts"), list)
    assert isinstance(data.get("top_gaps"), list)


def test_fixture_scenario_payloads() -> None:
    assert _load("conflicts.json")["conflicting_facts"]
    assert _load("gaps.json")["completeness_gaps"]
    assert _load("service_signal.json")["service_signals"]
    assert _load("marketing_signal.json")["marketing_signals"]
    assert _load("normal.json")["evidence_cards"]


def test_fixture_round_trip_through_vnext_contract() -> None:
    raw = _load("service_signal.json")
    pack = CaseContextPack(
        case_id=str(raw.get("case_id") or "fixture"),
        drive_documents_summary=[],
        conflicting_facts=list(raw.get("conflicting_facts") or []),
        completeness_gaps=list(raw.get("completeness_gaps") or []),
        graph_hints=list(raw.get("graph_hints") or []),
        source_refs=[],
    )
    contract = build_case_context_pack_vnext(pack)
    assert contract["contract_name"] == "CaseContextPack"
    assert contract["schema_version"] == "1"


def test_ingress_quality_snapshot_fixture_is_valid_contract() -> None:
    path = FIXTURE_DIR / "ingress_quality_snapshot.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("schema_name") == "daszek_ingress_quality_snapshot"
    assert data.get("schema_version") == "1"
    assert data.get("read_only") is True
    assert data.get("creates_cases") is False
    assert data.get("executes_actions") is False
    assert data.get("run_id")
    assert isinstance(data.get("counts"), dict)
    assert isinstance(data.get("items"), list)
    for row in data.get("items", []):
        assert "body" not in row
        assert "snippet" not in row


OPERATIONAL_FEED_FIXTURES = (
    "operational_feed_snapshot.json",
    "operational_feed_empty.json",
    "operational_feed_sparse.json",
    "operational_feed_agent_runtime.json",
)


def _walk_no_forbidden_keys(obj: object, label: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in FORBIDDEN_OPERATIONAL_KEYS, f"{label}: forbidden key {k!r}"
            _walk_no_forbidden_keys(v, label)
    elif isinstance(obj, list):
        for item in obj:
            _walk_no_forbidden_keys(item, label)


def _load_operational(name: str) -> dict:
    path = FIXTURE_DIR / name
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_operational_feed_fixture_files_contract() -> None:
    for name in OPERATIONAL_FEED_FIXTURES:
        data = _load_operational(name)
        assert data.get("schema_name") == "daszek_operational_feed_snapshot", name
        assert data.get("schema_version") == "1", name
        assert data.get("read_only") is True, name
        assert data.get("creates_cases") is False, name
        assert data.get("executes_actions") is False, name
        assert data.get("snapshot_id"), name
        feed = data.get("feed")
        assert isinstance(feed, dict), name
        assert isinstance(feed.get("desk"), list), name
        assert isinstance(feed.get("cases"), list), name
        assert isinstance(feed.get("tasks"), list), name
        assert isinstance(feed.get("case_details"), dict), name
        day = feed.get("day")
        assert day is None or isinstance(day, dict), name
        _walk_no_forbidden_keys(data, name)


def test_operational_feed_rich_fixture_coverage() -> None:
    data = _load_operational("operational_feed_snapshot.json")
    feed = data["feed"]
    assert len(feed["desk"]) >= 2
    assert len(feed["cases"]) >= 3
    assert len(feed["tasks"]) >= 3
    day = feed.get("day") or {}
    sections = day.get("sections") if isinstance(day, dict) else None
    assert isinstance(sections, list) and sections
    cd = feed["case_details"]
    assert "case-fix-1" in cd
    detail = cd["case-fix-1"]
    case_block = detail.get("case") if isinstance(detail, dict) else None
    assert isinstance(case_block, dict)
    assert case_block.get("completeness_gaps")
    assert case_block.get("conflicting_facts")
    assert case_block.get("evidence_cards")
    assert case_block.get("service_signals")
    assert case_block.get("marketing_signals")
    assert case_block.get("action_proposals")
    assert detail.get("execution_results")


def test_operational_feed_agent_runtime_fixture_hitl() -> None:
    data = _load_operational("operational_feed_agent_runtime.json")
    feed = data["feed"]
    assert feed["desk"][0].get("hitl_required") is True
    case_id = feed["cases"][0]["case_id"]
    detail = feed["case_details"][case_id]
    assert detail.get("view") == "case_detail_agent_runtime"
    turns = detail.get("agent_turns") or []
    assert isinstance(turns, list) and turns
    assert turns[0].get("tool_name")
