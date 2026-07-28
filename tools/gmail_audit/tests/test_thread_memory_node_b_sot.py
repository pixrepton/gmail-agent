from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from requests import ConnectionError as RequestsConnectionError

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from daszek_client import DaszekClientError
from gmail_intake import hydrate_intelligence_seam_config
from mailbox_memory.inmemory import InMemoryMailboxMemoryStore
from mailbox_memory_runtime import MailboxMemoryRuntime


class _Settings:
    attachment_extraction_enabled = False
    attachment_extraction_max_bytes = 8_000_000
    has_google_refresh_flow = False
    has_google_access_token = False
    signal_runtime_mode = "active"


def _thread_memory(*, summary: str = "Klient czeka na termin.") -> dict:
    return {
        "thread_id": "thread-1",
        "case_id": "case-1",
        "canonical_thread_summary": summary,
        "unresolved_questions": ["Kiedy będzie termin?"],
        "commitments_made": [],
        "last_decision": "",
        "participant_last_actions": {},
        "thread_state": "open_questions",
    }


def test_thread_memory_store_is_idempotent_and_versions_real_changes() -> None:
    store = InMemoryMailboxMemoryStore()
    first = {
        "thread_id": "thread-1",
        "case_id": "case-1",
        "source_message_id": "msg-1",
        "memory_json": _thread_memory(),
        "memory_sha256": "hash-1",
        "source_kind": "node_b_generated",
        "updated_at": "2026-07-28T12:00:00+00:00",
    }

    store.upsert_thread_memory(first)
    store.upsert_thread_memory(first)
    replayed = store.fetch_thread_memory("thread-1")

    assert replayed is not None
    assert replayed["version"] == 1
    assert replayed["memory_json"]["canonical_thread_summary"] == "Klient czeka na termin."

    changed = dict(first)
    changed["source_message_id"] = "msg-2"
    changed["memory_json"] = _thread_memory(summary="Termin został potwierdzony.")
    changed["memory_sha256"] = "hash-2"
    changed["updated_at"] = "2026-07-28T13:00:00+00:00"
    store.upsert_thread_memory(changed)

    current = store.fetch_thread_memory("thread-1")
    assert current is not None
    assert current["version"] == 2
    assert current["source_message_id"] == "msg-2"
    assert current["memory_json"]["canonical_thread_summary"] == "Termin został potwierdzony."


def test_daszek_migration_cannot_overwrite_node_b_thread_memory() -> None:
    store = InMemoryMailboxMemoryStore()
    canonical = {
        "thread_id": "thread-1",
        "case_id": "case-1",
        "source_message_id": "msg-current",
        "memory_json": _thread_memory(summary="Node B jest aktualny."),
        "memory_sha256": "node-b-hash",
        "source_kind": "node_b_generated",
        "updated_at": "2026-07-28T14:00:00+00:00",
    }
    stale_migration = {
        **canonical,
        "source_message_id": "",
        "memory_json": _thread_memory(summary="Stara projekcja Daszka."),
        "memory_sha256": "daszek-hash",
        "source_kind": "daszek_migration",
    }

    store.upsert_thread_memory(canonical)
    store.upsert_thread_memory(stale_migration, only_if_absent=True)

    current = store.fetch_thread_memory("thread-1")
    assert current is not None
    assert current["memory_sha256"] == "node-b-hash"
    assert current["memory_json"]["canonical_thread_summary"] == "Node B jest aktualny."


def test_hydrate_reads_thread_memory_from_node_b_without_daszek_read() -> None:
    runtime = MagicMock()
    runtime.fetch_thread_memory.return_value = _thread_memory()
    client = MagicMock()
    client.get_v2_calibration_profile.return_value = {}
    stage_config = {"settings": _Settings()}

    hydrate_intelligence_seam_config(
        {"runtime_controls": {}, "mailbox_memory_runtime": runtime, "daszek_client": client},
        {"source_message": {"thread_id": "thread-1", "message_id": "msg-1"}},
        stage_config,
    )

    assert stage_config["existing_thread_memory"]["thread_id"] == "thread-1"
    runtime.fetch_thread_memory.assert_called_once_with("thread-1")
    client.get_v2_thread_memory.assert_not_called()


def test_hydrate_lazily_migrates_existing_daszek_projection_once() -> None:
    runtime = MagicMock()
    runtime.fetch_thread_memory.return_value = {}
    client = MagicMock()
    remote = _thread_memory(summary="Historyczna projekcja.")
    runtime.persist_thread_memory.return_value = remote
    client.get_v2_thread_memory.return_value = remote
    client.get_v2_calibration_profile.return_value = {}
    stage_config = {"settings": _Settings()}

    hydrate_intelligence_seam_config(
        {"runtime_controls": {}, "mailbox_memory_runtime": runtime, "daszek_client": client},
        {"source_message": {"thread_id": "thread-1", "message_id": "msg-1"}},
        stage_config,
    )

    assert stage_config["existing_thread_memory"] == remote
    runtime.persist_thread_memory.assert_called_once_with(
        remote,
        case_id="case-1",
        message_id="msg-1",
        source_kind="daszek_migration",
        only_if_absent=True,
    )


def test_daszek_outage_does_not_fail_hydration_or_fabricate_memory() -> None:
    runtime = MagicMock()
    runtime.fetch_thread_memory.return_value = {}
    client = MagicMock()
    client.get_v2_thread_memory.side_effect = DaszekClientError("down")
    client.get_v2_calibration_profile.side_effect = DaszekClientError("down")
    stage_config = {"settings": _Settings()}

    hydrate_intelligence_seam_config(
        {"runtime_controls": {}, "mailbox_memory_runtime": runtime, "daszek_client": client},
        {"source_message": {"thread_id": "thread-1", "message_id": "msg-1"}},
        stage_config,
    )

    assert "existing_thread_memory" not in stage_config
    runtime.persist_thread_memory.assert_not_called()


def test_raw_network_outage_from_daszek_client_is_also_degraded() -> None:
    runtime = MagicMock()
    runtime.fetch_thread_memory.return_value = {}
    client = MagicMock()
    client.get_v2_thread_memory.side_effect = RequestsConnectionError("connection refused")
    client.get_v2_calibration_profile.side_effect = RequestsConnectionError("connection refused")
    stage_config = {"settings": _Settings()}

    hydrate_intelligence_seam_config(
        {"runtime_controls": {}, "mailbox_memory_runtime": runtime, "daszek_client": client},
        {"source_message": {"thread_id": "thread-1", "message_id": "msg-1"}},
        stage_config,
    )

    assert "existing_thread_memory" not in stage_config
    runtime.persist_thread_memory.assert_not_called()


def test_finalize_persists_generated_thread_memory_in_node_b(tmp_path: Path) -> None:
    store = InMemoryMailboxMemoryStore()
    runtime = MailboxMemoryRuntime(store=store, blob_root=tmp_path, stage_mode="live")
    store.upsert_case(
        {
            "case_id": "case-1",
            "thread_id": "thread-1",
            "status": "open",
            "metadata": {},
        }
    )

    runtime.finalize_case(
        case_id="case-1",
        message_id="msg-1",
        thread_id="thread-1",
        business_result={},
        reply_result={},
        action_plan_result={},
        case_intelligence_result={"thread_memory": _thread_memory()},
    )

    stored = store.fetch_thread_memory("thread-1")
    assert stored is not None
    assert stored["case_id"] == "case-1"
    assert stored["source_message_id"] == "msg-1"
    assert stored["source_kind"] == "node_b_generated"
    assert stored["memory_json"]["thread_id"] == "thread-1"
