from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app


_TOKEN_KEYS = (
    "NODE_B_REGISTRY_TOKEN",
    "DASZEK_NODE_B_API_TOKEN",
    "GMAIL_AGENT_INTERNAL_API_TOKEN",
    "GMAIL_AGENT_ENV_FILE",
)


def _clear_tokens(monkeypatch) -> None:
    for key in _TOKEN_KEYS:
        monkeypatch.delenv(key, raising=False)


def _client() -> TestClient:
    runtime = SimpleNamespace(store=SimpleNamespace(fetch_case=lambda case_id: {"case_id": case_id}))
    registry = SimpleNamespace(
        store=object(),
        lookup_by_case_id=lambda case_id: {"case_id": case_id, "engagement_id": "eng_edge", "links": []},
    )
    return TestClient(create_app(runtime_provider=lambda: runtime, registry_provider=lambda: registry))


def _public_headers(token: str = "edge-token", *, service: bool = False, authorization: bool = True) -> dict[str, str]:
    headers = {"X-Node-B-Public-Edge": "1"}
    if authorization:
        headers["Authorization"] = f"Bearer {token}"
    if service:
        headers["X-Node-B-Service-Authorization"] = f"Bearer {token}"
    return headers


def test_public_latest_offer_requires_registry_bearer(monkeypatch) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "edge-token")
    client = _client()

    missing = client.get("/cases/case_edge/offers/latest", headers={"X-Node-B-Public-Edge": "1"})
    assert missing.status_code == 401

    wrong = client.get(
        "/cases/case_edge/offers/latest",
        headers=_public_headers("wrong-token"),
    )
    assert wrong.status_code == 401

    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        with patch(
            "api_app.fetch_latest_offer_for_case",
            return_value={"offer_id": "cieplo:wf-edge", "source": "cieplo", "final_price_pln": 36856},
        ):
            with patch("api_app.fetch_offer_conflicts_for_case", return_value=[]):
                valid = client.get("/cases/case_edge/offers/latest", headers=_public_headers())

    assert valid.status_code == 200
    assert valid.json()["offer"]["offer_id"] == "cieplo:wf-edge"


def test_public_case_engagement_requires_registry_bearer(monkeypatch) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "edge-token")
    client = _client()

    assert client.get("/cases/case_edge/engagement", headers={"X-Node-B-Public-Edge": "1"}).status_code == 401

    valid = client.get("/cases/case_edge/engagement", headers=_public_headers())
    assert valid.status_code == 200
    assert valid.json()["engagement_id"] == "eng_edge"


def test_public_edge_denies_docs_and_internal_routes_even_with_valid_bearer(monkeypatch) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "edge-token")
    client = _client()

    for path in ("/docs", "/redoc", "/openapi.json", "/internal/os-events", "/system/trace"):
        response = client.get(path, headers=_public_headers())
        assert response.status_code == 404


def test_public_attachment_download_is_default_deny(monkeypatch) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "edge-token")
    client = _client()

    missing = client.get("/cases/case_edge/attachments/att-edge", headers={"X-Node-B-Public-Edge": "1"})
    assert missing.status_code == 401

    with patch("api_app.resolve_attachment_bytes", return_value=(b"pdf", "application/pdf", "offer.pdf")):
        valid = client.get("/cases/case_edge/attachments/att-edge", headers=_public_headers())

    assert valid.status_code == 200
    assert valid.content == b"pdf"


def test_public_conflict_resolution_requires_service_bearer_and_mutation_principal(monkeypatch) -> None:
    _clear_tokens(monkeypatch)
    monkeypatch.setenv("NODE_B_REGISTRY_TOKEN", "edge-token")
    client = _client()
    path = "/cases/case_edge/offers/cieplo:wf-edge/conflicts/resolve"
    body = {"conflict_id": "conflict-edge", "expected_revision": "rev-1", "candidate_id": "candidate-a"}

    no_service = client.post(path, headers=_public_headers(service=False, authorization=True), json=body)
    assert no_service.status_code == 401

    no_principal = client.post(path, headers=_public_headers(service=True, authorization=False), json=body)
    assert no_principal.status_code == 401

    with patch("api_app.load_settings", return_value=SimpleNamespace(mailbox_memory_database_url="postgresql://test")):
        with patch("api_app.record_operator_offer_resolution", return_value={"ok": True, "resolution_status": "OPERATOR_RESOLVED"}):
            with patch("api_app.fetch_latest_offer_for_case", return_value={"offer_id": "cieplo:wf-edge"}):
                with patch("api_app.fetch_offer_conflicts_for_case", return_value=[]):
                    valid = client.post(path, headers=_public_headers(service=True, authorization=True), json=body)

    assert valid.status_code == 200
    assert valid.json()["resolution_status"] == "OPERATOR_RESOLVED"
