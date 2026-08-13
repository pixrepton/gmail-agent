"""HTTP client for kalk-top calculate-offer (Node A)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from agent_runtime.settings import AgentRuntimeSettings


class KalkTopClientError(RuntimeError):
    """Permanent or retryable kalk-top failure."""


class KalkTopUnreachableError(KalkTopClientError):
    """Network / 5xx — map to node_a_error in agent graph."""


class KalkTopInvalidResponseError(KalkTopClientError):
    """The downstream endpoint responded, but violated its JSON contract."""


def build_calc_request_from_profile(snapshot_data: dict[str, Any]) -> dict[str, Any]:
    profile = snapshot_data.get("hvac_profile") if isinstance(snapshot_data.get("hvac_profile"), dict) else {}
    location = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    heated = profile.get("heated_area_m2")
    ozc_kw = profile.get("thermal_demand_kw")
    payload: dict[str, Any] = {
        "schemaVersion": "1.0",
        "traceId": str(snapshot_data.get("trace_id") or snapshot_data.get("engagement_id") or "")[:128],
        "lead": {"source": "gmail-agent", "channel": "agent_runtime"},
        "building": {
            "heated_area": heated,
            "city": location.get("city"),
            "postal_code": location.get("postal_code"),
            "building_type": profile.get("building_type") or "single_family",
        },
        "preferences": {
            "heating": {"enabled": True},
            "dhw": {"enabled": True, "persons": 4},
        },
    }
    if ozc_kw is not None and heated:
        try:
            payload["ozcResult"] = {
                "designHeatLoss_kW": float(ozc_kw),
                "heatedArea_m2": float(heated),
            }
        except (TypeError, ValueError):
            pass
    return payload


def call_calculate_offer(
    payload: dict[str, Any],
    *,
    settings: AgentRuntimeSettings,
) -> dict[str, Any]:
    base = str(settings.kalk_top_base_url or "").strip().rstrip("/")
    if not base:
        raise KalkTopClientError("KALK_TOP_BASE_URL is not configured")
    url = f"{base}/wp-json/topinstal/v1/calculate-offer"
    headers = {"Content-Type": "application/json"}
    key = str(settings.kalk_top_agent_key or "").strip()
    if key:
        headers["X-Top-Instal-Agent-Key"] = key
    timeout = float(settings.kalk_top_timeout_sec)
    last_error: Exception | None = None
    for attempt in range(max(1, int(settings.kalk_top_max_retries))):
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload, headers=headers)
            if response.status_code >= 500:
                last_error = KalkTopUnreachableError(f"kalk-top HTTP {response.status_code}")
                time.sleep(min(2.0, 0.5 * (attempt + 1)))
                continue
            if response.status_code >= 400:
                raise KalkTopClientError(
                    f"kalk-top HTTP {response.status_code}: {response.text[:500]}"
                )
            try:
                data = response.json()
            except ValueError as exc:
                raise KalkTopInvalidResponseError(
                    "kalk-top returned non-JSON response"
                ) from exc
            if not isinstance(data, dict):
                raise KalkTopInvalidResponseError("kalk-top returned non-object JSON")
            return data
        except httpx.TimeoutException as exc:
            last_error = KalkTopUnreachableError(f"kalk-top timeout after {timeout}s: {exc}")
        except httpx.TransportError as exc:
            last_error = KalkTopUnreachableError(str(exc))
        except TimeoutError as exc:
            last_error = KalkTopUnreachableError(str(exc))
        if attempt + 1 < settings.kalk_top_max_retries:
            time.sleep(min(2.0, 0.5 * (attempt + 1)))
    if last_error is not None:
        raise last_error
    raise KalkTopUnreachableError("kalk-top call failed")
