#!/usr/bin/env python
"""Cross-platform clean export and export verification for gmail-agent."""

from __future__ import annotations

import argparse
import shutil
from dataclasses import dataclass
from pathlib import Path


FORBIDDEN_DIR_NAMES = {
    ".git",
    ".gitnexus",
    ".mypy_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "blob",
    "cache",
    "coverage",
    "htmlcov",
    "node_modules",
    "runs",
    "test-results",
    "venv",
}
FORBIDDEN_FILE_NAMES = {
    ".coverage",
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}
FORBIDDEN_FILE_SUFFIXES = {
    ".db",
    ".db-shm",
    ".db-wal",
    ".lnk",
    ".log",
    ".pyc",
    ".pyo",
    ".sqlite3",
    ".sqlite",
    ".tmp",
}
FORBIDDEN_RELATIVE_PREFIXES = (
    ".tmp-gmail-hardening",
    "Daszek/uploads",
    "test-results",
    "tools/gmail_audit/.cache",
    "tools/gmail_audit/.pytest_cache",
    "tools/gmail_audit/__pycache__",
    "tools/gmail_audit/debug",
    "tools/gmail_audit/runs",
)
LARGE_FILE_WARN_THRESHOLD = 50 * 1024 * 1024


class ExportPolicyError(RuntimeError):
    """Raised when export verification or staging rules are violated."""


@dataclass(frozen=True)
class VerifyResult:
    warnings: tuple[str, ...]


def build_clean_export(source: Path | str, destination: Path | str) -> VerifyResult:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    _validate_export_paths(source_path, destination_path)

    if destination_path.exists():
        shutil.rmtree(destination_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    shutil.copytree(
        source_path,
        destination_path,
        ignore=_copy_ignore_factory(source_path),
        dirs_exist_ok=False,
    )
    return verify_export_tree(destination_path)


def verify_export_tree(destination: Path | str) -> VerifyResult:
    destination_path = Path(destination).resolve()
    if not destination_path.is_dir():
        raise ExportPolicyError(f"verify_export_clean: not a directory: {destination_path}")

    bad_paths: list[str] = []
    warnings: list[str] = []

    for path in sorted(destination_path.rglob("*")):
        relative = path.relative_to(destination_path).as_posix()
        reason = _classify_forbidden_export_path(relative, is_dir=path.is_dir())
        if reason:
            bad_paths.append(f"{relative} ({reason})")
            continue
        if path.is_file() and path.stat().st_size > LARGE_FILE_WARN_THRESHOLD:
            warnings.append(relative)

    if bad_paths:
        preview = "\n".join(bad_paths[:200])
        raise ExportPolicyError(
            "verify_export_clean: FAIL - forbidden paths under "
            f"{destination_path}\n{preview}"
        )

    return VerifyResult(warnings=tuple(warnings))


def _copy_ignore_factory(source_root: Path):
    def _ignore(current_dir: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        current_path = Path(current_dir)
        for name in names:
            candidate = current_path / name
            relative = candidate.relative_to(source_root).as_posix()
            reason = _classify_forbidden_export_path(relative, is_dir=candidate.is_dir())
            if reason:
                ignored.add(name)
        return ignored

    return _ignore


def _validate_export_paths(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_dir():
        raise ExportPolicyError(f"clean_export: source is not a directory: {source_path}")
    if source_path == destination_path:
        raise ExportPolicyError("clean_export: destination must differ from source.")
    if _is_relative_to(destination_path, source_path):
        raise ExportPolicyError(
            "clean_export: destination must be outside the source tree to keep staging clean."
        )


def _classify_forbidden_export_path(relative: str, *, is_dir: bool) -> str | None:
    normalized = relative.strip("/")
    if not normalized:
        return None

    name = Path(normalized).name
    lower_name = name.lower()

    if _matches_relative_prefix(normalized):
        return "forbidden export prefix"

    if _is_forbidden_env_name(name):
        return "local env file"

    if _looks_like_coverage_artifact(lower_name):
        return "coverage artifact"

    if lower_name in {entry.lower() for entry in FORBIDDEN_FILE_NAMES}:
        return "system garbage"

    if not is_dir and Path(name).suffix.lower() in FORBIDDEN_FILE_SUFFIXES:
        return "runtime/cache/garbage file"

    if is_dir and name in FORBIDDEN_DIR_NAMES:
        return "runtime/cache directory"

    if not is_dir and _looks_like_secret_json(lower_name):
        return "credential/token JSON"

    return None


def _matches_relative_prefix(relative: str) -> bool:
    for prefix in FORBIDDEN_RELATIVE_PREFIXES:
        if relative == prefix or relative.startswith(prefix + "/"):
            return True
    return False


def _is_forbidden_env_name(name: str) -> bool:
    if name == ".env":
        return True
    if name.endswith(".example"):
        return False
    return name.startswith(".env.")


def _looks_like_secret_json(lower_name: str) -> bool:
    if not lower_name.endswith(".json"):
        return False
    secret_tokens = (
        "credentials",
        "oauth",
        "secret",
        "service-account",
        "service_account",
        "token",
    )
    return any(token in lower_name for token in secret_tokens)


def _looks_like_coverage_artifact(lower_name: str) -> bool:
    return lower_name == ".coverage" or lower_name.startswith(".coverage.")


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _build_cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or verify clean gmail-agent exports.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    clean = subparsers.add_parser("clean", help="Build a clean export and verify it.")
    clean.add_argument("source", help="Source repo/workspace directory")
    clean.add_argument("destination", help="Clean export destination")

    verify = subparsers.add_parser("verify", help="Verify an existing export tree.")
    verify.add_argument("destination", help="Export directory to verify")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_cli()
    args = parser.parse_args(argv)

    if args.command == "clean":
        result = build_clean_export(args.source, args.destination)
        print(f"clean_export: OK - staged export at {Path(args.destination).resolve()}")
    else:
        result = verify_export_tree(args.destination)
        print(
            "verify_export_clean: OK - no forbidden secrets/runtime artifacts/garbage under "
            f"{Path(args.destination).resolve()}"
        )

    if result.warnings:
        print("verify_export_clean: note - files >50MB (manual review):")
        for warning in result.warnings[:20]:
            print(warning)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
