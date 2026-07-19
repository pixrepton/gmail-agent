from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from event_memory import EventLog, emit_case_intelligence, emit_signal_received
from tests.fixture_helpers import run_fixture
from v2_runtime import build_v2_ingest_payload


REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_PATH = Path(__file__).resolve().parents[4] / "daszek" / "tools" / "v2-store-harness.php"


def _run_store_harness(payloads: list[dict[str, object]]) -> dict[str, object]:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump({"payloads": payloads}, handle, ensure_ascii=False, indent=2)
        temp_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["php", str(HARNESS_PATH), str(temp_path)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    finally:
        temp_path.unlink(missing_ok=True)

    if result.returncode != 0:
        raise AssertionError(f"PHP harness failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")
    return json.loads(result.stdout)


def _build_operational_events(
    *,
    snapshot: dict[str, object],
    intelligence_result: dict[str, object],
    case_id: str,
) -> list[dict[str, object]]:
    event_log = EventLog()
    source_message = snapshot.get("source_message") or {}
    emit_signal_received(event_log, snapshot=snapshot, case_id=case_id)
    emit_case_intelligence(
        event_log,
        case_id=case_id,
        intelligence_result=intelligence_result,
        source_signal_id=str(source_message.get("message_id") or "").strip(),
        thread_id=str(source_message.get("thread_id") or "").strip(),
    )
    return event_log.events()


def _build_payload_from_fixture(result: dict[str, object], *, run_id: str) -> dict[str, object]:
    projection = copy.deepcopy(result["v2_projection"])
    snapshot = copy.deepcopy(result["snapshot"])
    intelligence = copy.deepcopy(result["case_intelligence"])
    case_id = str(projection["case_patch"]["case_id"])
    return build_v2_ingest_payload(
        run_id=run_id,
        message_key=str(snapshot["source_message"]["message_id"]),
        v2_projection=projection,
        thread_memory=copy.deepcopy(intelligence["thread_memory"]),
        operational_events=_build_operational_events(snapshot=snapshot, intelligence_result=intelligence, case_id=case_id),
    )


class OperationalMemoryRoundTripTests(unittest.TestCase):
    def test_replay_of_identical_payload_is_idempotent(self) -> None:
        payload = _build_payload_from_fixture(run_fixture("active_case_follow_up"), run_id="replay-a")
        result = _run_store_harness([payload, copy.deepcopy(payload)])

        self.assertEqual(result["counts"]["signals"], 1)
        self.assertEqual(result["counts"]["decision_traces"], 1)
        self.assertEqual(result["counts"]["event_log"], 2)
        self.assertEqual(result["persisted"][0]["signal_status"], "written")
        self.assertEqual(result["persisted"][1]["signal_status"], "duplicate")
        self.assertEqual(result["persisted"][0]["trace_status"], "written")
        self.assertEqual(result["persisted"][1]["trace_status"], "duplicate")
        self.assertEqual(result["persisted"][1]["event_log_results"]["duplicate"], 2)

    def test_operational_memory_survives_two_runs_of_same_case(self) -> None:
        first_result = run_fixture("active_case_follow_up")
        payload_one = _build_payload_from_fixture(first_result, run_id="roundtrip-1")

        payload_one["case_patch"]["business_area"] = "service"
        payload_one["case_patch"]["business_priority"] = "high"
        payload_one["case_patch"]["missing_info_summary_pl"] = "Brakuje potwierdzonego terminu realizacji."
        payload_one["case_patch"]["missing_info"] = [{"code": "termin_realizacji", "severity": "high"}]
        payload_one["case_patch"]["merge_candidates"] = [{"case_id": "case_candidate_1"}]
        payload_one["case_patch"]["split_suspicions"] = [{"reason": "dwa_tematy_w_jednym_watku"}]
        payload_one["case_patch"]["risk_summary_pl"] = "Najwazniejsze ryzyko: opoznienie realizacji."
        payload_one["case_patch"]["risks"] = [{"code": "delay", "severity": "high"}]
        payload_one["case_patch"]["case_key_source"] = "linked"
        payload_one["desk_note_patch"]["missing_info_summary_pl"] = "Brakuje potwierdzonego terminu realizacji."
        payload_one["desk_note_patch"]["missing_info"] = [{"code": "termin_realizacji", "severity": "high"}]
        payload_one["desk_note_patch"]["merge_candidates"] = [{"case_id": "case_candidate_1"}]
        payload_one["desk_note_patch"]["split_suspicions"] = [{"reason": "dwa_tematy_w_jednym_watku"}]
        payload_one["desk_note_patch"]["risk_summary_pl"] = "Najwazniejsze ryzyko: opoznienie realizacji."
        payload_one["desk_note_patch"]["risks"] = [{"code": "delay", "severity": "high"}]
        payload_one["desk_note_patch"]["automation_policy"] = {"mode": "review_before_write", "allow_live_push": False}

        payload_two = copy.deepcopy(payload_one)
        signal_two_id = "sig_roundtrip_002"
        message_two_id = "msg_roundtrip_002"
        occurred_two_at = "2026-04-12T09:15:00+02:00"

        payload_two["run_id"] = "roundtrip-2"
        payload_two["message_key"] = message_two_id
        payload_two["signal_projection"]["signal_id"] = signal_two_id
        payload_two["signal_projection"]["observed_at"] = occurred_two_at
        payload_two["signal_projection"]["source_ref"]["message_id"] = message_two_id
        payload_two["signal_projection"]["source_ref"]["received_at"] = occurred_two_at
        payload_two["case_patch"]["latest_signal_id"] = signal_two_id
        payload_two["case_patch"]["case_key"] = ""
        payload_two["case_patch"]["case_key_source"] = ""
        payload_two["case_patch"]["business_area"] = ""
        payload_two["case_patch"]["business_priority"] = ""
        payload_two["case_patch"]["missing_info_summary_pl"] = ""
        payload_two["case_patch"]["missing_info"] = []
        payload_two["case_patch"]["merge_candidates"] = []
        payload_two["case_patch"]["split_suspicions"] = []
        payload_two["case_patch"]["risk_summary_pl"] = ""
        payload_two["case_patch"]["risks"] = []
        payload_two["desk_note_patch"]["source_signal_ids"] = [signal_two_id]
        payload_two["desk_note_patch"]["source_message_id"] = message_two_id
        payload_two["desk_note_patch"]["missing_info_summary_pl"] = ""
        payload_two["desk_note_patch"]["missing_info"] = []
        payload_two["desk_note_patch"]["merge_candidates"] = []
        payload_two["desk_note_patch"]["split_suspicions"] = []
        payload_two["desk_note_patch"]["risk_summary_pl"] = ""
        payload_two["desk_note_patch"]["risks"] = []
        payload_two["desk_note_patch"]["automation_policy"] = {}
        payload_two["decision_trace"]["trace_id"] = "trace_roundtrip_002"
        payload_two["decision_trace"]["trigger_signal_id"] = signal_two_id
        payload_two["decision_trace"]["created_at"] = occurred_two_at

        second_thread_memory = copy.deepcopy(payload_two["thread_memory"])
        second_thread_memory["canonical_thread_summary"] = "Rozmowa o CASE-2026-001 z dodatkowym follow-upem i nowym terminem."
        second_thread_memory["unresolved_questions"] = ["Czy klient potwierdzil nowy termin realizacji?"]
        second_thread_memory["updated_at"] = occurred_two_at
        payload_two["thread_memory"] = second_thread_memory

        second_snapshot = copy.deepcopy(first_result["snapshot"])
        second_snapshot["source_message"]["message_id"] = message_two_id
        second_snapshot["source_message"]["date"] = occurred_two_at
        second_snapshot["source_message"]["thread_id"] = str(payload_two["signal_projection"]["source_ref"]["thread_id"])
        second_snapshot["source_message"]["subject"] = "Re: CASE-2026-001 - aktualizacja"
        second_intelligence = copy.deepcopy(first_result["case_intelligence"])
        second_intelligence["thread_memory"] = copy.deepcopy(second_thread_memory)
        payload_two["operational_events"] = _build_operational_events(
            snapshot=second_snapshot,
            intelligence_result=second_intelligence,
            case_id=str(payload_two["case_patch"]["case_id"]),
        )

        result = _run_store_harness([payload_one, payload_two])
        case_detail = result["case_detail"]
        note_detail = result["note_detail"]
        case_record = case_detail["case"]
        note_record = note_detail["note"]

        self.assertEqual(case_record["case_key"], payload_one["case_patch"]["case_key"])
        self.assertEqual(case_record["case_key_source"], payload_one["case_patch"]["case_key_source"])
        self.assertEqual(case_record["business_area"], payload_one["case_patch"]["business_area"])
        self.assertEqual(case_record["business_priority"], payload_one["case_patch"]["business_priority"])
        self.assertEqual(case_record["missing_info_summary_pl"], payload_one["case_patch"]["missing_info_summary_pl"])
        self.assertEqual(case_record["missing_info"], payload_one["case_patch"]["missing_info"])
        self.assertEqual(case_record["merge_candidates"], payload_one["case_patch"]["merge_candidates"])
        self.assertEqual(case_record["split_suspicions"], payload_one["case_patch"]["split_suspicions"])
        self.assertEqual(case_record["risk_summary_pl"], payload_one["case_patch"]["risk_summary_pl"])
        self.assertEqual(case_record["risks"], payload_one["case_patch"]["risks"])
        self.assertEqual(case_record["latest_signal_id"], signal_two_id)
        self.assertEqual(case_record["latest_signal_at"], occurred_two_at)
        self.assertTrue(case_record["open_desk_note_id"])

        self.assertEqual(note_record["missing_info_summary_pl"], payload_one["desk_note_patch"]["missing_info_summary_pl"])
        self.assertEqual(note_record["missing_info"], payload_one["desk_note_patch"]["missing_info"])
        self.assertEqual(note_record["merge_candidates"], payload_one["desk_note_patch"]["merge_candidates"])
        self.assertEqual(note_record["split_suspicions"], payload_one["desk_note_patch"]["split_suspicions"])
        self.assertEqual(note_record["risk_summary_pl"], payload_one["desk_note_patch"]["risk_summary_pl"])
        self.assertEqual(note_record["risks"], payload_one["desk_note_patch"]["risks"])
        self.assertEqual(note_record["automation_policy"], payload_one["desk_note_patch"]["automation_policy"])
        self.assertIn("feedback_state", note_record)
        self.assertEqual(note_record["feedback_state"]["trafne"], 0)

        self.assertEqual(case_detail["thread_memory"]["thread_id"], payload_one["thread_memory"]["thread_id"])
        self.assertIn("nowym terminem", case_detail["thread_memory"]["canonical_thread_summary"])
        self.assertIn("Czy klient potwierdzil nowy termin realizacji?", case_detail["thread_memory"]["unresolved_questions"])
        self.assertEqual(len(case_detail["operational_timeline"]), 4)
        self.assertEqual(result["counts"]["signals"], 2)
        self.assertEqual(result["counts"]["decision_traces"], 2)
        self.assertEqual(result["counts"]["event_log"], 4)


if __name__ == "__main__":
    unittest.main()
