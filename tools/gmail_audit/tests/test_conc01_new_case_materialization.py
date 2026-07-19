"""CONC-01 regression: new-case materialization must not lose a signal's
contribution when two signals race to materialize the same brand-new case_id.

Two independently-flagged bypasses of the atomic mutate_case contract are
covered here, matching the audit at
C:\\ai-os-critical-case-stability-audit-20260714T111814Z\\findings.json:

  1. signal_reconciler._stamp_case_runtime_state's fallback branch
     (case not found at read time -> unlocked fetch_case + upsert_case).
  2. signal_reconciler._reconcile_drive_signal's case_seed_row branch
     (unconditional store.upsert_case(enriched), never attempts mutate_case).
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_journal import SignalJournal
from signal_reconciler import (
    CanonicalSignal,
    ProjectionRefreshDecision,
    SignalRuntimeContext,
    _reconcile_drive_signal,
    _stamp_case_runtime_state,
)


def _runtime_context() -> tuple[InMemoryMailboxMemoryStore, SignalRuntimeContext]:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    settings = SimpleNamespace(
        mailbox_memory_blob_root=Path(tempfile.gettempdir()) / "conc01-new-case-tests",
        signal_journal_jsonl_mirror_enabled=False,
        signal_runtime_mode="active",
        groq_model="test-model",
    )
    context = SignalRuntimeContext(
        settings=settings,
        journal=SignalJournal(store),
        store=store,
        graph_store=None,
        run_state={},
        model="test-model",
        verbose=False,
        mode="active",
    )
    return store, context


def _signal(signal_id: str, source_kind: str) -> CanonicalSignal:
    return CanonicalSignal(
        signal_id=signal_id,
        schema_version="v1",
        signal_kind="mail_received" if source_kind == "gmail" else "drive_document_added",
        source_kind=source_kind,
        source_ref={},
        observed_at="2026-07-14T10:00:00+02:00",
        effective_at=None,
        case_key_hint=None,
        thread_key_hint=None,
        business_lane=None,
        signal_summary_pl="CONC-01 regression",
        payload={},
        artifacts={},
        processing_state="new",
        idempotency_key=f"idem-{signal_id}",
        content_hash=None,
        replayable=True,
        created_by_runtime="test",
    )


class _BarrieredNewCaseStore:
    """Mirrors PostgresMailboxMemoryStore's new-case contract: fetch_case and
    upsert_case are unlocked (mirroring an unlocked SELECT / plain
    ON CONFLICT DO UPDATE), while mutate_case holds a single lock across the
    whole read-mutate-write sequence (mirroring pg_advisory_xact_lock +
    SELECT ... FOR UPDATE + one transaction). The barrier forces both
    threads' "case not found" reads to land before either one commits --
    the exact TOCTOU window described in CONC-01.
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        self._rows: dict[str, dict] = {}
        self._barrier = barrier
        self._fetch_count = 0
        self._count_lock = threading.Lock()
        self._commit_lock = threading.Lock()

    def fetch_case(self, case_id: str) -> dict | None:
        with self._count_lock:
            self._fetch_count += 1
            call_no = self._fetch_count
        row = self._rows.get(case_id)
        if row is None and call_no <= 4:
            # _stamp_case_runtime_state's fallback branch calls fetch_case()
            # twice per invocation (probe at the top, re-fetch in the
            # fallback). Sync the first two pairs so both threads observe
            # "not found" before either commits.
            try:
                self._barrier.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
        return dict(row) if row else None

    def upsert_case(self, row: dict) -> None:
        case_id = row["case_id"]
        with self._commit_lock:
            self._rows[case_id] = dict(row)

    def mutate_case(self, case_id: str, mutator, *, create_if_missing: bool = False) -> dict:
        with self._commit_lock:
            row = self._rows.get(case_id)
            if row is None and not create_if_missing:
                raise LookupError(f"case not found: {case_id}")
            current = dict(row) if row else {"case_id": case_id, "metadata": {}}
            updated = mutator(current)
            if not isinstance(updated, dict):
                raise RuntimeError("case mutator must return dict row")
            self._rows[case_id] = dict(updated)
            return dict(updated)


def test_stamp_case_runtime_state_concurrent_new_case_preserves_both_signal_contributions() -> None:
    barrier = threading.Barrier(2)
    store = _BarrieredNewCaseStore(barrier)
    case_id = "case-conc01-stamp-repro"
    decision = ProjectionRefreshDecision(should_refresh=False, refresh_kind="none", reason="test")
    errors: list[BaseException] = []

    def run(signal_id: str, source_kind: str) -> None:
        try:
            _stamp_case_runtime_state(
                store,
                case_id=case_id,
                signal=_signal(signal_id, source_kind),
                projection_decision=decision,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run, args=("sig-gmail-conc01", "gmail"))
    t2 = threading.Thread(target=run, args=("sig-drive-conc01", "drive"))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive(), "materialization threads did not finish (possible deadlock)"
    assert errors == [], f"unexpected errors: {errors}"

    final_rows = [row for row in store._rows.values() if row.get("case_id") == case_id]
    assert len(final_rows) == 1, "expected exactly one materialized case row (no duplicate)"
    final = final_rows[0]
    seen = set(final.get("last_source_kinds_seen") or [])
    assert seen == {"gmail", "drive"}, (
        f"lost update: expected both signal contributions in last_source_kinds_seen, got {seen}"
    )


def test_reconcile_drive_case_seed_row_new_case_preserves_prior_signal_contribution() -> None:
    store, context = _runtime_context()
    case_id = "case-conc01-seedrow-repro"
    gmail_committed = threading.Event()
    errors: list[BaseException] = []

    def run_gmail() -> None:
        try:
            _stamp_case_runtime_state(
                store,
                case_id=case_id,
                signal=_signal("sig-gmail-seed-conc01", "gmail"),
                projection_decision=ProjectionRefreshDecision(should_refresh=False, refresh_kind="none", reason="test"),
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            gmail_committed.set()

    def run_drive() -> None:
        assert gmail_committed.wait(timeout=5), "gmail materialization never completed"
        drive_signal = CanonicalSignal(
            signal_id="sig-drive-seed-conc01",
            schema_version="v1",
            signal_kind="drive_document_added",
            source_kind="drive",
            source_ref={"file_id": "drv-conc01"},
            observed_at="2026-07-14T10:05:00+02:00",
            effective_at="2026-07-14T10:04:00+02:00",
            case_key_hint="case-key-conc01-seed",
            thread_key_hint=None,
            business_lane="finance",
            signal_summary_pl="CONC-01 drive seed regression",
            payload={
                "case_id": case_id,
                "case_key": "case-key-conc01-seed",
                "document_row": {},
                "fact_rows": [],
                "event_rows": [],
                "graph_upsert": {},
                "case_seed_row": {
                    "case_id": case_id,
                    "case_key": "case-key-conc01-seed",
                    "thread_id": "",
                    "case_family": "finance",
                    "mailbox": "drive",
                    "subject": "Drive seed subject",
                    "status": "open",
                    "customer_name": "",
                    "customer_email": "",
                    "metadata": {"source": "gdrive"},
                    "created_at": "2026-07-14T10:05:00+02:00",
                    "updated_at": "2026-07-14T10:05:00+02:00",
                },
            },
            artifacts={},
            processing_state="new",
            idempotency_key="idem-sig-drive-seed-conc01",
            content_hash=None,
            replayable=True,
            created_by_runtime="test",
        )
        try:
            _reconcile_drive_signal(
                drive_signal,
                runtime_context=context,
                dry_run=False,
                entity_link_dict={},
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=run_gmail)
    t2 = threading.Thread(target=run_drive)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not t1.is_alive() and not t2.is_alive(), "materialization threads did not finish (possible deadlock)"
    assert errors == [], f"unexpected errors: {errors}"

    final = store.fetch_case(case_id)
    assert final is not None
    seen = set(final.get("last_source_kinds_seen") or [])
    assert "gmail" in seen, (
        f"lost update: gmail's prior contribution to last_source_kinds_seen missing after drive "
        f"case_seed_row write, got {seen}"
    )
    assert final.get("subject") == "Drive seed subject"
