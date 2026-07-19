#!/usr/bin/env python3
"""Regenerate docs/contracts/engagement_snapshot_v2.schema.json from Pydantic."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm_contracts.engagement_snapshot_v2 import engagement_snapshot_v2_json_schema

OUT = ROOT.parents[1] / "docs" / "contracts" / "engagement_snapshot_v2.schema.json"


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(engagement_snapshot_v2_json_schema(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
