"""P1+ — RAG advisory slice: company knowledge + case pack in one Skrzat response (D1)."""

from __future__ import annotations

from typing import Any


def build_rag_advisory_slice(
    assembled_context: dict[str, Any] | None,
    *,
    case_context_pack: dict[str, Any] | None = None,
    query: str = "",
) -> dict[str, Any]:
    """Summarize bounded RAG + case facts assembled for Skrzat (read-only, no offer math)."""
    ac = assembled_context if isinstance(assembled_context, dict) else {}
    pack = case_context_pack if isinstance(case_context_pack, dict) else {}
    chunks = ac.get("relevant_chunks") if isinstance(ac.get("relevant_chunks"), list) else []
    facts = ac.get("case_facts") if isinstance(ac.get("case_facts"), dict) else {}
    case_id = str(pack.get("case_id") or ac.get("case_id_used") or "").strip()
    pipeline_meta: dict[str, Any] = {}
    if query.strip():
        try:
            from core_chat.unified_pipeline import run_unified_rag_pipeline

            pipeline_meta = run_unified_rag_pipeline(query, case_context_pack=pack)
        except Exception:
            pipeline_meta = {}
    return {
        "schema_version": "case_os.rag_advisory_slice.v1",
        "source": "context_assembler",
        "case_id": case_id,
        "pack_build": str(pack.get("pack_build") or pack.get("contract_version") or "").strip(),
        "company_knowledge_chunks": len(chunks),
        "case_facts_count": len(facts),
        "has_company_context": bool(str(ac.get("company_context") or "").strip()),
        "engagement_id": str(ac.get("engagement_id") or "").strip(),
        "read_only": True,
        "boundary": "D1_advisory_only",
        "unified_pipeline": pipeline_meta,
    }


__all__ = ["build_rag_advisory_slice"]
