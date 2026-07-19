"""RED/GREEN regression tests for AUTH-STREAM-RESIDUAL on:

  * POST /agent-chat/stream
  * POST /agent-chat/feedback

Same bug class as AUTH-02 (see test_auth02_auth03_mutation_gate.py):

- /agent-chat/stream used Depends(get_current_operator), whose explicit
  dev-mode fallback ("brak skonfigurowanego tokena = tryb deweloperski,
  przepuszczamy") granted full access when no mutation token was configured.
  FastAPI resolves Depends(...) before the endpoint body runs, so this was
  never a "auth after streaming starts" ordering bug — the defect was purely
  the fail-open behavior of the dependency itself.
- /agent-chat/feedback had no auth dependency at all — any caller could
  persist feedback/preference/proposal records regardless of credential.

Neither route accepts an operator_id/user_id/actor_id field from the client,
so AUTH-03-style identity spoofing does not apply to either route (confirmed
by direct inspection of both handlers) — no spoofing tests are added here.
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
    for key in _MUTATION_TOKEN_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.pop("NODE_B_READ_ONLY_TOKEN", None)
    os.environ.pop("GMAIL_AGENT_READ_ONLY_API_TOKEN", None)
    os.environ.pop("NODE_B_TASK_WRITE_DEV_BYPASS", None)
    os.environ.pop("GMAIL_AGENT_RUNTIME_PROFILE", None)
    os.environ.pop("MAILBOX_MEMORY_DATABASE_URL", None)


def _mock_settings_no_db() -> MagicMock:
    settings = MagicMock()
    settings.mailbox_memory_database_url = ""
    return settings


# ── /agent-chat/stream ───────────────────────────────────────────────────


class TestAgentChatStreamAuth:
    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._run_agent_chat") as mock_run:
            response = client.post("/agent-chat/stream", json={"user_input": "test"})
        assert response.status_code == 401
        assert response.headers.get("content-type", "").startswith("application/json")
        mock_run.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._run_agent_chat") as mock_run:
                response = client.post("/agent-chat/stream", json={"user_input": "test"})
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        assert response.headers.get("content-type", "").startswith("application/json")
        mock_run.assert_not_called()

    def test_token_configured_bad_token_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._run_agent_chat") as mock_run:
                response = client.post(
                    "/agent-chat/stream",
                    json={"user_input": "test"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        assert response.headers.get("content-type", "").startswith("application/json")
        mock_run.assert_not_called()

    def test_negative_paths_never_return_event_stream(self) -> None:
        """No 200, no text/event-stream content-type, no SSE bytes for any negative path."""
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._run_agent_chat") as mock_run:
            no_cred = client.post("/agent-chat/stream", json={"user_input": "test"})
            bad_cred = client.post(
                "/agent-chat/stream", json={"user_input": "test"}, headers=_auth_headers("whatever")
            )
        for response in (no_cred, bad_cred):
            assert response.status_code != 200
            assert "text/event-stream" not in response.headers.get("content-type", "")
            assert b"event:" not in response.content
        mock_run.assert_not_called()

    def test_valid_token_reaches_handler_and_opens_stream(self) -> None:
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
                        "/agent-chat/stream",
                        json={"user_input": "test", "session_id": "auth-stream-proof"},
                        headers=_auth_headers("good-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith("text/event-stream")
        assert b"event: done" in response.content
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        assert kwargs["operator_scope"] == "operator"


# ── /agent-chat/feedback ─────────────────────────────────────────────────


class TestAgentChatFeedbackAuth:
    _VALID_PAYLOAD = {"session_id": "s1", "turn_id": "t1", "rating": "thumbs_up"}

    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("psycopg.connect") as mock_connect:
            response = client.post("/agent-chat/feedback", json=self._VALID_PAYLOAD)
        assert response.status_code == 401
        mock_connect.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("psycopg.connect") as mock_connect:
                response = client.post("/agent-chat/feedback", json=self._VALID_PAYLOAD)
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_connect.assert_not_called()

    def test_token_configured_bad_token_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("psycopg.connect") as mock_connect:
                response = client.post(
                    "/agent-chat/feedback",
                    json=self._VALID_PAYLOAD,
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_connect.assert_not_called()

    def test_valid_token_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app.load_settings", return_value=_mock_settings_no_db()):
                response = client.post(
                    "/agent-chat/feedback",
                    json=self._VALID_PAYLOAD,
                    headers=_auth_headers("good-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_no_identity_field_accepted_in_payload(self) -> None:
        """AUTH-03 does not apply here: the feedback contract has no
        operator_id/user_id/actor_id field to spoof. This test documents that
        contract rather than adding one."""
        import inspect

        import api_app

        source = inspect.getsource(api_app)
        feedback_start = source.index("def agent_chat_feedback")
        feedback_body = source[feedback_start : feedback_start + 1200]
        for forbidden in ("operator_id", "user_id", "actor_id"):
            assert forbidden not in feedback_body


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
