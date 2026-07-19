"""Mem0-style memory consolidation worker (MAX-STACK W3)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, List


@dataclass
class ConsolidatedFact:
    fact_key: str
    value: str
    confidence: float = 0.7
    source_refs: List[str] = field(default_factory=list)


def extract_facts_from_turns(turns: List[dict[str, Any]], *, max_facts: int = 10) -> List[ConsolidatedFact]:
    """Deterministic extract v1 — LLM structured output hook reserved."""
    facts: list[ConsolidatedFact] = []
    for turn in turns or []:
        summary = str(turn.get("turn_summary_pl") or turn.get("summary_pl") or "").strip()
        tool = str(turn.get("tool_name") or "").strip()
        if not summary:
            continue
        facts.append(
            ConsolidatedFact(
                fact_key=f"turn:{tool or 'unknown'}",
                value=summary[:500],
                confidence=0.65,
                source_refs=[str(turn.get("trace_id") or "")],
            )
        )
        if len(facts) >= max_facts:
            break
    return facts


def dedupe_facts(existing: List[ConsolidatedFact], incoming: List[ConsolidatedFact]) -> List[ConsolidatedFact]:
    seen = {(f.fact_key, f.value.strip().lower()) for f in existing}
    out = list(existing)
    for fact in incoming:
        key = (fact.fact_key, fact.value.strip().lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(fact)
    return out


def consolidate_engagement_turns(
    turns: List[dict[str, Any]],
    *,
    existing_facts: List[ConsolidatedFact] | None = None,
) -> List[ConsolidatedFact]:
    extracted = extract_facts_from_turns(turns)
    return dedupe_facts(list(existing_facts or []), extracted)


def facts_to_recall_text(facts: List[ConsolidatedFact], *, max_chars: int = 1700) -> str:
    lines: list[str] = []
    used = 0
    for f in facts:
        line = f"- {f.fact_key}: {f.value}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


__all__ = [
    "ConsolidatedFact",
    "consolidate_engagement_turns",
    "dedupe_facts",
    "extract_facts_from_turns",
    "facts_to_recall_text",
]
