from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
SCRIPT_DIR = TOOL_DIR / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from central_llm_stage import _mailbox_case_loader
from bounded_skrzat_proof import run_skrzat_bounded_proof


def test_central_case_loader_never_enables_in_memory_fallback() -> None:
    settings = SimpleNamespace(
        mailbox_memory_database_url="postgresql://configured.invalid/mailbox",
        mailbox_memory_stage_mode="live",
    )
    runtime = SimpleNamespace(
        get_context_pack=lambda **_kwargs: SimpleNamespace(active_facts=[], relevant_chunks=[]),
    )

    with patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=runtime) as build_runtime:
        loader = _mailbox_case_loader(settings)
        assert loader is not None
        loader("case-1", "", 3)

    build_runtime.assert_called_once_with(settings, allow_in_memory=False)


def test_bounded_skrzat_proof_rejects_missing_durable_database() -> None:
    settings = SimpleNamespace(
        mailbox_memory_database_url="",
        mailbox_memory_stage_mode="live",
    )

    with patch("bounded_skrzat_proof.build_mailbox_memory_runtime") as build_runtime:
        result = run_skrzat_bounded_proof(case_id="case-1", settings=settings)

    assert result == {
        "ok": False,
        "skipped": False,
        "reason": "mailbox_memory_database_url_required",
    }
    build_runtime.assert_not_called()
