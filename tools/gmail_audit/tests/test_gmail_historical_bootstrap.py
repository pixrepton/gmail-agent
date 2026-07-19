from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_historical_bootstrap import (  # noqa: E402
    GmailHistoricalBootstrapOptions,
    LIVE_BOOTSTRAP_CONFIRMATION_ERROR,
    classify_bootstrap_candidate,
    run_gmail_historical_bootstrap,
)
from artifact_io import read_json  # noqa: E402
from mailbox_memory_runtime import MailboxMemoryRuntime  # noqa: E402
from mailbox_memory_store import InMemoryMailboxMemoryStore  # noqa: E402


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        runtime_profile="",
        mailbox_memory_stage_mode="shadow",
        mailbox_memory_vector_enabled=False,
        groq_api_key="",
        openai_compat_api_key="",
        groq_model="test-model",
    )


def _profile(_settings, **_kwargs):
    return {"email": "biuro@topinstal.pl", "historyId": "hist-900"}


def _metadata_searcher(_settings, *, query: str, max_results: int, next_page_token=None, **_kwargs):
    _ = (query, max_results, next_page_token)
    return {
        "responses": [
            {
                "message_id": "msg-lead-1",
                "thread_id": "thr-lead-1",
                "history_id": "hist-101",
                "date": "2026-04-12T08:15:00+02:00",
                "from": "Jan Kowalski <jan@example.com>",
                "sender": "Jan Kowalski <jan@example.com>",
                "to": ["biuro@topinstal.pl"],
                "subject": "Prosze o oferte pompy ciepla",
                "snippet": "Dom 180 m2, prosze o wycene.",
                "labels": ["INBOX"],
                "attachment_parts": [
                    {
                        "filename": "projekt.pdf",
                        "mime_type": "application/pdf",
                        "attachment_id": "att-1",
                        "size_bytes": 1200,
                    }
                ],
                "attachment_names": ["projekt.pdf"],
            },
            {
                "message_id": "msg-news-1",
                "thread_id": "thr-news-1",
                "history_id": "hist-102",
                "date": "2026-04-12T08:20:00+02:00",
                "from": "newsletter@example.com",
                "sender": "newsletter@example.com",
                "to": ["biuro@topinstal.pl"],
                "subject": "Newsletter - unsubscribe",
                "snippet": "Marketing update",
                "labels": ["INBOX"],
            },
        ],
        "next_page_token": "",
        "result_size_estimate": 2,
    }


def _body_fetcher(_settings, *, message_id: str, **_kwargs):
    assert message_id == "msg-lead-1"
    return {
        "message_id": "msg-lead-1",
        "thread_id": "thr-lead-1",
        "history_id": "hist-101",
        "date": "2026-04-12T08:15:00+02:00",
        "from": "Jan Kowalski <jan@example.com>",
        "sender": "Jan Kowalski <jan@example.com>",
        "to": ["biuro@topinstal.pl"],
        "subject": "Prosze o oferte pompy ciepla",
        "snippet": "Dom 180 m2, prosze o wycene.",
        "body": "Prosze o oferte pompy ciepla dla domu 180 m2.",
        "labels": ["INBOX"],
        "attachment_parts": [
            {
                "filename": "projekt.pdf",
                "mime_type": "application/pdf",
                "attachment_id": "att-1",
                "size_bytes": 1200,
            }
        ],
        "attachment_names": ["projekt.pdf"],
        "has_attachment": True,
    }


def _body_fetcher_rotated_attachment_id(_settings, *, message_id: str, **_kwargs):
    payload = _body_fetcher(_settings, message_id=message_id, **_kwargs)
    payload["attachment_parts"] = [dict(payload["attachment_parts"][0], attachment_id="att-rotated")]
    return payload


class GmailHistoricalBootstrapTests(unittest.TestCase):
    def test_metadata_scan_dry_run_has_no_memory_or_llm_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            options = GmailHistoricalBootstrapOptions(
                days_back=90,
                limit=50,
                metadata_only=True,
                dry_run=True,
                no_llm=True,
                proof_dir=Path(tmp),
            )
            summary = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=None,
                options=options,
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
            )

            self.assertTrue(summary["dry_run"])
            self.assertEqual(summary["metadata_scan_summary"]["total_seen"], 2)
            self.assertEqual(summary["candidate_selection_summary"]["candidate_count"], 1)
            self.assertEqual(summary["backfill_summary"]["persisted_count"], 0)
            self.assertEqual(summary["llm_summary"]["calls_used"], 0)
            self.assertTrue((Path(tmp) / "metadata-scan-summary.json").is_file())
            self.assertTrue((Path(tmp) / "coverage-summary.json").is_file())
            self.assertTrue((Path(tmp) / "candidate-tier-summary.json").is_file())
            self.assertTrue((Path(tmp) / "top-exclusions.json").is_file())
            self.assertTrue((Path(tmp) / "operator-review-cases.redacted.jsonl").is_file())
            self.assertTrue((Path(tmp) / "pre-fix-artifact-summary.json").is_file())
            env_summary = read_json(Path(tmp) / "environment-summary.json")
            self.assertFalse(env_summary["confirm_vps_node_b"])
            self.assertTrue(env_summary["dry_run"])
            self.assertTrue(env_summary["metadata_only"])
            sample_text = (Path(tmp) / "sample-records.redacted.jsonl").read_text(encoding="utf-8")
            self.assertIn("<email>", sample_text)
            self.assertNotIn("jan@example.com", sample_text)

    def test_candidate_selection_accepts_operational_and_excludes_noise(self) -> None:
        accepted = classify_bootstrap_candidate(
            {
                "from": "klient@example.com",
                "subject": "Prosze o oferte pompy ciepla",
                "snippet": "Dom 160 m2",
                "labels": ["INBOX"],
                "attachment_parts": [{"size_bytes": 100}],
            }
        )
        rejected = classify_bootstrap_candidate(
            {
                "from": "no-reply@example.com",
                "subject": "Newsletter unsubscribe",
                "snippet": "Marketing",
                "labels": ["INBOX"],
            }
        )

        self.assertTrue(accepted["candidate"])
        self.assertIn("active_lead_or_offer", accepted["priority_reasons"])
        self.assertFalse(rejected["candidate"])
        self.assertIn("no_reply_or_system_sender", rejected["exclusion_reasons"])

    def test_candidate_selection_hardens_logistics_marketing_and_document_review(self) -> None:
        logistics = classify_bootstrap_candidate(
            {
                "from": "powiadomienia@inpost.pl",
                "subject": "Twoja paczka Allegro jest w drodze",
                "snippet": "Status przesylki i tracking",
                "labels": ["INBOX"],
            }
        )
        supplier_newsletter = classify_bootstrap_candidate(
            {
                "from": "newsletter@stiebel-eltron.pl",
                "subject": "Webinar i promocja outlet",
                "snippet": "Wypisz sie z newslettera",
                "labels": ["INBOX"],
            }
        )
        google_ads = classify_bootstrap_candidate(
            {
                "from": "ads-noreply@google.com",
                "subject": "Google Ads campaign update",
                "snippet": "Marketing recommendation",
                "labels": ["INBOX"],
            }
        )
        invoice = classify_bootstrap_candidate(
            {
                "from": "no-reply@fakturownia.pl",
                "subject": "Faktura FV 12/2026",
                "snippet": "Dokument KSeF do platnosci",
                "labels": ["INBOX"],
            }
        )
        logistics_complaint = classify_bootstrap_candidate(
            {
                "from": "klient@example.com",
                "subject": "Reklamacja przesylki z dokumentami serwis",
                "snippet": "Potrzebuje naprawa i reklamacja",
                "labels": ["INBOX"],
            }
        )

        self.assertFalse(logistics["candidate"])
        self.assertIn("logistics_tracking_noise", logistics["exclusion_reasons"])
        self.assertFalse(supplier_newsletter["candidate"])
        self.assertIn("supplier_marketing_newsletter", supplier_newsletter["exclusion_reasons"])
        self.assertFalse(google_ads["candidate"])
        self.assertIn("google_ads_marketing_noise", google_ads["exclusion_reasons"])
        self.assertTrue(invoice["candidate"])
        self.assertEqual(invoice["candidate_tier"], "document_review_candidate")
        self.assertIn("document_review_candidate", invoice["priority_reasons"])
        self.assertTrue(logistics_complaint["candidate"])
        self.assertIn("complaint", logistics_complaint["priority_reasons"])

    def test_non_dry_run_backfill_without_confirm_vps_node_b_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )
            options = GmailHistoricalBootstrapOptions(
                run_id="bootstrap-missing-confirm",
                limit=25,
                fetch_body=True,
                fetch_attachments_metadata=True,
                dry_run=False,
                no_llm=True,
            )

            with self.assertRaisesRegex(ValueError, "Refusing live Gmail historical bootstrap"):
                run_gmail_historical_bootstrap(
                    settings=_settings(),
                    runtime=runtime,
                    options=options,
                    profile_fetcher=_profile,
                    metadata_searcher=_metadata_searcher,
                    body_fetcher=_body_fetcher,
                )
            self.assertEqual(str(LIVE_BOOTSTRAP_CONFIRMATION_ERROR).splitlines()[0], "Refusing live Gmail historical bootstrap without --confirm-vps-node-b.")

    def test_bounded_backfill_persists_provenance_without_attachment_documents_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )
            options = GmailHistoricalBootstrapOptions(
                run_id="bootstrap-test-1",
                days_back=30,
                limit=25,
                max_threads=10,
                fetch_body=True,
                fetch_attachments_metadata=True,
                max_attachment_bytes=0,
                dry_run=False,
                no_llm=True,
                confirm_vps_node_b=True,
                proof_dir=Path(tmp) / "proof",
            )

            first = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=runtime,
                options=options,
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
                body_fetcher=_body_fetcher,
            )
            second = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=runtime,
                options=options,
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
                body_fetcher=_body_fetcher_rotated_attachment_id,
            )

            self.assertEqual(first["backfill_summary"]["persisted_count"], 1)
            self.assertEqual(first["idempotency_check"]["signal_inserted_count"], 3)
            self.assertEqual(second["idempotency_check"]["message_existing_count"], 1)
            self.assertEqual(second["idempotency_check"]["raw_observation_duplicate_count"], 1)
            self.assertEqual(second["idempotency_check"]["signal_inserted_count"], 0)
            self.assertEqual(second["idempotency_check"]["signal_duplicate_count"], 3)
            self.assertEqual(len(runtime.store.messages), 1)
            self.assertEqual(len(runtime.store.signals), 3)
            self.assertEqual(len(runtime.store.documents), 0)
            stored_message = runtime.store.messages["msg-lead-1"]
            provenance = stored_message["raw_snapshot"]["bootstrap_provenance"]
            self.assertEqual(provenance["ingest_mode"], "historical_bootstrap")
            self.assertEqual(provenance["bootstrap_run_id"], "bootstrap-test-1")
            env_summary = read_json(Path(tmp) / "proof" / "environment-summary.json")
            self.assertTrue(env_summary["confirm_vps_node_b"])
            tier_summary = read_json(Path(tmp) / "proof" / "candidate-tier-summary.json")
            self.assertIn("candidate_tier_breakdown", tier_summary)

    def test_pre_fix_artifact_audit_reports_logical_signal_duplicates_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )
            runtime.store.append_signal(
                {
                    "signal_id": "sig-pre-1",
                    "signal_kind": "gmail_attachment_observed",
                    "source_kind": "gmail",
                    "source_ref_json": {
                        "message_id": "msg-pre",
                        "thread_id": "thr-pre",
                        "attachment_index": 0,
                        "filename": "projekt.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": "1200",
                        "ingest_mode": "historical_bootstrap",
                    },
                    "idempotency_key": "old-key-1",
                    "created_by_runtime": "gmail_historical_bootstrap",
                }
            )
            runtime.store.append_signal(
                {
                    "signal_id": "sig-pre-2",
                    "signal_kind": "gmail_attachment_observed",
                    "source_kind": "gmail",
                    "source_ref_json": {
                        "message_id": "msg-pre",
                        "thread_id": "thr-pre",
                        "attachment_index": 0,
                        "filename": "projekt.pdf",
                        "mime_type": "application/pdf",
                        "size_bytes": "1200",
                        "ingest_mode": "historical_bootstrap",
                    },
                    "idempotency_key": "old-key-2",
                    "created_by_runtime": "gmail_historical_bootstrap",
                }
            )

            summary = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=runtime,
                options=GmailHistoricalBootstrapOptions(
                    run_id="bootstrap-prefixed-audit",
                    limit=50,
                    metadata_only=True,
                    dry_run=True,
                    no_llm=True,
                    proof_dir=Path(tmp) / "proof",
                ),
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
            )

            audit = summary["pre_fix_artifact_summary"]
            self.assertEqual(audit["status"], "ok")
            self.assertFalse(audit["mutated"])
            self.assertEqual(audit["duplicate_logical_group_count"], 1)
            self.assertEqual(len(runtime.store.signals), 2)
            proof_audit = read_json(Path(tmp) / "proof" / "pre-fix-artifact-summary.json")
            self.assertEqual(proof_audit["duplicate_logical_group_count"], 1)

    def test_selective_llm_respects_total_and_thread_budget(self) -> None:
        calls: list[str] = []

        def enricher(snapshot, candidate):
            calls.append(snapshot["source_message"]["message_id"])
            return {
                "case_assessment": {"case_family": "heat_pump_offer"},
                "decision": {"action": "review"},
                "review": {"required": True, "flags": ["llm"]},
                "confidence": {"case_link_confidence": 0.2},
            }

        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )
            options = GmailHistoricalBootstrapOptions(
                run_id="bootstrap-llm-1",
                limit=25,
                fetch_body=True,
                fetch_attachments_metadata=True,
                dry_run=False,
                no_llm=False,
                selective_llm=True,
                confirm_vps_node_b=True,
                max_llm_calls=1,
                max_llm_calls_per_thread=1,
            )
            summary = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=runtime,
                options=options,
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
                body_fetcher=_body_fetcher,
                llm_enricher=enricher,
            )

            self.assertEqual(calls, ["msg-lead-1"])
            self.assertEqual(summary["llm_summary"]["calls_used"], 1)

    def test_source_cursor_finalize_without_confirm_vps_node_b_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )

            with self.assertRaisesRegex(ValueError, "Refusing live Gmail historical bootstrap"):
                run_gmail_historical_bootstrap(
                    settings=_settings(),
                    runtime=runtime,
                    options=GmailHistoricalBootstrapOptions(
                        finalize_source_cursor=True,
                        bootstrap_run_id="bootstrap-test-1",
                        dry_run=False,
                    ),
                    profile_fetcher=_profile,
                    metadata_searcher=_metadata_searcher,
                )

    def test_source_cursor_finalize_with_confirm_vps_node_b_writes_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = MailboxMemoryRuntime(
                store=InMemoryMailboxMemoryStore(),
                blob_root=Path(tmp) / "blobs",
                stage_mode="shadow",
            )
            live = run_gmail_historical_bootstrap(
                settings=_settings(),
                runtime=runtime,
                options=GmailHistoricalBootstrapOptions(
                    finalize_source_cursor=True,
                    bootstrap_run_id="bootstrap-test-1",
                    dry_run=False,
                    confirm_vps_node_b=True,
                    proof_dir=Path(tmp) / "live-proof",
                ),
                profile_fetcher=_profile,
                metadata_searcher=_metadata_searcher,
            )

            self.assertEqual(live["status"], "ok")
            cursor = runtime.store.fetch_source_cursor("gmail", "default")
            self.assertIsNotNone(cursor)
            self.assertEqual(cursor["last_cursor"], "hist-900")
            self.assertEqual(cursor["metadata"]["bootstrap_run_id"], "bootstrap-test-1")
            env_summary = read_json(Path(tmp) / "live-proof" / "environment-summary.json")
            self.assertTrue(env_summary["confirm_vps_node_b"])


if __name__ == "__main__":
    unittest.main()
