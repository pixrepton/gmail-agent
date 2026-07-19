from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from artifact_contracts import empty_run_summary
from gmail_intake import build_parser, hydrate_intelligence_seam_config, process_live_selection


def test_cli_accepts_attachments_metadata_only() -> None:
    args = build_parser().parse_args(
        [
            "period",
            "--attachments-metadata-only",
            "--gmail-source",
            "google_api",
        ]
    )
    assert args.attachments_metadata_only is True


def test_cli_accepts_llm_inter_item_delay_seconds() -> None:
    args = build_parser().parse_args(
        [
            "period",
            "--llm-inter-item-delay-seconds",
            "5",
            "--gmail-source",
            "google_api",
        ]
    )
    assert args.llm_inter_item_delay_seconds == 5.0


def test_cli_rejects_negative_llm_inter_item_delay() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            [
                "period",
                "--llm-inter-item-delay-seconds",
                "-1",
                "--gmail-source",
                "google_api",
            ]
        )


def test_hydrate_metadata_only_clears_attachment_fetcher() -> None:
    class _Settings:
        attachment_extraction_enabled = True
        attachment_extraction_max_bytes = 8_000_000
        has_google_refresh_flow = True

    run_state: dict = {"runtime_controls": {"attachments_metadata_only": True}, "mailbox_memory_runtime": None, "daszek_client": None}
    stage_config: dict = {"settings": _Settings()}
    hydrate_intelligence_seam_config(run_state, {"source_message": {}}, stage_config)
    assert stage_config["attachment_fetcher"] is None
    assert stage_config["attachment_max_bytes"] == 0


def test_hydrate_without_metadata_only_keeps_fetcher_when_enabled() -> None:
    class _Settings:
        attachment_extraction_enabled = True
        attachment_extraction_max_bytes = 8_000_000
        has_google_refresh_flow = True

    run_state: dict = {"runtime_controls": {}, "mailbox_memory_runtime": None, "daszek_client": None}
    stage_config: dict = {"settings": _Settings()}
    hydrate_intelligence_seam_config(run_state, {"source_message": {}}, stage_config)
    assert callable(stage_config["attachment_fetcher"])
    assert stage_config["attachment_max_bytes"] == 8_000_000


def test_llm_inter_item_delay_sleep_between_iterations(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("gmail_intake.time.sleep", lambda s: sleeps.append(float(s)))

    settings = MagicMock()
    schema: dict = {}
    instructions = ""
    run_state: dict = {
        "manifest": {"mailbox": "test@example.com"},
        "summary": empty_run_summary(),
        "runtime_controls": {"llm_inter_item_delay_seconds": 0.5},
        "artifacts": {},
    }

    def _record_error(*_args: object, **_kwargs: object) -> None:
        return None

    def _update_checkpoint(*_args: object, **_kwargs: object) -> None:
        return None

    def _check_runtime_stop_conditions(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr("gmail_intake.record_error", _record_error)
    monkeypatch.setattr("gmail_intake.update_checkpoint", _update_checkpoint)
    monkeypatch.setattr("gmail_intake.check_runtime_stop_conditions", _check_runtime_stop_conditions)

    selected_items: list[dict] = [{}, {}]
    process_live_selection(
        settings=settings,
        schema=schema,
        instructions=instructions,
        run_state=run_state,
        selected_items=selected_items,
        model=None,
        verbose=False,
        context_limit=0,
        keep_going=True,
        gmail_source="google_api",
    )
    assert sleeps == [0.5]
