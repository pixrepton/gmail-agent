"""Central context assembly for TOP-INSTAL LLM calls."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from intake_payload import _compact_inline_text, _smart_truncate
from redaction import sanitize_for_storage
from log_config import get_logger

logger = get_logger("context_assembler")

CaseContextLoader = Callable[[str, str, int], tuple[dict[str, Any], list[dict[str, Any]]]]


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class ContextBudgetLimits:
    """Caps assembled context size before LLM calls (prevents Groq 413)."""

    max_company_chars: int = 4500
    max_chunks: int = 4
    max_chunk_chars: int = 700
    max_facts: int = 24
    max_fact_value_chars: int = 180
    max_context_tokens: int = 5000

    @classmethod
    def from_env(cls, *, stage_name: str = "") -> ContextBudgetLimits:
        stage = str(stage_name or "").strip().lower()
        prefix = "CONTEXT_ASSEMBLER_"
        stage_prefix = f"{prefix}{stage.upper()}_" if stage else prefix
        defaults = cls()
        if stage == "business_reasoning":
            defaults = cls(max_chunks=3, max_chunk_chars=600, max_context_tokens=4200)
        elif stage == "reply_drafter":
            defaults = cls(
                max_company_chars=2200,
                max_chunks=1,
                max_chunk_chars=450,
                max_facts=16,
                max_fact_value_chars=140,
                max_context_tokens=2600,
            )
        return cls(
            max_company_chars=_env_int(f"{stage_prefix}MAX_COMPANY_CHARS", defaults.max_company_chars),
            max_chunks=_env_int(f"{stage_prefix}MAX_CHUNKS", defaults.max_chunks),
            max_chunk_chars=_env_int(f"{stage_prefix}MAX_CHUNK_CHARS", defaults.max_chunk_chars),
            max_facts=_env_int(f"{stage_prefix}MAX_FACTS", defaults.max_facts),
            max_fact_value_chars=_env_int(f"{stage_prefix}MAX_FACT_VALUE_CHARS", defaults.max_fact_value_chars),
            max_context_tokens=_env_int(f"{stage_prefix}MAX_CONTEXT_TOKENS", defaults.max_context_tokens),
        )


def estimate_context_tokens(text: str) -> int:
    """Fast token estimate for budget enforcement (word-based, conservative)."""
    stripped = str(text or "").strip()
    if not stripped:
        return 0
    words = len(stripped.split())
    return max(1, words, len(stripped) // 4)


def _chunk_score(chunk: dict[str, Any]) -> float:
    for key in ("retrieval_score", "final_score", "combined_score", "rerank_score", "score"):
        value = chunk.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return 0.0


def _chunk_text_value(chunk: dict[str, Any]) -> str:
    for key in ("chunk_text", "text", "content", "body"):
        value = chunk.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _trim_chunk_for_budget(chunk: dict[str, Any], *, max_chunk_chars: int) -> dict[str, Any]:
    trimmed = dict(chunk)
    text = _chunk_text_value(trimmed)
    if len(text) > max_chunk_chars:
        compact = _smart_truncate(text, max_chunk_chars)
        for key in ("chunk_text", "text", "content", "body"):
            if key in trimmed and isinstance(trimmed.get(key), str):
                trimmed[key] = compact
                break
        else:
            trimmed["chunk_text"] = compact
    metadata = trimmed.get("metadata")
    if isinstance(metadata, dict) and len(json.dumps(metadata, ensure_ascii=False)) > 400:
        trimmed["metadata"] = {
            k: metadata[k]
            for k in ("source_type", "document_id", "chunk_id", "filename", "source_relpath")
            if k in metadata
        }
    trimmed.pop("retrieval_signals", None)
    return trimmed


def _trim_facts_for_budget(
    facts: dict[str, Any],
    *,
    max_facts: int,
    max_fact_value_chars: int,
) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for index, (key, value) in enumerate(facts.items()):
        if index >= max_facts:
            break
        if isinstance(value, str):
            trimmed[key] = _compact_inline_text(value, max_fact_value_chars)
        elif isinstance(value, (int, float, bool)) or value is None:
            trimmed[key] = value
        else:
            serialized = json.dumps(value, ensure_ascii=False)
            trimmed[key] = _compact_inline_text(serialized, max_fact_value_chars)
    return trimmed


def apply_context_token_budget(
    context: AssembledContext,
    *,
    limits: ContextBudgetLimits | None = None,
    stage_name: str = "",
    query_text: str = "",
) -> tuple[AssembledContext, dict[str, Any]]:
    """Trim assembled context to stay within LLM payload limits."""
    budget = limits or ContextBudgetLimits.from_env(stage_name=stage_name)
    _ = query_text

    original_company_chars = len(context.company_context)
    original_chunks = len(context.relevant_chunks)
    original_facts = len(context.case_facts)

    token_estimate_before = estimate_context_tokens(context.company_context)
    token_estimate_before += estimate_context_tokens(
        json.dumps(sanitize_for_storage(context.case_facts), ensure_ascii=False)
    )
    for chunk in context.relevant_chunks:
        if isinstance(chunk, dict):
            token_estimate_before += estimate_context_tokens(
                json.dumps(sanitize_for_storage(chunk), ensure_ascii=False)
            )

    company_context = _compact_inline_text(context.company_context, budget.max_company_chars)
    case_facts = _trim_facts_for_budget(
        context.case_facts,
        max_facts=budget.max_facts,
        max_fact_value_chars=budget.max_fact_value_chars,
    )

    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for chunk in context.relevant_chunks:
        if not isinstance(chunk, dict):
            continue
        chunk_id = str(chunk.get("chunk_id") or chunk.get("id") or "")
        dedupe_key = chunk_id or str(id(chunk))
        if dedupe_key in seen_ids:
            continue
        seen_ids.add(dedupe_key)
        deduped.append(chunk)
    deduped.sort(key=_chunk_score, reverse=True)

    selected_chunks: list[dict[str, Any]] = []
    token_sum = estimate_context_tokens(company_context)
    token_sum += estimate_context_tokens(json.dumps(case_facts, ensure_ascii=False))

    for chunk in deduped:
        if len(selected_chunks) >= budget.max_chunks:
            break
        candidate = _trim_chunk_for_budget(chunk, max_chunk_chars=budget.max_chunk_chars)
        chunk_tokens = estimate_context_tokens(
            json.dumps(sanitize_for_storage(candidate), ensure_ascii=False)
        )
        if selected_chunks and (token_sum + chunk_tokens) > budget.max_context_tokens:
            continue
        selected_chunks.append(candidate)
        token_sum += chunk_tokens

    if not selected_chunks and deduped:
        fallback = _trim_chunk_for_budget(deduped[0], max_chunk_chars=budget.max_chunk_chars)
        selected_chunks = [fallback]
        token_sum = estimate_context_tokens(company_context) + estimate_context_tokens(
            json.dumps(case_facts, ensure_ascii=False)
        ) + estimate_context_tokens(json.dumps(sanitize_for_storage(fallback), ensure_ascii=False))

    budget_meta = {
        "applied": True,
        "stage_name": str(stage_name or "").strip(),
        "limits": asdict(budget),
        "token_estimate_before": token_estimate_before,
        "token_estimate": token_sum,
        "dropped_chunks": max(0, original_chunks - len(selected_chunks)),
        "dropped_facts": max(0, original_facts - len(case_facts)),
        "company_context_trimmed": original_company_chars > len(company_context),
        "kept_chunks": len(selected_chunks),
        "kept_facts": len(case_facts),
    }

    return (
        replace(
            context,
            company_context=company_context,
            case_facts=case_facts,
            relevant_chunks=selected_chunks,
            chunks_count=len(selected_chunks),
            facts_count=len(case_facts),
        ),
        budget_meta,
    )


@dataclass(slots=True)
class AssembledContext:
    version: str = "1.0"
    company_context: str = ""
    case_facts: dict[str, Any] = field(default_factory=dict)
    relevant_chunks: list[dict[str, Any]] = field(default_factory=list)
    engagement_id: str = ""
    assembled_at: str = ""
    chunks_count: int = 0
    facts_count: int = 0
    case_id_used: str = ""


def default_company_context_path() -> Path:
    """Resolve company_context.md: TOPINSTAL_COMPANY_CONTEXT_PATH or module data/."""
    override = str(os.environ.get("TOPINSTAL_COMPANY_CONTEXT_PATH", "") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "data" / "company_context.md"


_FACT_PROVENANCE_MAX_CHARS = 200


def _fact_provenance_suffix(row: dict[str, Any]) -> str:
    """SLICE-1 (B3): compact provenance for one fact, or '' when none exists.

    Only parts actually present on the row are rendered — nothing is invented. The deterministic
    projection (`understanding_output._prior_known_state_rows`) already reads `confidence`,
    `source_ref` and `observed_at` off these same rows; before this change the model deciding the
    next action knew strictly less than the projection describing that decision.
    """
    parts: list[str] = []
    raw_conf = row.get("confidence")
    if raw_conf not in (None, ""):
        try:
            parts.append(f"conf {float(raw_conf):.2g}")
        except (TypeError, ValueError):
            pass
    source_ref = str(row.get("source_ref") or "").strip()
    if source_ref:
        parts.append(f"src: {source_ref[:60]}")
    observed_at = str(row.get("observed_at") or "").strip()
    if observed_at:
        parts.append(f"seen {observed_at[:10]}")
    return f" ({', '.join(parts)})" if parts else ""


def _facts_dict_from_active_facts(active_facts: list[dict[str, Any]]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for row in active_facts:
        if not isinstance(row, dict):
            continue
        key = str(row.get("fact_key") or row.get("key") or "").strip()
        if not key:
            continue
        value = row.get("value")
        if value is None:
            value = row.get("normalized_value")
        suffix = _fact_provenance_suffix(row)
        if suffix:
            # keep the value intact and bounded; provenance never displaces the value
            facts[key] = f"{value}{suffix}"[:_FACT_PROVENANCE_MAX_CHARS]
        else:
            facts[key] = value
    return facts


class ContextAssembler:
    """Build deterministic LLM context: company profile + optional case memory."""

    def __init__(
        self,
        *,
        company_context_path: Path | None = None,
        case_loader: CaseContextLoader | None = None,
    ) -> None:
        self._company_context_path = company_context_path or default_company_context_path()
        self._case_loader = case_loader

    def _read_company_context(self) -> str:
        path = self._company_context_path
        if not path.is_file():
            logger.warning("COMPANY_CONTEXT_NOT_FOUND", extra={"x": {"path": str(path)}})
            raise FileNotFoundError(f"company_context not found: {path}")
        return path.read_text(encoding="utf-8").strip()

    def assemble(
        self,
        query_text: str,
        *,
        case_id: str | None = None,
        engagement_id: str | None = None,
        max_chunks: int = 6,
    ) -> AssembledContext:
        company_context = self._read_company_context()
        case_facts: dict[str, Any] = {}
        relevant_chunks: list[dict[str, Any]] = []
        case_id_used = ""

        normalized_case_id = str(case_id or "").strip()
        if normalized_case_id and self._case_loader is not None:
            case_id_used = normalized_case_id
            loaded_facts, loaded_chunks = self._case_loader(
                normalized_case_id,
                str(query_text or ""),
                max(1, int(max_chunks)),
            )
            if isinstance(loaded_facts, dict):
                case_facts = loaded_facts
            elif isinstance(loaded_facts, list):
                case_facts = _facts_dict_from_active_facts(loaded_facts)
            if isinstance(loaded_chunks, list):
                relevant_chunks = [c for c in loaded_chunks if isinstance(c, dict)][: max(1, int(max_chunks))]

        assembled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        logger.info("CONTEXT_ASSEMBLED", extra={"x": {
            "case_id": str(normalized_case_id or ""),
            "engagement_id": str(engagement_id or ""),
            "chunks_included": len(relevant_chunks),
            "facts_included": len(case_facts),
            "company_context_len": len(company_context),
        }})
        return AssembledContext(
            version="1.0",
            company_context=company_context,
            case_facts=case_facts,
            relevant_chunks=relevant_chunks,
            engagement_id=str(engagement_id or "").strip(),
            assembled_at=assembled_at,
            chunks_count=len(relevant_chunks),
            facts_count=len(case_facts),
            case_id_used=case_id_used,
        )

    def to_system_prompt(self, context: AssembledContext) -> str:
        """Render assembled context into a single system prompt string."""
        chunks_block = json.dumps(sanitize_for_storage(context.relevant_chunks), ensure_ascii=False)
        facts_block = json.dumps(sanitize_for_storage(context.case_facts), ensure_ascii=False)
        engagement_line = (
            f"Engagement ID: {context.engagement_id}\n" if context.engagement_id else ""
        )
        case_line = f"Case ID: {context.case_id_used}\n" if context.case_id_used else ""
        return (
            "You are TOP-INSTAL HVAC sales engineering assistant.\n"
            f"Context contract version: {context.version}\n"
            f"Assembled at (UTC): {context.assembled_at}\n"
            f"{engagement_line}"
            f"{case_line}"
            "\n"
            "## Company context\n"
            f"{context.company_context}\n"
            "\n"
            "## Case facts\n"
            f"{facts_block}\n"
            "\n"
            "## Relevant document chunks\n"
            f"{chunks_block}\n"
        )


def assembled_context_to_dict(
    context: AssembledContext,
    *,
    context_budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = asdict(context)
    if isinstance(context_budget, dict) and context_budget:
        payload["context_budget"] = context_budget
    return payload
