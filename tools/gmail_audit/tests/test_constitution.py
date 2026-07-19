"""Comprehensive tests for constitution modules — agent personality definitions."""
from __future__ import annotations

import unittest
from types import SimpleNamespace


class TestConstitutionChat(unittest.TestCase):
    """Test chat-agent constitution (cyfrowy wspolnik)."""

    def test_chat_allowlist_contains_business_pulse_tools(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
        bp_tools = {
            "get_pipeline_summary", "get_client_health", "get_daily_delta",
            "get_win_rate", "get_top_clients", "get_revenue_forecast",
            "get_system_health_snapshot", "get_business_signals", "get_agent_activity_summary",
        }
        self.assertTrue(
            bp_tools.issubset(set(CHAT_AGENT_TOOL_ALLOWLIST)),
            f"Missing BP tools: {bp_tools - set(CHAT_AGENT_TOOL_ALLOWLIST)}"
        )

    def test_chat_allowlist_contains_core_tools(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
        core = {"search_gmail_thread", "search_rag_knowledge", "query_anything"}
        self.assertTrue(
            core.issubset(set(CHAT_AGENT_TOOL_ALLOWLIST)),
            f"Missing core tools: {core - set(CHAT_AGENT_TOOL_ALLOWLIST)}"
        )

    def test_chat_allowlist_no_duplicates(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
        self.assertEqual(len(CHAT_AGENT_TOOL_ALLOWLIST), len(set(CHAT_AGENT_TOOL_ALLOWLIST)))

    def test_chat_budget_has_all_allowlisted_tools(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST, CHAT_AGENT_TOOL_BUDGET
        for tool in CHAT_AGENT_TOOL_ALLOWLIST:
            self.assertIn(
                tool, CHAT_AGENT_TOOL_BUDGET,
                f"Tool {tool!r} is in ALLOWLIST but missing from BUDGET"
            )

    def test_chat_budget_no_unknown_tools(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST, CHAT_AGENT_TOOL_BUDGET
        for tool in CHAT_AGENT_TOOL_BUDGET:
            self.assertIn(
                tool, CHAT_AGENT_TOOL_ALLOWLIST,
                f"Tool {tool!r} is in BUDGET but missing from ALLOWLIST"
            )

    def test_chat_system_note_contains_business_pulse_reference(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_SYSTEM_NOTE
        self.assertIn("get_pipeline_summary", CHAT_AGENT_SYSTEM_NOTE)
        self.assertIn("Business Pulse", CHAT_AGENT_SYSTEM_NOTE)
        self.assertIn("wspolnikiem", CHAT_AGENT_SYSTEM_NOTE)

    def test_chat_system_note_has_osobowosc(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_SYSTEM_NOTE
        self.assertIn("OSOBOWOSC", CHAT_AGENT_SYSTEM_NOTE)
        self.assertIn("PAMIEC", CHAT_AGENT_SYSTEM_NOTE)

    def test_chat_allowlist_length(self):
        from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
        self.assertGreaterEqual(len(CHAT_AGENT_TOOL_ALLOWLIST), 20)


class TestConstitutionMail(unittest.TestCase):
    """Test mail-agent constitution (wspolnik operacyjny)."""

    def test_mail_allowlist_has_core_tools(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
        core = {"search_gmail_thread", "extract_facts_from_text", "report_gaps_and_stop"}
        self.assertTrue(
            core.issubset(set(MAIL_AGENT_TOOL_ALLOWLIST)),
            f"Missing tools: {core - set(MAIL_AGENT_TOOL_ALLOWLIST)}"
        )

    def test_mail_allowlist_no_write_tools(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
        write_tools = {"propose_mutation", "propose_plan"}
        self.assertFalse(
            write_tools.intersection(set(MAIL_AGENT_TOOL_ALLOWLIST)),
            f"Mail agent has write tools: {write_tools.intersection(set(MAIL_AGENT_TOOL_ALLOWLIST))}"
        )

    def test_mail_allowlist_no_duplicates(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
        self.assertEqual(len(MAIL_AGENT_TOOL_ALLOWLIST), len(set(MAIL_AGENT_TOOL_ALLOWLIST)))

    def test_mail_budget_has_all_allowlisted_tools(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST, MAIL_AGENT_TOOL_BUDGET
        for tool in MAIL_AGENT_TOOL_ALLOWLIST:
            self.assertIn(
                tool, MAIL_AGENT_TOOL_BUDGET,
                f"Tool {tool!r} is in ALLOWLIST but missing from BUDGET"
            )

    def test_mail_system_note_contains_role(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_SYSTEM_NOTE
        self.assertIn("wspolnikiem", MAIL_AGENT_SYSTEM_NOTE)
        self.assertIn("TOP-INSTAL", MAIL_AGENT_SYSTEM_NOTE)
        self.assertIn("KLIENCI", MAIL_AGENT_SYSTEM_NOTE)

    def test_mail_budget_read_only_tight(self):
        from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_BUDGET
        for tool, limit in MAIL_AGENT_TOOL_BUDGET.items():
            self.assertLessEqual(limit, 10, f"Mail agent budget for {tool} is {limit}, expected <=10")


class TestConstitutionLoad(unittest.TestCase):
    """Test constitution loading from the base module."""

    def test_load_constitution_file_exists(self):
        from pathlib import Path
        from agent_runtime.constitution import DEFAULT_CONSTITUTION_PATH
        self.assertTrue(Path(DEFAULT_CONSTITUTION_PATH).is_file())

    def test_load_constitution_returns_object(self):
        from agent_runtime.constitution import load_constitution
        constitution = load_constitution()
        self.assertIsNotNone(constitution.hvac_rules)
        self.assertIsInstance(constitution.forbidden_actions, tuple)
        self.assertIsInstance(constitution.tool_allowlist, tuple)

    def test_constitution_has_sections(self):
        from agent_runtime.constitution import load_constitution
        constitution = load_constitution()
        self.assertGreater(len(constitution.sections), 0)

    def test_constitution_forbidden_actions(self):
        from agent_runtime.constitution import load_constitution
        constitution = load_constitution()
        self.assertIn("send_email", constitution.forbidden_actions)


if __name__ == "__main__":
    unittest.main()
