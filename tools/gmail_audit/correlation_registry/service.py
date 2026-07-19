"""High-level correlation registry API."""

from __future__ import annotations

from typing import Any

from correlation_registry.heuristics import register_link_bundle
from correlation_registry.link_types import normalize_link_type
from log_config import get_logger
from correlation_registry.store import (
    CorrelationRegistryStore,
    InMemoryCorrelationRegistryStore,
    PostgresCorrelationRegistryStore,
    build_registry_store,
)

log = get_logger(__name__)


class CorrelationRegistryService:
    def __init__(self, store: CorrelationRegistryStore) -> None:
        self.store = store

    def bootstrap(self) -> None:
        self.store.bootstrap()

    def _normalize_links(self, raw: Any) -> list[dict[str, Any]]:
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ValueError("links must be a list")
        out: list[dict[str, Any]] = []
        for idx, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"links[{idx}] must be an object")
            link_type = str(item.get("link_type") or "").strip()
            target_id = str(item.get("target_id") or "").strip()
            if not link_type:
                raise ValueError(f"links[{idx}].link_type is required")
            if not target_id:
                raise ValueError(f"links[{idx}].target_id is required")
            source_repo = str(item.get("source_repo") or "gmail-agent").strip() or "gmail-agent"
            confidence_raw = item.get("confidence")
            try:
                confidence = float(confidence_raw) if confidence_raw is not None else 1.0
            except (TypeError, ValueError) as exc:
                raise ValueError(f"links[{idx}].confidence must be a number") from exc
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            out.append(
                {
                    "link_type": normalize_link_type(link_type),
                    "target_id": target_id,
                    "source_repo": source_repo,
                    "confidence": confidence,
                    "metadata": metadata,
                }
            )
        return out

    def register_links_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        identity_email = str(payload.get("identity_email") or payload.get("client_email") or payload.get("customer_email") or "")
        links = self._normalize_links(payload.get("links"))
        within_days_raw = payload.get("within_days")
        try:
            within_days = int(within_days_raw) if within_days_raw is not None else 30
        except (TypeError, ValueError) as exc:
            raise ValueError("within_days must be an integer") from exc
        within_days = max(1, min(365, within_days))
        result = register_link_bundle(
            self.store,
            identity_email=identity_email,
            display_name=str(payload.get("display_name") or ""),
            message_id=str(payload.get("message_id") or ""),
            links=links,
            within_days=within_days,
            identity_metadata=payload.get("identity_metadata")
            if isinstance(payload.get("identity_metadata"), dict)
            else None,
            engagement_metadata=payload.get("engagement_metadata")
            if isinstance(payload.get("engagement_metadata"), dict)
            else None,
        )
        self._emit_registry_event("correlation_links_registered", result, payload)
        return result

    def _emit_registry_event(
        self,
        event_type: str,
        result: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> None:
        if not result:
            return
        db_url = str(getattr(self.store, "database_url", "") or "").strip()
        if not db_url:
            return
        try:
            from event_spine.emitter import publish_os_event

            publish_os_event(
                database_url=db_url,
                event_type=event_type,
                engagement_id=str(result.get("engagement_id") or ""),
                source_repo="gmail-agent",
                payload={"links_count": len(payload.get("links") or [])},
                correlation={
                    "identity_id": result.get("identity_id"),
                    "message_id": payload.get("message_id"),
                },
            )
        except Exception:  # noqa: BLE001
            log.debug("event_spine emit skipped", exc_info=True)

    def sync_mailbox_case(
        self,
        *,
        case_id: str,
        customer_email: str,
        thread_id: str = "",
        message_id: str = "",
        customer_name: str = "",
    ) -> dict[str, Any] | None:
        case_id = str(case_id or "").strip()
        if not case_id:
            return None
        email = str(customer_email or "").strip()
        links: list[dict[str, Any]] = [
            {
                "link_type": "mailbox_case",
                "target_id": case_id,
                "source_repo": "gmail-agent",
                "confidence": 1.0,
            },
        ]
        if thread_id:
            links.append(
                {
                    "link_type": "gmail_thread",
                    "target_id": thread_id,
                    "source_repo": "gmail-agent",
                    "confidence": 0.95,
                }
            )
        if message_id:
            links.append(
                {
                    "link_type": "gmail_message",
                    "target_id": message_id,
                    "source_repo": "gmail-agent",
                    "confidence": 1.0,
                }
            )
        if not email:
            log.debug("correlation_registry: skip case %s — no customer_email", case_id)
            return None
        try:
            return register_link_bundle(
                self.store,
                identity_email=email,
                display_name=customer_name,
                message_id=message_id,
                links=links,
            )
        except Exception:  # noqa: BLE001
            log.warning("correlation_registry: sync_mailbox_case failed case_id=%s", case_id, exc_info=True)
            return None

    def sync_cieplo_workflow(
        self,
        *,
        workflow_id: str,
        client_email: str,
        message_id: str = "",
        trace_id: str = "",
        external_key: str = "",
    ) -> dict[str, Any] | None:
        workflow_id = str(workflow_id or "").strip()
        if not workflow_id:
            return None
        links: list[dict[str, Any]] = [
            {
                "link_type": "cieplo_workflow",
                "target_id": workflow_id,
                "source_repo": "topinstal-cieplo-orchestrator",
                "confidence": 1.0,
            },
        ]
        if message_id:
            links.append(
                {
                    "link_type": "gmail_message",
                    "target_id": message_id,
                    "source_repo": "topinstal-cieplo-orchestrator",
                    "confidence": 1.0,
                }
            )
        if trace_id:
            links.append(
                {
                    "link_type": "canonical_trace",
                    "target_id": trace_id,
                    "source_repo": "topinstal-cieplo-orchestrator",
                    "confidence": 1.0,
                }
            )
        if external_key:
            links.append(
                {
                    "link_type": "cieplo_external_key",
                    "target_id": external_key,
                    "source_repo": "topinstal-cieplo-orchestrator",
                    "confidence": 0.85,
                }
            )
        email = str(client_email or "").strip()
        if not email:
            log.debug("correlation_registry: skip workflow %s — no client_email", workflow_id)
            return None
        try:
            return register_link_bundle(
                self.store,
                identity_email=email,
                message_id=message_id,
                links=links,
            )
        except Exception:  # noqa: BLE001
            log.warning("correlation_registry: sync_cieplo_workflow failed workflow_id=%s", workflow_id, exc_info=True)
            return None

    def get_snapshot_bundle(self, engagement_id: str) -> dict[str, Any] | None:
        engagement = self.store.get_engagement(engagement_id)
        if not engagement:
            return None
        identity = self.store.get_identity(str(engagement.get("identity_id") or ""))
        links = self.store.list_links_for_engagement(engagement_id)
        cieplo_workflow_ids: list[str] = []
        case_id = ""
        for link in links:
            if link.get("link_type") == "cieplo_workflow":
                wid = str(link.get("target_id") or "").strip()
                if wid and wid not in cieplo_workflow_ids:
                    cieplo_workflow_ids.append(wid)
            if link.get("link_type") == "mailbox_case":
                case_id = str(link.get("target_id") or "")
        return {
            "schema_version": "engagement_snapshot.v1",
            "engagement_id": engagement_id,
            "cieplo_workflow_id": cieplo_workflow_ids[0] if cieplo_workflow_ids else "",
            "cieplo_workflow_ids": cieplo_workflow_ids,
            "case_id": case_id,
            "identity": identity,
            "engagement": engagement,
            "correlation_links": links,
        }

    def lookup_by_case_id(self, case_id: str) -> dict[str, Any] | None:
        return self.store.find_engagement_by_case_id(case_id)


def build_correlation_registry_service(
    database_url: str = "",
    *,
    in_memory: bool = False,
) -> CorrelationRegistryService | None:
    if not database_url and not in_memory:
        return None
    store = build_registry_store(database_url, in_memory=in_memory)
    return CorrelationRegistryService(store)


__all__ = [
    "CorrelationRegistryService",
    "build_correlation_registry_service",
    "InMemoryCorrelationRegistryStore",
    "PostgresCorrelationRegistryStore",
]
