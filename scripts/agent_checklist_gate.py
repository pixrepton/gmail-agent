"""Compatibility wrapper for the gmail_audit agent checklist gate."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


def _load_impl() -> ModuleType:
    impl_path = Path(__file__).resolve().parents[1] / "tools" / "gmail_audit" / "scripts" / "agent_checklist_gate.py"
    spec = importlib.util.spec_from_file_location("_gmail_audit_agent_checklist_gate", impl_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load agent checklist gate from {impl_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_checks(*args, **kwargs):
    return _load_impl().run_checks(*args, **kwargs)


def main() -> int:
    report = run_checks()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
