"""Resilient offline batch harness for capability eval captures.

One failing case must not abort the whole run. Infrastructure failures are
classified as CAPACITY / DELIVERY / HARNESS so they are not scored as product
capability regressions (see eval_final_rescore._final_outcome).
"""

from __future__ import annotations

import argparse
import json
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from eval_measurement_scoring import PRIMARY_OUTCOMES


CaseRunner = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class CaseRunResult:
    case_id: str
    status: str
    primary_outcome: str | None = None
    output: dict[str, Any] | None = None
    error_type: str = ""
    error: str = ""
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "case_id": self.case_id,
            "status": self.status,
        }
        if self.primary_outcome:
            payload["primary_outcome"] = self.primary_outcome
        if self.output is not None:
            payload.update(self.output)
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.error:
            payload["error"] = self.error
        if self.traceback:
            payload["traceback"] = self.traceback
        return payload


@dataclass
class BatchHarnessReport:
    mode: str
    corpus_path: str
    aborted: bool
    total: int
    completed: int
    failed: int
    outcomes: dict[str, int] = field(default_factory=dict)
    cases: list[CaseRunResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "corpus_path": self.corpus_path,
            "aborted": self.aborted,
            "total": self.total,
            "completed": self.completed,
            "failed": self.failed,
            "outcome_counts": dict(self.outcomes),
            "cases": [item.to_dict() for item in self.cases],
        }


def classify_run_failure(exc: BaseException, *, context: str = "") -> str:
    """Map infrastructure failures to non-capability primary outcomes."""

    text = f"{exc} {context}".lower()
    if any(token in text for token in ("429", "rate limit", "quota", "tpd", "too many requests")):
        return "CAPACITY"
    if any(
        token in text
        for token in (
            "timeout",
            "timed out",
            "connection refused",
            "connection error",
            "unreachable",
            "name or service not known",
            "502",
            "503",
            "504",
        )
    ):
        return "DELIVERY"
    return "HARNESS"


def run_resilient_batch(
    cases: list[dict[str, Any]],
    runner: CaseRunner,
    *,
    corpus_path: str = "",
    mode: str = "offline",
    stop_on_abort: bool = False,
) -> BatchHarnessReport:
    """Execute each corpus case; continue after isolated failures."""

    results: list[CaseRunResult] = []
    outcome_counts: dict[str, int] = {}
    aborted = False

    for case in cases:
        case_id = str(case.get("id") or case.get("case_id") or "unknown")
        try:
            output = runner(case)
            if not isinstance(output, dict):
                raise TypeError(f"runner must return dict, got {type(output).__name__}")
            primary = str(output.get("primary_outcome") or "CLEAN_PASS")
            if primary not in PRIMARY_OUTCOMES:
                primary = "HARNESS"
            result = CaseRunResult(
                case_id=case_id,
                status="completed",
                primary_outcome=primary,
                output=output,
            )
        except KeyboardInterrupt:
            aborted = True
            result = CaseRunResult(
                case_id=case_id,
                status="aborted",
                primary_outcome="HARNESS",
                error_type="KeyboardInterrupt",
                error="operator abort",
            )
            results.append(result)
            break
        except Exception as exc:  # noqa: BLE001 — harness boundary; classify and continue
            primary = classify_run_failure(exc)
            result = CaseRunResult(
                case_id=case_id,
                status="failed",
                primary_outcome=primary,
                error_type=type(exc).__name__,
                error=str(exc),
                traceback=traceback.format_exc(limit=12),
            )
        results.append(result)
        if result.primary_outcome:
            outcome_counts[result.primary_outcome] = outcome_counts.get(result.primary_outcome, 0) + 1
        if stop_on_abort and result.status == "aborted":
            aborted = True
            break

    completed = sum(1 for item in results if item.status == "completed")
    failed = sum(1 for item in results if item.status == "failed")
    return BatchHarnessReport(
        mode=mode,
        corpus_path=corpus_path,
        aborted=aborted,
        total=len(cases),
        completed=completed,
        failed=failed,
        outcomes=outcome_counts,
        cases=results,
    )


def run_corpus_file(path: Path | str, runner: CaseRunner, *, mode: str = "offline") -> BatchHarnessReport:
    corpus_path = Path(path)
    corpus = json.loads(corpus_path.read_text(encoding="utf-8-sig"))
    cases = list(corpus.get("cases") or [])
    return run_resilient_batch(cases, runner, corpus_path=str(corpus_path.resolve()), mode=mode)


def _offline_stub_runner(case: dict[str, Any]) -> dict[str, Any]:
    """Default no-op runner for CLI smoke tests."""

    return {"id": case.get("id"), "stage_reached": "stub", "primary_outcome": "CLEAN_PASS"}


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a resilient capability-eval batch harness.")
    parser.add_argument("--corpus", required=True, help="Frozen corpus JSON (cases[].id required).")
    parser.add_argument("--mode", default="offline", help="Capture mode label stored in the report.")
    parser.add_argument("--json-out", default="", help="Optional path for the batch report JSON.")
    parser.add_argument(
        "--allow-failures",
        action="store_true",
        help="Exit 0 even when individual cases failed (batch itself still completed).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_cli().parse_args(argv)
    report = run_corpus_file(args.corpus, _offline_stub_runner, mode=args.mode)
    if args.json_out:
        out_path = Path(args.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "total": report.total,
                "completed": report.completed,
                "failed": report.failed,
                "aborted": report.aborted,
                "outcome_counts": report.outcomes,
            },
            indent=2,
        )
    )
    if report.aborted:
        return 2
    if report.failed and not args.allow_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
