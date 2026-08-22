"""P1.1P final runtime closeout: production worker boot wiring + Postgres proof.

The production boot seam is:

    build_mailbox_memory_runtime(settings) / SignalRuntimeContext construction
      -> MailboxMemoryRuntime.bootstrap()
         -> store.bootstrap() (tables)
         -> build_store_backed_decision_ledger(store)   # rebuild + fail closed
         -> runtime.decision_revision_ledger            # runtime dependency

These tests go through that seam (not a direct ``DecisionRevisionLedger``
construction). Postgres-backed tests run only when
``MAILBOX_MEMORY_TEST_DATABASE_URL`` is set (canonical mailbox-memory test DB).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from canonical_action_decision import (
    DecisionRevisionStateInvalidError,
    approval_binds_revision,
    artifact_version_matches,
    build_business_decision_proposal,
    canonicalize,
    evaluate_decision_revision,
    request_decision_revision,
    stale_artifact_reason,
)
from mailbox_memory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import MailboxMemoryRuntime

POSTGRES_TEST_DATABASE_URL = os.getenv("MAILBOX_MEMORY_TEST_DATABASE_URL", "").strip()
REQUIRES_POSTGRES = pytest.mark.skipif(
    not POSTGRES_TEST_DATABASE_URL,
    reason="MAILBOX_MEMORY_TEST_DATABASE_URL is not set",
)


def _br(*, missing: list[str] | None = None) -> dict[str, object]:
    return {
        "recommended_next_action": "collect_data",
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "recommended_action_reason": "Brak danych diagnostycznych.",
        "urgency": "normal",
        "confidence": {"action_confidence": 0.8, "business_confidence": 0.7},
    }


def _situation(missing: list[str] | None = None) -> dict[str, object]:
    return {
        "missing_information": missing or ["error_code", "exact_symptoms"],
        "missing_critical_fields": missing or ["error_code", "exact_symptoms"],
    }


def _register_r1(*, ledger, case_id: str = "case_boot") -> dict[str, object]:
    proposal = build_business_decision_proposal(_br())
    assert proposal is not None
    cad = canonicalize(
        proposal=proposal,
        situation_understanding=_situation(),
        case_id=case_id,
        situation_version="sv_1",
    )
    assert cad["semantic_status"] == "FROZEN"
    ledger.register_cad(cad)
    return cad


def _accept_to_r2(
    *,
    ledger,
    cad_r1: dict[str, object],
    missing: list[str] | None = None,
    reason_code: str = "CANONICAL_FACT_CHANGED",
) -> dict[str, object]:
    emitted = request_decision_revision(
        decision_id=cad_r1["decision_id"],
        current_revision=cad_r1["revision"],
        reason_code=reason_code,
        source_layer="worker_runtime",
        ledger=ledger,
    )
    assert emitted["status"] == "PENDING"
    return evaluate_decision_revision(
        request=emitted["request"],
        current_cad=cad_r1,
        business_reasoning_result=_br(missing=missing or ["exact_symptoms"]),
        situation_understanding=_situation(missing or ["exact_symptoms"]),
        ledger=ledger,
    )


def _boot_runtime(*, store, blob_root: Path) -> MailboxMemoryRuntime:
    runtime = MailboxMemoryRuntime(
        store=store,
        blob_root=blob_root,
        stage_mode="live",
    )
    runtime.bootstrap()
    return runtime


# --------------------------------------------------------------------------
# worker boot wiring (production seam, InMemory durable store)
# --------------------------------------------------------------------------


def test_worker_boot_builds_store_backed_ledger(tmp_path: Path) -> None:
    store = InMemoryMailboxMemoryStore()
    runtime = _boot_runtime(store=store, blob_root=tmp_path)

    ledger = runtime.decision_revision_ledger
    assert ledger is not None
    # Runtime ledger is store-backed: registration writes through to the store.
    cad_r1 = _register_r1(ledger=ledger, case_id="case_boot")
    assert store.fetch_decision_revisions(cad_r1["decision_id"]) != []

    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    assert result["outcome"] == "ACCEPTED"
    decision_id = cad_r1["decision_id"]

    # Process restart: new runtime boot rebuilds the ledger from durable state.
    runtime = None  # type: ignore[assignment]
    runtime2 = _boot_runtime(store=store, blob_root=tmp_path)
    ledger2 = runtime2.decision_revision_ledger
    assert ledger2 is not None
    assert ledger2.current_revision(decision_id) == 2
    current = ledger2.current_cad(decision_id)
    assert current is not None
    assert current["decision_version_id"] == f"{decision_id}:r2"
    assert current["revision_status"] == "CURRENT"
    revisions = ledger2.revisions(decision_id)
    assert revisions[0]["revision_status"] == "SUPERSEDED"
    assert revisions[0]["superseded_by_version_id"] == f"{decision_id}:r2"
    assert revisions[1]["supersedes_version_id"] == f"{decision_id}:r1"

    # Stale guards after restart.
    old_plan = {"tool_name": "generate_draft_reply", "decision_version_id": f"{decision_id}:r1"}
    old_approval = {"approval_id": "appr_1", "decision_version_id": f"{decision_id}:r1"}
    assert stale_artifact_reason(old_plan, current) == "STALE_DECISION_REVISION"
    assert artifact_version_matches(old_plan, current) is False
    assert approval_binds_revision(old_approval, current) is False

    # Duplicate accepted request after restart: no r3.
    replay = ledger2.record_request(dict(result["request"]))
    assert replay["status"] == "DUPLICATE_REVISION_REQUEST"
    assert len(ledger2.revisions(decision_id)) == 2

    # Stale request after restart: expected r1, durable current r2.
    stale = request_decision_revision(
        decision_id=decision_id,
        current_revision=1,
        reason_code="NEW_CONFLICTING_EVIDENCE",
        ledger=ledger2,
    )
    assert stale["status"] == "STALE_REVISION_REQUEST"


def test_worker_boot_fails_closed_on_multiple_current(tmp_path: Path) -> None:
    store = InMemoryMailboxMemoryStore()
    store.append_decision_revision(
        {
            "decision_id": "dec_bad",
            "revision": 1,
            "decision_version_id": "dec_bad:r1",
            "semantic_hash": "sh_a",
            "revision_status": "CURRENT",
        }
    )
    store.append_decision_revision(
        {
            "decision_id": "dec_bad",
            "revision": 2,
            "decision_version_id": "dec_bad:r2",
            "semantic_hash": "sh_b",
            "revision_status": "CURRENT",
        }
    )
    with pytest.raises(DecisionRevisionStateInvalidError) as exc:
        _boot_runtime(store=store, blob_root=tmp_path)
    assert exc.value.code == "REVISION_STATE_INVALID"
    assert "rebuild_one_current_violation" in str(exc.value)


def test_worker_boot_fails_closed_on_zero_current(tmp_path: Path) -> None:
    store = InMemoryMailboxMemoryStore()
    store.append_decision_revision(
        {
            "decision_id": "dec_zero",
            "revision": 1,
            "decision_version_id": "dec_zero:r1",
            "semantic_hash": "sh_a",
            "revision_status": "SUPERSEDED",
        }
    )
    with pytest.raises(DecisionRevisionStateInvalidError) as exc:
        _boot_runtime(store=store, blob_root=tmp_path)
    assert exc.value.code == "REVISION_STATE_INVALID"


# --------------------------------------------------------------------------
# real Postgres worker restart proof (canonical mailbox-memory test DB)
# --------------------------------------------------------------------------


@REQUIRES_POSTGRES
def test_postgres_worker_restart_roundtrip_through_boot_seam(tmp_path: Path) -> None:
    from mailbox_memory import PostgresMailboxMemoryStore

    store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    runtime = _boot_runtime(store=store, blob_root=tmp_path)
    ledger = runtime.decision_revision_ledger
    assert ledger is not None

    cad_r1 = _register_r1(ledger=ledger, case_id="case_pg_boot")
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    assert result["outcome"] == "ACCEPTED"
    decision_id = cad_r1["decision_id"]

    try:
        # Process restart: new worker boot rebuilds from Postgres.
        runtime = None  # type: ignore[assignment]
        runtime2 = _boot_runtime(store=store, blob_root=tmp_path)
        ledger2 = runtime2.decision_revision_ledger
        assert ledger2 is not None
        assert ledger2.current_revision(decision_id) == 2
        current = ledger2.current_cad(decision_id)
        assert current is not None
        assert current["decision_version_id"] == f"{decision_id}:r2"
        assert current["revision_status"] == "CURRENT"
        revisions = ledger2.revisions(decision_id)
        assert [r["revision_status"] for r in revisions] == ["SUPERSEDED", "CURRENT"]

        # Stale guards after real restart.
        old_plan = {
            "tool_name": "generate_draft_reply",
            "decision_version_id": f"{decision_id}:r1",
        }
        old_approval = {"approval_id": "appr_1", "decision_version_id": f"{decision_id}:r1"}
        assert stale_artifact_reason(old_plan, current) == "STALE_DECISION_REVISION"
        assert approval_binds_revision(old_approval, current) is False

        replay = ledger2.record_request(dict(result["request"]))
        assert replay["status"] == "DUPLICATE_REVISION_REQUEST"
        assert len(ledger2.revisions(decision_id)) == 2

        stale = request_decision_revision(
            decision_id=decision_id,
            current_revision=1,
            reason_code="NEW_CONFLICTING_EVIDENCE",
            ledger=ledger2,
        )
        assert stale["status"] == "STALE_REVISION_REQUEST"
    finally:
        _cleanup_pg_lineage(store, decision_id)


@REQUIRES_POSTGRES
def test_postgres_accept_transition_atomic_one_current(tmp_path: Path) -> None:
    from mailbox_memory import PostgresMailboxMemoryStore

    store = PostgresMailboxMemoryStore(POSTGRES_TEST_DATABASE_URL)
    runtime = _boot_runtime(store=store, blob_root=tmp_path)
    ledger = runtime.decision_revision_ledger
    assert ledger is not None

    cad_r1 = _register_r1(ledger=ledger, case_id="case_pg_atomic")
    result = _accept_to_r2(ledger=ledger, cad_r1=cad_r1)
    assert result["outcome"] == "ACCEPTED"
    decision_id = cad_r1["decision_id"]
    try:
        rows = store.fetch_decision_revisions(decision_id)
        current_count = sum(1 for row in rows if row.get("revision_status") == "CURRENT")
        assert current_count == 1
        assert rows[-1]["decision_version_id"] == f"{decision_id}:r2"

        # Replaying the transition for an already-superseded old CAD fails
        # closed and leaves durable state unchanged (one CURRENT).
        with pytest.raises(RuntimeError):
            store.accept_decision_revision_transition(
                old_cad=cad_r1,
                new_cad=result["new_cad"],
                request=result["request"],
            )
        rows_after = store.fetch_decision_revisions(decision_id)
        current_count_after = sum(1 for row in rows_after if row.get("revision_status") == "CURRENT")
        assert current_count_after == 1
        assert rows_after[-1]["decision_version_id"] == f"{decision_id}:r2"
    finally:
        _cleanup_pg_lineage(store, decision_id)


def _cleanup_pg_lineage(store: object, decision_id: str) -> None:
    """Remove the technical test lineage from the mailbox-memory test DB."""
    from mailbox_memory.postgres import PostgresMailboxMemoryStore

    if not isinstance(store, PostgresMailboxMemoryStore):
        return
    with store._connect() as conn:  # type: ignore[attr-defined]
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM mailbox_memory_decision_revision_requests WHERE decision_id = %(d)s",
                {"d": decision_id},
            )
            cur.execute(
                "DELETE FROM mailbox_memory_decision_revisions WHERE decision_id = %(d)s",
                {"d": decision_id},
            )
        conn.commit()
