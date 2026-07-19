"""EVAL-RECOVERY-1: `generate_draft_reply` argument-schema mismatch (clean-eval-rerun
finding, `C:\\ai-os-clean-eval-rerun-20260717T134659Z\\environment-and-capacity-state.md`).

Root cause (traced this session, not assumed): `generate_draft_reply`'s handler
(`agent_runtime/tools/handlers.py:103-134`) is Model A — a deterministic template
composer. Its `intent` argument is a classification label ("quote" | "missing_info")
that selects which hardcoded Polish template to fill from `ctx.snapshot.hvac_profile`;
the LLM never supplies draft content. This is consistent with
`docs/core/AGENT_RUNTIME_ARCHITECTURE.md` ("Python zawsze zapisuje delta przez
Pydantic; LLM tylko planuje ToolCallPlan").

But `agent_runtime/openai_agent_client.py`'s `_build_messages()` unconditionally told
every agent, including the mail agent (whose allowlist, `constitution_mail.py`,
structurally excludes `propose_mutation`), to draft via
`propose_mutation(operation=generate_draft)` — a *different*, Model B tool
(`agent_runtime/tools/write_executors.py:740-774`'s `execute_generate_draft`, which
persists a caller-supplied `body`/`subject`/`to` payload verbatim). The mail agent
cannot call `propose_mutation` (not offered in its `tools` list at all), so it fell
back to the only reachable drafting tool, `generate_draft_reply` — but carried over
the Model B mental shape from the instruction it was just given, producing arguments
like `{"quote": "<full text>", "missing_info": null, "intent": "quote"}`. Groq's
strict tool-call schema validation rejects this (`additionalProperties 'quote',
'missing_info' not allowed`) before the model's own draft judgment is ever scored.

These tests lock in the resolved, single contract: `generate_draft_reply` stays
strictly `intent`-only (no loosening to `additionalProperties: true`, no absorbing
model-invented fields), and the planner prompt must never prime an agent toward a
tool contract it cannot reach.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution import load_constitution
from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
from agent_runtime.openai_agent_client import OpenAIToolPlanner
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.tool_schemas import openai_tool_definitions
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _generate_draft_reply_schema(allowlist: tuple[str, ...]) -> dict:
    tools = openai_tool_definitions(allowlist)
    for tool in tools:
        if tool["function"]["name"] == "generate_draft_reply":
            return tool["function"]["parameters"]
    raise AssertionError("generate_draft_reply schema not found for allowlist")


def _snapshot(**kwargs: object) -> EngagementSnapshotV2:
    base = {
        "engagement_id": "eng_draft_contract",
        "case_id": "case_draft_contract",
        "version": 1,
        "trace_id": "sig_draft_contract",
        "operational_status": {"code": "enriching", "steps_remaining": 8},
        "hvac_profile": {"location": {}},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(kwargs)
    return EngagementSnapshotV2.model_validate(base)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


class TestGenerateDraftReplySchemaContract:
    """Structural evidence: the exact malformed call observed live (Groq,
    `run-1-log.txt`, cases INT-04/NEW-02) must be invalid against our own schema —
    proves the model's argument shape is wrong, not that our schema is too strict."""

    def test_schema_rejects_the_exact_observed_malformed_call(self) -> None:
        schema = _generate_draft_reply_schema(MAIL_AGENT_TOOL_ALLOWLIST)
        malformed = {
            "quote": "Dzień dobry, przygotowaliśmy wstępną kalkulację...",
            "missing_info": None,
            "intent": "quote",
        }
        errors = list(Draft202012Validator(schema).iter_errors(malformed))
        assert errors, "additionalProperties=False must reject content-bearing arguments"

    def test_schema_accepts_the_correct_intent_only_shape(self) -> None:
        schema = _generate_draft_reply_schema(MAIL_AGENT_TOOL_ALLOWLIST)
        for intent in ("quote", "missing_info"):
            errors = list(Draft202012Validator(schema).iter_errors({"intent": intent}))
            assert not errors, f"intent-only call must validate cleanly: {errors}"

    def test_schema_has_no_content_fields_at_all(self) -> None:
        """Guards against the wrong fix ('dopiszmy quote/missing_info jako pola') —
        the brief explicitly forbids absorbing model-invented fields into the schema."""
        schema = _generate_draft_reply_schema(MAIL_AGENT_TOOL_ALLOWLIST)
        assert set(schema["properties"].keys()) == {"intent"}
        assert schema.get("additionalProperties") is False


class TestPlannerPromptMatchesReachableToolContract:
    """The planner's system prompt must only reference tools it actually offers this
    turn. Before the fix, `_build_messages` unconditionally told every agent to draft
    via `propose_mutation(operation=generate_draft)`, even the mail agent, whose
    allowlist never includes `propose_mutation` — the real root cause of the
    argument-schema mismatch above."""

    def test_mail_agent_prompt_never_references_unreachable_propose_mutation(self) -> None:
        planner = OpenAIToolPlanner(settings=_settings())
        constitution = replace(load_constitution(), tool_allowlist=MAIL_AGENT_TOOL_ALLOWLIST)
        messages = planner._build_messages(
            snapshot=_snapshot(),
            constitution=constitution,
            available_tools=MAIL_AGENT_TOOL_ALLOWLIST,
        )
        system_text = messages[0]["content"]
        assert "propose_mutation" not in system_text, (
            "mail agent cannot call propose_mutation (constitution_mail.py excludes it) "
            "— the prompt must not instruct it to use one anyway"
        )
        assert "generate_draft_reply" in system_text

    def test_mail_agent_prompt_clarifies_intent_is_classification_only(self) -> None:
        planner = OpenAIToolPlanner(settings=_settings())
        constitution = replace(load_constitution(), tool_allowlist=MAIL_AGENT_TOOL_ALLOWLIST)
        messages = planner._build_messages(
            snapshot=_snapshot(),
            constitution=constitution,
            available_tools=MAIL_AGENT_TOOL_ALLOWLIST,
        )
        system_text = messages[0]["content"]
        assert "generate_draft_reply(intent=" in system_text

    def test_chat_agent_prompt_still_references_propose_mutation_when_reachable(self) -> None:
        """Regression guard: the fix must not remove the correct instruction for the
        chat agent, which genuinely has `propose_mutation` in its allowlist."""
        planner = OpenAIToolPlanner(settings=_settings())
        constitution = replace(load_constitution(), tool_allowlist=CHAT_AGENT_TOOL_ALLOWLIST)
        messages = planner._build_messages(
            snapshot=_snapshot(),
            constitution=constitution,
            available_tools=CHAT_AGENT_TOOL_ALLOWLIST,
        )
        system_text = messages[0]["content"]
        assert "propose_mutation(operation=generate_draft)" in system_text
