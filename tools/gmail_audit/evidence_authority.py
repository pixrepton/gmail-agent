"""Data vs Authority provenance contract (AI-OS Intelligence Spine P0.5).

Central principle:

    UNTRUSTED AS AUTHORITY != UNTRUSTED AS INFORMATION

External content (customer mail, quoted/forwarded content, attachments, RAG
evidence, external-data-derived tool results) is a legitimate source of
information about the world and may influence BusinessReasoning and a future
CanonicalActionDecision. It may NOT establish or override runtime authority,
policy authority, execution authority, approval, tool availability, or
protected execution arguments.

Three INDEPENDENT dimensions (deliberately not a single flat trust class):

* source_origin        - where the content came from;
* evidence_authority   - how strong the content is as a statement about the
                         world;
* instruction_authority- whether the content may issue runtime instructions.

This module is the ONE common provenance/authority seam for email, quoted and
forwarded content, attachments, RAG evidence and tool-result provenance.
Enforcement lives in the existing runtime boundary (untrusted_input_boundary +
reference monitor); this module only classifies and tags.
"""

from __future__ import annotations

from typing import Any, Mapping

SOURCE_ORIGINS = (
    "SYSTEM",
    "OPERATOR",
    "CUSTOMER_EMAIL",
    "QUOTED_CONTENT",
    "FORWARDED_CONTENT",
    "ATTACHMENT",
    "RAG",
    "TOOL_RESULT",
    "INTERNAL_STATE",
    "DERIVED",
    "UNKNOWN",
)

EVIDENCE_AUTHORITIES = (
    "INTERNAL_SOT",
    "OPERATOR_STATEMENT",
    "CUSTOMER_STATEMENT",
    "AUTHORITATIVE_DOCUMENT",
    "CUSTOMER_DOCUMENT",
    "DERIVED_LLM_CLAIM",
    "UNKNOWN",
)

INSTRUCTION_AUTHORITIES = ("NONE", "OPERATOR", "SYSTEM")

# Origins whose content is information about the world, never runtime
# instructions, without an explicit trusted transition.
EXTERNAL_ORIGINS = frozenset(
    {
        "CUSTOMER_EMAIL",
        "QUOTED_CONTENT",
        "FORWARDED_CONTENT",
        "ATTACHMENT",
        "RAG",
        "TOOL_RESULT",
        "DERIVED",
        "UNKNOWN",
    }
)

_EVIDENCE_BY_ORIGIN: dict[str, str] = {
    "SYSTEM": "INTERNAL_SOT",
    "OPERATOR": "OPERATOR_STATEMENT",
    "CUSTOMER_EMAIL": "CUSTOMER_STATEMENT",
    "QUOTED_CONTENT": "CUSTOMER_STATEMENT",
    "FORWARDED_CONTENT": "CUSTOMER_STATEMENT",
    "ATTACHMENT": "CUSTOMER_DOCUMENT",
    "RAG": "AUTHORITATIVE_DOCUMENT",
    "TOOL_RESULT": "DERIVED_LLM_CLAIM",
    "INTERNAL_STATE": "INTERNAL_SOT",
    "DERIVED": "DERIVED_LLM_CLAIM",
    "UNKNOWN": "UNKNOWN",
}

_INSTRUCTION_BY_ORIGIN: dict[str, str] = {
    "SYSTEM": "SYSTEM",
    "OPERATOR": "OPERATOR",
    "INTERNAL_STATE": "SYSTEM",
}

_OPERATOR_KINDS = frozenset(
    {
        "operator",
        "operator_command",
        "operator_signal",
        "operator_instruction",
    }
)
_SYSTEM_KINDS = frozenset({"system", "system_signal", "internal"})
_RAG_KINDS = frozenset({"rag", "rag_evidence"})
_TOOL_RESULT_KINDS = frozenset({"tool_result", "tool_result_observed"})
_ATTACHMENT_KINDS = frozenset(
    {
        "gmail_attachment",
        "gmail_attachment_observed",
        "mail_attachment",
        "attachment",
        "drive",
        "drive_document",
    }
)
_EMAIL_KINDS = frozenset(
    {
        "gmail",
        "gmail_inbound",
        "gmail_message",
        "gmail_message_observed",
        "gmail_thread_update_observed",
        "mail",
    }
)


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


def classify_source_origin(payload: Mapping[str, Any] | None) -> str:
    """Classify the origin of a content payload (intake, evidence, tool result)."""
    payload = payload if isinstance(payload, Mapping) else {}
    kind = _norm(payload.get("source_kind") or payload.get("signal_kind"))
    part = _norm(payload.get("content_part") or payload.get("evidence_part"))
    produced_by = _norm(payload.get("produced_by"))

    if payload.get("operator_instruction") is True or kind in _OPERATOR_KINDS:
        return "OPERATOR"
    if payload.get("system_instruction") is True or kind in _SYSTEM_KINDS:
        return "SYSTEM"
    if part == "quoted" or payload.get("quoted_content") is True:
        return "QUOTED_CONTENT"
    if part == "forwarded" or payload.get("forwarded_content") is True:
        return "FORWARDED_CONTENT"
    if kind in _ATTACHMENT_KINDS or payload.get("attachment_id"):
        return "ATTACHMENT"
    # Explicit evidence identity wins over the producing mechanism: a RAG
    # fragment of a customer document keeps CUSTOMER_DOCUMENT provenance, not
    # a generic RAG label.
    if kind in _RAG_KINDS or produced_by in {"rag_retriever", "search_rag_knowledge"}:
        return "RAG"
    if kind in _TOOL_RESULT_KINDS or produced_by:
        return "TOOL_RESULT"
    if kind in _EMAIL_KINDS:
        return "CUSTOMER_EMAIL"
    if kind or payload:
        return "DERIVED"
    return "UNKNOWN"


def evidence_authority_for_origin(origin: str) -> str:
    return _EVIDENCE_BY_ORIGIN.get(str(origin or "").strip().upper(), "UNKNOWN")


def instruction_authority_for_origin(origin: str) -> str:
    return _INSTRUCTION_BY_ORIGIN.get(str(origin or "").strip().upper(), "NONE")


def is_external_origin(origin: str) -> bool:
    return str(origin or "").strip().upper() in EXTERNAL_ORIGINS


def provenance_classification(
    payload: Mapping[str, Any] | None,
    *,
    produced_by: str = "",
) -> dict[str, str]:
    """Three-dimension classification for one content payload."""
    payload = payload if isinstance(payload, Mapping) else {}
    produced = str(produced_by or "").strip()
    origin = classify_source_origin(payload)
    if produced and origin in {"DERIVED", "UNKNOWN"}:
        origin = "TOOL_RESULT"
    return {
        "source_origin": origin,
        "evidence_authority": evidence_authority_for_origin(origin),
        "instruction_authority": instruction_authority_for_origin(origin),
        "produced_by": produced,
    }


def attach_evidence_provenance(
    record: dict[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
    produced_by: str = "",
) -> dict[str, Any]:
    """Attach the three provenance dimensions to an evidence/tool-result record.

    Never overwrites existing explicit values. Trusted transitions (operator or
    system origin) must be stamped by the trusted caller, never inferred from
    external text.
    """
    if not isinstance(record, dict):
        return record
    out = dict(record)
    explicit_origin = str(out.get("source_origin") or "").strip().upper()
    if explicit_origin:
        # A trusted caller explicitly stamped the origin: keep the trio
        # internally consistent instead of inferring from the payload.
        if not str(out.get("evidence_authority") or "").strip():
            out["evidence_authority"] = evidence_authority_for_origin(
                explicit_origin
            )
        if not str(out.get("instruction_authority") or "").strip():
            out["instruction_authority"] = instruction_authority_for_origin(
                explicit_origin
            )
        return out
    current = provenance_classification(payload, produced_by=produced_by)
    for key in ("source_origin", "evidence_authority", "instruction_authority"):
        if not str(out.get(key) or "").strip():
            out[key] = current[key]
    if not str(out.get("produced_by") or "").strip():
        out["produced_by"] = current["produced_by"]
    return out


def provenance_defaults(*, origin: str = "DERIVED") -> dict[str, str]:
    """Safe provenance trio for a known origin (storage/metadata stamping)."""
    origin = str(origin or "").strip().upper() or "DERIVED"
    return {
        "source_origin": origin,
        "evidence_authority": evidence_authority_for_origin(origin),
        "instruction_authority": instruction_authority_for_origin(origin),
    }


def ensure_provenance_defaults(
    record: dict[str, Any],
    *,
    default_origin: str = "DERIVED",
) -> dict[str, Any]:
    """Read-time normalization: fill missing provenance dims with safe defaults.

    Legacy records without provenance metadata get:

        source_origin        = default_origin (or DERIVED)
        evidence_authority   = UNKNOWN (never guessed higher)
        instruction_authority= NONE      (never guessed higher)

    Existing explicit values are never overwritten or upgraded.
    """
    out = dict(record) if isinstance(record, dict) else {}
    explicit_origin = str(out.get("source_origin") or "").strip().upper()
    if explicit_origin:
        if not str(out.get("evidence_authority") or "").strip():
            out["evidence_authority"] = evidence_authority_for_origin(
                explicit_origin
            )
        if not str(out.get("instruction_authority") or "").strip():
            out["instruction_authority"] = instruction_authority_for_origin(
                explicit_origin
            )
        return out
    if not str(out.get("source_origin") or "").strip():
        out["source_origin"] = str(default_origin or "").strip().upper() or "DERIVED"
    if not str(out.get("evidence_authority") or "").strip():
        out["evidence_authority"] = "UNKNOWN"
    if not str(out.get("instruction_authority") or "").strip():
        out["instruction_authority"] = "NONE"
    return out


__all__ = [
    "EVIDENCE_AUTHORITIES",
    "EXTERNAL_ORIGINS",
    "INSTRUCTION_AUTHORITIES",
    "SOURCE_ORIGINS",
    "attach_evidence_provenance",
    "classify_source_origin",
    "ensure_provenance_defaults",
    "evidence_authority_for_origin",
    "instruction_authority_for_origin",
    "is_external_origin",
    "provenance_defaults",
    "provenance_classification",
]
