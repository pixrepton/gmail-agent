"""CLI entrypoint for auditing Gmail via Groq Responses API and connector_gmail."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from config import ConfigError, load_settings
from groq_client import (
    IMPORTANT_MAILS_SCHEMA,
    GroqClientError,
    parse_important_mails_json,
    request_audit,
    sanitize_for_storage,
    sanitize_text,
)


PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
DEBUG_DIR = Path(__file__).resolve().parent / "debug"


def main() -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 2

    try:
        settings = load_settings()
        if args.verbose:
            env_path = str(settings.env_path) if settings.env_path else "no .env file"
            print(f"[verbose] Loaded env from: {env_path}", file=sys.stderr, flush=True)
            print(f"[verbose] Using model: {args.model or settings.groq_model}", file=sys.stderr, flush=True)
            print(f"[verbose] Responses URL: {settings.responses_url}", file=sys.stderr, flush=True)

        if args.command == "general-audit":
            prompt = build_prompt("general_audit.txt", days=args.days)
            result = request_audit(settings, prompt, model=args.model, verbose=args.verbose)
            emit_text(result.text, args.output)
            return 0

        if args.command == "rules-proposal":
            prompt = build_prompt("rules_proposal.txt", days=args.days)
            result = request_audit(settings, prompt, model=args.model, verbose=args.verbose)
            emit_text(result.text, args.output)
            return 0

        if args.command == "important-mails":
            prompt = build_prompt("important_mails.txt", days=args.days)
            result = request_audit(
                settings,
                prompt,
                model=args.model,
                json_schema=IMPORTANT_MAILS_SCHEMA,
                verbose=args.verbose,
            )
            try:
                items = parse_important_mails_json(result.text)
            except GroqClientError as exc:
                debug_path = save_invalid_json_debug(result.text, result.response_json)
                raise GroqClientError(
                    f"{sanitize_text(str(exc))} Raw response was saved to: {debug_path}"
                ) from exc

            rendered = json.dumps(items, indent=2, ensure_ascii=False)
            print(rendered)
            if args.output:
                write_text(args.output, rendered + "\n")
            return 0

        parser.error(f"Unsupported command: {args.command}")
        return 2
    except ConfigError as exc:
        print(f"Config error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except GroqClientError as exc:
        print(f"Groq error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"File error: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local CLI for read-only Gmail auditing via Groq Responses API and connector_gmail."
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--output", help="Path to an output file.")
    common.add_argument("--model", help="Override the Groq model for a single run.")
    common.add_argument("--verbose", action="store_true", help="Show basic diagnostics.")

    subparsers = parser.add_subparsers(dest="command")

    general = subparsers.add_parser(
        "general-audit",
        parents=[common],
        help="Return a high-level mailbox audit.",
    )
    general.add_argument("--days", type=positive_int, help="Optional lookback window in days.")

    rules = subparsers.add_parser(
        "rules-proposal",
        parents=[common],
        help="Return important-vs-noise rule proposals.",
    )
    rules.add_argument("--days", type=positive_int, help="Optional lookback window in days.")

    important = subparsers.add_parser(
        "important-mails",
        parents=[common],
        help="Return important messages from the last N days as JSON.",
    )
    important.add_argument("--days", type=positive_int, default=30, help="Look back N days. Default: 30.")
    important.add_argument(
        "--json",
        action="store_true",
        help="Explicit JSON mode. important-mails already enforces JSON array output.",
    )

    return parser


def build_prompt(prompt_name: str, *, days: int | None) -> str:
    prompt_path = PROMPTS_DIR / prompt_name
    raw_prompt = prompt_path.read_text(encoding="utf-8")
    base_prompt = raw_prompt.strip()
    if days is not None:
        base_prompt = base_prompt.replace("{{LOOKBACK_DAYS}}", str(days))
    if days is None:
        return base_prompt
    if "{{LOOKBACK_DAYS}}" not in raw_prompt:
        return f"{base_prompt}\n\nAdditional lookback window: prioritize the last {days} days."
    return base_prompt


def emit_text(text: str, output_path: str | None) -> None:
    print(text)
    if output_path:
        write_text(output_path, text + ("\n" if not text.endswith("\n") else ""))


def write_text(output_path: str, content: str) -> None:
    path = Path(output_path)
    if path.parent and path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def save_invalid_json_debug(raw_text: str, response_json: dict[str, object]) -> Path:
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = DEBUG_DIR / f"important-mails-invalid-json-{timestamp}.txt"

    sections = [
        "# Extracted assistant text",
        sanitize_text(raw_text.strip()) or "<empty>",
        "",
        "# Full Groq response JSON",
        json.dumps(sanitize_for_storage(response_json), indent=2, ensure_ascii=False),
    ]
    path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return path


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Value must be a positive integer.") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive integer.")
    return parsed


def configure_stdio() -> None:
    """Use UTF-8 for Windows terminals when possible."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
