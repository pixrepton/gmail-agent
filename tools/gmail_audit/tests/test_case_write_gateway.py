from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_family_boundary import is_operational_feed_case_row
from case_routing import desk_eligible, operator_priority_to_label
from case_write_gateway import patch_case_row, write_case_row
from signal_reconciler import _stamp_case_runtime_state


def test_manual_p1_in_operational_feed() -> None:
    row = {
        "case_id": "task-manual-p1",
        "case_family": "operations",
        "metadata": {
            "source_kind": "manual",
            "requires_action": True,
            "priority_label": operator_priority_to_label("pilne"),
        },
    }
    assert is_operational_feed_case_row(row) is True
    assert desk_eligible(row) is True


def test_manual_p3_not_desk_eligible() -> None:
    row = {
        "case_id": "task-manual-p3",
        "case_family": "operations",
        "metadata": {
            "source_kind": "manual",
            "requires_action": True,
            "priority_label": operator_priority_to_label("niski"),
        },
    }
    assert is_operational_feed_case_row(row) is True
    assert desk_eligible(row) is False


def test_write_case_row_uses_upsert_gateway() -> None:
    store = MagicMock()
    store.upsert_case = MagicMock()
    row = {
        "case_id": "task-gateway-1",
        "case_family": "operations",
        "subject": "Faktura XYZ",
        "metadata": {
            "source_kind": "manual",
            "requires_action": True,
            "export_case_type": "operations",
            "priority": "pilne",
            "priority_label": operator_priority_to_label("pilne"),
        },
    }
    enriched, routing = write_case_row(row, mailbox_store=store, source_kind="manual")
    store.upsert_case.assert_called_once()
    assert enriched["case_id"] == "task-gateway-1"
    assert routing.desk_eligible is True


def test_patch_case_row_merges_metadata() -> None:
    store = MagicMock()
    store.fetch_case = MagicMock(
        return_value={
            "case_id": "task-patch-1",
            "case_family": "operations",
            "subject": "Faktura",
            "metadata": {
                "source_kind": "manual",
                "requires_action": True,
                "export_case_type": "operations",
                "priority_label": operator_priority_to_label("pilne"),
            },
        }
    )
    store.upsert_case = MagicMock()
    enriched, _routing = patch_case_row(
        "task-patch-1",
        {"task_status": "done", "done_at": "2026-07-08T12:00:00"},
        mailbox_store=store,
        updated_at="2026-07-08T12:00:00",
    )
    store.fetch_case.assert_called_once_with("task-patch-1")
    store.upsert_case.assert_called_once()
    meta = enriched.get("metadata") or {}
    assert meta.get("task_status") == "done"
    assert meta.get("requires_action") is True


class _ControlledConcurrentCaseStore:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = dict(row)
        self._fetch_barrier = threading.Barrier(2)
        self._worker_write_done = threading.Event()
        self._lock = threading.Lock()

    def fetch_case(self, case_id: str) -> dict[str, object]:
        assert case_id == self._row["case_id"]
        snapshot = dict(self._row)
        metadata = snapshot.get("metadata")
        if isinstance(metadata, dict):
            snapshot["metadata"] = dict(metadata)
        try:
            self._fetch_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return snapshot

    def upsert_case(self, row: dict[str, object]) -> None:
        next_row = dict(row)
        metadata = next_row.get("metadata")
        if isinstance(metadata, dict):
            next_row["metadata"] = dict(metadata)
        if threading.current_thread().name == "worker":
            self._row = next_row
            self._worker_write_done.set()
            return
        self._worker_write_done.wait(timeout=5)
        self._row = next_row

    def mutate_case(self, case_id: str, mutator, *, create_if_missing: bool = False) -> dict[str, object]:
        with self._lock:
            if case_id != self._row.get("case_id") and not create_if_missing:
                raise LookupError(f"case not found: {case_id}")
            current = dict(self._row if case_id == self._row.get("case_id") else {"case_id": case_id, "metadata": {}})
            metadata = current.get("metadata")
            if isinstance(metadata, dict):
                current["metadata"] = dict(metadata)
            updated = mutator(current)
            if not isinstance(updated, dict):
                raise AssertionError("mutator must return dict row")
            next_row = dict(updated)
            next_meta = next_row.get("metadata")
            if isinstance(next_meta, dict):
                next_row["metadata"] = dict(next_meta)
            self._row = next_row
            return dict(self._row)

    @property
    def row(self) -> dict[str, object]:
        current = dict(self._row)
        metadata = current.get("metadata")
        if isinstance(metadata, dict):
            current["metadata"] = dict(metadata)
        return current


def test_parallel_worker_and_operator_updates_do_not_lose_independent_fields() -> None:
    store = _ControlledConcurrentCaseStore(
        {
            "case_id": "task-race-1",
            "case_family": "operations",
            "subject": "Operator task",
            "status": "open",
            "latest_signal_id": "",
            "latest_signal_at": "",
            "last_rebuild_at": "",
            "last_projection_refresh_at": "",
            "last_source_kinds_seen": [],
            "metadata": {
                "source_kind": "manual",
                "requires_action": True,
                "export_case_type": "operations",
                "priority_label": operator_priority_to_label("pilne"),
            },
            "created_at": "2026-07-13T08:00:00+00:00",
            "updated_at": "2026-07-13T08:00:00+00:00",
        }
    )
    errors: list[str] = []

    def run_worker() -> None:
        try:
            _stamp_case_runtime_state(
                store,
                case_id="task-race-1",
                signal=SimpleNamespace(
                    signal_id="sig-race-1",
                    observed_at="2026-07-13T08:01:00+00:00",
                    source_kind="gmail",
                ),
                projection_decision=SimpleNamespace(should_refresh=True),
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"worker:{type(exc).__name__}:{exc}")

    def run_operator() -> None:
        try:
            patch_case_row(
                "task-race-1",
                {"task_status": "done", "done_at": "2026-07-13T08:02:00+00:00"},
                mailbox_store=store,
                updated_at="2026-07-13T08:02:00+00:00",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"operator:{type(exc).__name__}:{exc}")

    worker = threading.Thread(target=run_worker, name="worker")
    operator = threading.Thread(target=run_operator, name="operator")
    worker.start()
    operator.start()
    worker.join(timeout=5)
    operator.join(timeout=5)

    assert errors == []
    final_row = store.row
    assert final_row.get("latest_signal_id") == "sig-race-1"
    assert final_row.get("last_projection_refresh_at")
    metadata = final_row.get("metadata") or {}
    assert metadata.get("task_status") == "done"
    assert metadata.get("requires_action") is True


# CASE_WRITE_GATEWAY_PROOF_OK · MANUAL_DESK_FEED_PROOF_OK
