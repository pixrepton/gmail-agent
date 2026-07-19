from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from mailbox_memory_store import InMemoryMailboxMemoryStore
from mailbox_v2_desk_note import (
    persist_open_desk_note_id_from_v2_projection,
    resolve_v2_desk_note_id,
    stable_v2_desk_note_id_for_case,
)


def test_stable_v2_desk_note_id_matches_gateb_handoff() -> None:
    assert stable_v2_desk_note_id_for_case("case_062a7aa4ed7b") == "note_211248db920e"


def test_resolve_v2_desk_note_id_from_metadata() -> None:
    case_row = {
        "case_id": "case_062a7aa4ed7b",
        "latest_signal_id": "sig_x",
        "metadata": {"open_desk_note_id": "note_custom"},
    }
    assert resolve_v2_desk_note_id(case_row, {}) == "note_custom"


def test_resolve_v2_desk_note_id_stable_fallback() -> None:
    case_row = {
        "case_id": "case_062a7aa4ed7b",
        "latest_signal_id": "sig_7191d147bf56c058da8e",
        "metadata": {},
    }
    assert resolve_v2_desk_note_id(case_row, None) == "note_211248db920e"


def test_resolve_v2_desk_note_id_no_signal_no_stable() -> None:
    case_row = {"case_id": "case_062a7aa4ed7b", "metadata": {}}
    assert resolve_v2_desk_note_id(case_row, None) == ""


def test_persist_open_desk_note_id_from_v2_projection() -> None:
    store = InMemoryMailboxMemoryStore()
    store.upsert_case({"case_id": "case_062a7aa4ed7b", "metadata": {}})
    ok = persist_open_desk_note_id_from_v2_projection(
        store,
        {
            "case_patch": {"case_id": "case_062a7aa4ed7b"},
            "desk_note_patch": {"desk_note_id": "note_211248db920e", "case_id": "case_062a7aa4ed7b"},
        },
    )
    assert ok is True
    row = store.fetch_case("case_062a7aa4ed7b") or {}
    assert row["metadata"]["open_desk_note_id"] == "note_211248db920e"
