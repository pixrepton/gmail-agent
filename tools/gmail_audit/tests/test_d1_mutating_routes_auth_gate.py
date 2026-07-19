"""D1 RED/GREEN regression tests: default-deny auth for the 4 remaining
unauthenticated Node B mutating routes identified by the 2026-07-15
intelligence-ceiling audit (L-10):

  * POST /learning/rule-candidates/{candidate_id}/status
  * POST /cases/{case_id}/operator-action
  * POST /identity/binding-suggestions/scan
  * POST /identity/binding-suggestions/{suggestion_id}/status

Before the fix, none of these routes declared any auth dependency: any
caller, with or without a token, reached the business handler and its
storage side effects. These tests assert the same default-deny contract
already proven for /engagements/*/hitl/approve, /materialize/approve and
/agent-chat (see test_auth02_auth03_mutation_gate.py): no configured
credential must reject the request (not admit it), and a client-supplied
identity field in the body must never override the verified principal.
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
from correlation_registry.identity_binding import upsert_binding_suggestions  # noqa: E402
from correlation_registry.service import CorrelationRegistryService  # noqa: E402
from correlation_registry.store import InMemoryCorrelationRegistryStore  # noqa: E402

_MUTATION_TOKEN_ENV_KEYS = (
    "DASZEK_NODE_B_API_TOKEN",
    "GMAIL_AGENT_INTERNAL_API_TOKEN",
    "NODE_B_REGISTRY_TOKEN",
)
_READ_ONLY_TOKEN_ENV_KEYS = (
    "NODE_B_READ_ONLY_TOKEN",
    "GMAIL_AGENT_READ_ONLY_API_TOKEN",
)


def _make_client(**kwargs) -> TestClient:
    app = create_app(
        runtime_provider=kwargs.pop("runtime_provider", lambda: None),
        cohort_reader=kwargs.pop("cohort_reader", lambda run_id: None),
        registry_provider=kwargs.pop("registry_provider", lambda: None),
        **kwargs,
    )
    return TestClient(app)


def _client_with_registry() -> tuple[TestClient, CorrelationRegistryService]:
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    app = create_app(registry_provider=lambda: registry)
    return TestClient(app), registry


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _clear_all_tokens() -> None:
    for key in _MUTATION_TOKEN_ENV_KEYS + _READ_ONLY_TOKEN_ENV_KEYS:
        os.environ.pop(key, None)
    os.environ.pop("NODE_B_TASK_WRITE_DEV_BYPASS", None)
    os.environ.pop("GMAIL_AGENT_RUNTIME_PROFILE", None)
    os.environ.pop("MAILBOX_MEMORY_DATABASE_URL", None)


def _mock_conn() -> MagicMock:
    """MagicMock usable as `with conn:` context manager and conn.commit()/close()."""
    return MagicMock()


# ── Route 1: POST /learning/rule-candidates/{candidate_id}/status ──────────


class TestRuleCandidateStatusAuth:
    ROUTE = "/learning/rule-candidates/cand_1/status"

    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._learning_db_conn") as mock_conn:
            response = client.post(self.ROUTE, json={"status": "approved", "approved_by": "attacker"})
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_no_token_configured_bad_header_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._learning_db_conn") as mock_conn:
            response = client.post(
                self.ROUTE,
                json={"status": "approved"},
                headers=_auth_headers("whatever"),
            )
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(self.ROUTE, json={"status": "approved"})
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_empty_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(
                    self.ROUTE,
                    json={"status": "approved"},
                    headers=_auth_headers(""),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_bad_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(
                    self.ROUTE,
                    json={"status": "approved"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_valid_bearer_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn", return_value=_mock_conn()):
                with patch("divergence_loop.update_candidate_status", return_value=True) as mock_update:
                    response = client.post(
                        self.ROUTE,
                        json={"status": "approved"},
                        headers=_auth_headers("good-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_update.assert_called_once()

    def test_spoofed_approved_by_does_not_override_verified_principal(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn", return_value=_mock_conn()):
                with patch("divergence_loop.update_candidate_status", return_value=True) as mock_update:
                    response = client.post(
                        self.ROUTE,
                        json={"status": "approved", "approved_by": "attacker_impersonation"},
                        headers=_auth_headers("good-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        _, kwargs = mock_update.call_args
        assert kwargs["approved_by"] == "operator"
        assert kwargs["approved_by"] != "attacker_impersonation"


# ── Route 2: POST /cases/{case_id}/operator-action ──────────────────────────


class TestOperatorActionAuth:
    ROUTE = "/cases/case_1/operator-action"

    def test_no_token_configured_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._learning_db_conn") as mock_conn:
            response = client.post(self.ROUTE, json={"action_type": "case_status_change"})
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_no_token_configured_bad_header_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("api_app._learning_db_conn") as mock_conn:
            response = client.post(
                self.ROUTE,
                json={"action_type": "case_status_change"},
                headers=_auth_headers("whatever"),
            )
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(self.ROUTE, json={"action_type": "case_status_change"})
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_empty_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(
                    self.ROUTE,
                    json={"action_type": "case_status_change"},
                    headers=_auth_headers(""),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_token_configured_bad_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn") as mock_conn:
                response = client.post(
                    self.ROUTE,
                    json={"action_type": "case_status_change"},
                    headers=_auth_headers("bad-token"),
                )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_conn.assert_not_called()

    def test_valid_bearer_reaches_handler(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("api_app._learning_db_conn", return_value=_mock_conn()):
                with patch(
                    "operator_learning_hooks.hook_process_operator_action",
                    return_value=[{"response_type": "EXACT_MATCH"}],
                ) as mock_hook:
                    response = client.post(
                        self.ROUTE,
                        json={"action_type": "case_status_change", "case_family": "hvac"},
                        headers=_auth_headers("good-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        mock_hook.assert_called_once()


# ── Route 3: POST /identity/binding-suggestions/scan ────────────────────────


class TestBindingSuggestionsScanAuth:
    ROUTE = "/identity/binding-suggestions/scan"

    def test_no_token_configured_is_default_deny_no_new_suggestions(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("correlation_registry.identity_binding.detect_identity_binding_suggestions") as mock_detect:
            response = client.post(self.ROUTE)
        assert response.status_code == 401
        mock_detect.assert_not_called()

    def test_no_token_configured_bad_header_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("correlation_registry.identity_binding.detect_identity_binding_suggestions") as mock_detect:
            response = client.post(self.ROUTE, headers=_auth_headers("whatever"))
        assert response.status_code == 401
        mock_detect.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.detect_identity_binding_suggestions") as mock_detect:
                response = client.post(self.ROUTE)
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_detect.assert_not_called()

    def test_token_configured_empty_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.detect_identity_binding_suggestions") as mock_detect:
                response = client.post(self.ROUTE, headers=_auth_headers(""))
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_detect.assert_not_called()

    def test_token_configured_bad_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.detect_identity_binding_suggestions") as mock_detect:
                response = client.post(self.ROUTE, headers=_auth_headers("bad-token"))
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_detect.assert_not_called()

    def test_valid_bearer_reaches_handler_and_detects_suggestions(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            client, registry = _client_with_registry()
            store = registry.store
            store.create_identity(email="one@firma.pl", metadata={"nip": "1234567890"})
            store.create_identity(email="two@firma.pl", metadata={"nip": "1234567890"})
            response = client.post(
                self.ROUTE,
                params={"limit": 10},
                headers=_auth_headers("good-token"),
            )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["detected"] >= 1


# ── Route 4: POST /identity/binding-suggestions/{suggestion_id}/status ─────


class TestBindingSuggestionStatusAuth:
    ROUTE = "/identity/binding-suggestions/sugg_1/status"

    def test_no_token_configured_is_default_deny_no_status_change(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("correlation_registry.identity_binding.update_binding_suggestion_status") as mock_update:
            with patch("correlation_registry.identity_binding.execute_identity_merge") as mock_merge:
                response = client.post(self.ROUTE, json={"status": "approved", "reviewed_by": "attacker"})
        assert response.status_code == 401
        mock_update.assert_not_called()
        mock_merge.assert_not_called()

    def test_no_token_configured_bad_header_is_default_deny(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        with patch("correlation_registry.identity_binding.update_binding_suggestion_status") as mock_update:
            with patch("correlation_registry.identity_binding.execute_identity_merge") as mock_merge:
                response = client.post(
                    self.ROUTE,
                    json={"status": "approved"},
                    headers=_auth_headers("whatever"),
                )
        assert response.status_code == 401
        mock_update.assert_not_called()
        mock_merge.assert_not_called()

    def test_token_configured_missing_header_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.update_binding_suggestion_status") as mock_update:
                with patch("correlation_registry.identity_binding.execute_identity_merge") as mock_merge:
                    response = client.post(self.ROUTE, json={"status": "approved"})
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_update.assert_not_called()
        mock_merge.assert_not_called()

    def test_token_configured_empty_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.update_binding_suggestion_status") as mock_update:
                with patch("correlation_registry.identity_binding.execute_identity_merge") as mock_merge:
                    response = client.post(
                        self.ROUTE,
                        json={"status": "approved"},
                        headers=_auth_headers(""),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_update.assert_not_called()
        mock_merge.assert_not_called()

    def test_token_configured_bad_bearer_rejected(self) -> None:
        client = _make_client()
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            with patch("correlation_registry.identity_binding.update_binding_suggestion_status") as mock_update:
                with patch("correlation_registry.identity_binding.execute_identity_merge") as mock_merge:
                    response = client.post(
                        self.ROUTE,
                        json={"status": "approved"},
                        headers=_auth_headers("bad-token"),
                    )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 401
        mock_update.assert_not_called()
        mock_merge.assert_not_called()

    def _seed_pending_suggestion(self, registry: CorrelationRegistryService) -> tuple[str, str, str]:
        store = registry.store
        src = store.create_identity(email="alpha@example.com", metadata={"nip": "5252445767"})
        tgt = store.create_identity(email="beta@example.com", metadata={"nip": "5252445767"})
        upsert_binding_suggestions(
            store,
            [
                {
                    "source_identity_id": src,
                    "target_identity_id": tgt,
                    "signal_type": "nip_match",
                    "confidence": 0.8,
                    "evidence_json": {"nip": "5252445767"},
                }
            ],
        )
        suggestion_id = next(iter(store.binding_suggestions.keys()))
        return suggestion_id, src, tgt

    def test_valid_bearer_approve_merges_and_ignores_spoofed_reviewed_by(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            client, registry = _client_with_registry()
            suggestion_id, src, tgt = self._seed_pending_suggestion(registry)
            response = client.post(
                f"/identity/binding-suggestions/{suggestion_id}/status",
                json={"status": "approved", "reviewed_by": "attacker_impersonation"},
                headers=_auth_headers("good-token"),
            )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["status"] == "approved"
        assert body["merge"]["merged"] is True
        merge_log = registry.store.merge_logs[-1]
        assert merge_log["operator_id"] == "operator"
        assert merge_log["operator_id"] != "attacker_impersonation"
        # functional regression: identity actually merged
        assert src not in registry.store.identities
        assert tgt in registry.store.identities

    def test_retry_with_same_valid_credential_does_not_repoint_engagements_twice(self) -> None:
        _clear_all_tokens()
        os.environ["DASZEK_NODE_B_API_TOKEN"] = "good-token"
        try:
            client, registry = _client_with_registry()
            suggestion_id, src, tgt = self._seed_pending_suggestion(registry)
            first = client.post(
                f"/identity/binding-suggestions/{suggestion_id}/status",
                json={"status": "approved"},
                headers=_auth_headers("good-token"),
            )
            second = client.post(
                f"/identity/binding-suggestions/{suggestion_id}/status",
                json={"status": "approved"},
                headers=_auth_headers("good-token"),
            )
        finally:
            os.environ.pop("DASZEK_NODE_B_API_TOKEN", None)
        assert first.status_code == 200
        assert first.json()["merge"]["engagements_repointed"] == 0
        # The source identity is deleted as part of the first merge, which cascades away the
        # identity_binding_suggestions row itself (ON DELETE CASCADE on source_identity_id).
        # A retry can therefore never reach a second merge attempt at all: the status-update
        # step fails to find the suggestion and returns 404 before execute_identity_merge is
        # even called -- a stronger retry guarantee than a silent no-op second merge would be.
        assert second.status_code == 404
        assert len(registry.store.merge_logs) == 1
        assert registry.store.merge_logs[0]["operator_id"] == "operator"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
