"""RED/GREEN regression tests for AUTH-02 (fail-open mutation auth) and
AUTH-03 (operator_id spoofing via request body) on:

  * POST /engagements/{engagement_id}/hitl/approve
  * POST /engagements/{engagement_id}/materialize/approve
  * POST /agent-chat

AUTH-02: before the fix, these three routes only rejected requests when a
mutation token *was* configured and the caller's credential failed
verification. When no token was configured at all, the check was skipped
entirely and the request was allowed through (fail-open). These tests assert
default-deny: no configured token must reject the request, not admit it.

AUTH-03: engagement_hitl_approve (and materialize_approve) persisted
`operator_id` verbatim from the untrusted request body into the decision,
os_event and audit-log sinks. These tests assert the persisted identity is
always the verified principal, never the client-supplied value.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app  # noqa: E402

_MUTATION_TOKEN_ENV_KEYS = (
    "DASZEK_NODE_B_API_TOKEN",
    "GMAIL_AGENT_INTERNAL_API_TOKEN",
    "NODE_B_REGISTRY_TOKEN",
)
_READ_ONLY_TOKEN_ENV_KEYS = (
    "NODE_B_READ_ONLY_TOKEN",
    "GMAIL_AGENT_READ_ONLY_API_TOKEN",
)


def _make_client() -> TestClient:
    app = create_app(
        runtime_provider=lambda: None,
        cohort_reader=lambda run_id: None,
        registry_provider=lambda: None,
    )
    return TestClient(app)


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _clear_all_tokens() -> None:
    for key in _MUTATION_TOKEN_ENV_KEYS + _READ_ONLY_TOKEN_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.pop("NODE_B_TASK_WRITE_DEV_BYPASS", None)
    os.environ.pop("GMAIL_AGENT_RUNTIME_PROFILE", None)
    # Prevent fail-open (pre-fix) negative-path tests from falling through into
    # real network/DB calls — this only affects test-process env, restored by
    # the autouse conftest fixture after each test.
    os.environ.pop("MAILBOX_MEMORY_DATABASE_URL", None)


def _mock_settings_no_db() -> MagicMock:
    settings = MagicMock()
    settings.mailbox_memory_database_url = ""
    return settings


# ── AUTH-02: default-deny for all three mutation routes ─────────────────────


class TestHitlApproveAuth02:
    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("agent_hitl_bridge.approve_hitl_engagement") as mock_approve:
            response = client.post(
                "/engagements/eng_1/hitl/approve",
                json={"action_id": "draft_reply", "operator_id": "attacker"},
            )
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_hitl_bridge.approve_hitl_engagement") as mock_approve:
                response = client.post(
                    "/engagements/eng_1/hitl/approve",
                    json={"action_id": "draft_reply"},
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_token_configured_bad_token_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_hitl_bridge.approve_hitl_engagement") as mock_approve:
                response = client.post(
                    "/engagements/eng_1/hitl/approve",
                    json={"action_id": "draft_reply"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_valid_token_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_hitl_bridge.approve_hitl_engagement", return_value={"ok": True, "engagement_id": "eng_1"}) as mock_approve:
                with patch("api_app._record_hitl_operator_action") as mock_record:
                    with patch("api_app.load_settings", return_value=_mock_settings_no_db()):
                        response = client.post(
                            "/engagements/eng_1/hitl/approve",
                            json={"action_id": "draft_reply"},
                            headers=_auth_headers("good-token"),
                        )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_approve.assert_called_once()
        mock_record.assert_called_once()


class TestMaterializeApproveAuth02:
    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("agent_runtime.materialize_bridge.approve_materialize_proposal") as mock_approve:
            response = client.post(
                "/engagements/eng_1/materialize/approve",
                json={"proposal_id": "prop_1", "operator_id": "attacker"},
            )
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_runtime.materialize_bridge.approve_materialize_proposal") as mock_approve:
                response = client.post(
                    "/engagements/eng_1/materialize/approve",
                    json={"proposal_id": "prop_1"},
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_token_configured_bad_token_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_runtime.materialize_bridge.approve_materialize_proposal") as mock_approve:
                response = client.post(
                    "/engagements/eng_1/materialize/approve",
                    json={"proposal_id": "prop_1"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_approve.assert_not_called()

    def test_valid_token_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("agent_runtime.agent_reconcile.build_operator_engagement_store", return_value=MagicMock()):
                with patch("mailbox_memory_runtime.build_mailbox_memory_runtime", return_value=None):
                    with patch(
                        "agent_runtime.materialize_bridge.approve_materialize_proposal",
                        return_value={"ok": True, "engagement_id": "eng_1", "case_id": "case_1"},
                    ) as mock_approve:
                        with patch("agent_hitl_bridge.best_effort_push_engagement_feed_after_hitl", return_value={"ok": False, "skipped": True}):
                            with patch("api_app._record_hitl_operator_action") as mock_record:
                                with patch("api_app.load_settings", return_value=_mock_settings_no_db()):
                                    response = client.post(
                                        "/engagements/eng_1/materialize/approve",
                                        json={"proposal_id": "prop_1"},
                                        headers=_auth_headers("good-token"),
                                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_approve.assert_called_once()
        mock_record.assert_called_once()


class TestAgentChatAuth02:
    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._run_agent_chat") as mock_run:
            response = client.post("/agent-chat", json={"user_input": "test"})
        assert response.status_code == 401
        mock_run.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._run_agent_chat") as mock_run:
                response = client.post("/agent-chat", json={"user_input": "test"})
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_run.assert_not_called()

    def test_token_configured_bad_token_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._run_agent_chat") as mock_run:
                response = client.post(
                    "/agent-chat",
                    json={"user_input": "test"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_run.assert_not_called()

    def test_valid_token_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch(
                "api_app._run_agent_chat",
                return_value={"turns": [], "proposals": [], "engagement_id": "", "signal_id": "", "warnings": []},
            ) as mock_run:
                with patch("api_app.load_settings", return_value=_mock_settings_no_db()):
                    response = client.post(
                        "/agent-chat",
                        json={"user_input": "test", "session_id": "auth02-proof"},
                        headers=_auth_headers("good-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["operator_scope"] == "operator"


# ── AUTH-03: operator_id spoofing via request body ───────────────────────────


class TestHitlApproveAuth03:
    def _call(self, body_operator_id, headers=None):
        client = _make_client()
        payload = {"action_id": "draft_reply"}
        if body_operator_id is not None:
            payload["operator_id"] = body_operator_id
        with patch("agent_hitl_bridge.approve_hitl_engagement", return_value={"ok": True, "engagement_id": "eng_1"}) as mock_approve:
            with patch("api_app._record_hitl_operator_action") as mock_record:
                with patch("api_app.load_settings", return_value=_mock_settings_no_db()):
                    response = client.post(
                        "/engagements/eng_1/hitl/approve",
                        json=payload,
                        headers=headers or _auth_headers("good-token"),
                    )
        return response, mock_approve, mock_record

    def test_foreign_operator_id_does_not_override_verified_principal(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            response, mock_approve, mock_record = self._call("operator_B_impersonation_attempt")
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        _, kwargs = mock_approve.call_args
        assert kwargs["operator_id"] == "operator"
        assert kwargs["operator_id"] != "operator_B_impersonation_attempt"
        _, record_kwargs = mock_record.call_args
        assert record_kwargs["payload"]["operator_id"] == "operator"

    def test_missing_operator_id_in_body_uses_verified_principal(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            response, mock_approve, _ = self._call(None)
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        _, kwargs = mock_approve.call_args
        assert kwargs["operator_id"] == "operator"

    def test_empty_operator_id_in_body_uses_verified_principal(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            response, mock_approve, _ = self._call("")
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        _, kwargs = mock_approve.call_args
        assert kwargs["operator_id"] == "operator"

    def test_technically_valid_but_foreign_operator_id_is_still_ignored(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            response, mock_approve, _ = self._call("konrad@top-instal.pl")
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        _, kwargs = mock_approve.call_args
        assert kwargs["operator_id"] == "operator"

    def test_repeated_call_with_same_credential_keeps_identity_stable(self) -> None:
        """Idempotency-adjacent: re-approving with the same verified credential
        must not let a differing body operator_id change the persisted identity
        between calls."""
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            first, mock_approve_1, _ = self._call("attacker_first_try")
            second, mock_approve_2, _ = self._call("different_attacker_second_try")
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert first.status_code == 200
        assert second.status_code == 200
        assert mock_approve_1.call_args.kwargs["operator_id"] == "operator"
        assert mock_approve_2.call_args.kwargs["operator_id"] == "operator"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
