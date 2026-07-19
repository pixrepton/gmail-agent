
"""Load agent semantic constitution from markdown (PR-B).

Fasada: get_constitution_for_signal() kieruje do wlasciwej constitution
w zaleznosci od source_kind sygnalu.
"""

from __future__ import annotations

from log_config import get_logger
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from agent_runtime.constitution_mail import (
    MAIL_AGENT_TOOL_ALLOWLIST,
    MAIL_AGENT_TOOL_BUDGET,
    MAIL_AGENT_SYSTEM_NOTE,
)
from agent_runtime.constitution_chat import (
    CHAT_AGENT_TOOL_ALLOWLIST,
    CHAT_AGENT_TOOL_BUDGET,
    CHAT_AGENT_SYSTEM_NOTE,
)

DEFAULT_CONSTITUTION_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "core" / "AGENT_CONSTITUTION.md"
)

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

_DEFAULT_FORBIDDEN = (
    "send_email",
    "auto_send",
    "create_offerdto",
    "archive_gmail",
    "calendar_live_write",
)

# Domyślna allowlista — backward compatibility, tożsama z agentem mailowym (read-only)
_DEFAULT_TOOL_ALLOWLIST = MAIL_AGENT_TOOL_ALLOWLIST

# I4.2 — System note dla czatu ogólnego (Agent-as-Gateway, bez case_id)
# Wstrzykiwany do prompta gdy operator rozmawia przez czat ogólny
GENERAL_GATEWAY_SYSTEM_NOTE = CHAT_AGENT_SYSTEM_NOTE


@dataclass(frozen=True)
class AgentConstitution:
    hvac_rules: str
    company_context: str
    forbidden_actions: tuple[str, ...]
    tool_allowlist: tuple[str, ...]
    tool_budget: dict[str, int] = field(default_factory=dict)
    language: str = "pl"
    sections: dict[str, str] = field(default_factory=dict)
    source_path: str = ""

    def section_headers(self) -> list[str]:
        return list(self.sections.keys())


def _parse_sections(text: str) -> dict[str, str]:
    matches = list(_SECTION_RE.finditer(text))
    if not matches:
        return {"body": text.strip()}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[title] = text[start:end].strip()
    return sections


def _extract_allowlist_from_sections(sections: dict[str, str]) -> tuple[str, ...]:
    block = sections.get("Allowlist narzędzi", "")
    tools: list[str] = []
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- `") and line.endswith("`"):
            tools.append(line[3:-1].strip())
        elif line.startswith("- "):
            tools.append(line[2:].strip())
    if not tools:
        return _DEFAULT_TOOL_ALLOWLIST
    merged = list(tools)
    for name in _DEFAULT_TOOL_ALLOWLIST:
        if name not in merged:
            merged.append(name)
    return tuple(merged)


def load_company_context(path: str | None = None) -> str:
    override = str(path or os.environ.get("TOPINSTAL_COMPANY_CONTEXT_PATH", "") or "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").strip()
    return ""


def get_constitution_for_signal(source_kind: str) -> tuple[tuple[str, ...], dict[str, int], str]:
    """Zwróć (tool_allowlist, tool_budget, system_note) dla danego source_kind sygnału.

    Agent mailowy (gmail_message_observed, drive, calendar):
      - Tylko read/search tools, NIE write
      - Budget jak w constitution_mail.py

    Agent czatowy (operator_command):
      - Pelny dostep: read/search/write przez HITL
      - Budget jak w constitution_chat.py

    Fallback (każdy inny source_kind): agent mailowy — bezpieczniejszy, read-only.
    """
    if source_kind in ("operator_command", "agent_chat"):
        return (
            CHAT_AGENT_TOOL_ALLOWLIST,
            CHAT_AGENT_TOOL_BUDGET,
            CHAT_AGENT_SYSTEM_NOTE,
        )
    # gmail_message_observed, drive, calendar, os_event, operator_command_with_case_id
    return (
        MAIL_AGENT_TOOL_ALLOWLIST,
        MAIL_AGENT_TOOL_BUDGET,
        MAIL_AGENT_SYSTEM_NOTE,
    )


def load_live(
    *,
    rag_enabled: bool = False,
    rag_query: str = "",
    database_url: str = "",
    constitution_path: Path | str | None = None,
    company_context_path: str | None = None,
) -> AgentConstitution:
    """Load constitution with optional RAG augmentation (degrades gracefully)."""
    base = load_constitution(
        constitution_path=constitution_path,
        company_context_path=company_context_path,
    )
    if not rag_enabled:
        return base
    from agent_runtime.semantic_memory import fetch_constitution_rag_chunks

    chunks = fetch_constitution_rag_chunks(
        rag_query or "HVAC operator procedure CP2025",
        database_url=database_url,
    )
    if not chunks:
        return base
    extra = "\n\n".join(str(c.get("text") or c.get("content") or "") for c in chunks).strip()
    if not extra:
        return base
    merged_rules = (base.hvac_rules + "\n\n## Kontekst RAG\n" + extra).strip()
    sections = dict(base.sections)
    sections["Kontekst RAG"] = extra
    return AgentConstitution(
        hvac_rules=merged_rules,
        company_context=base.company_context,
        forbidden_actions=base.forbidden_actions,
        tool_allowlist=base.tool_allowlist,
        tool_budget=dict(base.tool_budget),
        language=base.language,
        sections=sections,
        source_path=base.source_path,
    )


def load_constitution(
    *,
    constitution_path: Path | str | None = None,
    company_context_path: str | None = None,
) -> AgentConstitution:
    path = Path(constitution_path or os.environ.get("AGENT_CONSTITUTION_PATH", "") or DEFAULT_CONSTITUTION_PATH)
    if not path.is_file():
        raise FileNotFoundError(f"Agent constitution not found: {path}")
    raw = path.read_text(encoding="utf-8")
    sections = _parse_sections(raw)
    hvac_parts = [
        sections.get("Co wiesz (semantyka)", ""),
        sections.get("Jak myślisz (procedura)", ""),
    ]
    hvac_rules = "\n\n".join(part for part in hvac_parts if part).strip()

    # P2-B: Compute hash and detect changes
    import hashlib
    current_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    hash_path = path.with_suffix(path.suffix + ".sha256")
    if hash_path.is_file():
        stored_hash = hash_path.read_text(encoding="utf-8").strip()[:16]
        if stored_hash and stored_hash != current_hash:
            logger = get_logger(__name__)
            logger.warning("Constitution modified: %s (hash changed: %s -> %s)", path.name, stored_hash, current_hash)
    hash_path.write_text(current_hash, encoding="utf-8")

    return AgentConstitution(
        hvac_rules=hvac_rules or raw[:4000],
        company_context=load_company_context(company_context_path),
        forbidden_actions=_DEFAULT_FORBIDDEN,
        tool_allowlist=_extract_allowlist_from_sections(sections),
        tool_budget=dict(MAIL_AGENT_TOOL_BUDGET),
        sections=sections,
        source_path=str(path),
    )
