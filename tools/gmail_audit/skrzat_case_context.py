"""P1 — Skrzat operator path requires canonical CaseContextPack (vNext contract)."""

from __future__ import annotations

from typing import Any

PACK_BUILD_MARKERS = (
    "case_context_pack.vnext",
    "CaseContextPack",
)


def validate_operator_case_context_pack(contract: dict[str, Any]) -> dict[str, Any]:
    """Fail closed when pack contract is missing case identity or build marker."""
    if not isinstance(contract, dict):
        raise ValueError("case_context_pack contract must be a dict")
    case_id = str(contract.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("case_id required in CaseContextPack")
    pack_build = str(contract.get("pack_build") or contract.get("contract_version") or "").strip()
    contract_name = str(contract.get("contract_name") or "").strip()
    if not pack_build and contract_name not in PACK_BUILD_MARKERS:
        raise ValueError("pack_build or contract_name CaseContextPack required")
    return {
        "case_id": case_id,
        "pack_build": pack_build or contract_name,
        "contract_name": contract_name or "CaseContextPack",
        "generated_at": str(contract.get("generated_at") or "").strip(),
    }


def pack_lineage_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    validated = validate_operator_case_context_pack(contract)
    return {
        "source": "mailbox_memory_case_context_pack",
        "case_id": validated["case_id"],
        "pack_build": validated["pack_build"],
        "contract_name": validated["contract_name"],
        "generated_at": validated["generated_at"],
    }


__all__ = ["PACK_BUILD_MARKERS", "pack_lineage_from_contract", "validate_operator_case_context_pack"]
