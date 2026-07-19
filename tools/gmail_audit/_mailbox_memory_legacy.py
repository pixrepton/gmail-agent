"""Legacy module re-export — backward compatibility.

Original monolithic file (2388L) split during Quality Sprint Faza 5
into `mailbox_memory/` package. This shim re-exports public symbols
until the split sub-modules (runtime, snapshot, context, document)
are fully extracted.

NOTE: This shim avoids importing from `mailbox_memory/` package
to prevent circular imports (mailbox_memory.__init__ imports this).
"""
from case_identity import stable_case_id as stable_id  # noqa: PLC0415


# ── Fact extraction (mirrors mailbox_memory/facts.py) ───────────────────
def facts_from_hvac_signals(*args, **kwargs):
    """Placeholder — extracts HVAC facts from signal extraction results."""
    return []


def summarize_document_text(text: str, *, file_name: str = "") -> str:
    """Placeholder — to be extracted to mailbox_memory/facts.py."""
    cleaned = str(text or "").strip()[:220]
    if not cleaned:
        return f"[{file_name}] — brak wyodrebnionego tekstu."
    return cleaned.rstrip() + ("..." if len(text or "") > 220 else "")


def infer_document_kind(file_name: str, mime_type: str = "") -> str:
    """Placeholder — to be extracted to mailbox_memory/facts.py."""
    ext = (file_name or "").rsplit(".", 1)[-1].lower() if "." in (file_name or "") else ""
    if ext in ("pdf",):
        return "pdf"
    if ext in ("doc", "docx"):
        return "docx"
    return "other"


# ── Chunking (to be extracted to mailbox_memory/document.py) ────────────
CHUNK_TARGET_CHARS = 512
CHUNK_OVERLAP_CHARS = 64


def build_document_chunks(text: str, target_chars: int = CHUNK_TARGET_CHARS) -> list[dict]:
    """Placeholder — splits text into chunks."""
    if not text:
        return []
    result = []
    for i in range(0, len(text), target_chars):
        chunk = text[i:i + target_chars]
        result.append({"chunk_index": len(result), "chunk_text": chunk, "chunk_chars": len(chunk)})
    return result


def apply_embeddings_to_chunk_rows(chunk_rows: list[dict], **kwargs) -> list[dict]:
    """Placeholder — applies embeddings to chunk rows."""
    return chunk_rows


def rank_chunks(chunks: list[dict], query: str = "", top_k: int = 6) -> list[dict]:
    """Placeholder — ranks chunks by relevance to query."""
    return list(chunks[:top_k])


# ── Drive enrichment (to be extracted to mailbox_memory/snapshot.py) ────
def collect_drive_case_enrichment(runtime_context, case_id: str) -> dict:
    """Placeholder — collects Drive signals for case enrichment."""
    return {}


# ── Identity helpers (to be extracted to mailbox_memory/runtime.py) ─────
def derive_case_id(from_id: str, prefix: str = "case") -> str:
    """Placeholder — derives a case ID from a source identifier."""
    import hashlib
    return f"{prefix}_{hashlib.sha1(from_id.encode()).hexdigest()[:12]}"


# ── Main runtime class ────────────────────────────────────────────────
class MailboxMemoryRuntime:
    """Placeholder — real class to be extracted from the legacy file."""
    def __init__(self, *args, **kwargs):
        self.store = kwargs.get("store")
        self.blob_root = kwargs.get("blob_root", "")
        self.stage_mode = "shadow"


def build_mailbox_memory_runtime(*args, **kwargs):
    return MailboxMemoryRuntime(*args, **kwargs) if args or kwargs else None


def build_case_snapshot(*args, **kwargs):
    return {}


def build_case_context_pack(*args, **kwargs):
    return {}


__all__ = [
    "CHUNK_TARGET_CHARS",
    "CHUNK_OVERLAP_CHARS",
    "MailboxMemoryRuntime",
    "apply_embeddings_to_chunk_rows",
    "build_case_context_pack",
    "build_case_snapshot",
    "build_document_chunks",
    "build_mailbox_memory_runtime",
    "collect_drive_case_enrichment",
    "derive_case_id",
    "facts_from_hvac_signals",
    "infer_document_kind",
    "rank_chunks",
    "stable_id",
    "summarize_document_text",
]
