from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.kalk_top_client import (
    KalkTopClientError,
    KalkTopInvalidResponseError,
    call_calculate_offer,
    interpret_calculate_offer_success,
)
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.store import build_initial_snapshot
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tools_registry import AgentToolRegistry


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=2,
        openai_api_key="test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="http://kalk-top.test",
        kalk_top_agent_key="test",
        kalk_top_timeout_sec=1,
        kalk_top_max_retries=1,
    )


class _NonJsonResponse:
    status_code = 200
    text = "upstream body must not escape the client boundary"

    def json(self) -> object:
        raise ValueError("Expecting value: line 1 column 1")


class _NonJsonClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _NonJsonClient:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def post(self, *_args: object, **_kwargs: object) -> _NonJsonResponse:
        return _NonJsonResponse()


def test_calculate_offer_url_uses_canonical_wp_json_pretty_permalink() -> None:
    from agent_runtime.kalk_top_client import build_calculate_offer_url

    url = build_calculate_offer_url("http://127.0.0.1:8091/")
    assert url == "http://127.0.0.1:8091/wp-json/topinstal/v1/calculate-offer"
    assert "rest_route=" not in url


def test_local_compose_binds_pretty_permalink_router_and_container_key_alias() -> None:
    compose = Path(__file__).resolve().parents[4] / "docker-compose.kalk-top-local.yml"
    text = compose.read_text(encoding="utf-8")
    assert "scripts/kalk-top-local/_router.php" in text
    assert "/opt/topinstal-runtime/wordpress/_router.php" in text
    assert "KALKTOP_AGENT_KEY: ${KALK_TOP_AGENT_KEY" not in text
    assert "KALKTOP_AGENT_KEY:-${KALK_TOP_AGENT_KEY}" in text or (
        "KALKTOP_AGENT_KEY:-$${KALK_TOP_AGENT_KEY}" in text
    )
    router = Path(__file__).resolve().parents[4] / "scripts" / "kalk-top-local" / "_router.php"
    router_text = router.read_text(encoding="utf-8")
    assert "/wp-json/" in router_text
    assert "rest_route" in router_text


def test_json_error_envelope_is_not_business_success() -> None:
    with pytest.raises(KalkTopInvalidResponseError, match="error envelope: AGENT_KEY_INVALID"):
        interpret_calculate_offer_success(
            {"errorCode": "AGENT_KEY_INVALID", "message": "Agent key is invalid."}
        )


def test_json_dict_without_offer_structure_is_not_business_success() -> None:
    with pytest.raises(KalkTopInvalidResponseError, match="missing engineering"):
        interpret_calculate_offer_success({"schemaVersion": "1.0", "ok": True})


def test_success_payload_requires_pricing_totals() -> None:
    with pytest.raises(KalkTopInvalidResponseError, match="pricing.totals"):
        interpret_calculate_offer_success(
            {
                "schemaVersion": "1.0",
                "engineering": {"ozc": {"designHeatLoss_kW": 5}},
                "pricing": {"currency": "PLN"},
            }
        )


def test_valid_success_payload_is_accepted() -> None:
    payload = {
        "schemaVersion": "1.0",
        "traceId": "t1",
        "engineering": {"ozc": {"designHeatLoss_kW": 5}},
        "pricing": {"currency": "PLN", "totals": {"gross": 1000}},
    }
    assert interpret_calculate_offer_success(payload) is payload


class _JsonErrorEnvelope:
    status_code = 200
    text = '{"errorCode":"AGENT_KEY_INVALID"}'
    headers = {"content-type": "application/json"}

    def json(self) -> object:
        return {"errorCode": "AGENT_KEY_INVALID", "message": "Agent key is invalid."}


class _JsonErrorClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> _JsonErrorClient:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def post(self, *_args: object, **_kwargs: object) -> _JsonErrorEnvelope:
        return _JsonErrorEnvelope()


def test_http_200_error_envelope_cannot_escape_client_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _JsonErrorClient)
    with pytest.raises(KalkTopInvalidResponseError, match="error envelope"):
        call_calculate_offer({"schemaVersion": "1.0"}, settings=_settings())


def test_missing_agent_key_does_not_send_secret_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _Capture:
        status_code = 401
        text = '{"errorCode":"AUTH_REQUIRED"}'
        headers = {"content-type": "application/json"}

        def json(self) -> object:
            return {"errorCode": "AUTH_REQUIRED"}

    class _Client:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

        def post(self, url: str, **kwargs: object) -> _Capture:
            captured["url"] = url
            captured["headers"] = dict(kwargs.get("headers") or {})  # type: ignore[arg-type]
            return _Capture()

    monkeypatch.setattr(httpx, "Client", _Client)
    settings = replace(_settings(), kalk_top_agent_key="")
    with pytest.raises(KalkTopClientError, match="HTTP 401"):
        call_calculate_offer({"schemaVersion": "1.0"}, settings=settings)
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert "X-Top-Instal-Agent-Key" not in headers
    assert "Authorization" not in headers
    assert captured["url"] == "http://kalk-top.test/wp-json/topinstal/v1/calculate-offer"
    dumped = json.dumps(captured)
    assert "X-Top-Instal-Agent-Key" not in dumped
    assert "Authorization" not in dumped


def test_non_json_success_is_a_typed_kalk_top_client_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _NonJsonClient)

    with pytest.raises(KalkTopClientError, match="non-JSON response"):
        call_calculate_offer({"schemaVersion": "1.0"}, settings=_settings())


def test_non_json_success_cannot_escape_the_planner_tool_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(httpx, "Client", _NonJsonClient)
    snapshot = build_initial_snapshot(
        case_id="case-kalk-invalid-response",
        engagement_id="eng-kalk-invalid-response",
        trace_id="trace-kalk-invalid-response",
    )
    from llm_contracts.engagement_snapshot_v2 import HvacProfile

    snapshot = snapshot.model_copy(
        update={
            "case_kind": "wycena_oferta",
            "hvac_profile": HvacProfile(heated_area_m2=100),
        }
    )
    context = ToolExecutionContext.from_snapshot(snapshot, settings=_settings())
    context.signal_payload = {
        "decision_comparison_inputs": {
            "business_recommended_action": "reply",
            "action_planner_primary_action": "prepare_reply",
            "next_best_action_type": "answer_customer",
        }
    }

    result = AgentToolRegistry().execute(
        ToolCallPlan(tool_name="call_kalk_top_quote", arguments={}),
        context=context,
    )

    assert result.status == "error"
    assert result.failure_class == "DOWNSTREAM_RESULT_INVALID"
    assert result.failure_owner == "infra"
    assert result.retryable is False
    assert result.snapshot_delta["execution_attribution"]["safe_next_step"] == (
        "escalate_downstream_contract"
    )
