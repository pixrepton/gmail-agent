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
    building: dict[str, Any] = {"heated_area": heated}
    city = location.get("city")
    postal_code = location.get("postal_code")
    if city:
        building["city"] = city
    if postal_code:
        building["postal_code"] = postal_code
    known_building_type = profile.get("building_type")
    if known_building_type:
        # Only a real case fact; no fabricated "single_family" default.
        building["building_type"] = known_building_type
    # DHW is only asserted when a real case fact exists. The kalk-top validator
    # requires persons > 0 when dhw.enabled=true; with no known persons we send
    # no DHW assertion at all (domain applies its own documented default).
    dhw: dict[str, Any] = {}
    known_persons = profile.get("dhw_persons")
    if known_persons is not None:
        dhw["enabled"] = True
        dhw["persons"] = known_persons
    payload: dict[str, Any] = {
        "schemaVersion": "1.0",
        "traceId": str(snapshot_data.get("trace_id") or snapshot_data.get("engagement_id") or "")[:128],
        "lead": {"source": "gmail-agent", "channel": "agent_runtime"},
        "building": building,
        "preferences": {
            "heating": {"enabled": True},
            "dhw": dhw,
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


def build_calculate_offer_url(base: str) -> str:
    """Build the calculate-offer URL for the current WordPress REST contract.

    Pretty permalinks (`/wp-json/topinstal/v1/calculate-offer`) require a front
    controller that maps `/wp-json/*` onto `rest_route`. The local PHP built-in
    server historically served the WordPress HTML shell instead, producing
    HTTP 200 + `text/html` + non-JSON. The explicit REST query form works on
    php -S and on Apache/nginx permalink installs.
    """
    normalized = str(base or "").strip().rstrip("/")
    return f"{normalized}/index.php?rest_route=/topinstal/v1/calculate-offer"


def call_calculate_offer(
    payload: dict[str, Any],
    *,
    settings: AgentRuntimeSettings,
) -> dict[str, Any]:
    base = str(settings.kalk_top_base_url or "").strip().rstrip("/")
    if not base:
        raise KalkTopClientError("KALK_TOP_BASE_URL is not configured")
    url = build_calculate_offer_url(base)
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
