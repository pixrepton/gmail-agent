"""Canonical runtime ingress for AI-OS Phase 3 bounded proofs (3.5 / 3.6).

Forbidden during an active ingress scope:
- direct PostgresOperatorEngagementStore.insert_snapshot from harness
- manual ActionItem / draft / HITL / approval-ready seeding
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from agent_runtime.draft_lineage_transport import build_upstream_draft_transport
from agent_runtime.planner import MockSequencePlanner
from agent_runtime.store import PostgresOperatorEngagementStore, build_initial_snapshot
from config import load_settings
from fixture_helpers import build_fixture_snapshot, load_fixture, run_fixture
from gmail_intake import _build_lane_stage_plan, _build_spine_first_intake_validation_result
from gmail_signal_adapter import build_gmail_raw_observation, run_gmail_signal_runtime
from mailbox_memory_runtime import build_mailbox_memory_runtime
from mailbox_memory_store import PostgresMailboxMemoryStore
from observation_triage import triage_gmail_observation
from preclassifier import preclassify_snapshot

_DIRECT_SEED_FORBIDDEN: ContextVar[bool] = ContextVar("aios_direct_seed_forbidden", default=False)

FORBIDDEN_DIRECT_SEED_OPS = frozenset(
    {
        "insert_snapshot",
        "save_snapshot",
        "build_initial_snapshot_for_seed",
        "manual_hitl_seed",
        "manual_draft_seed",
    }
)


class DirectDatabaseSeedForbidden(RuntimeError):
    """Raised when harness attempts forbidden direct SoT seeding during canonical ingress."""


@contextmanager
def canonical_runtime_ingress_scope():
    token = _DIRECT_SEED_FORBIDDEN.set(True)
    # Fail closed if harness helpers call insert/save on engagement store directly.
    original_insert = PostgresOperatorEngagementStore.insert_snapshot
    original_save = PostgresOperatorEngagementStore.save_snapshot

    def _guarded_insert(self, snapshot):  # type: ignore[no-untyped-def]
        # Production reconcile/agent path is allowed; forbid only explicit harness seed helpers.
        # Detection: call stack frames named like historical seed helpers.
        import inspect

        for frame in inspect.stack()[1:12]:
            name = str(frame.function or "")
            if name.startswith("_seed_") or name in {"_seed_hitl_engagement_from_fixture", "_seed_complaint_hitl"}:
                assert_no_direct_database_seed("insert_snapshot")
        return original_insert(self, snapshot)

    def _guarded_save(self, snapshot, expected_version: int):  # type: ignore[no-untyped-def]
        import inspect

        for frame in inspect.stack()[1:12]:
            name = str(frame.function or "")
            if name.startswith("_seed_"):
                assert_no_direct_database_seed("save_snapshot")
        return original_save(self, snapshot, expected_version)

    PostgresOperatorEngagementStore.insert_snapshot = _guarded_insert  # type: ignore[method-assign]
    PostgresOperatorEngagementStore.save_snapshot = _guarded_save  # type: ignore[method-assign]
    try:
        yield
    finally:
        PostgresOperatorEngagementStore.insert_snapshot = original_insert  # type: ignore[method-assign]
        PostgresOperatorEngagementStore.save_snapshot = original_save  # type: ignore[method-assign]
        _DIRECT_SEED_FORBIDDEN.reset(token)


def assert_no_direct_database_seed(operation: str) -> None:
    if _DIRECT_SEED_FORBIDDEN.get() and operation in FORBIDDEN_DIRECT_SEED_OPS:
        raise DirectDatabaseSeedForbidden(
            f"direct_database_seed_used: forbidden operation {operation!r} during canonical ingress"
        )


@dataclass(frozen=True, slots=True)
class CanonicalIngressResult:
    seed_method: str
    direct_database_seed_used: bool
    ingress_receipt_id: str
    signal_id: str
    message_id: str
    case_id: str
    engagement_id: str
    draft_id: str
    hitl_id: str
    draft_body: str
    draft_body_hash: str
    draft_revision: int
    preclassification_lane: str
    accepted_reason: str
    store: PostgresOperatorEngagementStore
    settings: Any
    runtime_result: Any

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "seed_method": self.seed_method,
            "direct_database_seed_used": self.direct_database_seed_used,
            "ingress_receipt_id": self.ingress_receipt_id,
            "signal_id": self.signal_id,
            "message_id": self.message_id,
            "case_id": self.case_id,
            "engagement_id": self.engagement_id,
            "draft_id": self.draft_id,
            "hitl_id": self.hitl_id,
            "preclassification_lane": self.preclassification_lane,
            "accepted_reason": self.accepted_reason,
        }


def _unique_suffix() -> str:
    return uuid.uuid4().hex[:10]


def _unique_snapshot(snapshot: dict[str, Any], *, suffix: str) -> dict[str, Any]:
    out = dict(snapshot)
    source = dict(out.get("source_message") or {})
    message_id = str(source.get("message_id") or "msg").strip()
    thread_id = str(source.get("thread_id") or "thread").strip()
    source["message_id"] = f"{message_id}-{suffix}"
    source["thread_id"] = f"{thread_id}-{suffix}"
    out["source_message"] = source
    return out


def _agent_env_patch() -> dict[str, str]:
    return {
        "AGENT_RUNTIME_MODE": "prep",
        "AGENT_RUNTIME_ENABLED": "1",
        "AGENT_OPENAI_API_KEY": "sk-test",
        "KALK_TOP_BASE_URL": "",
        "KALK_TOP_AGENT_KEY": "",
        "TOPINSTAL_CALC_AGENT_API_KEY": "",
        "GMAIL_AUDIT_KALK_TOP_TEST_OPT_IN": "",
    }


def _fixture_downstream_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_intelligence_result": bundle["case_intelligence"],
        "mailbox_memory_result": {
            "case_id": "",
            "enabled": True,
            "context_pack": {},
        },
        "reply_draft_result": bundle["expected"]["reply_draft"],
    }


def _build_runtime_settings(settings: Any, *, blob_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        mailbox_memory_blob_root=blob_root,
        mailbox_memory_database_url=str(getattr(settings, "mailbox_memory_database_url", "") or ""),
        mailbox_memory_stage_mode=str(getattr(settings, "mailbox_memory_stage_mode", "live") or "live"),
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test",
        agent_runtime_enabled=True,
        agent_runtime_mode="prep",
        understanding_output_enabled=True,
        decision_pipeline_enabled=True,
        daszek_operational_feed_auto_push_enabled=True,
        daszek_operational_feed_case_limit=50,
        daszek_base_url=os.getenv("DASZEK_BASE_URL", "http://127.0.0.1:8090").rstrip("/"),
    )


def _poll_engagement_hitl(
    store: PostgresOperatorEngagementStore,
    *,
    message_id: str,
    engagement_id_hint: str = "",
    timeout_sec: float = 45.0,
) -> tuple[Any, Any]:
    import time

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if engagement_id_hint:
            snap = store.load_snapshot(engagement_id_hint)
            if snap is not None and snap.hitl_gate.required:
                enabled = [action for action in snap.actions if action.enabled]
                if len(enabled) == 1:
                    return snap, enabled[0]
        for snap in store.list_recent_snapshots(limit=100):
            trace = str(snap.trace_id or "")
            signal_id = str(getattr(snap, "signal_id", "") or "")
            if message_id not in {trace, signal_id} and message_id not in trace:
                continue
            if not snap.hitl_gate.required:
                continue
            enabled = [action for action in snap.actions if action.enabled]
            if len(enabled) != 1:
                continue
            return snap, enabled[0]
        time.sleep(0.5)
    raise TimeoutError(f"HITL engagement not ready for message_id={message_id}")


def run_canonical_runtime_ingress_from_fixture(
    fixture_name: str,
    *,
    unique_suffix: str | None = None,
) -> CanonicalIngressResult:
    """Customer / actionable fixture via run_gmail_signal_runtime + agent reconcile."""
    suffix = unique_suffix or _unique_suffix()
    with canonical_runtime_ingress_scope():
        bundle = run_fixture(fixture_name)
        snapshot = _unique_snapshot(bundle["snapshot"], suffix=suffix)
        pre = preclassify_snapshot(snapshot)
        if str(pre.get("lane") or "") == "skip":
            raise AssertionError(f"fixture {fixture_name} unexpectedly classified as noise")

        lane_plan = _build_lane_stage_plan(pre)
        intake_bundle = _build_spine_first_intake_validation_result(
            snapshot=snapshot,
            preclassification_result=pre,
            lane_stage_plan=lane_plan,
        )
        intake_final = intake_bundle["intake_result_final"]
        message_id = str((snapshot.get("source_message") or {}).get("message_id") or "")

        settings = load_settings(require_groq=False, require_google=False)
        db_url = str(getattr(settings, "mailbox_memory_database_url", "") or os.getenv("MAILBOX_MEMORY_DATABASE_URL", "")).strip()
        if not db_url:
            raise RuntimeError("MAILBOX_MEMORY_DATABASE_URL required for canonical runtime ingress")

        mailbox_store = PostgresMailboxMemoryStore(db_url)
        mailbox_store.bootstrap()
        operator_store = PostgresOperatorEngagementStore(db_url)
        operator_store.bootstrap()
        mailbox_runtime = build_mailbox_memory_runtime(settings)

        raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="aios_canonical_runtime_ingress")
        triage = triage_gmail_observation(raw_observation)

        from correlation_registry.service import build_correlation_registry_service

        case_id_hint = f"case_{suffix}"
        registry = build_correlation_registry_service(db_url)
        registry.bootstrap()
        customer_email = str((snapshot.get("source_message") or {}).get("from") or "biuro@klient-dom.pl").strip()
        registry.sync_mailbox_case(
            case_id=case_id_hint,
            customer_email=customer_email,
            message_id=message_id,
        )

        downstream = _fixture_downstream_bundle(bundle)
        downstream["mailbox_memory_result"] = {
            **downstream["mailbox_memory_result"],
            "case_id": case_id_hint,
        }

        def _planner(_settings: Any) -> MockSequencePlanner:
            return MockSequencePlanner(["generate_draft_reply", "report_gaps_and_stop"])

        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, _agent_env_patch(), clear=False):
            runtime_settings = _build_runtime_settings(settings, blob_root=Path(tmp_dir))
            run_state: dict[str, Any] = {
                "signal_store": mailbox_store,
                "mailbox_memory_runtime": mailbox_runtime,
                "runtime_controls": {"projection_proof": True},
            }
            with patch(
                "agent_runtime.agent_reconcile._run_mailbox_intelligence_downstream",
                return_value=(
                    downstream["case_intelligence_result"],
                    downstream["mailbox_memory_result"],
                    downstream["reply_draft_result"],
                    [],
                ),
            ), patch(
                "agent_runtime.agent_reconcile.build_registry_for_reconcile",
                return_value=registry,
            ), patch(
                "agent_runtime.agent_reconcile.resolve_case_id_for_agent",
                return_value=case_id_hint,
            ), patch(
                "agent_runtime.run.build_planner",
                side_effect=_planner,
            ):
                runtime_result = run_gmail_signal_runtime(
                    settings=runtime_settings,
                    run_state=run_state,
                    snapshot=snapshot,
                    intake_result_final=intake_final,
                    preclassification_result=pre,
                    lane_stage_plan=lane_plan,
                    context_bundle={},
                    raw_observation=raw_observation,
                    triage_result=triage,
                    model="test",
                    verbose=False,
                    dry_run=False,
                )

        signal_id = str(runtime_result.primary_signal.signal_id or "")
        engagement_hint = str(run_state.get("agent_engagement_id") or "")
        snap, action = _poll_engagement_hitl(
            operator_store,
            message_id=message_id,
            engagement_id_hint=engagement_hint,
        )
        case_id = str(snap.case_id or case_id_hint)
        return CanonicalIngressResult(
            seed_method="canonical_runtime_ingress",
            direct_database_seed_used=False,
            ingress_receipt_id=signal_id,
            signal_id=signal_id,
            message_id=message_id,
            case_id=case_id,
            engagement_id=str(snap.engagement_id or ""),
            draft_id=str(action.draft_id or ""),
            hitl_id=str(action.id or "draft_reply"),
            draft_body=str(action.payload_pl or ""),
            draft_body_hash=str(action.body_hash or ""),
            draft_revision=int(action.revision or 1),
            preclassification_lane=str(pre.get("lane") or ""),
            accepted_reason=str(pre.get("reason") or intake_final.get("decision", {}).get("action_rationale") or "accepted"),
            store=operator_store,
            settings=settings,
            runtime_result=runtime_result,
        )


def run_canonical_runtime_ingress_from_snapshot(snapshot: dict[str, Any]) -> CanonicalIngressResult:
    """Complaint or custom actionable snapshot through the same ingress spine."""
    suffix = _unique_suffix()
    snapshot = _unique_snapshot(snapshot, suffix=suffix)
    with canonical_runtime_ingress_scope():
        pre = preclassify_snapshot(snapshot)
        if str(pre.get("lane") or "") == "skip":
            raise AssertionError("complaint snapshot unexpectedly classified as noise")

        bundle = run_fixture("post_offer_question")
        bundle["snapshot"] = snapshot
        bundle["preclassification"] = pre

        lane_plan = _build_lane_stage_plan(pre)
        intake_bundle = _build_spine_first_intake_validation_result(
            snapshot=snapshot,
            preclassification_result=pre,
            lane_stage_plan=lane_plan,
        )
        intake_final = dict(intake_bundle["intake_result_final"])
        intake_final.setdefault("decision", {})
        intake_final["decision"]["action"] = "create_task"
        intake_final["decision"]["action_rationale"] = "Complaint requires operator review."
        message_id = str((snapshot.get("source_message") or {}).get("message_id") or "")

        settings = load_settings(require_groq=False, require_google=False)
        db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "").strip()
        if not db_url:
            raise RuntimeError("MAILBOX_MEMORY_DATABASE_URL required for canonical runtime ingress")

        mailbox_store = PostgresMailboxMemoryStore(db_url)
        mailbox_store.bootstrap()
        operator_store = PostgresOperatorEngagementStore(db_url)
        operator_store.bootstrap()
        mailbox_runtime = build_mailbox_memory_runtime(settings)

        complaint_reply = {
            "draft_enabled": True,
            "drafts": [
                {
                    "variant": "customer_friendly",
                    "subject_suggestion": str((snapshot.get("source_message") or {}).get("subject") or "Reklamacja"),
                    "body": "Dzien dobry, przyjelismy zgloszenie reklamacyjne i umowimy wizyte serwisowa.",
                    "goal": "complaint_ack",
                }
            ],
            "recommended_variant": "customer_friendly",
        }
        downstream = {
            "case_intelligence_result": {
                **bundle["case_intelligence"],
                "case_summary_pl": "Reklamacja montazu klimatyzacji — urzadzenie nie chlodzi.",
            },
            "mailbox_memory_result": {"case_id": f"case_complaint_{suffix}", "enabled": True, "context_pack": {}},
            "reply_draft_result": complaint_reply,
        }

        raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="aios_canonical_runtime_ingress")
        triage = triage_gmail_observation(raw_observation)

        from correlation_registry.service import build_correlation_registry_service

        registry = build_correlation_registry_service(db_url)
        registry.bootstrap()
        case_id_hint = f"case_complaint_{suffix}"
        customer_email = str((snapshot.get("source_message") or {}).get("from") or "klient@example.com").strip()
        registry.sync_mailbox_case(
            case_id=case_id_hint,
            customer_email=customer_email,
            message_id=message_id,
        )

        def _planner(_settings: Any) -> MockSequencePlanner:
            return MockSequencePlanner(["generate_draft_reply", "report_gaps_and_stop"])

        with tempfile.TemporaryDirectory() as tmp_dir, patch.dict(os.environ, _agent_env_patch(), clear=False):
            runtime_settings = _build_runtime_settings(settings, blob_root=Path(tmp_dir))
            run_state = {
                "signal_store": mailbox_store,
                "mailbox_memory_runtime": mailbox_runtime,
                "runtime_controls": {"projection_proof": True},
            }
            with patch(
                "agent_runtime.agent_reconcile._run_mailbox_intelligence_downstream",
                return_value=(
                    downstream["case_intelligence_result"],
                    downstream["mailbox_memory_result"],
                    downstream["reply_draft_result"],
                    [],
                ),
            ), patch(
                "agent_runtime.agent_reconcile.build_registry_for_reconcile",
                return_value=registry,
            ), patch(
                "agent_runtime.agent_reconcile.resolve_case_id_for_agent",
                return_value=case_id_hint,
            ), patch(
                "agent_runtime.run.build_planner",
                side_effect=_planner,
            ):
                runtime_result = run_gmail_signal_runtime(
                    settings=runtime_settings,
                    run_state=run_state,
                    snapshot=snapshot,
                    intake_result_final=intake_final,
                    preclassification_result=pre,
                    lane_stage_plan=lane_plan,
                    context_bundle={},
                    raw_observation=raw_observation,
                    triage_result=triage,
                    model="test",
                    verbose=False,
                    dry_run=False,
                )

        signal_id = str(runtime_result.primary_signal.signal_id or "")
        engagement_hint = str(run_state.get("agent_engagement_id") or "")
        snap, action = _poll_engagement_hitl(
            operator_store,
            message_id=message_id,
            engagement_id_hint=engagement_hint,
        )
        return CanonicalIngressResult(
            seed_method="canonical_runtime_ingress",
            direct_database_seed_used=False,
            ingress_receipt_id=signal_id,
            signal_id=signal_id,
            message_id=message_id,
            case_id=str(snap.case_id or case_id_hint),
            engagement_id=str(snap.engagement_id or ""),
            draft_id=str(action.draft_id or ""),
            hitl_id=str(action.id or "draft_reply"),
            draft_body=str(action.payload_pl or ""),
            draft_body_hash=str(action.body_hash or ""),
            draft_revision=int(action.revision or 1),
            preclassification_lane=str(pre.get("lane") or ""),
            accepted_reason=str(pre.get("reason") or "complaint_accepted"),
            store=operator_store,
            settings=settings,
            runtime_result=runtime_result,
        )


def run_canonical_runtime_noise_ingress(*, unique_suffix: str | None = None) -> dict[str, Any]:
    """Noise control via the same run_gmail_signal_runtime entrypoint."""
    suffix = unique_suffix or _unique_suffix()
    message_payload, _expected = load_fixture("obvious_noise")
    snapshot = _unique_snapshot(build_fixture_snapshot(message_payload), suffix=suffix)
    message_id = str((snapshot.get("source_message") or {}).get("message_id") or "")

    with canonical_runtime_ingress_scope():
        pre = preclassify_snapshot(snapshot)
        assert pre["lane"] == "skip"

        raw_observation = build_gmail_raw_observation(snapshot=snapshot, created_by_runtime="aios_canonical_runtime_ingress")
        triage = triage_gmail_observation(raw_observation)
        lane_plan = _build_lane_stage_plan(pre)
        bundle = _build_spine_first_intake_validation_result(
            snapshot=snapshot,
            preclassification_result=pre,
            lane_stage_plan=lane_plan,
        )
        intake_final = bundle["intake_result_final"]

        from agent_runtime.store import InMemoryOperatorEngagementStore

        mailbox_store = PostgresMailboxMemoryStore(
            str(load_settings(require_groq=False, require_google=False).mailbox_memory_database_url or "")
        )
        mailbox_store.bootstrap()
        eng_store = InMemoryOperatorEngagementStore()

        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime_settings = SimpleNamespace(
                mailbox_memory_blob_root=Path(tmp_dir),
                mailbox_memory_database_url=str(getattr(load_settings(require_groq=False, require_google=False), "mailbox_memory_database_url", "") or ""),
                signal_journal_jsonl_mirror_enabled=False,
                signal_runtime_mode="active",
                groq_model="test",
                agent_runtime_enabled=False,
                agent_runtime_mode="legacy",
            )
            run_state = {"signal_store": mailbox_store}
            with patch(
                "agent_runtime.agent_reconcile.agent_runtime_reconcile_active",
                return_value=False,
            ), patch(
                "agent_runtime.agent_reconcile.build_operator_engagement_store",
                return_value=eng_store,
            ):
                first = run_gmail_signal_runtime(
                    settings=runtime_settings,
                    run_state=run_state,
                    snapshot=snapshot,
                    intake_result_final=intake_final,
                    preclassification_result=pre,
                    lane_stage_plan=lane_plan,
                    context_bundle={},
                    raw_observation=raw_observation,
                    triage_result=triage,
                    model="test",
                    verbose=False,
                    dry_run=False,
                )
                second = run_gmail_signal_runtime(
                    settings=runtime_settings,
                    run_state=run_state,
                    snapshot=snapshot,
                    intake_result_final=intake_final,
                    preclassification_result=pre,
                    lane_stage_plan=lane_plan,
                    context_bundle={},
                    raw_observation=raw_observation,
                    triage_result=triage,
                    model="test",
                    verbose=False,
                    dry_run=False,
                )

        signal_id = str(first.primary_signal.signal_id or "")
        assert second.primary_signal.signal_id == signal_id
        return {
            "seed_method": "canonical_runtime_ingress",
            "direct_database_seed_used": False,
            "ingress_receipt_id": signal_id,
            "signal_id": signal_id,
            "message_id": message_id,
            "classification": "noise",
            "reason_codes": list(pre.get("reasons") or []),
            "case_created": False,
            "hitl_created": False,
        }


def push_engagement_feed_for_ingress(result: CanonicalIngressResult) -> dict[str, Any]:
    from dataclasses import replace

    from agent_hitl_bridge import best_effort_push_engagement_feed_after_hitl
    from aios_bounded_runtime_support import _load_playwright_env_file_only, peek_daszek_credentials

    _load_playwright_env_file_only()
    login, password = peek_daszek_credentials()
    settings = result.settings
    if login and password and not str(getattr(settings, "daszek_login", "") or "").strip():
        settings = replace(settings, daszek_login=login, daszek_password=password)
    if not bool(getattr(settings, "daszek_operational_feed_auto_push_enabled", False)):
        settings = replace(settings, daszek_operational_feed_auto_push_enabled=True)

    return best_effort_push_engagement_feed_after_hitl(
        settings=settings,
        operator_store=result.store,
        engagement_id=result.engagement_id,
        case_id=result.case_id,
    )


__all__ = [
    "CanonicalIngressResult",
    "DirectDatabaseSeedForbidden",
    "assert_no_direct_database_seed",
    "canonical_runtime_ingress_scope",
    "push_engagement_feed_for_ingress",
    "run_canonical_runtime_ingress_from_fixture",
    "run_canonical_runtime_ingress_from_snapshot",
    "run_canonical_runtime_noise_ingress",
]
