"""Tests for divergence_loop.py — auto-approve, cross-family, pattern learning."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from divergence_loop import (
    CANDIDATE_APPROVED,
    CANDIDATE_PENDING,
    CANDIDATE_PATTERN,
    CONFIDENCE_AUTO_APPROVE_THRESHOLD,
    RESPONSE_DIVERGENT_ACTION,
    RESPONSE_EDITED_MATCH,
    RESPONSE_EXACT_MATCH,
    RESPONSE_IGNORED,
    _auto_approve_candidate,
    _find_similar_candidates,
    adaptive_threshold,
    classify_operator_response,
    fetch_decision_queue,
    maybe_create_learning_candidate,
    record_agent_proposal,
    record_operator_response,
    update_candidate_status,
    update_rule_application,
)


def _mock_conn() -> MagicMock:
    """Create a mock DB connection following the project pattern."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []
    cur.fetchone.return_value = None
    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cur
    cursor_cm.__exit__.return_value = None
    conn.cursor.return_value = cursor_cm
    return conn


# ============================================================================
# fetch_decision_queue — must request dict rows, or _row_to_proposal silently
# drops every field except proposal_id (found via X1 v0 runtime proof: the
# already-live GET /system/decision-queue calls store._connect() without
# row_factory=True, so plain-tuple rows fall into _row_to_proposal's
# `{"proposal_id": str(row[0])}` fallback and case_id/proposal_type/
# summary_pl/source_pipeline are silently blank).
# ============================================================================

class TestFetchDecisionQueueRowFactory:
    def test_requests_dict_row_cursor_explicitly(self):
        from psycopg.rows import dict_row

        conn = _mock_conn()
        fetch_decision_queue(conn, limit=5)
        conn.cursor.assert_called_once_with(row_factory=dict_row)

    def test_preserves_case_id_and_proposal_type_from_dict_row(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            {
                "proposal_id": "p1",
                "engagement_id": "eng1",
                "case_id": "case1",
                "created_at": "2026-07-15T10:00:00+00:00",
                "proposal_type": "draft_reply",
                "proposal_content_json": {},
                "proposal_reasoning_pl": "Testowa propozycja.",
                "source_pipeline": "gmail",
            }
        ]
        queue = fetch_decision_queue(conn, limit=5)
        assert queue[0]["case_id"] == "case1"
        assert queue[0]["proposal_type"] == "draft_reply"
        assert queue[0]["summary_pl"] == "Testowa propozycja."


# ============================================================================
# Faza 1: Auto-apply dla confidence > 0.9
# ============================================================================


class TestAutoApproveHighConfidence:
    """maybe_create_learning_candidate powinien auto-approve przy confidence >= 0.9."""

    def test_auto_approve_high_confidence(self):
        """confidence=0.95, supporting_count >= threshold → approved."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        cur.fetchone.side_effect = [
            None,  # No existing pending candidate
            (2,),  # total=2 observations → total+1=3 >= threshold=3
        ]

        cid = maybe_create_learning_candidate(
            conn,
            case_family="test_family",
            proposal_type="prepare_reply_draft",
            response_type=RESPONSE_DIVERGENT_ACTION,
            parent_observation_count=5,
            confidence=0.95,
        )

        assert cid is not None
        approved_updates = [
            call for call in cur.execute.call_args_list
            if "UPDATE" in str(call[0][0]) and "approved" in str(call[0][0])
        ]
        assert len(approved_updates) > 0, "Expected auto-approve UPDATE to be called"

    def test_auto_approve_below_threshold(self):
        """confidence=0.8 → pozostaje pending (brak auto-approve)."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        cur.fetchone.side_effect = [
            None,  # No existing pending candidate
            (5,),  # total=5 → total+1=6 >= threshold=3
        ]

        cid = maybe_create_learning_candidate(
            conn,
            case_family="test_family",
            proposal_type="prepare_reply_draft",
            response_type=RESPONSE_DIVERGENT_ACTION,
            parent_observation_count=None,  # nie uruchamia parent_obs auto-approve
            confidence=0.8,  # 0.8 < CONFIDENCE_AUTO_APPROVE_THRESHOLD=0.9
        )

        assert cid is not None
        approved_updates = [
            call for call in cur.execute.call_args_list
            if "UPDATE" in str(call[0][0]) and "approved" in str(call[0][0])
        ]
        assert len(approved_updates) == 0, (
            "Expected no auto-approve UPDATE for low confidence"
        )

    def test_auto_approve_strong_pattern_via_parent_obs(self):
        """parent_observation_count > threshold * 2 → auto-approve nawet bez confidence."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        cur.fetchone.side_effect = [
            None,  # No existing pending candidate
            (2,),  # total=2 → total+1=3 >= threshold=3
        ]

        cid = maybe_create_learning_candidate(
            conn,
            case_family="test_family",
            proposal_type="prepare_reply_draft",
            response_type=RESPONSE_DIVERGENT_ACTION,
            parent_observation_count=10,  # threshold=3, 10 > 6 → auto-approve
        )

        assert cid is not None
        approved_updates = [
            call for call in cur.execute.call_args_list
            if "UPDATE" in str(call[0][0]) and "approved" in str(call[0][0])
        ]
        assert len(approved_updates) > 0, (
            "Expected auto-approve for strong pattern (parent_obs > threshold*2)"
        )

    def test_constant_defined(self):
        """Sprawdza czy stała CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.9."""
        assert CONFIDENCE_AUTO_APPROVE_THRESHOLD == 0.9

    def test_auto_approve_candidate_function(self):
        """_auto_approve_candidate ustawia status='approved' i approved_by='auto_approve'."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.execute.return_value = MagicMock()
        cur.rowcount = 1

        result = _auto_approve_candidate(conn, "cand_test_123")

        assert result is True
        # Verify the UPDATE was called with approved status
        approved_update_called = any(
            "approved" in str(call[0][0]) and "UPDATE" in str(call[0][0])
            for call in cur.execute.call_args_list
        )
        assert approved_update_called, "Expected UPDATE with approved in SQL"


# ============================================================================
# Faza 2: Cross-family pattern inheritance (już istnieje w kodzie)
# ============================================================================


class TestCrossFamilyInheritance:
    """Testy dla istniejącego mechanizmu cross-family."""

    def test_cross_family_inheritance(self):
        """_find_similar_candidates zwraca approved kandydaty z podobnych rodzin."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = [
            ("cand_1", "lead::prepare_reply_draft::DIVERGENT_ACTION", 5, "approved"),
            ("cand_2", "lead::request_missing_info::EDITED_MATCH", 7, "approved"),
        ]

        results = _find_similar_candidates(cur, "lead_opportunity", "prepare_reply_draft")

        assert len(results) == 2
        assert results[0]["candidate_id"] == "cand_1"
        assert results[0]["status"] == "approved"
        assert results[1]["candidate_id"] == "cand_2"

    def test_cross_family_no_similar(self):
        """Gdy rodzina nie ma podobnych, zwraca []."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value = []

        results = _find_similar_candidates(cur, "unknown_family", "prepare_reply_draft")

        assert results == []

    def test_cross_family_threshold_lowered(self):
        """Cross-family boost obniża threshold gdy istnieją approved w podobnych."""
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [
            None,  # No existing pending candidate
            (1,),  # total=1 → total+1=2 < threshold=3, triggers cross-family check
        ]
        cur.fetchall.return_value = [
            ("cand_x", "quote::prepare_reply_draft::DIVERGENT_ACTION", 5, "approved"),
        ]

        cid = maybe_create_learning_candidate(
            conn,
            case_family="lead_opportunity",
            proposal_type="prepare_reply_draft",
            response_type=RESPONSE_DIVERGENT_ACTION,
            parent_observation_count=2,
        )

        # With cross-family boost: threshold=3 * 0.7 = 2.1 → int=2, total+1=2 >= 2 → OK
        assert cid is not None, (
            "Cross-family boost should have lowered threshold enough to create candidate"
        )


# ============================================================================
# Pozostałe funkcje divergence_loop
# ============================================================================


class TestClassifyOperatorResponse:
    """Testy dla classify_operator_response."""

    def test_exact_match(self):
        proposal = {"proposal_type": "prepare_reply_draft", "proposal_content_json": {}}
        rtype, conf, reason = classify_operator_response(
            proposal=proposal, operator_action_type="prepare_reply_draft"
        )
        assert rtype == RESPONSE_EXACT_MATCH
        assert conf == 0.95

    def test_edited_match(self):
        proposal = {"proposal_type": "prepare_reply_draft", "proposal_content_json": {}}
        rtype, conf, reason = classify_operator_response(
            proposal=proposal, operator_action_type="edit"
        )
        assert rtype == RESPONSE_EDITED_MATCH
        assert conf == 0.85

    def test_divergent_action(self):
        proposal = {"proposal_type": "prepare_reply_draft", "proposal_content_json": {}}
        rtype, conf, reason = classify_operator_response(
            proposal=proposal, operator_action_type="delete_case"
        )
        assert rtype == RESPONSE_DIVERGENT_ACTION
        assert conf == 0.8

    def test_ignored(self):
        proposal = {"proposal_type": "prepare_reply_draft", "proposal_content_json": {}}
        rtype, conf, reason = classify_operator_response(
            proposal=proposal, operator_action_type="skip"
        )
        assert rtype == RESPONSE_IGNORED


class TestAdaptiveThreshold:
    def test_low_observations_returns_min(self):
        assert adaptive_threshold("test", 0) == 2

    def test_high_observations_returns_capped(self):
        val = adaptive_threshold("test", 10000)
        assert val <= 10
        assert val >= 2

    def test_none_returns_min(self):
        assert adaptive_threshold("test") == 2


class TestUpdateCandidateStatus:
    def test_approve(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        assert update_candidate_status(conn, candidate_id="c1", status=CANDIDATE_APPROVED)

    def test_reject(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        assert update_candidate_status(conn, candidate_id="c1", status="rejected")


class TestRecordFunctions:
    def test_record_agent_proposal(self):
        conn = _mock_conn()
        pid = record_agent_proposal(
            conn,
            engagement_id="eng1",
            case_id="case1",
            proposal_type="prepare_reply_draft",
            proposal_content={"action": "reply"},
        )
        assert pid is not None
        assert pid.startswith("prop_")

    def test_record_operator_response(self):
        conn = _mock_conn()
        rid = record_operator_response(
            conn,
            proposal_id="prop_123",
            response_type=RESPONSE_EXACT_MATCH,
            detection_confidence=0.95,
        )
        assert rid is not None
        assert rid.startswith("resp_")

    def test_update_rule_application(self):
        conn = _mock_conn()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.rowcount = 1
        assert update_rule_application(conn, candidate_id="cand_1") is True


class TestConstants:
    def test_candidate_pattern_constant(self):
        assert CANDIDATE_PATTERN == "pattern_candidate"

    def test_response_types(self):
        assert RESPONSE_EXACT_MATCH == "EXACT_MATCH"
        assert RESPONSE_EDITED_MATCH == "EDITED_MATCH"
        assert RESPONSE_DIVERGENT_ACTION == "DIVERGENT_ACTION"
