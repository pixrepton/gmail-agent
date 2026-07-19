"""Deterministic Drive-to-case linking using review artifacts and mailbox memory."""

from __future__ import annotations

import re
from typing import Any

from drive_ingest_models import DriveCaseLinkCandidate, DriveIngestCandidate
from case_family_boundary import filter_operational_feed_case_rows


CASE_TOKEN_RE = re.compile(r"\b(?:zam|zal|fv)[- /\d]+\b", re.IGNORECASE)
MODEL_TOKEN_RE = re.compile(r"\b(?:WH|KIT|PUZ|ERST|MAC|PAR|LG|H[A-Z]{1,4})[-A-Z0-9]{3,}\b")
ADDRESS_TOKEN_RE = re.compile(r"\b(?:ul\.?\s+[A-ZŻŹĆĄŚĘŁÓŃ][^,\n]+|\b[A-ZŻŹĆĄŚĘŁÓŃ][a-zżźćąśęłóń-]{2,}\s+\d+[A-Za-z]?)")


def link_drive_candidate(
    candidate: DriveIngestCandidate,
    *,
    extracted_facts: list[dict[str, Any]],
    store: Any,
) -> dict[str, Any]:
    raw_cases = filter_operational_feed_case_rows(
        list(getattr(store, "fetch_cases", lambda limit=200: [])(limit=200) or [])
    )
    case_snapshots: dict[str, dict[str, Any]] = {}
    for case in raw_cases:
        case_id = str((case or {}).get("case_id") or "").strip()
        if case_id:
            case_snapshots[case_id] = getattr(store, "fetch_snapshot", lambda _case_id: None)(case_id) or {}

    probable_case_key = str(candidate.probable_case_key or "").strip()
    deterministic = _deterministic_match(probable_case_key=probable_case_key, raw_cases=raw_cases)
    if deterministic is not None:
        return _result_from_candidate(deterministic, "deterministic", 0.99, ["probable_case_key_exact_match"])

    parent_folder_anchor = _parent_folder_anchor_match(candidate=candidate, store=store)
    if parent_folder_anchor is not None:
        return _pack_result(parent_folder_anchor, candidates=[parent_folder_anchor])

    tokens = collect_case_tokens(candidate, extracted_facts=extracted_facts)
    scored_candidates: list[DriveCaseLinkCandidate] = []
    for case in raw_cases:
        score, reasons, matched_facts = _score_case(case, case_snapshots.get(str(case.get("case_id") or ""), {}), tokens=tokens)
        if score <= 0:
            continue
        status = "inferred_high" if score >= 0.82 else "inferred_medium"
        scored_candidates.append(
            DriveCaseLinkCandidate(
                case_id=str(case.get("case_id") or "").strip(),
                case_key=str(case.get("case_key") or "").strip(),
                linkage_status=status,
                confidence=round(score, 4),
                reasons=reasons,
                matched_facts=matched_facts,
                metadata={"subject": str(case.get("subject") or "")},
            )
        )

    scored_candidates.sort(key=lambda item: item.confidence, reverse=True)
    if scored_candidates:
        best = scored_candidates[0]
        if best.confidence >= 0.82:
            return _pack_result(best, candidates=scored_candidates[:5])
        if best.confidence >= 0.58:
            best.linkage_status = "inferred_medium"
            return _pack_result(best, candidates=scored_candidates[:5])

    unresolved = DriveCaseLinkCandidate(
        case_id="",
        case_key=probable_case_key,
        linkage_status="unresolved_candidate",
        confidence=0.0,
        reasons=["no_confident_case_match"],
        matched_facts=[],
        metadata={"tokens": tokens},
    )
    return _pack_result(unresolved, candidates=scored_candidates[:5])


def collect_case_tokens(candidate: DriveIngestCandidate, *, extracted_facts: list[dict[str, Any]]) -> dict[str, set[str]]:
    token_map = {
        "case": set(),
        "model": set(),
        "address": set(),
        "customer": set(),
    }
    text_blob = " ".join(
        [
            candidate.title,
            candidate.folder_path,
            " ".join(str(fact.get("normalized_value") or "") for fact in extracted_facts),
        ]
    )
    if candidate.probable_case_key:
        token_map["case"].add(str(candidate.probable_case_key).strip().lower())
    for match in CASE_TOKEN_RE.finditer(text_blob):
        token_map["case"].add(_normalize_token(match.group(0)))
    for match in MODEL_TOKEN_RE.finditer(text_blob):
        token_map["model"].add(_normalize_token(match.group(0)))
    for match in ADDRESS_TOKEN_RE.finditer(text_blob):
        token_map["address"].add(_normalize_token(match.group(0)))
    for fact in extracted_facts:
        key = str(fact.get("fact_key") or "")
        value = str(fact.get("normalized_value") or "").strip()
        if not value:
            continue
        normalized = _normalize_token(value)
        if key in {"customer_name", "buyer_name"}:
            token_map["customer"].add(normalized)
        if key in {"investment_address", "installation_address", "city"}:
            token_map["address"].add(normalized)
        if key in {"device_model", "device_model_bundle", "model_bundle", "offer_family"}:
            token_map["model"].add(normalized)
        if key in {"order_number", "invoice_number", "linked_order_number", "reference_token"}:
            token_map["case"].add(normalized)
    return token_map


def _deterministic_match(*, probable_case_key: str, raw_cases: list[dict[str, Any]]) -> DriveCaseLinkCandidate | None:
    if not probable_case_key:
        return None
    target = probable_case_key.strip().lower()
    for case in raw_cases:
        case_key = str(case.get("case_key") or "").strip()
        if case_key and case_key.lower() == target:
            return DriveCaseLinkCandidate(
                case_id=str(case.get("case_id") or "").strip(),
                case_key=case_key,
                linkage_status="deterministic",
                confidence=0.99,
                reasons=["probable_case_key_exact_match"],
                matched_facts=["probable_case_key"],
            )
    return None


def _parent_folder_anchor_match(candidate: DriveIngestCandidate, *, store: Any) -> DriveCaseLinkCandidate | None:
    parent_drive_item_id = str(candidate.parent_drive_item_id or "").strip()
    if not parent_drive_item_id:
        return None

    anchored_rows: list[dict[str, Any]] = []
    fetch_drive_document_by_item_id = getattr(store, "fetch_drive_document_by_item_id", None)
    if callable(fetch_drive_document_by_item_id):
        parent_row = fetch_drive_document_by_item_id(parent_drive_item_id)
        if isinstance(parent_row, dict) and parent_row:
            anchored_rows.append(parent_row)

    fetch_drive_documents = getattr(store, "fetch_drive_documents", None)
    if callable(fetch_drive_documents):
        for row in list(fetch_drive_documents(limit=500) or []):
            if not isinstance(row, dict):
                continue
            if str(row.get("drive_item_id") or "").strip() == str(candidate.drive_item_id or "").strip():
                continue
            if str(row.get("parent_drive_item_id") or "").strip() != parent_drive_item_id:
                continue
            anchored_rows.append(row)

    case_ids = {
        str(row.get("case_id") or "").strip()
        for row in anchored_rows
        if str(row.get("case_id") or "").strip()
    }
    case_keys = {
        str(row.get("probable_case_key") or "").strip()
        for row in anchored_rows
        if str(row.get("probable_case_key") or "").strip()
    }

    if len(case_ids) == 1:
        case_id = next(iter(case_ids))
        case_key = _resolve_case_key_from_case_id(store=store, case_id=case_id)
        if not case_key and len(case_keys) == 1:
            case_key = next(iter(case_keys))
        return DriveCaseLinkCandidate(
            case_id=case_id,
            case_key=case_key,
            linkage_status="deterministic",
            confidence=0.97,
            reasons=["parent_folder_case_anchor"],
            matched_facts=["parent_drive_item_id"],
            metadata={"parent_drive_item_id": parent_drive_item_id},
        )

    if not case_ids and len(case_keys) == 1:
        return DriveCaseLinkCandidate(
            case_id="",
            case_key=next(iter(case_keys)),
            linkage_status="deterministic",
            confidence=0.95,
            reasons=["parent_folder_case_key_anchor"],
            matched_facts=["parent_drive_item_id"],
            metadata={"parent_drive_item_id": parent_drive_item_id},
        )

    return None


def _resolve_case_key_from_case_id(*, store: Any, case_id: str) -> str:
    fetch_case = getattr(store, "fetch_case", None)
    if not callable(fetch_case):
        return ""
    case_row = fetch_case(case_id)
    if not isinstance(case_row, dict):
        return ""
    return str(case_row.get("case_key") or "").strip()


def _score_case(case: dict[str, Any], snapshot_row: dict[str, Any], *, tokens: dict[str, set[str]]) -> tuple[float, list[str], list[str]]:
    reasons: list[str] = []
    matched_facts: list[str] = []
    score = 0.0

    case_blob = " ".join(
        part
        for part in (
            str(case.get("case_key") or ""),
            str(case.get("subject") or ""),
            str(case.get("customer_name") or ""),
            str(case.get("customer_email") or ""),
            str((case.get("metadata") or {}).get("installation_address") or ""),
            str((case.get("metadata") or {}).get("model_bundle") or ""),
        )
        if part
    ).lower()
    key_facts = snapshot_row.get("snapshot_json", snapshot_row)
    if isinstance(key_facts, dict):
        for fact in key_facts.get("key_facts") or []:
            value = str((fact or {}).get("value") or "").strip()
            if value:
                case_blob += " " + value.lower()
        for document in key_facts.get("latest_documents") or []:
            summary_text = str((document or {}).get("summary_text") or "").strip()
            if summary_text:
                case_blob += " " + summary_text.lower()
        for document in key_facts.get("reference_documents") or []:
            summary_text = str((document or {}).get("summary_text") or "").strip()
            if summary_text:
                case_blob += " " + summary_text.lower()

    case_tokens = {_normalize_token(case_blob)}
    if any(case_key in case_blob for case_key in tokens["case"] if case_key):
        score += 0.92
        reasons.append("case_identifier_overlap")
        matched_facts.append("case")

    address_overlap = [value for value in tokens["address"] if value and value in case_blob]
    if address_overlap:
        score += 0.48
        reasons.append("address_overlap")
        matched_facts.append("address")

    model_overlap = [value for value in tokens["model"] if value and value in case_blob]
    if model_overlap:
        score += 0.44
        reasons.append("model_overlap")
        matched_facts.append("model")

    customer_overlap = [value for value in tokens["customer"] if value and value in case_blob]
    if customer_overlap:
        score += 0.26
        reasons.append("customer_overlap")
        matched_facts.append("customer")

    if address_overlap and model_overlap:
        score += 0.12
        reasons.append("address_and_model_pair")

    if not address_overlap and "case_identifier_overlap" not in reasons and score >= 0.58:
        score = min(score, 0.57)
        reasons.append("needs_address_or_case_anchor")

    if not reasons and any(token for token in tokens["case"] if token and token in " ".join(case_tokens)):
        score += 0.5
        reasons.append("soft_case_token_overlap")
        matched_facts.append("case")

    return min(score, 0.99), reasons, matched_facts


def _result_from_candidate(candidate: DriveCaseLinkCandidate, linkage_status: str, confidence: float, reasons: list[str]) -> dict[str, Any]:
    candidate.linkage_status = linkage_status
    candidate.confidence = confidence
    candidate.reasons = reasons
    return _pack_result(candidate, candidates=[candidate])


def _pack_result(best: DriveCaseLinkCandidate, *, candidates: list[DriveCaseLinkCandidate]) -> dict[str, Any]:
    return {
        "case_id": best.case_id,
        "case_key": best.case_key,
        "linkage_status": best.linkage_status,
        "confidence": round(best.confidence, 4),
        "reasons": list(best.reasons),
        "matched_facts": list(best.matched_facts),
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


def _normalize_token(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


__all__ = ["collect_case_tokens", "link_drive_candidate"]
