from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from api_app import create_app
from correlation_registry.service import CorrelationRegistryService
from correlation_registry.store import InMemoryCorrelationRegistryStore
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        if case_id == "case_corr_1":
            return CaseContextPack(case_id=case_id, active_facts=[])
        return CaseContextPack(case_id="")


def test_engagement_routes_with_in_memory_registry() -> None:
    registry = CorrelationRegistryService(InMemoryCorrelationRegistryStore())
    registry.bootstrap()
    registry.sync_mailbox_case(
        case_id="case_corr_1",
        customer_email="corr@test.pl",
    )
    lookup = registry.lookup_by_case_id("case_corr_1")
    assert lookup is not None
    engagement_id = str(lookup["engagement_id"])

    app = create_app(
        runtime_provider=lambda: _Runtime(),
        registry_provider=lambda: registry,
    )
    client = TestClient(app)

    case_eng = client.get("/cases/case_corr_1/engagement")
    assert case_eng.status_code == 200
    assert case_eng.json()["engagement_id"] == engagement_id

    snapshot = client.get(f"/engagements/{engagement_id}/snapshot")
    assert snapshot.status_code == 200
    body = snapshot.json()
    assert body["contract_name"] == "EngagementSnapshot"
    assert body["cieplo_workflow_id"] == ""
    assert body["labels_pl"]["cieplo_workflow"] == "Zlecenie Cieplo"

    token = "test-registry-token"
    import os

    os.environ["NODE_B_REGISTRY_TOKEN"] = token
    try:
        denied = client.post("/internal/registry/links", json={"identity_email": "x@y.z", "links": []})
        assert denied.status_code == 401

        ok = client.post(
            "/internal/registry/links",
            json={
                "identity_email": "new@test.pl",
                "links": [{"link_type": "cieplo_workflow", "target_id": "wf-2", "source_repo": "topinstal-cieplo-orchestrator"}],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ok.status_code == 200
        assert ok.json()["engagement_id"]
    finally:
        os.environ.pop("NODE_B_REGISTRY_TOKEN", None)
