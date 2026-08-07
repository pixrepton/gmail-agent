"""AI-OS 6.1 — instruction envelope outside system prompt."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from unittest.mock import MagicMock, patch

from agent_runtime.constitution import AgentConstitution
from agent_runtime.openai_agent_client import OpenAIToolPlanner


def test_user_instruction_not_in_system_message() -> None:
    settings = SimpleNamespace(
        openai_api_key="test",
        openai_base_url="http://localhost",
        agent_constitution_rag_enabled=False,
    )
    planner = OpenAIToolPlanner(settings=settings, client=object())
    snapshot = SimpleNamespace(
        case_kind="zapytanie_klienta",
        user_instruction="Priorytet: wycena pompy ciepla 12kW",
        case_id="case_1",
        gaps=[],
        hitl_gate=SimpleNamespace(required=False),
    )
    constitution = AgentConstitution(
        hvac_rules="",
        sections={"Rola": "Jestes asystentem operatora."},
        forbidden_actions=("send_email",),
        tool_allowlist=("request_operator_clarification",),
        language="pl",
        company_context="",
        source_path="test",
    )
    with patch(
        "agent_runtime.openai_agent_client._compact_view",
        return_value={"case_id": "case_1", "case_kind": "zapytanie_klienta"},
    ):
        messages = planner._build_messages(
            snapshot=snapshot,
            constitution=constitution,
            available_tools=("request_operator_clarification",),
        )
    system_content = messages[0]["content"]
    assert "Priorytet: wycena pompy ciepla 12kW" not in system_content
    assert "Instrukcja operatora:" not in system_content
    envelope = messages[-1]["content"]
    assert "<operator_instruction>" in envelope
    assert "Priorytet: wycena pompy ciepla 12kW" in envelope
