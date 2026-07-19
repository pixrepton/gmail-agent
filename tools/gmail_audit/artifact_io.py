"""Atomic read/write helpers for Gmail Intake run artifacts."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from disk."""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise OSError(f"Expected JSON object in {path}")
    return payload


def read_jsonl(path: Path, *, allow_missing: bool = False) -> list[Any]:
    """Read JSONL records from disk."""
    if not path.is_file():
        if allow_missing:
            return []
        raise OSError(f"JSONL file not found: {path}")

    rows: list[Any] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise OSError(f"Invalid JSONL line #{line_number} in {path}") from exc
    return rows


def write_json(path: Path, payload: Any) -> None:
    """Write JSON with stable formatting and atomic replacement."""
    render = json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"
    write_text(path, render)


def write_jsonl(path: Path, rows: list[Any]) -> None:
    """Write a full JSONL file atomically."""
    render = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
    write_text(path, render)


def append_jsonl(path: Path, payload: Any) -> None:
    """Append a JSONL record via atomic rewrite to avoid partial writes."""
    rows = read_jsonl(path, allow_missing=True)
    rows.append(payload)
    write_jsonl(path, rows)


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    """Write a CSV file atomically with UTF-8 and stable newlines."""
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    write_text(path, buffer.getvalue())


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    """Read CSV rows as dictionaries."""
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def write_text(path: Path, text: str) -> None:
    """Write text atomically using a temporary file in the target directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)
