"""Contract tests for search_rag_knowledge: constitution -> schema -> authz -> dispatch -> handler."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

from agent_runtime.authz import TOOL_PERMISSION_LEVELS, check_tool_permission
from agent_runtime.constitution import load_constitution
from agent_runtime.constitution_chat import CHAT_AGENT_TOOL_ALLOWLIST
from agent_runtime.constitution_mail import MAIL_AGENT_TOOL_ALLOWLIST
from agent_runtime.openai_agent_client import OpenAIToolPlanner, _compact_view
from agent_runtime.settings import AgentRuntimeSettings
from agent_runtime.snapshot_delta import apply_snapshot_delta
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tool_schemas import openai_tool_definitions
from agent_runtime.tools.handlers import search_rag_knowledge
from agent_runtime.tools_registry import AgentToolRegistry
from llm_contracts.engagement_snapshot_v2 import EngagementSnapshotV2


def _snapshot(**kwargs: object) -> EngagementSnapshotV2:
    base = {
        "engagement_id": "eng_rag",
        "case_id": "case_rag",
        "version": 1,
        "trace_id": "sig_rag",
        "operational_status": {"code": "enriching", "steps_remaining": 8},
        "hvac_profile": {"location": {}},
        "gaps": [],
        "agent_memory": {
            "reasoning_trace": [],
            "tool_calls": [],
            "constitution_sections_used": [],
        },
        "actions": [],
        "hitl_gate": {"required": False, "reason": ""},
    }
    base.update(kwargs)
    return EngagementSnapshotV2.model_validate(base)


def _settings() -> AgentRuntimeSettings:
    return AgentRuntimeSettings(
        enabled=True,
        mode="prep",
        model="gpt-4o-mini",
        model_fallback="",
        max_rounds=12,
        openai_api_key="sk-test",
        openai_base_url="https://api.openai.com/v1",
        kalk_top_base_url="",
        kalk_top_agent_key="",
        kalk_top_timeout_sec=4,
        kalk_top_max_retries=3,
    )


def _mock_client_returning(tool_name: str, arguments_json: str = "{}") -> MagicMock:
    mock_client = MagicMock()
    fn = MagicMock()
    fn.name = tool_name
    fn.arguments = arguments_json
    tool_call = MagicMock()
    tool_call.function = fn
    message = MagicMock()
    message.tool_calls = [tool_call]
    choice = MagicMock()
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    mock_client.chat.completions.create.return_value = response
    return mock_client


# ── real-contract test doubles ─────────────────────────────────────────────
#
# The real store protocol (mailbox_memory.protocol.MailboxMemoryStore) is:
#
#   fetch_semantic_chunk_candidates_for_case(
#       case_id, query_vector_literal, *, limit_mailbox=50, limit_drive=50
#   )
#
# i.e. it takes a pre-computed pgvector literal string, not raw query text,
# and only accepts limit_mailbox/limit_drive (keyword-only). Fakes below
# enforce that shape so a handler that still passes query_text=/limit= blows
# up with TypeError instead of silently degrading.


class _FakeEmbeddingRuntime:
    def __init__(self, vectors: list[list[float] | None] | None = None, *, error: Exception | None = None) -> None:
        self._vectors = vectors if vectors is not None else [[0.1, 0.2, 0.3]]
        self._error = error
        self.calls: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float] | None]:
        self.calls.append(list(texts))
        if self._error is not None:
            raise self._error
        return list(self._vectors)


class _WorkingStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls: list[tuple] = []

    def fetch_semantic_chunk_candidates_for_case(
        self, case_id, query_vector_literal, *, limit_mailbox: int = 50, limit_drive: int = 50
    ):
        self.calls.append((case_id, query_vector_literal, limit_mailbox, limit_drive))
        return list(self._rows)


class _BrokenStore:
    def fetch_semantic_chunk_candidates_for_case(
        self, case_id, query_vector_literal, *, limit_mailbox: int = 50, limit_drive: int = 50
    ):
        raise RuntimeError("rag backend down")


class _SpyStore:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def fetch_semantic_chunk_candidates_for_case(
        self, case_id, query_vector_literal, *, limit_mailbox: int = 50, limit_drive: int = 50
    ):
        self.calls.append((case_id, query_vector_literal, limit_mailbox, limit_drive))
        return []


def _wire_embedding_runtime(monkeypatch: pytest.MonkeyPatch, runtime: object | None) -> None:
    """Patch the canonical embedding-runtime factory the handler resolves at call time."""
    monkeypatch.setattr("config.load_settings", lambda **_kwargs: MagicMock(openai_compat_embedding_dimensions=0))
    monkeypatch.setattr("embedding_runtime.build_embedding_runtime", lambda _settings: runtime)


# ── 1. Allowlists: search_rag_knowledge is present where expected ─────────


def test_mail_and_chat_allowlists_contain_search_rag_knowledge() -> None:
    assert "search_rag_knowledge" in MAIL_AGENT_TOOL_ALLOWLIST
    assert "search_rag_knowledge" in CHAT_AGENT_TOOL_ALLOWLIST
    assert "query_anything" not in CHAT_AGENT_TOOL_ALLOWLIST


def test_active_constitution_allows_search_rag_knowledge() -> None:
    constitution = load_constitution()
    assert "search_rag_knowledge" in constitution.tool_allowlist


# ── 2. Schema registry: the actual bug (RED before fix) ───────────────────


def test_openai_tool_definitions_includes_search_rag_knowledge_for_active_constitution() -> None:
    """The real openai_tool_definitions() entrypoint must not silently drop an allowlisted tool."""
    constitution = load_constitution()
    assert "search_rag_knowledge" in constitution.tool_allowlist
    defs = openai_tool_definitions(constitution.tool_allowlist)
    names = [d["function"]["name"] for d in defs]
    assert "search_rag_knowledge" in names, (
        f"search_rag_knowledge is allowlisted but missing from openai_tool_definitions() output: {names}"
    )


def test_schema_not_present_when_tool_not_allowlisted() -> None:
    defs = openai_tool_definitions(("read_google_drive_file",))
    names = [d["function"]["name"] for d in defs]
    assert "search_rag_knowledge" not in names
    assert "query_anything" not in names


def test_search_rag_knowledge_schema_matches_handler_contract() -> None:
    defs = openai_tool_definitions(("search_rag_knowledge",))
    assert len(defs) == 1
    fn = defs[0]["function"]
    assert fn["name"] == "search_rag_knowledge"
    params = fn["parameters"]
    props = params["properties"]
    assert "query" in props
    assert props["query"]["type"] == "string"
    assert params.get("additionalProperties") is False
    assert params.get("required") == ["query"]
    assert "case_id" not in props


# ── 3. Real model-request entrypoint sees the tool (contract, not just dict) ─


def test_openai_planner_offers_search_rag_knowledge_when_allowlisted() -> None:
    mock_client = _mock_client_returning("search_rag_knowledge", '{"query": "Mitsubishi dobor mocy"}')
    constitution = load_constitution()
    planner = OpenAIToolPlanner(settings=_settings(), client=mock_client)
    plan = planner.plan_next_tool(
        snapshot=_snapshot(),
        available_tools=constitution.tool_allowlist,
        constitution=constitution,
    )
    assert plan.tool_name == "search_rag_knowledge"
    _, call_kwargs = mock_client.chat.completions.create.call_args
    sent_tool_names = [t["function"]["name"] for t in call_kwargs["tools"]]
    assert "search_rag_knowledge" in sent_tool_names


# ── 4. authz: fail-closed ──────────────────────────────────────────────────


def test_authz_search_rag_knowledge_requires_at_least_service_scope() -> None:
    assert TOOL_PERMISSION_LEVELS.get("search_rag_knowledge") == "service"
    assert check_tool_permission("search_rag_knowledge", "service") is True
    assert check_tool_permission("search_rag_knowledge", "operator") is True
    assert check_tool_permission("search_rag_knowledge", "") is False


# ── 5. dispatch -> handler ─────────────────────────────────────────────────


def test_dispatch_reaches_search_rag_knowledge_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    registry = AgentToolRegistry()
    store = _WorkingStore([{"chunk_text": "fragment testowy", "source": "mailbox"}])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "test"})
    result = registry.execute(plan, context=ctx)
    assert result.status == "ok"
    assert "RAG" in result.turn_summary_pl


def test_search_rag_knowledge_no_store_is_reported_as_backend_unavailable() -> None:
    """No mailbox store wired is a backend-unavailable condition, not a fake 'ok'."""
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=None)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "test"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert "niedostępny" in result.turn_summary_pl.lower()


# ── 6. controlled degradation when RAG backend errors (RED before fix) ────


def test_search_rag_knowledge_backend_error_is_reported_as_error_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=_BrokenStore())
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "test"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert result.status != "ok"


def test_search_rag_knowledge_zero_results_is_ok_with_empty_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real, successful call that finds nothing must be status=ok, distinct from a backend error."""
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "brak trafien xyz"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "ok"
    assert "0 fragmentów" in result.turn_summary_pl


def test_search_rag_knowledge_results_preserve_source_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    rows = [{"chunk_text": "instrukcja doboru mocy pompy", "source": "drive", "document_id": "doc-1"}]
    store = _WorkingStore(rows)
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "ok"
    trace = result.snapshot_delta["agent_memory"]["reasoning_trace"]
    assert "instrukcja doboru mocy pompy" in trace[0]["summary_pl"]


def test_successful_rag_result_preserves_query_and_evidence_for_next_planner_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    rows = [
        {
            "chunk_text": "procedura follow-up: klient odklada decyzje na miesiac",
            "source": "mailbox",
            "document_id": "doc-fu-05",
            "chunk_id": "chunk-fu-05",
        }
    ]
    store = _WorkingStore(rows)
    snapshot = _snapshot()
    ctx = ToolExecutionContext.from_snapshot(snapshot, mailbox_store=store)
    plan = ToolCallPlan(
        tool_name="search_rag_knowledge",
        arguments={"query": "klient odklada decyzje follow-up"},
    )

    result = search_rag_knowledge(plan, ctx)
    updated = apply_snapshot_delta(snapshot, result.snapshot_delta)
    planner_input = _compact_view(updated)
    recent = "\n".join(planner_input["recent_steps"])

    assert result.status == "ok"
    assert "query=klient odklada decyzje follow-up" in recent
    assert "hits=1" in recent
    assert "chunk-fu-05" in recent
    assert "procedura follow-up" in recent


# ── 7. required query: backend must not be called without a real query ────


def test_search_rag_knowledge_missing_query_does_not_call_backend() -> None:
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_search_rag_knowledge_empty_query_does_not_call_backend() -> None:
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": ""})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_search_rag_knowledge_whitespace_only_query_does_not_call_backend() -> None:
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "   \t  "})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_search_rag_knowledge_no_mitsubishi_fallback_in_reasoning() -> None:
    """Regression: an empty query must never be silently substituted with a hardcoded domain query."""
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=None)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": ""})
    result = search_rag_knowledge(plan, ctx)
    assert "mitsubishi" not in result.turn_summary_pl.lower()


# ── 8. embedding + vector-store contract (the actual production bug) ──────


def test_embedding_runtime_receives_trimmed_query(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_embed = _FakeEmbeddingRuntime()
    _wire_embedding_runtime(monkeypatch, fake_embed)
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "  dobor mocy  "})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "ok"
    assert fake_embed.calls == [["dobor mocy"]]


def test_embedding_generated_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_embed = _FakeEmbeddingRuntime()
    _wire_embedding_runtime(monkeypatch, fake_embed)
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    search_rag_knowledge(plan, ctx)
    assert len(fake_embed.calls) == 1


def test_store_receives_correct_query_vector_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime(vectors=[[1.0, 2.5, -3.0]]))
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    search_rag_knowledge(plan, ctx)
    assert len(store.calls) == 1
    _, query_vector_literal, _, _ = store.calls[0]
    assert query_vector_literal == "[1.0,2.5,-3.0]"


def test_store_receives_limit_mailbox_and_limit_drive_not_legacy_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store protocol is keyword-only limit_mailbox/limit_drive — no bare `limit`."""
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    search_rag_knowledge(plan, ctx)
    _, _, limit_mailbox, limit_drive = store.calls[0]
    assert isinstance(limit_mailbox, int) and limit_mailbox > 0
    assert isinstance(limit_drive, int) and limit_drive > 0


def test_store_never_receives_query_text_kwarg(monkeypatch: pytest.MonkeyPatch) -> None:
    """A store double that rejects `query_text=` must still be called successfully."""

    class _StrictStore:
        def fetch_semantic_chunk_candidates_for_case(
            self, case_id, query_vector_literal, *, limit_mailbox: int = 50, limit_drive: int = 50
        ):
            return []

    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=_StrictStore())
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "ok"


def test_embedding_error_does_not_call_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime(error=RuntimeError("ollama down")))
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_missing_embedding_runtime_does_not_call_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, None)
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_wrong_embedding_dimension_does_not_call_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "config.load_settings",
        lambda **_kwargs: MagicMock(openai_compat_embedding_dimensions=768),
    )
    monkeypatch.setattr(
        "embedding_runtime.build_embedding_runtime",
        lambda _settings: _FakeEmbeddingRuntime(vectors=[[0.1, 0.2, 0.3]]),  # only 3 dims, expected 768
    )
    store = _SpyStore()
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=store)
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"
    assert store.calls == []


def test_store_error_gives_controlled_error_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=_BrokenStore())
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "error"


def test_case_id_cannot_be_overridden_by_model_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Case scope always comes from ctx.snapshot, never from model-supplied arguments."""
    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    store = _WorkingStore([])
    ctx = ToolExecutionContext.from_snapshot(_snapshot(case_id="case_rag"), mailbox_store=store)
    plan = ToolCallPlan(
        tool_name="search_rag_knowledge",
        arguments={"query": "dobor mocy", "case_id": "case_attacker_controlled"},
    )
    search_rag_knowledge(plan, ctx)
    called_case_id = store.calls[0][0]
    assert called_case_id == "case_rag"


def test_search_rag_knowledge_tool_is_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store double exposes no write methods — a passing call proves the tool never invoked one."""

    class _ReadOnlyStore:
        def fetch_semantic_chunk_candidates_for_case(
            self, case_id, query_vector_literal, *, limit_mailbox: int = 50, limit_drive: int = 50
        ):
            return [{"chunk_text": "x", "source": "mailbox"}]

    _wire_embedding_runtime(monkeypatch, _FakeEmbeddingRuntime())
    ctx = ToolExecutionContext.from_snapshot(_snapshot(), mailbox_store=_ReadOnlyStore())
    plan = ToolCallPlan(tool_name="search_rag_knowledge", arguments={"query": "dobor mocy"})
    result = search_rag_knowledge(plan, ctx)
    assert result.status == "ok"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
