"""service_request intake playbook."""

from __future__ import annotations

import unittest

from playbooks.service_request_intake_v1 import run_service_request_intake_v1


class ServiceRequestPlaybookTests(unittest.TestCase):
    def test_matched_service_topic(self) -> None:
        topic = {"topic_id": "service_request", "confidence": 0.8}
        pb = run_service_request_intake_v1(topic_result=topic, missing_info={"critical": []}, conflicting_facts=[])
        self.assertTrue(pb["matched"])
        self.assertIn("request_missing_info", pb["allowed_action_types"])

    def test_not_matched(self) -> None:
        topic = {"topic_id": "noise", "confidence": 0.9}
        pb = run_service_request_intake_v1(topic_result=topic, missing_info={}, conflicting_facts=[])
        self.assertFalse(pb["matched"])

    def test_case_link_and_calendar_complete_steps(self) -> None:
        topic = {"topic_id": "service_request", "confidence": 0.85}
        pb = run_service_request_intake_v1(
            topic_result=topic,
            missing_info={"critical": ["brak_telefon"]},
            conflicting_facts=[{"field_name": "addr"}],
            case_link_decision="linked",
            calendar_event_count=2,
        )
        self.assertTrue(pb["matched"])
        self.assertTrue(pb["case_link_ok"])
        self.assertIn("verify_case_link", pb["completed_steps"])
        self.assertIn("check_calendar_memory", pb["completed_steps"])
        self.assertGreater(pb["conflict_signal_count"], 0)
        self.assertIn("Nie wysyłaj automatycznie", pb["operator_instruction"])


if __name__ == "__main__":
    unittest.main()
