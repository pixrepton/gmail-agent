"""Light fixture runner for Gmail Intake v2 shadow contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


TOOL_DIR = Path(__file__).resolve().parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from tests.fixture_helpers import assert_fixture_expectations, fixture_names, run_fixture


def main() -> int:
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []

    for name in fixture_names():
        try:
            result = run_fixture(name)
            assert_fixture_expectations(result)
            results.append(
                {
                    "fixture": name,
                    "lane": result["preclassification"]["lane"],
                    "case_link": result["case_link_result"]["decision"],
                    "primary_action": result["action_plan"]["primary_action"],
                    "projection_mode": result["action_plan"]["daszek_projection_mode"],
                }
            )
        except Exception as exc:  # noqa: BLE001 - fixture runner should report all failures
            failures.append({"fixture": name, "error": str(exc)})

    summary = {
        "fixtures_compared": len(results) + len(failures),
        "fixtures_passed": len(results),
        "fixtures_failed": len(failures),
        "results": results,
        "failures": failures,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
