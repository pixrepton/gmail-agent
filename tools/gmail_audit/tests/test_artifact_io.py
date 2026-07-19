from __future__ import annotations

from datetime import date, datetime, timezone

from artifact_io import read_json, read_jsonl, write_json, write_jsonl
from redaction import sanitize_for_storage


def test_sanitize_for_storage_normalizes_date_types() -> None:
    payload = {
        "created_at": datetime(2026, 4, 12, 13, 15, 0, tzinfo=timezone.utc),
        "service_day": date(2026, 4, 12),
    }

    sanitized = sanitize_for_storage(payload)

    assert sanitized == {
        "created_at": "2026-04-12T13:15:00+00:00",
        "service_day": "2026-04-12",
    }


def test_write_jsonl_serializes_datetime_values(tmp_path) -> None:
    path = tmp_path / "artifacts.jsonl"

    write_jsonl(
        path,
        [
            {
                "message_id": "mid-1",
                "created_at": datetime(2026, 4, 12, 13, 20, 0, tzinfo=timezone.utc),
            }
        ],
    )

    assert read_jsonl(path) == [
        {
            "message_id": "mid-1",
            "created_at": "2026-04-12 13:20:00+00:00",
        }
    ]


def test_write_json_serializes_datetime_values(tmp_path) -> None:
    path = tmp_path / "summary.json"

    write_json(
        path,
        {
            "completed_at": datetime(2026, 4, 12, 13, 25, 0, tzinfo=timezone.utc),
        },
    )

    assert read_json(path) == {
        "completed_at": "2026-04-12 13:25:00+00:00",
    }
