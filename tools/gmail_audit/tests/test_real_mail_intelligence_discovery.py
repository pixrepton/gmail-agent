from __future__ import annotations

import json
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from gmail_intake_parser import build_parser  # noqa: E402
from real_mail_intelligence_discovery import (  # noqa: E402
    NO_GAP,
    RealMailDiscoveryOptions,
    run_real_mail_intelligence_discovery,
    write_real_mail_discovery_proof,
)


def test_discovery_classifies_real_mail_gap_categories(tmp_path: Path) -> None:
    input_path = tmp_path / "cases.json"
    input_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "case_id": "technical-question",
                        "message": {
                            "message_id": "m1",
                            "subject": "Panasonic 55C",
                            "body": "Czy model moze pracowac z grzejnikami 55C?",
                        },
                        "expected": {
                            "best_action": "technical_answer",
                            "required_facts": ["device_model", "installation_parameters"],
                            "required_capabilities": ["technical_product_rag"],
                        },
                        "actual": {
                            "action": "generate_draft_reply",
                            "available_facts": ["device_model"],
                            "qualified_capabilities": [],
                        },
                    },
                    {
                        "case_id": "doc-ack",
                        "message": {"message_id": "m2", "subject": "Zdjecie kotlowni", "body": "W zalaczniku zdjecie."},
                        "expected": {"best_action": "acknowledge_document"},
                        "actual": {"action": "acknowledge_document"},
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_real_mail_intelligence_discovery(
        RealMailDiscoveryOptions(
            input_path=input_path,
            output_dir=tmp_path / "out",
            run_id="sample",
            allow_small_sample=True,
        )
    )

    assert summary["status"] == "completed_small_sample"
    assert summary["qualification"] == "SMOKE_ONLY"
    assert summary["counts"]["case_count"] == 2
    assert summary["gap_counts"]["BUSINESS_REASONING_GAP"] == 1
    assert summary["gap_counts"]["FACT_GAP"] == 1
    assert summary["gap_counts"]["RAG_GAP"] == 1
    assert summary["items"][1]["gap_categories"] == [NO_GAP]


def test_discovery_fails_closed_without_required_real_case_count(tmp_path: Path) -> None:
    input_path = tmp_path / "cases.jsonl"
    input_path.write_text(
        json.dumps(
            {
                "case_id": "one-case",
                "expected": {"best_action": "acknowledge_document"},
                "actual": {"action": "acknowledge_document"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = run_real_mail_intelligence_discovery(
        RealMailDiscoveryOptions(input_path=input_path, output_dir=tmp_path / "out", run_id="small")
    )

    assert summary["status"] == "blocked_insufficient_case_count"
    assert summary["qualification"] == "DATASET_REQUIRED"


def test_discovery_qualifies_default_ten_case_cohort(tmp_path: Path) -> None:
    input_path = tmp_path / "cases.json"
    cases = [
        {
            "case_id": f"case-{index}",
            "expected": {"best_action": "acknowledge_document"},
            "actual": {"action": "acknowledge_document"},
        }
        for index in range(10)
    ]
    input_path.write_text(json.dumps(cases), encoding="utf-8")

    summary = run_real_mail_intelligence_discovery(
        RealMailDiscoveryOptions(input_path=input_path, output_dir=tmp_path / "out", run_id="ten")
    )

    assert summary["status"] == "completed"
    assert summary["qualification"] == "DISCOVERY_QUALIFIED"
    assert summary["counts"]["case_count"] == 10


def test_discovery_artifacts_do_not_write_raw_mail_body(tmp_path: Path) -> None:
    body = "Poufny opis klienta 12345 i szczegoly instalacji."
    input_path = tmp_path / "cases.json"
    input_path.write_text(
        json.dumps(
            [
                {
                    "case_id": "privacy",
                    "message": {"message_id": "m1", "subject": "Poufny temat", "body": body},
                    "expected": {"best_action": "acknowledge_document"},
                    "actual": {"action": "acknowledge_document"},
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = run_real_mail_intelligence_discovery(
        RealMailDiscoveryOptions(
            input_path=input_path,
            output_dir=tmp_path / "out",
            run_id="privacy",
            allow_small_sample=True,
        )
    )
    paths = write_real_mail_discovery_proof(summary, output_dir=tmp_path / "proof")

    rendered = "\n".join(Path(path).read_text(encoding="utf-8") for path in paths.values())
    assert body not in rendered
    assert summary["items"][0]["message_ref"]["body_sha256"]
    assert summary["items"][0]["message_ref"]["body_chars"] == len(body)


def test_cli_parser_exposes_real_mail_discovery_command(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "real-mail-discovery",
            "--input",
            str(tmp_path / "cases.jsonl"),
            "--output-dir",
            str(tmp_path / "out"),
            "--allow-small-sample",
        ]
    )

    assert args.command == "real-mail-discovery"
    assert args.input == tmp_path / "cases.jsonl"
    assert args.allow_small_sample is True
