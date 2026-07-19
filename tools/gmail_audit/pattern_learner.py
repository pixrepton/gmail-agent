"""Pattern learner — porównuje LLM extraction z regex extraction, proponuje nowe wzorce.

Gdy LLM wyciąga fakt, który regex przegapił, system tworzy PatternCandidate
i zapisuje do learning_rule_candidates z status=CANDIDATE_PATTERN.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, NamedTuple

from _protocols import DatabaseConnection
from divergence_loop import CANDIDATE_PATTERN


class PatternCandidate(NamedTuple):
    """Kandydat na nowy regex pattern — porównanie LLM vs regex."""
    pattern_key: str
    llm_value: str
    regex_value: str | None
    confidence: float
    source_text: str


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def compare_llm_vs_regex(
    llm_facts: dict[str, Any],
    regex_facts: dict[str, Any],
) -> list[PatternCandidate]:
    """Porównuje fakty z LLM vs regex i zwraca kandydatów dla różnic.

    Args:
        llm_facts: Słownik faktów wykrytych przez LLM {fact_key: normalized_value}
        regex_facts: Słownik faktów wykrytych przez regex {fact_key: normalized_value}

    Returns:
        Lista PatternCandidate dla faktów, które LLM znalazł a regex nie,
        lub gdzie wartości znacząco się różnią.
    """
    candidates: list[PatternCandidate] = []

    for fact_key, llm_value in llm_facts.items():
        llm_str = str(llm_value or "").strip()
        if not llm_str:
            continue

        regex_value = regex_facts.get(fact_key)
        regex_str = str(regex_value or "").strip() if regex_value else ""

        if not regex_str:
            # LLM znalazł fakt, regex nie → silny kandydat
            confidence = min(1.0, 0.7 + (len(llm_str) / 200))
            candidates.append(PatternCandidate(
                pattern_key=fact_key,
                llm_value=llm_str,
                regex_value=None,
                confidence=round(confidence, 3),
                source_text=llm_str[:200],
            ))
        elif llm_str.lower() != regex_str.lower():
            # Wartości się różnią — słabszy kandydat
            confidence = 0.5
            candidates.append(PatternCandidate(
                pattern_key=fact_key,
                llm_value=llm_str,
                regex_value=regex_str,
                confidence=confidence,
                source_text=llm_str[:200],
            ))

    return candidates


def store_pattern_candidates(
    conn: DatabaseConnection,
    candidates: list[PatternCandidate],
) -> int:
    """Zapisuje PatternCandidate do learning_rule_candidates z status=CANDIDATE_PATTERN.

    Args:
        conn: Połączenie DB
        candidates: Lista kandydatów do zapisania

    Returns:
        Liczba zapisanych kandydatów
    """
    if not candidates:
        return 0

    now = datetime.now(timezone.utc)
    stored = 0

    # Note: bare `with conn.cursor()`, not `with conn:` -- the caller (maybe_create_learning_candidate,
    # via _run_pattern_learner) does not own this connection and still uses it afterward.
    with conn.cursor() as cur:
        for cand in candidates:
            cid = _new_id("pat")
            rule_text = (
                f"Pattern learner: LLM wykrył «{cand.pattern_key}» = "
                f"«{cand.llm_value[:100]}»"
            )
            if cand.regex_value:
                rule_text += (
                    f" (regex: «{cand.regex_value[:100]}»)"
                )

            metadata = {
                "llm_value": cand.llm_value,
                "regex_value": cand.regex_value,
                "confidence": cand.confidence,
                "source_text": cand.source_text[:500],
                "source": "pattern_learner",
            }

            cur.execute(
                """
                INSERT INTO learning_rule_candidates (
                    candidate_id, pattern_key, rule_text_pl, supporting_count,
                    status, case_family, proposal_type, created_at, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (candidate_id) DO NOTHING
                """,
                (
                    cid,
                    cand.pattern_key,
                    rule_text,
                    1,
                    CANDIDATE_PATTERN,
                    "pattern_learner",
                    cand.pattern_key,
                    now,
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            stored += 1
    conn.commit()

    return stored
