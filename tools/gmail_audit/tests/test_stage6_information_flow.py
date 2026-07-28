"""Stage 6: bounded BusinessReasoning input and thread narrative continuity."""

from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from intake_payload import build_business_reasoning_payload
from thread_memory import build_thread_memory


def _snapshot(*, message_id: str, subject: str, body: str) -> dict:
    return {
        "source_message": {
            "message_id": message_id,
            "thread_id": "thread-stage6",
            "sender": "customer@example.com",
            "subject": subject,
            "snippet": body[:120],
            "body": body,
            "date": "2026-07-28T10:00:00+00:00",
        },
        "context_messages": [],
        "thread_context_quality": "strong",
    }


def test_business_reasoning_payload_includes_bounded_current_body() -> None:
    body = "A" * 950 + "TAIL"

    payload = build_business_reasoning_payload(
        _snapshot(message_id="msg-body", subject="Oferta", body=body),
        intake_result={},
        case_link_result={},
        business_context_bundle={},
    )

    assert payload["message_summary"]["body_excerpt"] == body[:900]
    assert len(payload["message_summary"]["body_excerpt"]) == 900
    assert "TAIL" not in payload["message_summary"]["body_excerpt"]


def test_thread_summary_preserves_narrative_across_multiple_messages() -> None:
    first = build_thread_memory(
        _snapshot(
            message_id="msg-1",
            subject="Pierwsze zapytanie",
            body="Prosze o oferte.",
        ),
        business_result={
            "business_interpretation": "Klient poprosil o pierwsza wycene."
        },
    )
    second = build_thread_memory(
        _snapshot(
            message_id="msg-2",
            subject="Uzupelnienie danych",
            body="Powierzchnia wynosi 160 m2.",
        ),
        business_result={
            "business_interpretation": "Klient uzupelnil powierzchnie budynku."
        },
        existing_thread_memory=first,
    )
    third = build_thread_memory(
        _snapshot(
            message_id="msg-3",
            subject="Powtorzenie danych",
            body="Powierzchnia nadal wynosi 160 m2.",
        ),
        business_result={
            "business_interpretation": "Klient uzupelnil powierzchnie budynku."
        },
        existing_thread_memory=second,
    )

    summary = third["canonical_thread_summary"]
    assert "Klient poprosil o pierwsza wycene." in summary
    assert "Klient uzupelnil powierzchnie budynku." in summary
    assert summary.count("Klient uzupelnil powierzchnie budynku.") == 1
