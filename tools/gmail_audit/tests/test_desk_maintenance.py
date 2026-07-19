from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from desk_maintenance import (
    FEEDBACK_BLOCK_DAYS,
    apply_maintenance_actions,
    build_maintenance_ingest_payload,
    collect_maintenance_preview,
    persist_maintenance_artifacts,
)
from tests.fixture_helpers import run_fixture
from v2_runtime import build_v2_ingest_payload


REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS_PATH = Path(__file__).resolve().parents[4] / "daszek" / "tools" / "v2-store-harness.php"
CLI_PATH = TOOL_DIR / "gmail_intake.py"
FIXED_NOW = datetime(2026, 4, 11, 12, 0, tzinfo=timezone.utc)


def _iso_at(moment: datetime) -> str:
    return moment.isoformat()


def _note_card_from_detail(detail: dict[str, object]) -> dict[str, object]:
    note = detail["note"]
    return {
        "note_id": note["note_id"],
        "case_id": note["case_id"],
        "presence_mode": note["presence_mode"],
        "visibility_score": note.get("visibility_score", 0.6),
        "updated_at": note["updated_at"],
        "day_bucket": note.get("day_bucket", "dzisiaj"),
        "surface_zone": note.get("surface_zone", "desk"),
    }


def _build_detail(
    *,
    note_id: str,
    case_id: str,
    now: datetime,
    case_status: str = "open",
    open_desk_note_id: str = "",
    presence_mode: str = "strong",
    surface_zone: str = "desk",
    lifecycle_state: str = "active",
    updated_at: datetime | None = None,
    latest_signal_at: datetime | None = None,
    feedback_last_action_at: datetime | None = None,
    unresolved_questions: list[str] | None = None,
    primary_next_action_type: str = "",
    missing_info_summary_pl: str = "",
    risk_summary_pl: str = "",
) -> dict[str, object]:
    updated = updated_at or now
    latest_signal = latest_signal_at or updated
    feedback_state = {
        "trafne": 0,
        "za_mocne": 0,
        "za_slabe": 0,
        "tylko_w_sprawie": 0,
        "nie_pokazuj_takich": 0,
        "polacz_ze_sprawa": 0,
        "to_juz_nieaktualne": 0,
        "ostatnia_akcja": "trafne" if feedback_last_action_at else "",
        "ostatnia_akcja_at": _iso_at(feedback_last_action_at) if feedback_last_action_at else "",
    }
    note = {
        "note_id": note_id,
        "desk_note_id": note_id,
        "case_id": case_id,
        "title": f"Note {note_id}",
        "summary": f"Summary {note_id}",
        "why_on_desk": f"Why {note_id}",
        "recommended_next_step": "Check manually.",
        "presence_mode": presence_mode,
        "surface_zone": surface_zone,
        "lifecycle_state": lifecycle_state,
        "updated_at": _iso_at(updated),
        "latest_signal_at": _iso_at(latest_signal),
        "source_signal_ids": [f"sig_{note_id}"],
        "feedback_state": feedback_state,
        "day_bucket": "teraz",
        "unresolved_questions": unresolved_questions or [],
        "primary_next_action_type": primary_next_action_type,
        "missing_info_summary_pl": missing_info_summary_pl,
        "risk_summary_pl": risk_summary_pl,
        "missing_info": [{"code": "missing"}] if missing_info_summary_pl else [],
        "risks": [{"code": "risk"}] if risk_summary_pl else [],
        "visibility_score": 0.8,
        "source_message_id": f"msg_{note_id}",
    }
    case = {
        "case_id": case_id,
        "title": f"Case {case_id}",
        "status": case_status,
        "latest_signal_id": f"sig_case_{case_id}",
        "latest_signal_at": _iso_at(latest_signal),
        "open_desk_note_id": open_desk_note_id or note_id,
    }
    return {
        "ok": True,
        "generated_at": _iso_at(now),
        "note": note,
        "case": case,
        "signals": [],
        "decision_traces": [],
        "operational_timeline": [],
    }


class StaticMaintenanceClient:
    def __init__(self, details: list[dict[str, object]]) -> None:
        self._details = {
            str((detail.get("note") or {}).get("note_id") or ""): copy.deepcopy(detail)
            for detail in details
            if isinstance(detail, dict) and isinstance(detail.get("note"), dict)
        }

    def _visible_cards(self, *, surface_scope: str, include_subtle: bool) -> list[dict[str, object]]:
        cards: list[dict[str, object]] = []
        for detail in self._details.values():
            note = detail["note"]
            if note.get("lifecycle_state") != "active":
                continue
            if note.get("presence_mode") == "silent":
                continue
            if surface_scope == "desk" and note.get("surface_zone") != "desk":
                continue
            if surface_scope == "day" and note.get("surface_zone") not in {"desk", "day"}:
                continue
            if not include_subtle and note.get("presence_mode") == "subtle":
                continue
            cards.append(_note_card_from_detail(detail))
        return cards

    def get_v2_desk(self, *, include_subtle: bool = False) -> dict[str, object]:
        items = self._visible_cards(surface_scope="desk", include_subtle=include_subtle)
        return {"ok": True, "items": items}

    def get_v2_day(self, *, include_subtle: bool = False) -> dict[str, object]:
        items = self._visible_cards(surface_scope="day", include_subtle=include_subtle)
        return {"ok": True, "sections": [{"key": "teraz", "title": "Teraz", "items": items}]}

    def get_v2_note_detail(self, note_id: str) -> dict[str, object]:
        return copy.deepcopy(self._details[str(note_id)])


def _run_store_harness(
    *,
    payloads: list[dict[str, object]] | None = None,
    feedback_actions: list[dict[str, object]] | None = None,
    storage_dir: Path | None = None,
    case_id: str = "",
    note_id: str = "",
    include_subtle: bool = True,
) -> dict[str, object]:
    input_payload = {
        "payloads": payloads or [],
        "feedback_actions": feedback_actions or [],
        "case_id": case_id,
        "note_id": note_id,
        "include_subtle": include_subtle,
    }
    if storage_dir is not None:
        input_payload["storage_dir"] = str(storage_dir)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(input_payload, handle, ensure_ascii=False, indent=2)
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


class HarnessMaintenanceClient:
    def __init__(self, storage_dir: Path) -> None:
        self.storage_dir = storage_dir

    def seed(self, payloads: list[dict[str, object]], *, case_id: str = "", note_id: str = "") -> dict[str, object]:
        return _run_store_harness(
            payloads=payloads,
            storage_dir=self.storage_dir,
            case_id=case_id,
            note_id=note_id,
        )

    def snapshot(self, *, case_id: str = "", note_id: str = "", include_subtle: bool = True) -> dict[str, object]:
        return _run_store_harness(
            storage_dir=self.storage_dir,
            case_id=case_id,
            note_id=note_id,
            include_subtle=include_subtle,
        )

    def apply_feedback(self, note_id: str, action: str, target_case_id: str = "") -> dict[str, object]:
        return _run_store_harness(
            storage_dir=self.storage_dir,
            note_id=note_id,
            feedback_actions=[{"note_id": note_id, "action": action, "target_case_id": target_case_id}],
        )

    def get_v2_desk(self, *, include_subtle: bool = False) -> dict[str, object]:
        return self.snapshot(include_subtle=include_subtle)["desk"]

    def get_v2_day(self, *, include_subtle: bool = False) -> dict[str, object]:
        return self.snapshot(include_subtle=include_subtle)["day"]

    def get_v2_note_detail(self, note_id: str) -> dict[str, object]:
        return self.snapshot(note_id=note_id)["note_detail"]

    def push_v2_projection(self, payload: dict[str, object]) -> SimpleNamespace:
        note_id = str(((payload.get("desk_note_patch") or {}).get("desk_note_id")) or "")
        case_id = str(((payload.get("case_patch") or {}).get("case_id")) or ((payload.get("desk_note_patch") or {}).get("case_id")) or "")
        result = _run_store_harness(
            payloads=[payload],
            storage_dir=self.storage_dir,
            case_id=case_id,
            note_id=note_id,
        )
        persisted = (result.get("persisted") or [{}])[0]
        return SimpleNamespace(
            status="ingested",
            message_id=str(payload.get("message_key") or ""),
            signal_id=str(persisted.get("signal_id") or "") or None,
            trace_id=str(persisted.get("trace_id") or "") or None,
            details={"persisted": persisted},
        )


def _build_seed_payload(*, case_status: str = "open", presence_mode: str = "strong") -> dict[str, object]:
    fixture = run_fixture("active_case_follow_up")
    payload = build_v2_ingest_payload(
        run_id="maintenance-seed",
        message_key=str(fixture["snapshot"]["source_message"]["message_id"]),
        v2_projection=copy.deepcopy(fixture["v2_projection"]),
    )
    payload["case_patch"]["status"] = case_status
    payload["desk_note_patch"]["presence_mode"] = presence_mode
    payload["desk_note_patch"]["surface_zone"] = "desk"
    payload["desk_note_patch"]["lifecycle"] = "active"
    payload["desk_note_patch"]["day_bucket"] = "teraz"
    payload["operational_events"] = []
    return payload


class DeskMaintenancePreviewTests(unittest.TestCase):
    def test_closed_case_moves_note_to_case_only(self) -> None:
        detail = _build_detail(note_id="note_closed", case_id="case_closed", now=FIXED_NOW, case_status="closed")
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)

        self.assertEqual(preview["summary"]["candidate_count"], 1)
        self.assertEqual(len(preview["proposed_actions"]), 1)
        proposal = preview["proposed_actions"][0]
        self.assertEqual(proposal["rule_name"], "closed_case_move_to_case_only")
        self.assertEqual(proposal["lifecycle_intent"], "move_to_case_only")
        self.assertEqual(proposal["persistence_command"], "deescalate_presence")

    def test_duplicate_active_note_withdraws_non_keeper(self) -> None:
        keep_detail = _build_detail(
            note_id="note_keep",
            case_id="case_dup",
            now=FIXED_NOW,
            open_desk_note_id="note_keep",
            updated_at=FIXED_NOW - timedelta(hours=1),
        )
        lose_detail = _build_detail(
            note_id="note_lose",
            case_id="case_dup",
            now=FIXED_NOW,
            open_desk_note_id="note_keep",
            presence_mode="advisory",
            updated_at=FIXED_NOW - timedelta(hours=2),
        )
        preview = collect_maintenance_preview(StaticMaintenanceClient([keep_detail, lose_detail]), now=FIXED_NOW)

        self.assertEqual(len(preview["proposed_actions"]), 1)
        proposal = preview["proposed_actions"][0]
        self.assertEqual(proposal["note_id"], "note_lose")
        self.assertEqual(proposal["rule_name"], "duplicate_active_note_withdraw")
        self.assertEqual(proposal["lifecycle_intent"], "withdraw")

    def test_stale_note_softens_only_one_presence_level(self) -> None:
        stale_at = FIXED_NOW - timedelta(days=5)
        detail = _build_detail(
            note_id="note_stale",
            case_id="case_stale",
            now=FIXED_NOW,
            presence_mode="strong",
            updated_at=stale_at,
            latest_signal_at=stale_at,
        )
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)

        self.assertEqual(len(preview["proposed_actions"]), 1)
        proposal = preview["proposed_actions"][0]
        self.assertEqual(proposal["rule_name"], "stale_presence_soften")
        self.assertEqual(proposal["lifecycle_intent"], "deescalate_presence")
        self.assertEqual(proposal["target_presence_mode"], "advisory")

    def test_subtle_without_attention_moves_to_case_only(self) -> None:
        stale_at = FIXED_NOW - timedelta(days=6)
        detail = _build_detail(
            note_id="note_subtle",
            case_id="case_subtle",
            now=FIXED_NOW,
            presence_mode="subtle",
            updated_at=stale_at,
            latest_signal_at=stale_at,
        )
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)

        self.assertEqual(len(preview["proposed_actions"]), 1)
        proposal = preview["proposed_actions"][0]
        self.assertEqual(proposal["rule_name"], "subtle_without_attention_move_to_case_only")
        self.assertEqual(proposal["target_surface_zone"], "case_only")

    def test_recent_manual_feedback_blocks_preview_apply(self) -> None:
        detail = _build_detail(
            note_id="note_feedback",
            case_id="case_feedback",
            now=FIXED_NOW,
            case_status="closed",
            feedback_last_action_at=FIXED_NOW - timedelta(days=2),
        )
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)

        self.assertEqual(len(preview["proposed_actions"]), 0)
        self.assertEqual(len(preview["noops"]), 1)
        self.assertEqual(preview["noops"][0]["reason_code"], "blocked_by_recent_feedback")
        self.assertEqual(preview["summary"]["feedback_blocked_count"], 1)

    def test_maintenance_payload_uses_stable_ids_and_actor(self) -> None:
        detail = _build_detail(note_id="note_payload", case_id="case_payload", now=FIXED_NOW, case_status="closed")
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)
        proposal = preview["proposed_actions"][0]

        payload_one = build_maintenance_ingest_payload(
            run_id="maintenance-a",
            proposal=proposal,
            note_detail=detail,
            emitted_at="2026-04-11T12:00:00+00:00",
        )
        payload_two = build_maintenance_ingest_payload(
            run_id="maintenance-b",
            proposal=proposal,
            note_detail=detail,
            emitted_at="2026-04-11T12:00:00+00:00",
        )

        self.assertEqual(payload_one["signal_projection"]["signal_id"], payload_two["signal_projection"]["signal_id"])
        self.assertEqual(payload_one["decision_trace"]["trace_id"], payload_two["decision_trace"]["trace_id"])
        self.assertEqual(
            payload_one["operational_events"][0]["event_id"],
            payload_two["operational_events"][0]["event_id"],
        )
        self.assertEqual(payload_one["decision_trace"]["actor"], "system_maintenance")
        self.assertTrue(payload_one["decision_trace"]["decision_type"].startswith("maintenance_"))

    def test_preview_artifacts_are_written_without_side_effects(self) -> None:
        detail = _build_detail(note_id="note_artifact", case_id="case_artifact", now=FIXED_NOW, case_status="closed")
        preview = collect_maintenance_preview(StaticMaintenanceClient([detail]), now=FIXED_NOW)

        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            persist_maintenance_artifacts(run_dir, preview=preview, manifest={"run_id": "maintenance-preview"})
            self.assertTrue((run_dir / "candidates.jsonl").is_file())
            self.assertTrue((run_dir / "proposed_actions.jsonl").is_file())
            self.assertTrue((run_dir / "noops.jsonl").is_file())
            self.assertTrue((run_dir / "summary.json").is_file())

            summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "preview")
            self.assertEqual(summary["candidate_count"], 1)


class DeskMaintenanceIntegrationTests(unittest.TestCase):
    def test_preview_apply_apply_is_idempotent_via_php_store(self) -> None:
        payload = _build_seed_payload(case_status="closed", presence_mode="strong")
        case_id = str(payload["case_patch"]["case_id"])
        note_id = str(payload["desk_note_patch"]["desk_note_id"])

        with tempfile.TemporaryDirectory() as temp_dir:
            client = HarnessMaintenanceClient(Path(temp_dir))
            seeded = client.seed([payload], case_id=case_id, note_id=note_id)
            before_note = seeded["note_detail"]["note"]
            before_case = seeded["case_detail"]["case"]

            preview = collect_maintenance_preview(client, case_id=case_id, note_id=note_id, now=FIXED_NOW)
            self.assertEqual(len(preview["proposed_actions"]), 1)
            self.assertEqual(preview["proposed_actions"][0]["rule_name"], "closed_case_move_to_case_only")

            first_apply = apply_maintenance_actions(client, run_id="maintenance-apply-1", preview=preview, now=FIXED_NOW)
            after_first = client.snapshot(case_id=case_id, note_id=note_id)
            after_first_note = after_first["note_detail"]["note"]
            after_first_case = after_first["case_detail"]["case"]

            self.assertEqual(first_apply["summary"]["apply_failed_count"], 0)
            self.assertEqual(after_first_note["surface_zone"], "case_only")
            self.assertEqual(after_first_note["presence_mode"], "silent")
            self.assertEqual(after_first_note["latest_signal_at"], before_note["latest_signal_at"])
            self.assertEqual(after_first_case["latest_signal_id"], before_case["latest_signal_id"])
            self.assertEqual(after_first_case["latest_signal_at"], before_case["latest_signal_at"])
            self.assertEqual(after_first["counts"]["signals"], seeded["counts"]["signals"] + 1)
            self.assertEqual(after_first["counts"]["decision_traces"], seeded["counts"]["decision_traces"] + 1)
            self.assertEqual(after_first["counts"]["event_log"], seeded["counts"]["event_log"] + 1)
            self.assertEqual(after_first["note_detail"]["last_change"]["source"], "maintenance")
            self.assertTrue(after_first["note_detail"]["last_change"]["decision_type"].startswith("maintenance_"))
            self.assertEqual(after_first["desk"]["counts"]["visible"], 0)

            second_apply = apply_maintenance_actions(client, run_id="maintenance-apply-2", preview=preview, now=FIXED_NOW)
            after_second = client.snapshot(case_id=case_id, note_id=note_id)
            preview_after = collect_maintenance_preview(client, case_id=case_id, note_id=note_id, now=FIXED_NOW)

            self.assertEqual(second_apply["summary"]["apply_noop_count"], 1)
            self.assertEqual(after_second["counts"]["signals"], after_first["counts"]["signals"])
            self.assertEqual(after_second["counts"]["decision_traces"], after_first["counts"]["decision_traces"])
            self.assertEqual(after_second["counts"]["event_log"], after_first["counts"]["event_log"])
            self.assertEqual(preview_after["summary"]["proposed_action_count"], 0)

    def test_recent_operator_feedback_blocks_maintenance_and_surfaces_operator_provenance(self) -> None:
        payload = _build_seed_payload(case_status="closed", presence_mode="strong")
        case_id = str(payload["case_patch"]["case_id"])
        note_id = str(payload["desk_note_patch"]["desk_note_id"])
        now = datetime.now().astimezone()

        with tempfile.TemporaryDirectory() as temp_dir:
            client = HarnessMaintenanceClient(Path(temp_dir))
            client.seed([payload], case_id=case_id, note_id=note_id)
            client.apply_feedback(note_id, "trafne")

            preview = collect_maintenance_preview(client, case_id=case_id, note_id=note_id, now=now)
            note_detail = client.get_v2_note_detail(note_id)

            self.assertEqual(preview["summary"]["proposed_action_count"], 0)
            self.assertEqual(preview["summary"]["feedback_blocked_count"], 1)
            self.assertEqual(preview["noops"][0]["reason_code"], "blocked_by_recent_feedback")
            self.assertTrue(note_detail["maintenance_guard"]["blocked"])
            self.assertEqual(note_detail["last_change"]["source"], "operator")
            self.assertTrue(
                any(trace.get("actor_source") == "operator" for trace in note_detail["decision_traces"]),
            )


class DeskMaintenanceCliSmokeTests(unittest.TestCase):
    def test_maintain_desk_help_lists_preview_and_apply(self) -> None:
        result = subprocess.run(
            [sys.executable, str(CLI_PATH), "maintain-desk", "--help"],
            cwd=TOOL_DIR,
            capture_output=True,
            text=True,
        )

        self.assertEqual(
            result.returncode,
            0,
            msg=f"CLI help failed\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}",
        )
        self.assertIn("--preview", result.stdout)
        self.assertIn("--apply", result.stdout)
        self.assertIn("--case-id", result.stdout)
        self.assertIn("--note-id", result.stdout)


if __name__ == "__main__":
    unittest.main()
