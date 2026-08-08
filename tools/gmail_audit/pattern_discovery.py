"""Pattern discovery — porownuje LLM extraction z regex extraction, proponuje nowe wzorce.

Gdy LLM wyciaga fakt ktory regex przegapil, system automatycznie proponuje nowy regex.
"""
from __future__ import annotations

from collections import Counter
from typing import Any
from _protocols import DatabaseConnection

from divergence_loop import CANDIDATE_PATTERN


class PatternDiscovery:
    """Wykrywa wzorce ktore regex przegapil, a LLM znalazl."""

    def __init__(self, conn: DatabaseConnection) -> None:
        self.conn = conn

    def find_regex_gaps(self, limit: int = 100) -> list[dict[str, str]]:
        """Znajdz przypadki gdzie LLM znalazl fakt, a regex nie."""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT f.case_id, f.fact_key, f.normalized_value
                FROM mailbox_memory_facts f
                WHERE f.source_type = 'llm'
                  AND COALESCE(f.status, 'active') <> 'superseded'
                  AND NOT EXISTS (
                      SELECT 1 FROM mailbox_memory_facts f2
                      WHERE f2.case_id = f.case_id
                        AND f2.fact_key = f.fact_key
                        AND f2.source_type = 'regex'
                  )
                LIMIT %s
            """, (limit,))
            return [{"case_id": str(r[0]), "fact_key": str(r[1]), "value": str(r[2])} for r in cur.fetchall() or []]

    def suggest_pattern(self, values: list[str]) -> str | None:
        """Na podstawie zbioru wartosci, zaproponuj regex pattern."""
        if len(values) < 3:
            return None
        common_prefix = _longest_common_prefix(values)
        common_suffix = _longest_common_suffix(values)
        if common_prefix and len(common_prefix) > 3:
            return f"{_re_escape(common_prefix)}(.+?)"
        if common_suffix and len(common_suffix) > 3:
            return f"(.+?){_re_escape(common_suffix)}"
        return None

    def generate_proposal(
        self, fact_key: str, pattern: str, examples: list[str]
    ) -> dict[str, Any]:
        """Generuje kandydata na nowy regex."""
        return {
            "fact_key": fact_key,
            "proposed_pattern": pattern,
            "supporting_examples": examples[:5],
            "confidence": min(1.0, len(examples) / 10),
            "status": CANDIDATE_PATTERN,
        }

    def run_discovery(self) -> list[dict[str, Any]]:
        """Glowna petla: znajdz luki, zaproponuj patterny, zwroc kandydatow."""
        gaps = self.find_regex_gaps()
        by_key: dict[str, list[str]] = {}
        for g in gaps:
            by_key.setdefault(g["fact_key"], []).append(g["value"])
        proposals = []
        for fact_key, values in by_key.items():
            pattern = self.suggest_pattern(values)
            if pattern:
                proposals.append(self.generate_proposal(fact_key, pattern, values))
        return proposals


def _longest_common_prefix(strings: list[str]) -> str:
    if not strings:
        return ""
    shortest = min(strings, key=len)
    for i, ch in enumerate(shortest):
        if any(s[i] != ch for s in strings):
            return shortest[:i]
    return shortest


def _longest_common_suffix(strings: list[str]) -> str:
    if not strings:
        return ""
    reversed_strs = [s[::-1] for s in strings]
    common_rev = _longest_common_prefix(reversed_strs)
    return common_rev[::-1]


def _re_escape(s: str) -> str:
    """Minimal re.escape — only special chars."""
    import re
    return re.escape(s)
