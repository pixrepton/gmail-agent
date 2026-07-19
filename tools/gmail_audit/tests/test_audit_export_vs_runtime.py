"""Golden fixtures + export audit smoke tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = TOOL_DIR.parents[1]
FIXTURES = TOOL_DIR / "tests" / "fixtures" / "case_routing_golden.json"
AUDIT_SCRIPT = WORKSPACE_ROOT / "scripts" / "audit_export_vs_db.py"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from case_routing import classify_mailbox_row, route_gmail_message  # noqa: E402
from mail_classification import classify_message  # noqa: E402


def _load_golden() -> list[dict]:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def test_case_routing_golden_fixtures() -> None:
    for item in _load_golden():
        item_id = item["id"]
        if item.get("source_kind") == "manual":
            routing = classify_mailbox_row(
                item.get("export_case_type"),
                "manual",
                item.get("export_case_type"),
            )
            assert routing.case_family == item["expected_case_family"], item_id
            assert routing.requires_action == item["expected_requires_action"], item_id
            continue

        cls = classify_message(
            subject=item["subject"],
            snippet=item.get("body", "")[:240],
            sender=item["sender"],
            labels=item.get("labels", []),
            body=item.get("body", ""),
            has_attachment=False,
            direction="inbound",
        )
        routing = route_gmail_message(
            subject=item["subject"],
            snippet=item.get("body", "")[:240],
            sender=item["sender"],
            labels=item.get("labels", []),
            body=item.get("body", ""),
            has_attachment=False,
        )
        assert cls.get("case_type") == item["expected_case_type"], f"{item_id} type {cls}"
        if "expected_is_task" in item:
            assert bool(cls.get("is_task")) == item["expected_is_task"], item_id
        if "expected_case_family" in item:
            assert routing.case_family == item["expected_case_family"], item_id
        if "expected_upsert" in item:
            assert routing.upsert_allowed == item["expected_upsert"], item_id


def test_supplier_opportunity_routes_to_supplier_family() -> None:
    routing = route_gmail_message(
        subject="Nowa promocja dla instalatorów — rabat hurtownia",
        snippet="",
        sender="marketing@schiessl.pl",
        labels=["CATEGORY_PROMOTIONS", "INBOX"],
        body="SCHIESSL POLSKA — sprawdź cennik i rabat dla instalatorów.",
        has_attachment=False,
    )
    assert routing.export_case_type == "supplier_opportunity"
    assert routing.case_family == "supplier"
    assert routing.requires_action is True


def test_panasonic_brand_newsletter_is_noise() -> None:
    routing = route_gmail_message(
        subject="Pompa ciepła w 100-letnim domu!",
        snippet="",
        sender="panasonicproclub.com@crlsrv.com",
        labels=["CATEGORY_PROMOTIONS", "INBOX"],
        body="Panasonic Pro Club newsletter. Unsubscribe from this list.",
        has_attachment=False,
    )
    assert routing.export_case_type == "noise"
    assert routing.upsert_allowed is False


def test_audit_export_vs_runtime_script_runs() -> None:
    assert AUDIT_SCRIPT.is_file(), f"missing {AUDIT_SCRIPT}"
    proc = subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(WORKSPACE_ROOT),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout.strip().splitlines()[0])
    assert payload["total"] >= 100
    assert "match_rate" in payload


# Proof token (gate): EXPORT_RUNTIME_AUDIT_PROOF_OK
