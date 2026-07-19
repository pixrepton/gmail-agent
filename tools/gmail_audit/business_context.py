"""Local TOP-INSTAL business-context provider for shadow reasoning stages."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


BUSINESS_CONTEXT_DIR = Path(__file__).resolve().parent / "business_context"


@lru_cache(maxsize=1)
def load_business_context() -> dict[str, Any]:
    """Load the small local business-context bundle from disk."""
    return {
        "business_areas": _load_json("business_areas.json"),
        "customer_states": _load_json("customer_states.json"),
        "missing_info_rules": _load_json("missing_info_rules.json"),
        "action_policy": _load_json("action_policy.json"),
        "reply_policy": (BUSINESS_CONTEXT_DIR / "reply_policy.md").read_text(encoding="utf-8").strip(),
    }


def build_business_context_bundle(
    snapshot: dict[str, Any],
    intake_result: dict[str, Any],
    case_link_result: dict[str, Any],
) -> dict[str, Any]:
    """Build a compact business-context bundle for prompt inputs and shadow artifacts."""
    catalog = load_business_context()
    message = snapshot.get("source_message") or {}
    intake_area = str(intake_result.get("business_area") or "").strip()
    action = str(intake_result.get("decision", {}).get("action") or "").strip()
    selected_case_key = str(case_link_result.get("selected_case_key") or "").strip()

    return {
        "business_areas": catalog["business_areas"],
        "customer_states": catalog["customer_states"],
        "missing_info_rules": catalog["missing_info_rules"],
        "action_policy": catalog["action_policy"],
        "reply_policy": catalog["reply_policy"],
        "interpretation_hints": {
            "intake_business_area": intake_area,
            "intake_action": action,
            "selected_case_key": selected_case_key,
            "thread_quality": str(snapshot.get("thread_context_quality") or "weak"),
            "sender": str(message.get("sender") or ""),
            "normalized_subject": str(snapshot.get("normalized_subject") or ""),
        },
    }


def _load_json(name: str) -> dict[str, Any]:
    path = BUSINESS_CONTEXT_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


__all__ = ["build_business_context_bundle", "load_business_context"]
