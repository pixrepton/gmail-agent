"""Small regression pack for recently closed intake paths (serialization, OCR/ZIP smoke)."""

from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO

from artifact_io import write_jsonl
from attachment_content_extraction import extract_attachment_text, inspect_ocr_runtime
from redaction import sanitize_for_storage


def test_json_dumps_datetime_safe_for_nested_stage_record(tmp_path) -> None:
    """Regression: nested datetimes must not crash artifact persistence (default=str)."""
    nested = {
        "at": datetime(2026, 4, 12, 10, 0, 0, tzinfo=timezone.utc),
        "inner": {"ts": datetime(2026, 4, 12, 11, 0, 0, tzinfo=timezone.utc)},
    }
    raw = json.dumps(nested, ensure_ascii=False, default=str)
    assert "2026" in raw
    roundtrip = json.loads(raw)
    assert isinstance(roundtrip["at"], str)


def test_write_jsonl_accepts_datetime_nested_dict(tmp_path) -> None:
    path = tmp_path / "rows.jsonl"
    write_jsonl(path, [{"stage": "x", "t": datetime(2026, 4, 12, 12, 0, tzinfo=timezone.utc)}])
    line = path.read_text(encoding="utf-8").strip()
    assert "2026-04-12" in line


def test_sanitize_for_storage_nested_datetime() -> None:
    out = sanitize_for_storage(
        {"emitted_at": datetime(2026, 4, 12, 14, 0, tzinfo=timezone.utc), "n": {"d": datetime(2026, 1, 1, tzinfo=timezone.utc)}}
    )
    assert isinstance(out["emitted_at"], str)
    assert isinstance(out["n"]["d"], str)


def test_extract_zip_archive_path_smoke() -> None:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.txt", "hello zip")
    result = extract_attachment_text(
        buf.getvalue(),
        mime_type="application/zip",
        file_name="bundle.zip",
    )
    assert result["parser_provenance"] == "zipfile"
    assert result["extraction_status"] == "archive_container"


def test_inspect_ocr_runtime_returns_status_shape() -> None:
    r = inspect_ocr_runtime()
    assert r["status"] in {"ok", "deps_missing", "binary_missing", "disabled"}
