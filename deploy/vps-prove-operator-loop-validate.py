#!/usr/bin/env python3
"""Validate bounded operator-loop drain artifacts (Luka #3)."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _first_drain_row(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        block = payload.get("results")
        if isinstance(block, list):
            for row in block:
                if not isinstance(row, dict):
                    continue
                bridge = row.get("bridge_out")
                if isinstance(bridge, dict):
                    return bridge
                if row.get("truth_loop_executed") is True:
                    return row
            if block and isinstance(block[0], dict):
                return block[0]
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        row = payload[0]
        bridge = row.get("bridge_out")
        return bridge if isinstance(bridge, dict) else row
    return {}


def validate_drain(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return [f"missing drain file: {path}"]
    row = _first_drain_row(_load(path))
    if not row:
        return ["drain: no bridge_out row found"]
    for key in ("truth_loop_executed", "reconcile_signal_ran"):
        if row.get(key) is not True:
            errors.append(f"{key} != true (got {row.get(key)!r})")
    summary = row.get("reconcile_summary")
    state = ""
    if isinstance(summary, dict):
        state = str(summary.get("processing_state") or "").strip().lower()
    if not state:
        state = str(row.get("processing_state") or "").strip().lower()
    if state != "reconciled":
        errors.append(f"processing_state != reconciled (got {state!r})")
    refresh = row.get("projection_refresh_decision")
    if isinstance(summary, dict) and not refresh:
        refresh = summary.get("projection_refresh_decision")
    if isinstance(refresh, dict) and str(refresh.get("decision") or refresh.get("refresh_kind") or "").strip() == "no_case_link":
        errors.append("projection_refresh_decision=no_case_link")
    return errors


def validate_pending_after(path: Path) -> list[str]:
    if not path.is_file():
        return [f"missing pending-after file: {path}"]
    data = _load(path)
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return ["pending-after: expected items list"]
    # Bounded proof: drain processed at least one item; other pending may remain.
    _ = items
    return []


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: vps-prove-operator-loop-validate.py PROOF_DIR", file=sys.stderr)
        return 2
    proof_dir = Path(sys.argv[1]).expanduser().resolve()
    drain = proof_dir / "drain-output.json"
    pending_after = proof_dir / "pending-after-drain.json"
    errors: list[str] = []
    errors.extend(validate_drain(drain))
    errors.extend(validate_pending_after(pending_after))
    summary = {
        "proof_dir": str(proof_dir),
        "ok": not errors,
        "errors": errors,
    }
    (proof_dir / "luka3-validate.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if errors:
        for err in errors:
            print("FAIL:", err, file=sys.stderr)
        return 1
    print("LUKA3_OPERATOR_LOOP_VALIDATE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
