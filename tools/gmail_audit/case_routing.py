"""Case routing: export taxonomy → case_family, requires_action, desk hints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from case_family_boundary import is_internal_task_row
from mail_classification import classify_message

EXPORT_CASE_TYPE_LABELS_PL: dict[str, str] = {
    "lead_oferta": "Lead / Oferta",
    "serwis": "Serwis / Awaria",
    "reklamacja_gwarancja": "Reklamacja / Gwarancja",
    "dofinansowanie": "Dofinansowanie",
    "wspolpraca": "Współpraca / Partnerstwo",
    "ksiegowosc_podatki": "Księgowość / Podatki",
    "dokumenty": "Dokumenty",
    "finansowanie": "Finansowanie / Leasing",
    "it_strona": "IT / Strona www",
    "dostawca": "Dostawca / hurtownia",
    "supplier_opportunity": "Okazja dostawcy",
    "noise": "Szum (newsletter itp)",
    "unknown_low_value": "Niska wartość",
    "other": "Inne",
}

EXPORT_LABEL_TO_CASE_TYPE: dict[str, str] = {v: k for k, v in EXPORT_CASE_TYPE_LABELS_PL.items()}


@dataclass(frozen=True)
class CaseRouting:
    case_family: str
    requires_action: bool
    desk_eligible: bool
    priority_hint: str
    export_case_type: str
    source_kind: str
    upsert_allowed: bool
    orchestrator_status: str | None = None


def _row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    meta = row.get("metadata")
    return dict(meta) if isinstance(meta, dict) else {}


def _row_priority_label(row: dict[str, Any]) -> str:
    meta = _row_metadata(row)
    return str(meta.get("priority_label") or meta.get("priority") or "").strip()


def _row_source_kind(row: dict[str, Any]) -> str:
    return str(_row_metadata(row).get("source_kind") or "").strip()


def _row_requires_action(row: dict[str, Any]) -> bool:
    meta = _row_metadata(row)
    if "requires_action" in meta:
        return bool(meta.get("requires_action"))
    return False


def case_row_requires_action(row: dict[str, Any]) -> bool:
    """Public helper for API/UI: whether operator action is expected."""
    return _row_requires_action(row)


def _is_noise_row(row: dict[str, Any]) -> bool:
    meta = _row_metadata(row)
    export_type = str(meta.get("export_case_type") or "").strip()
    if export_type == "noise":
        return True
    return str(meta.get("candidate_tier") or "") == "noise_excluded"


def desk_eligible(row: dict[str, Any]) -> bool:
    """Biurko feed subset: P1/P2 band + Cieplo info cards; excludes noise."""
    if is_internal_task_row(row):
        return _priority_in_desk_band(_row_priority_label(row))
    if _is_noise_row(row):
        return False
    if _row_source_kind(row) == "cieplo_orchestrated" and not _row_requires_action(row):
        return True
    return _priority_in_desk_band(_row_priority_label(row))


def _routing_from_existing_row(row: dict[str, Any], *, source_kind: str) -> CaseRouting:
    meta = _row_metadata(row)
    priority = _row_priority_label(row)
    sk = str(meta.get("source_kind") or source_kind or "gmail_inbound").strip()
    return CaseRouting(
        case_family=str(row.get("case_family") or ""),
        requires_action=_row_requires_action(row),
        desk_eligible=desk_eligible(row),
        priority_hint=priority,
        export_case_type=str(meta.get("export_case_type") or ""),
        source_kind=sk,
        upsert_allowed=True,
        orchestrator_status=meta.get("orchestrator_status"),
    )


def export_case_type_from_classification(cls: dict[str, Any]) -> str:
    return str(cls.get("case_type") or "unknown_low_value").strip()


def _priority_in_desk_band(priority_label: str) -> bool:
    label = str(priority_label or "").strip().upper()
    return label.startswith("P1") or label.startswith("P2")


def operator_priority_to_label(priority: str) -> str:
    """Map operator task priority to export priority_label band."""
    p = str(priority or "normalny").strip().lower()
    if p == "pilne":
        return "P1 - pilne"
    if p == "niski":
        return "P3 - niski"
    return "P2 - ważne"


def _family_for_export_type(export_case_type: str, classification: dict[str, Any] | None) -> tuple[str, bool]:
    reasons = set((classification or {}).get("priority_reasons") or [])
    is_task = bool((classification or {}).get("is_task"))

    mapping: dict[str, tuple[str, bool]] = {
        "lead_oferta": ("lead_opportunity", True),
        "serwis": ("service", True),
        "reklamacja_gwarancja": ("service", True),
        "ksiegowosc_podatki": ("accounting", True),
        "dofinansowanie": ("lead_opportunity", True),
        "wspolpraca": ("partnership", True),
        "it_strona": ("operations", True),
        "finansowanie": ("operations", True),
        "dostawca": ("supplier", is_task),
        "supplier_opportunity": ("supplier", True),
        "other": ("operations", is_task),
    }

    if export_case_type == "dokumenty":
        if "accounting_tax" in reasons:
            return "accounting", True
        return "documents", True

    return mapping.get(export_case_type, ("operations", is_task))


def classify_mailbox_row(
    case_family: str | None,
    source_kind: str | None,
    export_case_type: str | None,
    orchestrator_status: str | None = None,
    *,
    classification: dict[str, Any] | None = None,
) -> CaseRouting:
    sk = str(source_kind or "gmail_inbound").strip() or "gmail_inbound"
    export_type = str(export_case_type or "").strip()

    if sk in {"manual", "operator_manual", "operator_scheduled"}:
        priority = str((classification or {}).get("priority_label") or "").strip()
        if not priority:
            meta_priority = str((classification or {}).get("priority") or "normalny").strip()
            priority = operator_priority_to_label(meta_priority)
        desk = _priority_in_desk_band(priority)
        return CaseRouting(
            case_family="operations",
            requires_action=True,
            desk_eligible=desk,
            priority_hint=priority,
            export_case_type="operations",
            source_kind="manual",
            upsert_allowed=True,
            orchestrator_status=orchestrator_status,
        )

    if sk == "materialize":
        family = str(case_family or "lead_opportunity").strip() or "lead_opportunity"
        export_type = export_type or "lead_oferta"
        _, requires = _family_for_export_type(export_type, classification)
        priority = str((classification or {}).get("priority_label") or "P2 - ważne")
        return CaseRouting(
            case_family=family,
            requires_action=requires,
            desk_eligible=_priority_in_desk_band(priority) and requires,
            priority_hint=priority,
            export_case_type=export_type,
            source_kind=sk,
            upsert_allowed=True,
            orchestrator_status=orchestrator_status,
        )

    if export_type == "noise":
        return CaseRouting(
            case_family="",
            requires_action=False,
            desk_eligible=False,
            priority_hint="pomijany",
            export_case_type="noise",
            source_kind=sk,
            upsert_allowed=False,
            orchestrator_status=orchestrator_status,
        )

    if export_type == "unknown_low_value":
        return CaseRouting(
            case_family="reference_only",
            requires_action=False,
            desk_eligible=False,
            priority_hint="pomijany",
            export_case_type="unknown_low_value",
            source_kind=sk,
            upsert_allowed=True,
            orchestrator_status=orchestrator_status,
        )

    family, default_requires = _family_for_export_type(export_type, classification)
    if case_family and str(case_family).strip() not in {"", "unknown"}:
        family = str(case_family).strip()

    requires_action = default_requires
    if orchestrator_status == "ok":
        requires_action = False
    elif orchestrator_status == "failed":
        requires_action = True

    priority_hint = str((classification or {}).get("priority_label") or "")
    desk_eligible = _priority_in_desk_band(priority_hint)
    if sk == "cieplo_orchestrated" and orchestrator_status == "ok":
        desk_eligible = True

    return CaseRouting(
        case_family=family,
        requires_action=requires_action,
        desk_eligible=desk_eligible,
        priority_hint=priority_hint,
        export_case_type=export_type,
        source_kind=sk,
        upsert_allowed=True,
        orchestrator_status=orchestrator_status,
    )


def route_from_classification(
    classification: dict[str, Any],
    *,
    source_kind: str = "gmail_inbound",
    orchestrator_status: str | None = None,
    case_family_hint: str | None = None,
) -> CaseRouting:
    export_type = export_case_type_from_classification(classification)
    return classify_mailbox_row(
        case_family_hint,
        source_kind,
        export_type,
        orchestrator_status,
        classification=classification,
    )


def route_gmail_message(
    *,
    subject: str,
    snippet: str,
    sender: str,
    labels: list,
    body: str,
    has_attachment: bool,
    direction: str = "inbound",
    source_kind: str = "gmail_inbound",
    orchestrator_status: str | None = None,
) -> CaseRouting:
    classification = classify_message(
        subject=subject,
        snippet=snippet,
        sender=sender,
        labels=labels,
        body=body,
        has_attachment=has_attachment,
        direction=direction,
    )
    return route_from_classification(
        classification,
        source_kind=source_kind,
        orchestrator_status=orchestrator_status,
    )


def apply_routing_to_case_row(row: dict[str, Any], routing: CaseRouting) -> dict[str, Any]:
    out = dict(row)
    if routing.case_family:
        out["case_family"] = routing.case_family
    meta = dict(out.get("metadata") or {})
    meta["requires_action"] = routing.requires_action
    meta["source_kind"] = routing.source_kind
    meta["export_case_type"] = routing.export_case_type
    if routing.priority_hint:
        meta["priority_label"] = routing.priority_hint
    if routing.orchestrator_status is not None:
        meta["orchestrator_status"] = routing.orchestrator_status
    out["metadata"] = meta
    return out


def enrich_case_row_before_upsert(
    row: dict[str, Any],
    *,
    source_kind: str,
    classification: dict[str, Any] | None = None,
    orchestrator_status: str | None = None,
) -> tuple[dict[str, Any], CaseRouting]:
    meta = _row_metadata(row)
    export_type = str(meta.get("export_case_type") or "").strip()
    if classification is None and not export_type:
        if meta.get("requires_action") is not None or meta.get("source_kind"):
            preserved = _routing_from_existing_row(row, source_kind=source_kind)
            if source_kind and not meta.get("source_kind"):
                row = apply_routing_to_case_row(row, preserved)
            return row, preserved
    if classification is None and export_type and meta.get("requires_action") is not None and orchestrator_status is None:
        return row, _routing_from_existing_row(row, source_kind=source_kind)
    if classification is None and export_type:
        routing = classify_mailbox_row(
            str(row.get("case_family") or ""),
            source_kind,
            export_type,
            orchestrator_status,
        )
    elif classification is not None:
        routing = route_from_classification(
            classification,
            source_kind=source_kind,
            orchestrator_status=orchestrator_status,
            case_family_hint=str(row.get("case_family") or ""),
        )
    else:
        routing = classify_mailbox_row(
            str(row.get("case_family") or ""),
            source_kind,
            export_type or "other",
            orchestrator_status,
        )
    if not routing.upsert_allowed:
        return row, routing
    return apply_routing_to_case_row(row, routing), routing


__all__ = [
    "CaseRouting",
    "EXPORT_CASE_TYPE_LABELS_PL",
    "EXPORT_LABEL_TO_CASE_TYPE",
    "apply_routing_to_case_row",
    "classify_mailbox_row",
    "desk_eligible",
    "enrich_case_row_before_upsert",
    "operator_priority_to_label",
    "export_case_type_from_classification",
    "route_from_classification",
    "route_gmail_message",
]
