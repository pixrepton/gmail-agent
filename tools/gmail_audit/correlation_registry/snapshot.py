"""EngagementSnapshot assembly (read-model) with concurrent workflow fetches."""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from case_context_contract import build_case_context_pack_vnext
from correlation_registry.service import CorrelationRegistryService
from log_config import get_logger

log = get_logger(__name__)

WORKFLOW_FETCH_TIMEOUT_SEC = 2.0

ContextPackLoader = Callable[[str, str], dict[str, Any] | None]
WorkflowPackLoader = Callable[[str], dict[str, Any] | None]


def _workflow_base_url() -> str:
    return str(
        os.environ.get("CIEPLO_WORKFLOW_CONTEXT_BASE_URL")
        or os.environ.get("CIEPLO_WORKER_BASE_URL")
        or ""
    ).strip().rstrip("/")


def _workflow_auth_headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = str(
        os.environ.get("CIEPLO_WORKFLOW_CONTEXT_TOKEN")
        or os.environ.get("NODE_B_REGISTRY_TOKEN")
        or ""
    ).strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_workflow_context_pack_async(
    client: Any,
    workflow_id: str,
) -> tuple[str, dict[str, Any] | None, str | None]:
    """Returns (workflow_id, pack_or_none, missing_reason_or_none)."""
    base = _workflow_base_url()
    wf_id = str(workflow_id or "").strip()
    if not base or not wf_id:
        return wf_id, None, "loader_unconfigured"
    url = f"{base}/internal/workflows/{wf_id}/context-pack"
    try:
        response = await client.get(url, headers=_workflow_auth_headers())
        if response.status_code == 404:
            return wf_id, None, "not_found"
        if response.status_code >= 400:
            return wf_id, None, "error"
        data = response.json()
        if isinstance(data, dict):
            return wf_id, data, None
        return wf_id, None, "invalid_response"
    except Exception as exc:  # noqa: BLE001
        if "timeout" in type(exc).__name__.lower() or "Timeout" in type(exc).__name__:
            log.warning("workflow context-pack timeout workflow_id=%s", wf_id)
            return wf_id, None, "timeout"
        log.warning("workflow context-pack fetch failed workflow_id=%s err=%s", wf_id, exc)
        return wf_id, None, "error"


async def fetch_workflow_context_packs_parallel(
    workflow_ids: list[str],
    *,
    timeout_sec: float = WORKFLOW_FETCH_TIMEOUT_SEC,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    ids = [str(wid).strip() for wid in workflow_ids if str(wid).strip()]
    if not ids:
        return [], []

    try:
        import httpx
    except ImportError:  # pragma: no cover
        log.warning("httpx not installed; workflow packs unavailable")
        return [], [{"component": "workflow_context_pack", "workflow_id": wid, "reason": "httpx_missing"} for wid in ids]

    packs: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    timeout = httpx.Timeout(timeout_sec)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await asyncio.gather(
            *[fetch_workflow_context_pack_async(client, wid) for wid in ids],
            return_exceptions=False,
        )
    for wf_id, pack, reason in results:
        if pack:
            packs.append(pack)
        elif reason:
            missing.append(
                {
                    "component": "workflow_context_pack",
                    "workflow_id": wf_id,
                    "reason": reason,
                }
            )
    return packs, missing


def fetch_workflow_context_pack_http(workflow_id: str) -> dict[str, Any] | None:
    """Sync fallback for tests and environments without async event loop."""
    try:
        packs, _missing = asyncio.run(fetch_workflow_context_packs_parallel([workflow_id]))
    except RuntimeError:
        return None
    return packs[0] if packs else None


def _collect_workflow_ids(bundle: dict[str, Any]) -> list[str]:
    explicit = bundle.get("cieplo_workflow_ids")
    if isinstance(explicit, list):
        return [str(wid).strip() for wid in explicit if str(wid).strip()]
    single = str(bundle.get("cieplo_workflow_id") or "").strip()
    if single:
        return [single]
    links = bundle.get("correlation_links") if isinstance(bundle.get("correlation_links"), list) else []
    ids: list[str] = []
    for link in links:
        if isinstance(link, dict) and link.get("link_type") == "cieplo_workflow":
            target = str(link.get("target_id") or "").strip()
            if target and target not in ids:
                ids.append(target)
    return ids


async def build_engagement_snapshot_async(
    registry: CorrelationRegistryService,
    engagement_id: str,
    *,
    load_case_context_pack: ContextPackLoader | None = None,
    load_workflow_context_pack: WorkflowPackLoader | None = None,
) -> dict[str, Any] | None:
    bundle = registry.get_snapshot_bundle(engagement_id)
    if not bundle:
        return None

    case_pack = None
    missing: list[dict[str, str]] = []

    case_id = str(bundle.get("case_id") or "")
    workflow_ids = _collect_workflow_ids(bundle)

    if case_id and load_case_context_pack:
        try:
            raw = load_case_context_pack(case_id, "")
            if raw:
                case_pack = build_case_context_pack_vnext(raw) if not raw.get("contract_name") else raw
                case_pack["engagement_id"] = engagement_id
                case_pack["correlation_links"] = bundle.get("correlation_links") or []
            else:
                missing.append({"component": "case_context_pack", "reason": "not_found"})
        except Exception:  # noqa: BLE001
            log.warning("case context-pack failed case_id=%s", case_id, exc_info=True)
            missing.append({"component": "case_context_pack", "reason": "error"})
    elif case_id:
        missing.append({"component": "case_context_pack", "reason": "loader_unconfigured"})

    workflow_packs: list[dict[str, Any]] = []
    if workflow_ids:
        if load_workflow_context_pack:
            for wid in workflow_ids:
                try:
                    pack = load_workflow_context_pack(wid)
                    if pack:
                        workflow_packs.append(pack)
                    else:
                        missing.append(
                            {"component": "workflow_context_pack", "workflow_id": wid, "reason": "not_found"}
                        )
                except Exception:  # noqa: BLE001
                    missing.append(
                        {"component": "workflow_context_pack", "workflow_id": wid, "reason": "error"}
                    )
        else:
            workflow_packs, wf_missing = await fetch_workflow_context_packs_parallel(workflow_ids)
            missing.extend(wf_missing)

    primary_workflow_pack = workflow_packs[0] if workflow_packs else None

    return {
        **bundle,
        "contract_name": "EngagementSnapshot",
        "read_only": True,
        "case_context_pack": case_pack,
        "workflow_context_pack": primary_workflow_pack,
        "workflow_context_packs": workflow_packs,
        "missing_components": missing,
        "labels_pl": {
            "mail_case": "Sprawa mailowa",
            "cieplo_workflow": "Zlecenie Cieplo",
        },
    }


def build_engagement_snapshot(
    registry: CorrelationRegistryService,
    engagement_id: str,
    *,
    load_case_context_pack: ContextPackLoader | None = None,
    load_workflow_context_pack: WorkflowPackLoader | None = None,
) -> dict[str, Any] | None:
    """Sync entry for tests; uses asyncio.run for parallel workflow fetch."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        raise RuntimeError("build_engagement_snapshot cannot run inside active event loop; use build_engagement_snapshot_async")
    return asyncio.run(
        build_engagement_snapshot_async(
            registry,
            engagement_id,
            load_case_context_pack=load_case_context_pack,
            load_workflow_context_pack=load_workflow_context_pack,
        )
    )
