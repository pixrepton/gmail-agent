"""DELIVERY-1 RC-2: every EXPLICITLY LLM-exposable tool must have schema + handler +
runtime registration, or the planner can choose an action the runtime cannot deliver.

This locks in the reachability invariant identified pre-EVAL-1 (Intelligence Ceiling
audit, 2026-07-15, item A2/L-03) and empirically proven to cause real S4 harm by EVAL-1
(RC-2, `generate_draft_reply`). See DELIVERY-1's tool-reachability-inventory.md for the
full classification (EXPLICITLY LLM-EXPOSABLE / LEGACY-AMBIGUOUS / not applicable) this
test's exclusion lists are drawn from.
"""
from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
from agent_runtime.policy_guardrails import _FORBIDDEN_TOOL_NAMES
from agent_runtime.tool_schemas import openai_tool_definitions
from agent_runtime.tools.handlers import HANDLERS


def _schema_covered_names(allowlist: tuple[str, ...]) -> set[str]:
    return {t["function"]["name"] for t in openai_tool_definitions(allowlist)}


def test_mail_agent_allowlist_fully_covered_by_schemas() -> None:
    missing = set(MAIL_AGENT_TOOL_ALLOWLIST) - _schema_covered_names(MAIL_AGENT_TOOL_ALLOWLIST)
    assert not missing, f"MAIL_AGENT_TOOL_ALLOWLIST tools with no OpenAI schema: {missing}"


def test_chat_agent_allowlist_fully_covered_by_schemas() -> None:
    missing = set(CHAT_AGENT_TOOL_ALLOWLIST) - _schema_covered_names(CHAT_AGENT_TOOL_ALLOWLIST)
    assert not missing, f"CHAT_AGENT_TOOL_ALLOWLIST tools with no OpenAI schema: {missing}"


def test_mail_agent_allowlist_fully_covered_by_handlers() -> None:
    missing = [name for name in MAIL_AGENT_TOOL_ALLOWLIST if name not in HANDLERS]
    assert not missing, f"MAIL_AGENT_TOOL_ALLOWLIST tools with no handler: {missing}"


def test_chat_agent_allowlist_fully_covered_by_handlers() -> None:
    missing = [name for name in CHAT_AGENT_TOOL_ALLOWLIST if name not in HANDLERS]
    assert not missing, f"CHAT_AGENT_TOOL_ALLOWLIST tools with no handler: {missing}"


def test_openai_tool_definitions_never_silently_drops_an_allowlisted_tool() -> None:
    """The exact proximate mechanism of RC-2: openai_tool_definitions(allowlist) filters
    to `name in specs` with no error/warning on a miss. Once schemas are complete this
    must be a true no-op filter — assert it, so a future allowlist addition without a
    matching schema fails loudly here instead of crashing a live planner turn."""
    for allowlist in (MAIL_AGENT_TOOL_ALLOWLIST, CHAT_AGENT_TOOL_ALLOWLIST):
        tools = openai_tool_definitions(allowlist)
        assert len(tools) == len(allowlist), (
            f"openai_tool_definitions silently dropped "
            f"{set(allowlist) - {t['function']['name'] for t in tools}}"
        )


def test_no_forbidden_tool_is_ever_allowlisted() -> None:
    """Defense-in-depth check on policy_guardrails._FORBIDDEN_TOOL_NAMES: a forbidden
    tool must never even be offered to the planner, not merely blocked at execution time."""
    for allowlist in (MAIL_AGENT_TOOL_ALLOWLIST, CHAT_AGENT_TOOL_ALLOWLIST):
        overlap = set(allowlist) & _FORBIDDEN_TOOL_NAMES
        assert not overlap, f"Forbidden tool(s) present in allowlist: {overlap}"


def test_every_handler_is_either_allowlisted_or_explicitly_documented_as_not() -> None:
    """A handler with no allowlist membership anywhere is either dead code or a
    silent extra capability nobody can reach. Either way it must be a deliberate,
    named exception (tool-reachability-inventory.md), not an accident."""
    all_allowlisted = set(MAIL_AGENT_TOOL_ALLOWLIST) | set(CHAT_AGENT_TOOL_ALLOWLIST)
    orphaned = set(HANDLERS) - all_allowlisted
    assert not orphaned, (
        f"Handler(s) with no allowlist membership: {orphaned}. Either allowlist them "
        f"(with a schema) or remove the orphaned runtime entrypoint."
    )
