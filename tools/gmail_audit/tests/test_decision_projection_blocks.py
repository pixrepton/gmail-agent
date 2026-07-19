"""Decision view projection blocks (mailbox fallback + full CI).

Glossary is the semantic source of truth for fixed PL copy: see
``docs/core/action_semantics_glossary.md`` (Projection surface copy) and
``decision_projection_blocks.PROJECTION_COPY_PL`` / ``V2_PROJECTION_LABEL_PL`` in code.
"""

from __future__ import annotations

import unittest

from decision_projection_blocks import (
    PROJECTION_COPY_PL,
    V2_PROJECTION_LABEL_PL,
    build_decision_view_blocks,
)


class DecisionProjectionBlocksTests(unittest.TestCase):
    def test_mailbox_fallback_populates_headline(self) -> None:
        vnext = {
            "case_summary": {"summary_text": "Serwis pompy — klient prosi o wizytę."},
            "completeness_gaps": ["brak_telefon"],
            "conflicting_facts": [],
        }
        pack = {"snapshot": {}, "runtime_state": {"latest_signal_at": "2026-01-02T10:00:00Z"}}
        dv = build_decision_view_blocks(case_intelligence=None, mailbox_context={"vnext": vnext, "pack": pack})
        self.assertIn("Serwis pompy", dv.get("headline_co_pl", ""))
        self.assertTrue(dv.get("missing_info_cards"))

    def test_operator_essence_precedence_over_reasoning_and_context(self) -> None:
        ci = {
            "understanding_output": {
                "operator_explanation": {"essence_pl": "OPERATOR ESSENCE"},
                "situation_summary_pl": "SITUATION SUMMARY",
            },
            "case_guidance": {"reason_summary_pl": "REASONING SHOULD NOT WIN"},
            "case_understanding": {"summary_operator": "CASE SUMMARY SHOULD NOT WIN"},
            "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc_ess"}}},
        }
        mailbox = {
            "vnext": {"case_summary": {"summary_text": "CONTEXT SHOULD NOT WIN"}},
            "pack": {"snapshot": {"summary_text": "MAILBOX SHOULD NOT WIN"}},
        }

        dv = build_decision_view_blocks(case_intelligence=ci, mailbox_context=mailbox)

        ribbon = dv.get("collapsed_operator_pl") or {}
        self.assertEqual(ribbon.get("essence_pl"), "OPERATOR ESSENCE")
        self.assertEqual(ribbon.get("essence_source_role"), "operator_essence")
        self.assertIn("essence<=understanding_output.operator_explanation", ribbon.get("derivation_tags") or [])

    def test_empty_operator_essence_falls_back_to_situation_then_context(self) -> None:
        ci = {
            "understanding_output": {
                "operator_explanation": {"essence_pl": ""},
                "situation_summary_pl": "SITUATION WINS",
            },
            "case_guidance": {"reason_summary_pl": "REASONING SHOULD NOT WIN"},
            "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc_sit"}}},
        }
        mailbox = {"vnext": {"case_summary": {"summary_text": "CONTEXT NEXT"}}}

        dv = build_decision_view_blocks(case_intelligence=ci, mailbox_context=mailbox)

        ribbon = dv.get("collapsed_operator_pl") or {}
        self.assertEqual(ribbon.get("essence_pl"), "SITUATION WINS")
        self.assertEqual(ribbon.get("essence_source_role"), "situation_summary")

        ci["understanding_output"]["situation_summary_pl"] = ""
        dv2 = build_decision_view_blocks(case_intelligence=ci, mailbox_context=mailbox)
        ribbon2 = dv2.get("collapsed_operator_pl") or {}
        self.assertEqual(ribbon2.get("essence_pl"), "CONTEXT NEXT")
        self.assertEqual(ribbon2.get("essence_source_role"), "context_pack_summary")

    def test_mailbox_snapshot_fallback_is_labeled_read_only(self) -> None:
        dv = build_decision_view_blocks(
            case_intelligence={"decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc_mbox"}}}},
            mailbox_context={"vnext": {"case_summary": {}}, "pack": {"snapshot": {"summary_text": "MAILBOX SNAPSHOT"}}},
        )

        ribbon = dv.get("collapsed_operator_pl") or {}
        self.assertEqual(ribbon.get("essence_pl"), "MAILBOX SNAPSHOT")
        self.assertEqual(ribbon.get("essence_source_role"), "mailbox_snapshot_summary")
        self.assertIn("pamięci skrzynki", ribbon.get("essence_source_label_pl", "").lower())

    def test_summary_precedence_does_not_read_raw_mail_keys(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": ""}},
            "body": "RAW_BODY_SHOULD_NOT_WIN",
            "snippet": "RAW_SNIPPET_SHOULD_NOT_WIN",
        }
        dv = build_decision_view_blocks(case_intelligence=ci, mailbox_context={})
        rendered = repr(dv)
        self.assertNotIn("RAW_BODY_SHOULD_NOT_WIN", rendered)
        self.assertNotIn("RAW_SNIPPET_SHOULD_NOT_WIN", rendered)
        self.assertEqual((dv.get("collapsed_operator_pl") or {}).get("essence_source_role"), "projection_fallback")

    def test_full_ci_includes_candidate_ids(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "Test essence"}},
            "decision_pipeline": {
                "outputs": {
                    "decision_candidate": {
                        "decision_candidate_id": "dc_x",
                        "recommended_mode": "projection_only",
                        "topic": "service",
                        "priority": "high",
                    },
                    "service_request_playbook": {"operator_instruction": "Instrukcja testowa"},
                },
                "projection_ready": True,
                "finished_at": "2026-01-01Z",
            },
            "policy_decision": {"policy_decision_id": "pdec_y", "status": "needs_human"},
            "action_proposals_v2": [
                {
                    "proposal_id": "apv2_z",
                    "action_type": "prepare_reply_draft",
                    "policy_decision_id": "pdec_y",
                    "summary": "S",
                    "status": "proposed",
                    "allowed_by_policy": True,
                }
            ],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        self.assertEqual(dv.get("decision_candidate_id"), "dc_x")
        self.assertEqual(dv.get("policy_decision_id"), "pdec_y")
        self.assertIn("apv2_z", dv.get("action_proposal_ids", []))
        actions = dv.get("action_proposals") or []
        self.assertTrue(actions)
        self.assertTrue(actions[0].get("policy_spine_ok"))
        self.assertNotIn("brak zezwolenia", (actions[0].get("action_type_label_pl") or "").lower())
        ribbon = dv.get("collapsed_operator_pl") or {}
        self.assertTrue(ribbon.get("details_collapsed_by_default"))
        self.assertEqual(ribbon.get("priority"), "high")
        why = dv.get("why_pl") or ""
        self.assertIn(PROJECTION_COPY_PL["why_candidate_mode_prefix_pl"].rstrip(":"), why)
        self.assertIn(PROJECTION_COPY_PL["why_playbook_prefix_pl"].rstrip(":"), why)
        self.assertNotIn("Tryb rekomendowany", why)
        self.assertIn(V2_PROJECTION_LABEL_PL["prepare_reply_draft"], dv.get("proposal_summary_pl", ""))
        self.assertEqual(
            (dv.get("primary_button") or {}).get("label_pl"),
            PROJECTION_COPY_PL["primary_review_label_pl"],
        )

    def test_blocked_v2_proposal_appends_non_executable_suffix(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "e"}},
            "decision_pipeline": {
                "outputs": {"decision_candidate": {"decision_candidate_id": "dc_b"}},
                "projection_ready": True,
            },
            "policy_decision": {"policy_decision_id": "pdec_b", "status": "needs_human"},
            "action_proposals_v2": [
                {
                    "proposal_id": "apv2_b",
                    "action_type": "prepare_reply_draft",
                    "summary": "S",
                    "status": "proposed",
                    "allowed_by_policy": False,
                    "policy_decision_id": "pdec_b",
                }
            ],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        actions = dv.get("action_proposals") or []
        self.assertTrue(actions)
        self.assertFalse(actions[0].get("policy_spine_ok"))
        label = actions[0].get("action_type_label_pl") or ""
        self.assertIn(PROJECTION_COPY_PL["v2_action_policy_blocked_suffix_pl"], label)

    def test_v2_row_missing_policy_decision_id_is_treated_as_blocked_in_projection(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "e"}},
            "decision_pipeline": {
                "outputs": {"decision_candidate": {"decision_candidate_id": "dc_m"}},
                "projection_ready": True,
            },
            "policy_decision": {"policy_decision_id": "pdec_m", "status": "allowed"},
            "action_proposals_v2": [
                {
                    "proposal_id": "apv2_m",
                    "action_type": "prepare_reply_draft",
                    "summary": "S",
                    "status": "proposed",
                    "allowed_by_policy": True,
                }
            ],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        actions = dv.get("action_proposals") or []
        self.assertTrue(actions)
        self.assertFalse(actions[0].get("policy_spine_ok"))
        self.assertIn(PROJECTION_COPY_PL["v2_action_policy_blocked_suffix_pl"], actions[0].get("action_type_label_pl") or "")

    def test_collapsed_ribbon_recommendation_from_understanding(self) -> None:
        ci = {
            "understanding_output": {
                "operator_explanation": {"essence_pl": "Klient prosi o termin"},
                "next_best_action_recommendation": {"title_pl": "Umów wizytę serwisową", "action_type": "request_missing_info"},
            },
            "decision_pipeline": {
                "outputs": {
                    "decision_candidate": {
                        "decision_candidate_id": "dc_r",
                        "topic": "service",
                        "priority": "high",
                        "sla_risk": "elevated",
                        "risk_class_candidate": "medium",
                    }
                },
                "projection_ready": True,
            },
            "policy_decision": {},
            "action_proposals_v2": [],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        ribbon = dv.get("collapsed_operator_pl") or {}
        self.assertIn("wizyt", ribbon.get("recommendation_one_liner_pl", ""))
        tags = ribbon.get("derivation_tags") or []
        self.assertIn("triage<=decision_candidate", tags)
        self.assertIn("recommendation<=understanding_output.next_best_action_recommendation", tags)
        self.assertEqual(
            ribbon.get("situation_vs_decision_hint_pl"),
            PROJECTION_COPY_PL["ribbon_situation_vs_decision_hint_pl"],
        )
        self.assertEqual(ribbon.get("expand_hint_pl"), PROJECTION_COPY_PL["expand_hint_pl"])

    def test_top_level_decision_candidate_projection_is_safe(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "Test essence"}},
            "decision_candidate": {
                "decision_candidate_id": "dc_top",
                "recommended_mode": "operator_review_only",
                "review_only_warnings": [{"summary": "client@example.invalid", "body": "private"}],
            },
        }
        dv = build_decision_view_blocks(case_intelligence=ci)

        self.assertEqual(dv.get("decision_candidate_id"), "dc_top")
        why = dv.get("why_pl") or ""
        self.assertIn("operator_review_only", why)
        self.assertIn(PROJECTION_COPY_PL["why_candidate_mode_prefix_pl"].rstrip(":"), why)
        self.assertNotIn("Tryb rekomendowany", why)
        rendered = repr(dv)
        self.assertNotIn("client@example.invalid", rendered)
        self.assertNotIn("body", rendered)

    def test_evidence_cards_from_understanding_strip_excerpt(self) -> None:
        ci = {
            "understanding_output": {
                "operator_explanation": {"essence_pl": "e"},
                "evidence_refs": [{"source_type": "gmail_message", "source_id": "mid", "excerpt": "LEAK_SECRET"}],
            },
            "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc1"}}},
            "policy_decision": {},
            "action_proposals_v2": [],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        ec = dv.get("evidence_cards") or []
        self.assertTrue(ec)
        self.assertNotIn("LEAK_SECRET", repr(ec))

    def test_mailbox_synthetic_policy_label_documents_missing_policy(self) -> None:
        vnext = {"case_summary": {}, "completeness_gaps": [], "conflicting_facts": []}
        pack = {"snapshot": {}, "runtime_state": {}}
        dv = build_decision_view_blocks(case_intelligence=None, mailbox_context={"vnext": vnext, "pack": pack})
        self.assertEqual(dv.get("policy_status_pl"), PROJECTION_COPY_PL["policy_mailbox_synthetic_status_pl"])

    def test_headline_fallback_matches_glossary_registry(self) -> None:
        vnext = {"case_summary": {}, "completeness_gaps": [], "conflicting_facts": []}
        pack = {"snapshot": {}}
        dv = build_decision_view_blocks(case_intelligence=None, mailbox_context={"vnext": vnext, "pack": pack})
        self.assertEqual(
            dv.get("headline_co_pl"),
            PROJECTION_COPY_PL["mailbox_fallback_essence_pl"][:160],
        )

    def test_wrong_case_button_labels_adjudication_not_calibration(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "x"}},
            "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc1"}}},
            "policy_decision": {},
            "action_proposals_v2": [],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        sec = dv.get("secondary_buttons") or []
        wrong = next((b for b in sec if b.get("id") == "wrong_case"), {})
        self.assertIn("adjudikacja", wrong.get("label_pl", "").lower())

    def test_no_v2_proposals_copy_matches_registry(self) -> None:
        ci = {
            "understanding_output": {"operator_explanation": {"essence_pl": "e"}},
            "decision_pipeline": {"outputs": {"decision_candidate": {"decision_candidate_id": "dc1"}}},
            "policy_decision": {},
            "action_proposals_v2": [],
        }
        dv = build_decision_view_blocks(case_intelligence=ci)
        self.assertEqual(dv.get("proposal_summary_pl"), PROJECTION_COPY_PL["no_v2_proposals_pl"])


if __name__ == "__main__":
    unittest.main()
