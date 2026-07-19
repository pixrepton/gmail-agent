"""Tests for the Gate B runtime proof guard script."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gate_b_runtime_proof.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("gate_b_runtime_proof", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_activation(proof: Path, *, intake_schema_host: str = "def456", intake_schema_container: str = "def456") -> None:
    activation = proof / "activation"
    activation.mkdir()
    (activation / "host-gmail_intake.sha256").write_text(
        "abc123  tools/gmail_audit/gmail_intake.py\n", encoding="utf-8"
    )
    (activation / "container-gmail_intake.sha256").write_text(
        "abc123  /app/tools/gmail_audit/gmail_intake.py\n", encoding="utf-8"
    )
    (activation / "host-intake_schema.sha256").write_text(
        f"{intake_schema_host}  tools/gmail_audit/intake_schema.py\n", encoding="utf-8"
    )
    (activation / "container-intake_schema.sha256").write_text(
        f"{intake_schema_container}  /app/tools/gmail_audit/intake_schema.py\n", encoding="utf-8"
    )
    _write_json(
        activation / "runtime-import.json",
        {
            "container_import_path": "/app/tools/gmail_audit/gmail_intake.py",
            "artifact_mount_verified": True,
        },
    )


def _write_row3_v3_transport_only(batch_dir: Path, *, requested: int, attempted: int, failed: int = 0) -> None:
    _write_json(
        batch_dir / "sequential_summary.json",
        {
            "status": "completed",
            "requested_count": requested,
            "attempted_count": attempted,
            "failed_count": failed,
        },
    )
    _write_json(
        batch_dir / "projection_proof_report.batch.json",
        {
            "summary": {
                "primary_surface_mode": "feed_first",
                "daszek_feed_source": "engagement_snapshot_v2",
                "v3_feed_push_ok": attempted,
                "v3_feed_push_failed": 0,
                "feed_handoff_actionable": 0,
                "aggregates_by_policy_status": {"accepted_projection": attempted},
            },
            "items": [
                {
                    "message_id": "mid-feed",
                    "signal_id": "sig-feed",
                    "case_id": "",
                    "engagement_id": "",
                    "snapshot_id": "snap-feed",
                    "title": "Feed title",
                    "source_message_id": "mid-feed",
                    "policy_status": "accepted_projection",
                    "surface": "v3_operational_feed",
                    "feed_handoff_actionable": False,
                }
            ],
        },
    )


def _write_row3_v3_feed(batch_dir: Path, *, requested: int, attempted: int, failed: int = 0, staging: bool = False) -> None:
    item = {
        "message_id": "mid-feed",
        "signal_id": "sig-feed",
        "case_id": "" if staging else "case-feed",
        "engagement_id": "eng-feed" if staging else "",
        "snapshot_id": "snap-feed",
        "title": "Feed title",
        "source_message_id": "mid-feed",
        "policy_status": "accepted_projection",
        "surface": "v3_operational_feed",
        "feed_handoff_actionable": True,
        "feed_handoff_mode": "staging" if staging else "case_ready",
        "handoff_tier": "row4a",
    }
    if not staging:
        item["case_id"] = "case-feed"
    _write_json(
        batch_dir / "sequential_summary.json",
        {
            "status": "completed",
            "requested_count": requested,
            "attempted_count": attempted,
            "failed_count": failed,
        },
    )
    _write_json(
        batch_dir / "projection_proof_report.batch.json",
        {
            "summary": {
                "primary_surface_mode": "feed_first",
                "daszek_feed_source": "engagement_snapshot_v2",
                "v3_feed_push_ok": attempted,
                "v3_feed_push_failed": 0,
                "feed_handoff_actionable": 1,
                "aggregates_by_policy_status": {"accepted_projection": attempted},
            },
            "items": [item],
        },
    )


def _write_row3(batch_dir: Path, *, requested: int, attempted: int, failed: int = 0) -> None:
    _write_json(
        batch_dir / "sequential_summary.json",
        {
            "status": "completed",
            "requested_count": requested,
            "attempted_count": attempted,
            "failed_count": failed,
        },
    )
    _write_json(
        batch_dir / "projection_proof_report.batch.json",
        {
            "summary": {
                "v2_projection_accepted": 1,
                "v2_readback_found": 1,
                "aggregates_by_policy_status": {"accepted_projection": attempted},
            },
            "items": [
                {
                    "message_id": "mid-actionable",
                    "signal_id": "sig-actionable",
                    "case_id": "case-actionable",
                    "note_id": "note-actionable",
                    "title": "Actionable title",
                    "source_message_id": "mid-actionable",
                    "policy_status": "accepted_projection",
                    "surface": "v2_ingest",
                    "store_readback": "found",
                    "handoff_actionable": True,
                    "operator_action_available": True,
                    "allowed_operator_actions": ["zla_sprawa"],
                    "expected_bridge_domain": "adjudication",
                    "expected_adjudication_kind": "reject_same_case",
                }
            ],
        },
    )


class GateBRuntimeProofTests(unittest.TestCase):
    def test_classify_row4a_blocked_when_doctor_feed_primary_without_handoff(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_json(
                proof / "doctor.json",
                {
                    "checks": {
                        "config": {
                            "daszek_operational_feed_auto_push_enabled": True,
                            "daszek_v2_push_enabled": False,
                        },
                        "daszek_engagement_feed": {
                            "engagement_feed_enabled": True,
                            "feed_source_env": "engagement_snapshot_v2",
                        },
                    }
                },
            )
            _write_row3_v3_feed(proof / "row3-1", requested=1, attempted=1)
            batch = json.loads((proof / "row3-1" / "projection_proof_report.batch.json").read_text(encoding="utf-8"))
            batch["items"][0]["case_id"] = ""
            batch["items"][0]["feed_handoff_actionable"] = False
            batch["summary"]["feed_handoff_actionable"] = 0
            _write_json(proof / "row3-1" / "projection_proof_report.batch.json", batch)
            row4a = mod.classify_row4a(proof)
        self.assertEqual(row4a["status"], "blocked")
        self.assertIn("Row4a handoff anchor", " ".join(row4a["reasons"]))

    def test_render_vps_script_resolves_curated_row3_cohort(self) -> None:
        mod = _load_guard()
        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            phase="row3-stop",
        )
        self.assertIn("pick_gate_b_row3_cohort.py", script)
        self.assertIn("ROW3_COHORT_ARGS", script)
        self.assertIn("$ROW3_COHORT_ARGS", script)

    def test_render_vps_script_forces_image_activation_and_host_visible_artifacts(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
        )

        self.assertIn('--profile worker up -d --build --force-recreate "$SERVICE"', script)
        self.assertIn('PROOF_DIR=$(cd "$PROOF_DIR" && pwd)', script)
        self.assertIn('-v "$PROOF_DIR:/app/gate-b-proof:rw"', script)
        self.assertIn("trap classify_on_exit EXIT", script)
        self.assertIn("--check-daszek-v3-feed", script)
        self.assertNotIn("--check-daszek-v2-read", script)
        self.assertIn("--batch-dir /app/gate-b-proof/row3-1", script)
        self.assertIn("--batch-dir /app/gate-b-proof/row3-10", script)
        self.assertIn("--max-retries-per-message 5", script)
        self.assertIn("--retry-base-delay 45", script)
        self.assertIn("CASE_OS_RUNTIME_PROFILE=${CASE_OS_RUNTIME_PROFILE:-full}", script)
        self.assertIn("INTAKE_LLM_BEFORE_SIGNAL=${INTAKE_LLM_BEFORE_SIGNAL:-1}", script)
        self.assertIn("host-intake_schema.sha256", script)
        self.assertIn("container-intake_schema.sha256", script)
        self.assertNotIn("DASZEK_V2_PUSH=${", script)
        self.assertNotIn("DASZEK_OPERATIONAL_FEED_AUTO_PUSH=${", script)
        self.assertNotIn("DASZEK_FEED_SOURCE=${", script)
        self.assertIn("--delay 45", script)
        self.assertIn("daszek-bridge-drain --remote --domain adjudication --dry-run", script)
        self.assertIn("daszek-bridge-drain --remote --domain adjudication --max-items 1", script)
        self.assertIn("create one real Row 4 pending decision", script)
        self.assertIn('HOST_PYTHON="${HOST_PYTHON:-python3}"', script)
        self.assertIn("\"$HOST_PYTHON\" scripts/gate_b_runtime_proof.py classify --proof-dir \"$PROOF_DIR\"", script)

    def test_render_phase_row3_stop_writes_handoff_and_exits_before_row4_block(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            phase="row3-stop",
        )

        self.assertIn('GATE_B_PHASE="row3-stop"', script)
        self.assertIn("OPERATOR_ROW4_HANDOFF.txt", script)
        self.assertIn("PHASE=row3-stop COMPLETE", script)
        self.assertIn("trap - EXIT", script)

    def test_render_phase_row4_only_skips_activation_prefix_branch(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            phase="row4-only",
        )

        self.assertIn('GATE_B_PHASE="row4-only"', script)
        self.assertIn('[[ "$GATE_B_PHASE" != "row4-only" ]]', script)
        self.assertIn("daszek-bridge-drain --remote --domain adjudication --dry-run", script)

    def test_render_row3_exclude_message_ids_passes_to_all_row3_cohorts(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            phase="row3-stop",
            row3_exclude_message_ids=["skip-mid"],
        )

        self.assertEqual(script.count("--exclude-message-id skip-mid"), 3)

    def test_render_row3_exclude_message_ids_dedupes_and_shell_quotes(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            row3_exclude_message_ids=["plain", "plain", "space id"],
        )

        self.assertEqual(script.count("--exclude-message-id plain"), 3)
        self.assertEqual(script.count("--exclude-message-id 'space id'"), 3)

    def test_render_row3_message_ids_passes_explicit_cohort_to_all_row3_runs(self) -> None:
        mod = _load_guard()

        script = mod.render_vps_runner_script(
            proof_dir="runs/gate-b-proof-test",
            env_file=".env.vps",
            compose_file="docker-compose.vps.yml",
            service="gmail-agent-worker",
            row3_message_ids=["accepted-mid", "blocked-mid"],
        )

        self.assertEqual(script.count("--message-id accepted-mid --message-id blocked-mid"), 3)

    def test_render_invalid_phase_raises(self) -> None:
        mod = _load_guard()

        with self.assertRaises(ValueError):
            mod.render_vps_runner_script(
                proof_dir="runs/x",
                env_file=".env.vps",
                compose_file="docker-compose.vps.yml",
                service="gmail-agent-worker",
                phase="invalid-phase",
            )

    def test_classify_activation_blocks_when_intake_schema_hashes_differ(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_activation(proof, intake_schema_host="host123", intake_schema_container="container456")

            activation = mod.classify_activation(proof)

        self.assertEqual(activation["status"], "blocked")
        self.assertIn("intake_schema.py hashes differ", " ".join(activation["reasons"]))

    def test_classify_gate_b_artifacts_green_when_activation_row3_and_row4_are_proven(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_activation(proof)
            _write_row3(proof / "row3-1", requested=1, attempted=1)
            _write_row3(proof / "row3-3", requested=3, attempted=3)
            _write_row3(proof / "row3-10", requested=10, attempted=10)
            _write_json(proof / "row4" / "dry-run.json", {"ok": True, "dry_run": True, "items": [{"queue_id": "q1"}]})
            _write_json(
                proof / "row4" / "drain.json",
                {
                    "ok": True,
                    "source": "remote",
                    "results": [
                        {
                            "queue_id": "q1",
                            "ok": True,
                            "bridge_out": {
                                "truth_loop_executed": True,
                                "reconcile_signal_ran": True,
                                "reconcile_summary": {"processing_state": "reconciled"},
                            },
                        }
                    ],
                },
            )

            status = mod.classify_gate_b_artifacts(proof)

        self.assertEqual(status["status"], "green")
        self.assertEqual(status["rows"]["activation"]["status"], "green")
        self.assertEqual(status["rows"]["row3"]["status"], "green")
        self.assertEqual(status["rows"]["row4"]["status"], "green")
        self.assertEqual(status["rows"]["row4a"]["status"], "green")

    def test_classify_row3_green_with_v3_feed_primary(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_row3_v3_feed(proof / "row3-1", requested=1, attempted=1)
            _write_row3_v3_feed(proof / "row3-3", requested=3, attempted=3)
            _write_row3_v3_feed(proof / "row3-10", requested=10, attempted=10)
            row3 = mod.classify_row3(proof)
        self.assertEqual(row3["status"], "green")

    def test_classify_gate_b_bounded_yellow_when_row4b_blocked_in_feed_primary(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_activation(proof)
            _write_row3_v3_feed(proof / "row3-1", requested=1, attempted=1)
            _write_row3_v3_feed(proof / "row3-3", requested=3, attempted=3)
            _write_row3_v3_feed(proof / "row3-10", requested=10, attempted=10)
            (proof / "row4").mkdir(parents=True, exist_ok=True)
            _write_json(proof / "row4" / "dry-run.json", {"ok": False})
            status = mod.classify_gate_b_artifacts(proof)
        self.assertEqual(status["rows"]["row3"]["status"], "green")
        self.assertEqual(status["rows"]["row4a"]["status"], "green")
        self.assertEqual(status["rows"]["row4b"]["status"], "blocked")
        self.assertEqual(status["status"], "yellow")

    def test_classify_gate_b_artifacts_blocks_on_unknown_row3_policy(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_activation(proof)
            _write_row3(proof / "row3-1", requested=1, attempted=1)
            _write_row3(proof / "row3-3", requested=3, attempted=3)
            _write_row3(proof / "row3-10", requested=10, attempted=10)
            batch = json.loads((proof / "row3-10" / "projection_proof_report.batch.json").read_text(encoding="utf-8"))
            batch["items"] = [{"policy_status": "unknown", "surface": "v2_ingest"}]
            _write_json(proof / "row3-10" / "projection_proof_report.batch.json", batch)
            _write_json(proof / "row4" / "dry-run.json", {"ok": True, "dry_run": True, "items": [{"queue_id": "q1"}]})
            _write_json(
                proof / "row4" / "drain.json",
                {
                    "ok": True,
                    "results": [
                        {
                            "queue_id": "q1",
                            "ok": True,
                            "bridge_out": {
                                "truth_loop_executed": True,
                                "reconcile_signal_ran": True,
                                "reconcile_summary": {"processing_state": "reconciled"},
                            },
                        }
                    ],
                },
            )

            status = mod.classify_gate_b_artifacts(proof)

        self.assertEqual(status["rows"]["row3"]["status"], "yellow")
        self.assertIn("unknown", " ".join(status["rows"]["row3"]["reasons"]))

    def test_classify_row4a_green_with_staging_engagement_anchor(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_row3_v3_feed(proof / "row3-1", requested=1, attempted=1, staging=True)
            row4a = mod.classify_row4a(proof)
        self.assertEqual(row4a["status"], "green")

    def test_classify_gate_b_row3_stop_green_when_transport_ok_without_row4(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_activation(proof)
            _write_row3_v3_transport_only(proof / "row3-1", requested=1, attempted=1)
            _write_row3_v3_transport_only(proof / "row3-3", requested=3, attempted=3)
            _write_row3_v3_transport_only(proof / "row3-10", requested=10, attempted=10)
            status = mod.classify_gate_b_artifacts(proof)
        self.assertEqual(status["status"], "green")
        self.assertEqual(status["rows"]["row3"]["status"], "green")
        self.assertEqual(status["rows"]["row4a"]["status"], "blocked")
        self.assertIn("row3-stop", status["gate"])

    def test_classify_row3_blocks_when_projection_has_no_actionable_handoff_anchor(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_row3(proof / "row3-1", requested=1, attempted=1)
            _write_row3(proof / "row3-3", requested=3, attempted=3)
            _write_row3(proof / "row3-10", requested=10, attempted=10)
            for batch_name in ("row3-1", "row3-3", "row3-10"):
                batch_path = proof / batch_name / "projection_proof_report.batch.json"
                batch = json.loads(batch_path.read_text(encoding="utf-8"))
                batch["summary"]["operator_handoff_actionable"] = 0
                batch["items"][0]["case_id"] = ""
                batch["items"][0]["handoff_actionable"] = False
                batch["items"][0]["operator_action_available"] = False
                _write_json(batch_path, batch)

            row3 = mod.classify_row3(proof)

        self.assertEqual(row3["status"], "green")

    def test_classify_row3_ignores_stale_unknown_outside_cohort(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            for name, requested, attempted in (("row3-1", 1, 1), ("row3-3", 3, 3), ("row3-10", 10, 10)):
                _write_row3_v3_feed(proof / name, requested=requested, attempted=attempted)
                _write_json(proof / name / "selected_message_ids.json", ["mid-feed"])
            batch_path = proof / "row3-1" / "projection_proof_report.batch.json"
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            batch["items"].append({"message_id": "stale-mid", "policy_status": "unknown", "surface": "none"})
            _write_json(batch_path, batch)
            row3 = mod.classify_row3(proof)
        self.assertEqual(row3["status"], "green")

    def test_classify_row3_yellow_when_cohort_has_unknown_items(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            for name, requested, attempted in (("row3-1", 1, 1), ("row3-3", 3, 3), ("row3-10", 10, 10)):
                _write_row3_v3_feed(proof / name, requested=requested, attempted=attempted)
                _write_json(proof / name / "selected_message_ids.json", ["mid-feed"])
            batch_path = proof / "row3-1" / "projection_proof_report.batch.json"
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            batch["items"][0]["policy_status"] = "unknown"
            batch["items"][0]["surface"] = "none"
            _write_json(batch_path, batch)
            row3 = mod.classify_row3(proof)
        self.assertEqual(row3["status"], "yellow")
        self.assertIn("unknown", " ".join(row3["reasons"]))

    def test_operator_handoff_artifact_includes_exact_actionable_anchor(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_row3(proof / "row3-1", requested=1, attempted=1)
            _write_row3(proof / "row3-3", requested=3, attempted=3)
            _write_row3(proof / "row3-10", requested=10, attempted=10)

            handoff = mod.build_operator_handoff(proof)
            text = mod.render_operator_handoff_text(handoff)

        self.assertTrue(handoff["actionable"])
        self.assertEqual(handoff["item"]["note_id"], "note-actionable")
        self.assertIn("Actionable title", text)
        self.assertIn("note-actionable", text)
        self.assertIn("case-actionable", text)
        self.assertIn("mid-actionable", text)
        self.assertIn("zla_sprawa", text)
        self.assertIn("domain=adjudication", text)
        self.assertIn("adjudication_kind=reject_same_case", text)

    def test_operator_handoff_artifact_refuses_when_anchor_incomplete(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            _write_row3(proof / "row3-1", requested=1, attempted=1)
            batch_path = proof / "row3-1" / "projection_proof_report.batch.json"
            batch = json.loads(batch_path.read_text(encoding="utf-8"))
            batch["items"][0]["case_id"] = ""
            batch["items"][0]["handoff_actionable"] = False
            _write_json(batch_path, batch)

            handoff = mod.build_operator_handoff(proof)
            text = mod.render_operator_handoff_text(handoff)

        self.assertFalse(handoff["actionable"])
        self.assertIn("handoff not actionable", text)
        self.assertIn("Do not run Row 4", text)

    def test_row3_cohort_minimums_relax_when_single_message_pinned(self) -> None:
        mod = _load_guard()
        with tempfile.TemporaryDirectory() as tmp:
            proof = Path(tmp)
            for name in ("row3-1", "row3-3", "row3-10"):
                batch = proof / name
                batch.mkdir(parents=True)
                _write_json(batch / "selected_message_ids.json", ["pinned-mid"])
                _write_row3(batch, requested=1, attempted=1)
            row3 = mod.classify_row3(proof)
        self.assertEqual(row3["status"], "green")


if __name__ == "__main__":
    unittest.main()
