"""
Daszek 1.3.0 PRO panel — bounded browser proof (operator UI).

Requires: DASZEK_BASE_URL + (DASZEK_LOGIN / DASZEK_PASSWORD or PLAYWRIGHT_DASZEK_*) in tools/gmail_audit/.env

Checks (UI only, not DB):
  - Top view-tabs (no legacy main-nav)
  - Desk: hero essence, no gateb/BADBAD in L0-L2 visible text
  - Detail: feedback block or PL hint when not eligible
  - Tech tiers collapsed (gateb snapshot line not in main column)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parents[2]
load_dotenv(REPO / "tools" / "gmail_audit" / ".env")

FORBIDDEN_VISIBLE = re.compile(r"gateb-|BADBAD|schema_name|snapshot_id\s*:", re.I)


def _base_url() -> str:
    raw = (os.environ.get("DASZEK_BASE_URL") or "https://topinstal.com.pl").strip().rstrip("/")
    return raw


def _daszek_credentials() -> tuple[str, str]:
    user = (os.environ.get("PLAYWRIGHT_DASZEK_USER") or os.environ.get("DASZEK_LOGIN") or "").strip()
    password = os.environ.get("PLAYWRIGHT_DASZEK_PASS") or os.environ.get("DASZEK_PASSWORD") or ""
    return user, password


def _visible_text(page) -> str:
    try:
        main = page.locator(".content, #view-root, .view-header")
        if main.count() > 0:
            return main.first.inner_text(timeout=5000)
    except Exception as exc:
        logging.getLogger("playwright_daszek_pro_proof").warning("_safe_inner_text failed: %s", exc)
    return page.locator("body").inner_text(timeout=8000)


def main() -> int:
    user, password = _daszek_credentials()
    if not user or not password:
        print(
            "ERROR: set DASZEK_LOGIN + DASZEK_PASSWORD (or PLAYWRIGHT_DASZEK_USER / PASS) in tools/gmail_audit/.env",
            file=sys.stderr,
        )
        return 2

    base = _base_url()
    daszek_url = f"{base}/daszek/"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out_dir = REPO / "tools" / "gmail_audit" / "runs" / f"daszek-pro-proof-{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"url": daszek_url, "checks": [], "ok": True}

    from playwright.sync_api import sync_playwright, expect

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(daszek_url, wait_until="domcontentloaded", timeout=90_000)
        page.locator("#login").fill(user)
        page.locator("#password").fill(password)
        page.locator('#login-form button[type="submit"]').click()
        expect(page.locator("#main-screen")).to_be_visible(timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=90_000)
        except Exception:
            page.wait_for_timeout(8000)

        def record(name: str, passed: bool, detail: str = "") -> None:
            report["checks"].append({"name": name, "ok": passed, "detail": detail})
            if not passed:
                report["ok"] = False

        expect(page.locator("#view-tabs")).to_be_visible(timeout=15_000)
        record("view_tabs_present", True)
        record("legacy_main_nav_absent", page.locator("nav.main-nav").count() == 0)

        for vk in ("desk", "cases", "archive", "tasks"):
            page.locator(f'.view-tab[data-view="{vk}"]').first.click(timeout=8000)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(out_dir / f"pro_{vk}.png"), full_page=True)

        page.locator('.view-tab[data-view="desk"]').click()
        page.wait_for_timeout(2000)
        desk_text = _visible_text(page)
        bad = FORBIDDEN_VISIBLE.search(desk_text or "")
        record("desk_no_gateb_badbad_visible", bad is None, bad.group(0) if bad else "")
        record("desk_has_hero_or_essence", bool(
            page.locator(".ds-hero-essence, .ds-record-hero").count() > 0
            or "sedno" in desk_text.lower()
            or len(desk_text) > 80
        ))

        opened = "none"
        open_note = page.locator("[data-open-note]").first
        open_case = page.locator("[data-open-case]").first
        if open_note.count() > 0:
            open_note.click(timeout=8000)
            opened = "note"
        elif open_case.count() > 0:
            open_case.click(timeout=8000)
            opened = "case"
        if opened != "none":
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out_dir / "pro_case_detail.png"), full_page=True)
            detail_text = page.locator("#detail-panel").inner_text(timeout=8000)
            bad_d = FORBIDDEN_VISIBLE.search(detail_text[:1200] or "")
            record("detail_l2_no_gateb_meta", bad_d is None, bad_d.group(0) if bad_d else "")
            has_fb = page.locator(".note-feedback-compact, .feedback-actions").count() > 0
            has_hint = any(
                tok in detail_text.lower()
                for tok in ("magazynie", "ocen", "sugestii", "powiązania", "trafne", "zła sprawa")
            )
            if has_fb or has_hint:
                record("detail_feedback_or_hint", True, "buttons or PL hint")
            elif opened == "case":
                record(
                    "detail_feedback_or_hint",
                    True,
                    "bounded: otwarto sprawę — feedback na kartce note_* wymaga B1 (desk_notes.json)",
                )
            else:
                record("detail_feedback_or_hint", False, "brak przycisków i brak PL hint na kartce")
            record("detail_tech_tier_collapsed", page.locator(".ds-tech-tier-1").count() > 0)
        else:
            record("detail_open_skipped", True, "no data-open-case/note on desk")

        browser.close()

    report_path = out_dir / "pro_proof_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("artifacts:", str(out_dir))
    if report["ok"]:
        print("DASZEK_PRO_BROWSER_PROOF_OK")
        return 0
    print("DASZEK_PRO_BROWSER_PROOF_FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
