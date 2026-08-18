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
    """Canonical calculate-offer URL shared with other kalk-top consumers.

    Contract: `{base}/wp-json/topinstal/v1/calculate-offer` (pretty permalink).
    The WordPress query form `index.php?rest_route=...` is an equivalent REST
    dispatcher, not the product canonical URL. Local php -S mapping of
    `/wp-json/*` is a local-runtime owner concern.
    """
    normalized = str(base or "").strip().rstrip("/")
    return f"{normalized}/wp-json/topinstal/v1/calculate-offer"


def interpret_calculate_offer_success(data: object) -> dict[str, Any]:
    """Fail closed unless the payload is the current calculate-offer success contract.

    Does not invent an OfferDTO. Validates only the documented success envelope
    and the fields consumed by `call_kalk_top_quote` (`pricing.totals`).
    """
    if not isinstance(data, dict):
        raise KalkTopInvalidResponseError("kalk-top returned non-object JSON")
    error_code = str(data.get("errorCode") or "").strip()
    if error_code:
        raise KalkTopInvalidResponseError(
            f"kalk-top returned error envelope: {error_code}"
        )
    schema_version = str(data.get("schemaVersion") or "").strip()
    if not schema_version:
        raise KalkTopInvalidResponseError("kalk-top success payload missing schemaVersion")
    engineering = data.get("engineering")
    if not isinstance(engineering, dict) or not engineering:
        raise KalkTopInvalidResponseError("kalk-top success payload missing engineering")
    pricing = data.get("pricing")
    if not isinstance(pricing, dict):
        raise KalkTopInvalidResponseError("kalk-top success payload missing pricing")
    totals = pricing.get("totals")
    if not isinstance(totals, dict) or not totals:
        raise KalkTopInvalidResponseError("kalk-top success payload missing pricing.totals")
    return data


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
            return interpret_calculate_offer_success(data)
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
