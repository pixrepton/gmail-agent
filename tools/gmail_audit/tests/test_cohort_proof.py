from __future__ import annotations

import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from cohort_proof import build_cohort_run_record, is_drive_document_candidate
from gmail_intake import build_parser
from mailbox_memory_models import CaseContextPack


def test_drive_candidate_filter_excludes_images_but_keeps_office_documents() -> None:
    assert not is_drive_document_candidate({"mime_type": "image/jpeg", "file_name": "photo.jpg"})
    assert not is_drive_document_candidate({"mimeType": "image/png", "name": "scan.png"})
    assert is_drive_document_candidate({"mime_type": "application/pdf", "file_name": "umowa.pdf"})
    assert is_drive_document_candidate({"mimeType": "application/vnd.google-apps.document", "name": "Notatka"})


def test_cohort_run_record_counts_shared_gmail_drive_cases() -> None:
    packs = [
        CaseContextPack(
            case_id="case_shared",
            source_refs=[{"type": "gmail_message", "message_id": "msg-1"}],
            drive_documents_summary=[{"document_id": "drv-1", "file_name": "umowa.pdf"}],
            conflicting_facts=[{"fact_key": "device_power", "values": ["8 kW", "10 kW"]}],
            completeness_gaps=["Missing invoice"],
            action_proposals=[{"proposal_id": "ap-1"}],
        ),
        CaseContextPack(case_id="case_mail_only", source_refs=[{"type": "gmail_message", "message_id": "msg-2"}]),
    ]

    record = build_cohort_run_record(
        run_id="cohort-1",
        gmail_items=[{"message_id": "msg-1"}, {"message_id": "msg-2"}],
        drive_items=[
            {"document_id": "drv-1", "mime_type": "application/pdf", "file_name": "umowa.pdf"},
            {"document_id": "drv-img", "mime_type": "image/jpeg", "file_name": "foto.jpg"},
        ],
        context_packs=packs,
    )

    assert record["schema_version"] == "cohort_proof_run.v1"
    assert record["counts"]["gmail_selected"] == 2
    assert record["counts"]["drive_documents_selected"] == 1
    assert record["counts"]["case_count"] == 2
    assert record["counts"]["shared_gmail_drive_case_count"] == 1
    assert record["counts"]["conflict_count"] == 1
    assert record["counts"]["gap_count"] == 1
    assert record["counts"]["proposal_count"] == 1
    assert record["items"][0]["case_id"] == "case_shared"


def test_cohort_proof_cli_defaults_to_memory_only() -> None:
    parser = build_parser()
    args = parser.parse_args(["cohort-proof"])
    assert args.command == "cohort-proof"
    assert not args.live_gmail_cohort


def test_cohort_proof_cli_live_flag_enables_gmail_path() -> None:
    args = build_parser().parse_args(["cohort-proof", "--live-gmail-cohort"])
    assert args.live_gmail_cohort


def test_cohort_proof_cli_ingest_requires_live_gmail() -> None:
    import pytest

    from gmail_intake import build_parser, run_cohort_proof_command

    args = build_parser().parse_args(["cohort-proof", "--ingest-selected"])
    with pytest.raises(SystemExit):
        run_cohort_proof_command(args)
