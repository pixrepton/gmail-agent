from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from unittest.mock import patch

from api_app import create_app
from mailbox_memory_models import CaseContextPack


class _Runtime:
    def __init__(self) -> None:
        self.pack = CaseContextPack(
            case_id="case_api_1",
            active_facts=[{"fact_id": "fact-1", "fact_key": "status", "value": "open", "source_ref": "gmail:msg-1"}],
            conflicting_facts=[{"fact_key": "device_power", "values": ["8 kW", "10 kW"]}],
            completeness_gaps=["Missing signed protocol"],
            source_refs=[{"type": "gmail_message", "message_id": "msg-1", "source_ref": "gmail:msg-1"}],
        )

    def get_context_pack(self, *, case_id: str = "", message_id: str = "", query_text: str = "") -> CaseContextPack:
        if case_id == self.pack.case_id:
            return self.pack
        return CaseContextPack(case_id="")


def test_fastapi_read_only_case_context_routes() -> None:
    with patch("api_app.registry_token_configured", return_value=False):
        app = create_app(
            runtime_provider=lambda: _Runtime(),
            cohort_reader=lambda run_id: {"run_id": run_id, "items": []},
            registry_provider=lambda: None,
        )
        client = TestClient(app)

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "read_mostly"

        context = client.get("/cases/case_api_1/context-pack")
        assert context.status_code == 200
        assert context.json()["case_id"] == "case_api_1"
        assert context.json()["contract_name"] == "CaseContextPack"
        assert context.json()["schema_version"] == "1"
        assert context.json()["contract_version"] == "vNext-2026-04"
        assert context.json()["pack_build"] == "case_context_pack.vnext.3"

        assert client.get("/cases/case_api_1/evidence").json()["items"]
        assert client.get("/cases/case_api_1/conflicts").json()["items"][0]["severity"] == "warning"
        assert client.get("/cases/case_api_1/gaps").json()["items"][0]["severity"] == "warning"

        trays = client.get("/cases/case_api_1/context-trays")
        assert trays.status_code == 200
        assert trays.json()["schema_version"] == "context_tray_set.v1"
        assert trays.json()["read_only"] is True
        assert trays.json()["gaps_tray"]

        os.environ["NODE_B_REGISTRY_TOKEN"] = "test-registry-token"
        try:
            skrzat = client.post(
                "/cases/case_api_1/skrzat/ask",
                json={"question": "Czego brakuje?", "mode": "investigate"},
                headers={"Authorization": "Bearer test-registry-token"},
            )
        finally:
            os.environ.pop("NODE_B_REGISTRY_TOKEN", None)
        assert skrzat.status_code == 200
        answer = skrzat.json()
        assert answer["schema_version"] == "conversation_answer_envelope.v1"
        assert answer["read_only"] is True
        assert answer["action_allowed"] is False
        assert answer["gaps"]
        assert "context_audit" in answer
        audit = answer["context_audit"]
        assert audit["stage_name"] == "skrzat_copilot"
        assert audit["answer_mode"] == "deterministic"
        assert answer.get("quality_metrics", {}).get("skrzat_evidence_coverage_rate") is not None
        assert answer.get("context_pack_lineage", {}).get("case_id") == "case_api_1"
        assert "pack_build" in answer.get("context_pack_lineage", {})

        missing = client.get("/cases/missing/context-pack")
        assert missing.status_code == 404
        err = missing.json()
        assert "error" in err
        assert err["error"]["code"] == "not_found"


def test_fastapi_cohort_run_route_is_read_only() -> None:
    app = create_app(
        runtime_provider=lambda: _Runtime(),
        cohort_reader=lambda run_id: {"run_id": run_id, "counts": {"gmail_selected": 2}},
        registry_provider=lambda: None,
    )
    client = TestClient(app)

    response = client.get("/cohort-runs/run-1")

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-1"
    assert response.json()["counts"]["gmail_selected"] == 2


def test_fastapi_503_returns_error_envelope() -> None:
    app = create_app(runtime_provider=lambda: None, cohort_reader=lambda _rid: None, registry_provider=lambda: None)
    client = TestClient(app)
    r = client.get("/cases/any/context-pack")
    assert r.status_code == 503
    body = r.json()
    assert body["error"]["code"] == "service_unavailable"
    assert "message" in body["error"]
