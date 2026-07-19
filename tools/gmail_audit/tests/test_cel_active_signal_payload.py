"""CEL phase 2: active path carries intake in CanonicalSignal payload for reconcile."""

from __future__ import annotations

from gmail_signal_adapter import build_gmail_signals


def test_build_gmail_signals_embeds_intake_result_final() -> None:
    snapshot = {
        "mailbox": "ops@topinstal.pl",
        "source_message": {
            "message_id": "msg-cel-payload",
            "thread_id": "th-1",
            "subject": "Pompa ciepla",
        },
    }
    intake = {"decision": {"action": "append_to_existing_case"}, "review_required": False}
    signals = build_gmail_signals(
        snapshot=snapshot,
        intake_result_final=intake,
        preclassification_result={"lane": "service"},
        lane_stage_plan={"run_case_linking": True},
        context_bundle={"context_messages": []},
        created_by_runtime="test",
    )
    assert len(signals) >= 1
    payload = signals[0].payload
    assert payload.get("intake_result_final") == intake
    assert payload.get("snapshot") is not None
    assert payload.get("preclassification_result", {}).get("lane") == "service"
