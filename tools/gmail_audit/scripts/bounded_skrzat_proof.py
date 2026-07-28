"""Skrzat bounded proof helpers (CONTEXT_PROJECTION_BOUNDED_PROOF kroki 5–6)."""

from __future__ import annotations

from typing import Any

from api_app import create_app
from config import load_settings
from context_tray_set import build_context_tray_set
from mailbox_memory_runtime import build_mailbox_memory_runtime
from skrzat_copilot import resolve_skrzat_answer
from skrzat_runtime import SCHEMA_VERSION


def run_skrzat_bounded_proof(
    *,
    case_id: str,
    settings: Any | None = None,
    question: str = "Jakie dane kontaktowe mamy w tej sprawie i czy sa sprzecznosci?",
    mode: str = "investigate",
) -> dict[str, Any]:
    """Krok 5–6: Skrzat ask + answer quality envelope validation."""
    settings = settings or load_settings(require_groq=False, require_google=False)
    if not str(getattr(settings, "mailbox_memory_database_url", "") or "").strip():
        return {
            "ok": False,
            "skipped": False,
            "reason": "mailbox_memory_database_url_required",
        }
    runtime = build_mailbox_memory_runtime(settings, allow_in_memory=False)
    if runtime is None:
        return {"ok": False, "skipped": False, "reason": "durable_mailbox_runtime_unavailable"}

    runtime.bootstrap()
    pack = runtime.get_context_pack(case_id=case_id, query_text=question)
    resolved_case_id = _pack_case_id(pack)
    if not resolved_case_id:
        return {"ok": False, "reason": "case_not_found", "case_id": case_id}

    from case_context_contract import build_case_context_pack_vnext

    contract = build_case_context_pack_vnext(pack)
    trays = build_context_tray_set(contract, generated_at=str(contract.get("generated_at") or ""))
    direct_body = resolve_skrzat_answer(
        settings=settings,
        context_tray_set=trays,
        question=question,
        mode=mode,
        query_text=question,
    )
    direct_errors = _validate_envelope(direct_body, case_id=case_id)

    http_errors: list[str] = []
    http_status = 0
    try:
        from fastapi.testclient import TestClient

        app = create_app(
            runtime_provider=lambda: runtime,
            cohort_reader=lambda _rid: None,
        )
        client = TestClient(app)
        resp = client.post(
            f"/cases/{case_id}/skrzat/ask",
            json={"question": question, "mode": mode, "query_text": question},
        )
        http_status = resp.status_code
        if resp.status_code != 200:
            http_errors.append(f"http_{resp.status_code}")
        else:
            http_errors = _validate_envelope(resp.json(), case_id=case_id)
    except ImportError as exc:
        http_errors.append(f"fastapi_missing:{exc}")

    quality = _quality_summary(direct_body if isinstance(direct_body, dict) else {})
    return {
        "ok": not direct_errors and not http_errors,
        "case_id": case_id,
        "direct_errors": direct_errors,
        "http_errors": http_errors,
        "http_status": http_status,
        "quality": quality,
        "envelope_schema": direct_body.get("schema_version") if isinstance(direct_body, dict) else None,
        "read_only": direct_body.get("read_only") if isinstance(direct_body, dict) else None,
        "action_allowed": direct_body.get("action_allowed") if isinstance(direct_body, dict) else None,
    }


def _pack_case_id(pack: Any) -> str:
    if isinstance(pack, dict):
        return str(pack.get("case_id") or "").strip()
    return str(getattr(pack, "case_id", "") or "").strip()


def _validate_envelope(body: dict[str, Any], *, case_id: str) -> list[str]:
    errors: list[str] = []
    if body.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version")
    if str(body.get("case_id") or "") != case_id:
        errors.append("case_id")
    if body.get("read_only") is not True:
        errors.append("read_only")
    if body.get("action_allowed") is not False:
        errors.append("action_allowed")
    if not str(body.get("answer_text") or "").strip():
        errors.append("answer_text_empty")
    audit = body.get("context_audit") if isinstance(body.get("context_audit"), dict) else {}
    if audit.get("stage_name") != "skrzat_copilot":
        errors.append("context_audit.stage_name")
    if not body.get("quality_metrics"):
        errors.append("quality_metrics")
    return errors


def _quality_summary(body: dict[str, Any]) -> dict[str, Any]:
    metrics = body.get("quality_metrics") if isinstance(body.get("quality_metrics"), dict) else {}
    return {
        "evidence_count": len(body.get("evidence") or []),
        "gaps_count": len(body.get("gaps") or []),
        "conflicts_count": len(body.get("conflicts") or []),
        "warnings_count": len(body.get("warnings") or []),
        "skrzat_evidence_coverage_rate": metrics.get("skrzat_evidence_coverage_rate"),
        "answer_preview": str(body.get("answer_text") or "")[:240],
    }
