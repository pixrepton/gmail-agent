"""Synthetic regression harness for EntityLinker + identity hints (corpus JSON).

Corpus cases are mirrored from unit tests — not production proof. See
docs/archive/runbooks/ENTITY_LINK_REGRESSION_HARNESS.md.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from entity_linker import EntityLinker, extract_identity_hints
from mailbox_memory_store import InMemoryMailboxMemoryStore
from signal_contract import build_canonical_signal


@dataclass
class CaseOutcome:
    case_id: str
    passed: bool
    detail: str = ""


@dataclass
class HarnessReport:
    corpus_path: str
    status: str
    total: int
    passed: int
    failed: int
    thresholds: dict[str, float] = field(default_factory=dict)
    quality_gates: dict[str, Any] = field(default_factory=dict)
    by_cohort: dict[str, dict[str, int]] = field(default_factory=dict)
    outcomes: list[CaseOutcome] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_path": self.corpus_path,
            "status": self.status,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "thresholds": dict(self.thresholds),
            "quality_gates": dict(self.quality_gates),
            "by_cohort": {key: dict(value) for key, value in self.by_cohort.items()},
            "outcomes": [
                {
                    "case_id": outcome.case_id,
                    "passed": outcome.passed,
                    "detail": outcome.detail,
                }
                for outcome in self.outcomes
            ],
            "misses": list(self.misses),
        }


def _build_store(fixture: dict[str, Any]) -> InMemoryMailboxMemoryStore:
    store = InMemoryMailboxMemoryStore()
    store.bootstrap()
    for row in fixture.get("cases") or []:
        store.upsert_case(
            {
                "case_id": row["case_id"],
                "case_key": row.get("case_key") or "",
                "thread_id": "",
                "case_family": "lead_opportunity",
                "mailbox": "test",
                "subject": "Test case",
                "status": "open",
                "customer_name": "",
                "customer_email": "",
                "metadata": row.get("metadata") or {},
            }
        )
        for fact in row.get("facts") or []:
            store.append_fact_rows([fact])
    return store


def _check_entity_link(
    case: dict[str, Any],
    thresholds: dict[str, float],
) -> tuple[bool, str]:
    store = _build_store(case["store_fixture"])
    payload = dict(case["signal_payload"])
    signal = build_canonical_signal(**payload)
    result = EntityLinker(store).find_case(signal)
    exp = case["expect"]
    if result.link_status != exp["link_status"]:
        return False, f"link_status want {exp['link_status']!r} got {result.link_status!r}"
    if result.phase != exp["phase"]:
        return False, f"phase want {exp['phase']!r} got {result.phase!r}"
    if "case_id" in exp and exp["case_id"] and result.case_id != exp["case_id"]:
        return False, f"case_id want {exp['case_id']!r} got {result.case_id!r}"
    if "case_key" in exp and exp.get("case_key") and result.case_key != exp["case_key"]:
        return False, f"case_key want {exp['case_key']!r} got {result.case_key!r}"
    min_c = exp.get("min_confidence")
    if min_c is not None and result.confidence < float(min_c):
        return False, f"confidence {result.confidence} < {min_c}"
    if "case_proposal_kind" in exp:
        kind = (result.case_proposal or {}).get("kind")
        if kind != exp["case_proposal_kind"]:
            return False, f"case_proposal.kind want {exp['case_proposal_kind']!r} got {kind!r}"
    min_fuzzy = thresholds.get("min_confidence_fuzzy", 0.85)
    if result.phase == "fuzzy" and result.link_status == "VERIFIED":
        if result.confidence < min_fuzzy:
            return False, f"fuzzy VERIFIED confidence {result.confidence} < threshold {min_fuzzy}"
    return True, "ok"


def _check_identity_hints(case: dict[str, Any]) -> tuple[bool, str]:
    hints = extract_identity_hints(case["payload_for_hints"])
    exp = case["expect"]
    needle_raw = exp.get("invoice_id_contains", "")
    if not needle_raw:
        return False, "expect.invoice_id_contains required for identity_hints"
    joined = "".join(hints.get("invoice_id", [])).upper().replace(" ", "")
    needle = needle_raw.upper().replace(" ", "")
    if needle not in joined:
        return False, f"invoice hints {joined!r} missing {needle!r}"
    return True, "ok"


def run_corpus_file(path: Path | str) -> HarnessReport:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    thresholds = dict(raw.get("thresholds") or {})
    quality_gates = dict(raw.get("quality_gates") or {})
    quality_gates.setdefault(
        "cohort_update_procedure",
        "Add or revise anonymized cohort rows only after operator review and evidence-backed misses.",
    )
    cases = raw.get("cases") or []
    outcomes: list[CaseOutcome] = []
    by_cohort: dict[str, dict[str, int]] = {}
    misses: list[str] = []
    passed_n = 0
    for case in cases:
        cid = str(case.get("id") or "unknown")
        cohort = str(case.get("cohort") or "default")
        if cohort not in by_cohort:
            by_cohort[cohort] = {"pass": 0, "fail": 0}
        kind = case.get("harness_kind") or "entity_link"
        if kind == "identity_hints":
            ok, detail = _check_identity_hints(case)
        else:
            ok, detail = _check_entity_link(case, thresholds)
        outcomes.append(CaseOutcome(case_id=cid, passed=ok, detail=detail))
        if ok:
            passed_n += 1
            by_cohort[cohort]["pass"] += 1
        else:
            misses.append(f"{cid}: {detail}")
            by_cohort[cohort]["fail"] += 1

    return HarnessReport(
        corpus_path=str(p.resolve()),
        status=raw.get("status", ""),
        total=len(cases),
        passed=passed_n,
        failed=len(cases) - passed_n,
        thresholds=thresholds,
        quality_gates=quality_gates,
        by_cohort=by_cohort,
        outcomes=outcomes,
        misses=misses,
    )


def default_corpus_path() -> Path:
    return Path(__file__).resolve().parent / "tests" / "fixtures" / "entity_link_regression_corpus.json"


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the entity-link regression harness.")
    parser.add_argument(
        "--corpus",
        default=str(default_corpus_path()),
        help="Path to a synthetic or anonymized cohort JSON corpus.",
    )
    parser.add_argument(
        "--json-out",
        default="",
        help="Optional path to write a JSON report for CI/operator review.",
    )
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit 0 even when the report contains misses.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    report = run_corpus_file(args.corpus)

    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    if report.failed:
        print("\n".join(report.misses))
        return 0 if args.allow_failures else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
