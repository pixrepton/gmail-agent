"""OpenAI gpt-4o-mini tool planner (PR-C) with OpenRouter fallback.

LLM Resilience features:
- One owned planner budget (3 x 30s) shared across the endpoint chain, via llm_deadline
- Per-attempt timeout (30s) handed to the OpenAI-compatible client, clamped by remaining budget
- SDK-internal retry disabled (max_retries=0); retry/fallback belongs to the visible endpoint loop
- Retryable vs permanent failure status codes
- Circuit breaker per provider (3 failures → 30s cooldown)
- Hallucination detection — no silent write fallback
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import yaml

from llm_deadline import (
    attempt_timeout_sec,
    provider_budget_scope,
    provider_budget_sec,
    remaining_sec as deadline_remaining_sec,
    resolve_min_attempt_sec,
    stage_deadline,
)
from agent_runtime.constitution import AgentConstitution
from agent_runtime.settings import AgentRuntimeSettings, build_agent_planner_endpoints
from agent_runtime.tool_result import ToolCallPlan
from agent_runtime.tool_schemas import openai_tool_definitions
from exceptions import (
    LLMError,
    LLMHallucinationError,
    LLMRateLimitError,
    LLMTimeoutError,
    OpenAIAgentPlannerError,
)
from log_config import get_logger
from agent_runtime.circuit_breaker import get_breaker

logger = get_logger("llm_planner")

LLM_CLIENT_TIMEOUT_SEC = 30   # per-attempt HTTP timeout handed to the OpenAI-compatible client

# Total wall-clock budget for one planner call across its whole endpoint chain, owned here and
# shared downward (see llm_deadline). Derived, not chosen: three real endpoint attempts at the
# configured per-attempt timeout, i.e. 3 x LLM_CLIENT_TIMEOUT_SEC.
#
# This replaces LLM_TIMEOUT_SEC = 45, which was a ThreadPoolExecutor hard kill. That value was
# *shorter than the mechanism it wrapped*: the OpenAI SDK defaults to max_retries=2 and applies
# `timeout` per attempt, so one create() call could legitimately need 3 x 30s plus backoff -- and
# the 45s kill fired first, making the SDK's own retry unreachable. It also never cancelled
# anything, so the abandoned request kept running and kept occupying one of only two slots in a
# shared pool, letting orphaned work block later planner calls at the queue.
AGENT_PLANNER_BUDGET_SEC = 3 * LLM_CLIENT_TIMEOUT_SEC

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_PERMANENT_FAILURE_STATUS = frozenset({400, 401, 402, 403})

# ── Agent goals from YAML (externalized) ─────────────────────────────

_GOAL_BY_KIND_FALLBACK: dict[str, str] = {
    "wycena_oferta": "Lead ofertowy. Zbierz profil (metraż, miasto, OZC), policz wycenę (call_kalk_top_quote) i przygotuj draft (generate_draft_reply). Pytaj o metraż TYLKO gdy brak.",
    "zapytanie_klienta": "Lead przedofertowy. Doprecyzuj potrzebę, zbierz metraż/miasto; gdy dość danych — wycena i draft. O metraż pytaj tylko gdy brak.",
    "awaria_naprawa": "Zgłoszenie awarii/usterki. NIE pytaj o metraż. Ustal urządzenie i objaw, przygotuj draft serwisowy lub przekaż operatorowi (request_operator_clarification).",
    "przeglad_konserwacja": "Przegląd/konserwacja. NIE pytaj o metraż. Zaproponuj termin/zakres, przygotuj draft lub przekaż operatorowi.",
    "faktura_sprzedaz": "Faktura sprzedażowa do klienta. Nie prowadź doboru HVAC. Przekaż operatorowi z krótkim podsumowaniem (kogo/czego dotyczy).",
    "ksiegowosc": "Korespondencja księgowa/bank. Nie prowadź HVAC. Podsumuj i przekaż operatorowi (request_operator_clarification).",
    "faktura_zakup": "Faktura zakupowa/kosztowa. Nie prowadź HVAC. Podsumuj dostawcę i kwotę, przekaż operatorowi.",
    "zakupy_materialow": "Zamówienie części/narzędzi (np. Allegro). Nie prowadź HVAC. Podsumuj przedmiot, przekaż operatorowi.",
    "szkolenie": "Szkolenie/webinar/zaproszenie. Nie prowadź HVAC. Podsumuj temat/termin, przekaż operatorowi.",
    "inne": "Sklasyfikuj i przekaż operatorowi z jednozdaniowym podsumowaniem.",
    "niezaklasyfikowane": "Najpierw uruchom extract_facts_from_text, aby ustalić case_kind.",
}


def _load_agent_goals() -> dict[str, str]:
    """Load agent goals from YAML config file; fallback to hardcoded dict on failure."""
    config_path = Path(__file__).resolve().parent.parent / "config" / "agent_goals.yaml"
    try:
        with open(str(config_path), "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        goals = data.get("goals", _GOAL_BY_KIND_FALLBACK) if isinstance(data, dict) else _GOAL_BY_KIND_FALLBACK
        if isinstance(goals, dict) and len(goals) >= 5:
            return goals
        return _GOAL_BY_KIND_FALLBACK
    except Exception:
        logger.warning("Failed to load agent_goals.yaml, using fallback")
        return _GOAL_BY_KIND_FALLBACK


_GOAL_BY_KIND = _load_agent_goals()


def _completed_rag_research(snapshot: Any) -> list[str]:
    items: list[str] = []
    for step in getattr(snapshot.agent_memory, "reasoning_trace", []) or []:
        summary = str(getattr(step, "summary_pl", "") or "").strip()
        if summary.startswith("RAG query="):
            items.append(summary[:320])
    return items[-8:]


def _brain1_context(snapshot: Any) -> dict[str, Any] | None:
    """SLICE-3A: Brain 1's Understanding, structured, for the planner.

    Brain 1 authors what the case MEANS; Brain 2 plans what to DO about it. Until this slice the
    planner received that meaning only as a <=400-char Polish sentence folded into
    `agent_memory.reasoning_trace` by `graph._ground_current_signal` — so `missing_critical_fields`,
    `risks` and `recommended_next_step_pl` reached it, if at all, as prose it would have to
    re-parse, and were dropped entirely once the trace scrolled past the last three entries.

    This reads the EXISTING `snapshot.case_understanding` (+ its provenance envelope). It creates
    no second semantic representation and persists nothing: it is an ephemeral view assembled at
    prompt-build time from fields Brain 1 already owns.

    Absent Understanding returns None rather than an empty scaffold — the planner must be able to
    tell "Brain 1 said nothing" from "Brain 1 said nothing was missing".
    """
    understanding = getattr(snapshot, "case_understanding", None)
    if understanding is None:
        return None
    provenance = getattr(snapshot, "case_understanding_provenance", None)
    view: dict[str, Any] = {"understanding": understanding.model_dump(exclude_none=True)}
    if provenance is not None:
        view["provenance"] = provenance.model_dump(exclude_none=True)
    return view


def _compact_view(snapshot: Any) -> dict[str, Any]:
    """Kompaktowy widok stanu dla plannera — bez pełnego dumpa (oszczędność tokenów)."""
    view = {
        "case_id": snapshot.case_id or "(nowy lead — brak case_id)",
        "case_kind": snapshot.case_kind,
        "operational_status": snapshot.operational_status.model_dump(),
        "hvac_profile": snapshot.hvac_profile.model_dump(exclude_none=True),
        "gaps": [g.model_dump() for g in snapshot.gaps][:6],
        "actions": [a.model_dump() for a in snapshot.actions][:4],
        "completed_rag_research": _completed_rag_research(snapshot),
        # kept for backward compatibility: the one-line brief still travels here, but it is no
        # longer the only channel through which Brain 1 reaches the planner
        "recent_steps": [r.summary_pl for r in snapshot.agent_memory.reasoning_trace[-3:]],
    }
    brain1 = _brain1_context(snapshot)
    if brain1 is not None:
        view["brain1_context"] = brain1
        # Roadmap 1.3: surface next-step + tool-class at top level so the planner
        # does not have to dig into nested understanding / re-parse vague escalate.
        und = brain1.get("understanding") if isinstance(brain1.get("understanding"), dict) else {}
        next_step = str(und.get("recommended_next_step_pl") or "").strip()
        hint = str(und.get("planner_action_hint") or "").strip()
        if next_step:
            view["preferred_operator_next_step_pl"] = next_step[:400]
        if hint:
            view["preferred_tool_class"] = hint[:80]
    policy_envelope = getattr(snapshot, "policy_action_envelope", None)
    if policy_envelope is not None:
        view["policy_action_envelope"] = policy_envelope.model_dump(exclude_none=True)
    return view


class OpenAIAgentPlannerError(RuntimeError):
    pass


class OpenAIToolPlanner:
    """Implements ToolPlanner via Chat Completions + tool_calls."""

    def __init__(
        self,
        *,
        settings: AgentRuntimeSettings,
        client: Any | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self.last_tokens_used: int = 0
        self.last_effective_tools: dict[str, Any] = {}

    def plan_next_tool(
        self,
        *,
        snapshot: Any,
        available_tools: tuple[str, ...],
        constitution: AgentConstitution,
    ) -> ToolCallPlan:
        endpoints = build_agent_planner_endpoints(self._settings)
        if not endpoints and not str(self._settings.openai_api_key or "").strip():
            raise OpenAIAgentPlannerError(
                "No agent planner LLM configured (set AGENT_OPENAI_NATIVE_API_KEY, "
                "AGENT_OPENAI_API_KEY, or GROQ/CEREBRAS/NVIDIA keys)"
            )
        from agent_runtime.effective_tools import compute_effective_available_tools

        # Graph already applied the eligibility gate with authoritative
        # decision_context. Re-evaluating here without that context would
        # fail-closed eligible quote-ready cases. This layer only reapplies
        # the config/freeze filter on the already-scoped offered set.
        effective = compute_effective_available_tools(
            available_tools,
            constitution=constitution,
            settings=self._settings,
        )
        filtered = effective.offered
        if not filtered:
            raise OpenAIAgentPlannerError("No tools available after policy/config filter")
        tools = openai_tool_definitions(filtered)
        messages = self._build_messages(
            snapshot=snapshot,
            constitution=constitution,
            available_tools=filtered,
            unavailable_notes=effective.unavailable_notes,
        )
        logger.info(
            "LLM_TOOL_OFFER",
            extra={
                "x": {
                    "tools": list(filtered),
                    "filtered_out": list(effective.unavailable_notes),
                }
            },
        )
        # Expose last effective availability for telemetry / proofs (no persistence).
        self.last_effective_tools = effective.as_dict()
        last_exc: Exception | None = None
        # One owned budget for the whole planner call, shared with the endpoint chain below.
        with stage_deadline("agent_planner", AGENT_PLANNER_BUDGET_SEC) as deadline:
            for index, endpoint in enumerate(endpoints):
                provider_name = endpoint.base_url or "openai"
                breaker = get_breaker(provider_name)
                if breaker.is_open:
                    logger.warning("CIRCUIT_SKIP_PROVIDER", extra={"x": {"provider": provider_name}})
                    continue
                # Never start an endpoint attempt the budget cannot let finish: it would only
                # manufacture a timeout and hide why the chain actually stopped.
                if not deadline.has_room_for_attempt():
                    logger.warning("PLANNER_BUDGET_EXHAUSTED", extra={"x": {
                        "provider": provider_name,
                        "attempt": index + 1,
                        "terminal_failure_reason": "planner_deadline_exhausted",
                        **deadline.telemetry(),
                    }})
                    break
                client = self._client if self._client is not None else self._build_client(
                    base_url=endpoint.base_url,
                    api_key=endpoint.api_key,
                )
                endpoint_budget = provider_budget_sec(
                    providers_remaining=len(endpoints) - index,
                    deadline=deadline,
                )
                try:
                    logger.info("LLM_CALL_START", extra={"x": {
                        "provider": provider_name,
                        "model": endpoint.model,
                        "attempt": index + 1,
                        "total_endpoints": len(endpoints),
                        "endpoint_budget_ms": (
                            None if endpoint_budget is None else int(round(endpoint_budget * 1000))
                        ),
                    }})
                    # Each endpoint gets an even share of what is left, so a slow endpoint
                    # cannot consume the turn reserved for the ones not yet tried.
                    with provider_budget_scope(provider_name, endpoint_budget):
                        response = _call_llm_with_timeout(
                            client=client,
                            model=endpoint.model,
                            messages=messages,
                            tools=tools,
                            provider_name=provider_name,
                            reasoning_effort=endpoint.reasoning_effort,
                            thinking_enabled=endpoint.thinking_enabled,
                        )
                    self.last_tokens_used = _extract_token_usage(response)
                    logger.info("LLM_CALL_COMPLETED", extra={"x": {
                        "provider": provider_name,
                        "model": endpoint.model,
                        "tokens_used": self.last_tokens_used,
                        "attempt": index + 1,
                    }})
                    breaker.record_success()
                    plan = self._parse_tool_call(response)
                    # Bind the observed canonical identity offered to the LLM
                    # (the same envelope serialized into the prompt). The
                    # reference monitor compares this with the current
                    # envelope's source_semantic_hash; a mismatch denies the
                    # plan before any tool executes (canonical_semantic_drift).
                    envelope = getattr(snapshot, "policy_action_envelope", None)
                    source_hash = (
                        str(getattr(envelope, "source_semantic_hash", "") or "")
                        if envelope is not None
                        else ""
                    )
                    if source_hash and not str(plan.semantic_hash or "").strip():
                        plan = plan.model_copy(update={"semantic_hash": source_hash})
                    return plan
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    breaker.record_failure()
                    is_hallucinated = "tool call validation failed" in str(exc).lower()

                    # Detect rate limit or timeout for backoff
                    is_rate_limit = isinstance(exc, LLMRateLimitError) or _is_rate_limit_error(exc)
                    is_timeout = isinstance(exc, LLMTimeoutError) or "timeout" in str(exc).lower()

                    logger.warning("LLM_FALLBACK", extra={"x": {
                        "from_provider": provider_name,
                        "model": endpoint.model,
                        "reason": str(exc)[:200],
                        "attempt": index + 1,
                        "is_hallucination": is_hallucinated,
                        "is_rate_limit": is_rate_limit,
                    }})
                    if is_hallucinated:
                        raise  # re-raise immediately — never fallback a hallucination
                    # DeepSeek priority-1 (DEEPSEEK-MIGRATION-1): a DeepSeek-side failure — retryable
                    # or not — must not abort the whole planner call when the pre-existing chain
                    # (Cerebras/NVIDIA/Groq/OpenRouter/...) behind it can still be tried. Every other
                    # position keeps the prior "non-retryable aborts the chain" semantics unchanged.
                    is_deepseek_fallback_allowed = endpoint.label.startswith("deepseek") and _deepseek_should_fallback(exc)
                    if not _is_retryable(exc) and not is_deepseek_fallback_allowed:
                        raise OpenAIAgentPlannerError(str(exc)) from exc

                    # Exponential backoff before next provider (rate limit / timeout).
                    # Clamped by the budget: sleeping past the point where the next endpoint
                    # could still run turns "we will fall back" into "we will time out waiting".
                    if is_rate_limit or is_timeout:
                        wait_sec: float = 2 ** index
                        remaining = deadline_remaining_sec()
                        if remaining is not None:
                            wait_sec = max(0.0, min(wait_sec, remaining - resolve_min_attempt_sec(None)))
                        logger.info("LLM_BACKOFF", extra={"x": {
                            "provider": provider_name,
                            "wait_sec": wait_sec,
                            "attempt": index + 1,
                        }})
                        if wait_sec > 0:
                            time.sleep(wait_sec)
        # All endpoints exhausted via hallucination → raise, never write-fallback
        if last_exc and "tool call validation failed" in str(last_exc).lower():
            raw_tool_name = str(getattr(last_exc, "tool_name", "") or "unknown")
            raise LLMHallucinationError(
                "LLM called unknown tool or returned empty target — no silent write fallback",
                context={
                    "raw_tool_name": raw_tool_name,
                    "turn": getattr(self, "_turn_count", 0),
                    "action": "BLOCKED — propose_mutation fallback disabled by security policy",
                }
            ) from last_exc
        raise OpenAIAgentPlannerError(str(last_exc or "planner failed"))

    def _build_client(self, *, base_url: str | None = None, api_key: str | None = None) -> Any:
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover
            raise OpenAIAgentPlannerError("openai package is required for PR-C") from exc
        return OpenAI(
            api_key=api_key or self._settings.openai_api_key,
            base_url=base_url or self._settings.openai_base_url,
            # The SDK's own retry loop is a second, invisible retry layer whose worst case
            # (max_retries=2 -> 3 attempts, timeout applied per attempt, plus backoff) used to
            # exceed the wrapper meant to bound it. Retry and fallback belong to the endpoint
            # loop above, which is budgeted, logged and observable.
            max_retries=0,
        )

    def _build_messages(
        self,
        *,
        snapshot: Any,
        constitution: AgentConstitution,
        available_tools: tuple[str, ...] = (),
        unavailable_notes: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        sections = "\n\n".join(
            f"## {title}\n{body}"
            for title, body in constitution.sections.items()
            if body.strip() and "allowlist" not in title.lower()
        )
        goal = _GOAL_BY_KIND.get(snapshot.case_kind, _GOAL_BY_KIND["niezaklasyfikowane"])
        unavailable_block = ""
        if unavailable_notes:
            unavailable_block = (
                "Narzedzia niedostepne w tej turze (NIE wywoluj ich; to brak konfiguracji/capability, "
                f"nie blad rozumienia sprawy): {'; '.join(unavailable_notes)}.\n"
            )

        # EVAL-RECOVERY-1: the draft/follow-up instructions below must only reference
        # tools actually offered this turn. propose_mutation(operation=generate_draft)
        # (Model B: caller supplies drafted content in `payload`) and generate_draft_reply
        # (Model A: `intent` is a classification label only, the handler composes content
        # deterministically — handlers.py generate_draft_reply) are two different
        # contracts. Priming an agent whose allowlist excludes propose_mutation
        # (constitution_mail.py) toward propose_mutation's content-bearing shape was the
        # root cause of the observed generate_draft_reply argument-schema mismatch
        # (clean-eval-rerun INT-04/NEW-02: model sent {"quote": "<text>", ...} instead of
        # {"intent": "quote"}).
        if "propose_mutation" in available_tools:
            draft_instruction = (
                "Gdy masz wystarczające dane — przygotuj draft przez propose_mutation(operation=generate_draft) zamiast pytać operatora; "
            )
            followup_instruction = (
                "NIE wywołuj extract_facts_from_text — przejdź od razu do propose_mutation z odpowiednią operacją "
                "(np. update_case_status lub generate_draft). "
            )
        elif "generate_draft_reply" in available_tools:
            draft_instruction = (
                'Gdy masz wystarczające dane — przygotuj draft przez generate_draft_reply(intent="quote"|"missing_info") zamiast pytać operatora. '
                "intent to WYŁĄCZNIE etykieta klasyfikacji szablonu — NIE dodawaj żadnych innych argumentów ani treści draftu, "
                "treść generuje system automatycznie z profilu klienta. "
            )
            followup_instruction = (
                # CLOSEOUT-01 Phase 3: deterministic, ordered first-action policy for follow-ups.
                # Removes the ambiguous "request_operator_clarification LUB report_gaps_and_stop"
                # (an unresolved OR) and the turn-0 research ambiguity that made the first tool
                # choice nondeterministic on identical follow-up input. General (all follow-ups),
                # grounded in hitl_policy, prefers the safe operator-handoff class; not case-specific.
                "NIE wywołuj extract_facts_from_text — sprawa już istnieje. "
                "Nie zaczynaj tury od narzędzi read/search (search_*, list_*, read_*), jeśli stan nie wskazuje "
                "konkretnej, blokującej luki informacyjnej wymagającej ich użycia. "
                "Wybierz pierwsze działanie deterministycznie w tej kolejności: "
                "(1) generate_draft_reply — tylko gdy masz wystarczające dane do konkretnej, bezpiecznej odpowiedzi; "
                "(2) w przeciwnym razie request_operator_clarification — gdy potrzebna jest decyzja operatora albo brakuje "
                "danych do bezpiecznego działania (to jest domyślne działanie follow-upu bez gotowego draftu); "
                "(3) report_gaps_and_stop — tylko gdy nie ma ani gotowego draftu, ani żadnej kwestii/decyzji do przekazania operatorowi. "
            )
        else:
            draft_instruction = ""
            followup_instruction = "NIE wywołuj extract_facts_from_text — sprawa już istnieje. "

        system = (
            f"{sections}\n\n"
            f"Narzedzia dostepne w tej turze: {', '.join(available_tools)}.\n"
            f"{unavailable_block}"
            f"Zakazane akcje: {', '.join(constitution.forbidden_actions)}.\n"
            f"Język odpowiedzi operatora: {constitution.language}.\n"
            f"Typ sprawy (case_kind): {snapshot.case_kind}. {goal}\n"
            "Wybierz dokładnie jedno narzędzie z allowlisty na tę turę. "
            "Kolejnego narzedzia read/search uzyj tylko wtedy, gdy potrafisz wskazac konkretny brak informacji niepokryty przez completed_rag_research lub recent_steps. "
            "Nie powtarzaj research objective, dla ktorego istnieje successful RAG evidence. Gdy dowody wystarczaja, przejdz do draftu, clarification albo stopu. "
            f"{draft_instruction}"
            # INTELLIGENCE-QUALITY-BASELINE-LIFT-01: operator policy for out-of-system context
            # references (OUT_OF_SYSTEM_CONTEXT_REFERENCE). General (any channel/phrasing, no
            # case-id, no hardcoded phrase), gated on being UNVERIFIABLE from the thread/case data,
            # so cases with linked context are unaffected. The operator holds context the system
            # cannot: ask them first, draft only after.
            "Jeśli wiadomość opiera się na wcześniejszych ustaleniach spoza tego systemu (odwołuje się do wcześniejszej "
            "rozmowy, spotkania lub ustaleń), których NIE możesz zweryfikować w wątku ani w danych sprawy, a ten kontekst "
            "jest potrzebny do bezpiecznego działania — PIERWSZYM działaniem musi być request_operator_clarification, aby "
            "operator uzupełnił znany mu kontekst. NIE zaczynaj wtedy od search_*/read_*/list_* — tego kontekstu z definicji "
            "nie ma w wątku, w sprawie ani w RAG, więc research go nie odnajdzie; od razu wywołaj request_operator_clarification. "
            "Wiadomość do klienta (generate_draft_reply) przygotuj dopiero po jego uzyskaniu lub po potwierdzeniu operatora, "
            "że kontekst jest niedostępny. "
            "o metraż pytaj tylko dla spraw ofertowych (wycena_oferta / zapytanie_klienta).\n"
            "Jeśli preferred_operator_next_step_pl LUB brain1_context.understanding.recommended_next_step_pl "
            "jest ustawione — to jest DOMINUJĄCY cel tej tury. "
            "preferred_tool_class (jeśli obecne) wskazuje klasę narzędzia; wybierz konkretne narzędzie "
            "z dostępnej allowlisty zgodne z tą klasą i z treścią next_step. "
            "ZAKAZ: nie zastępuj tego ogólnym escalate_internal / «wymagana ręczna ocena» / ignore / wait. "
            "Nie pytaj ponownie o fakty już obecne w hvac_profile; brain1 missing_critical_fields "
            "to lista BRAKÓW — pytaj tylko o te pola.\n"
            "WAŻNE: Jeśli case_id jest już ustawione (nie jest puste ani '(nowy lead)') — to jest FOLLOW-UP na istniejącej sprawie. "
            f"{followup_instruction}"
            "extract_facts_from_text używaj TYLKO gdy case_id jest puste (nowy lead)."
        )
        if constitution.company_context:
            system += f"\n\nKontekst firmy:\n{constitution.company_context[:8000]}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Bieżący stan sprawy (kompaktowy widok EngagementSnapshot.v2). "
                    "Zrób następny krok zgodny z celem dla tego typu sprawy.\n\n"
                    + json.dumps(_compact_view(snapshot), ensure_ascii=False)
                ),
            },
        ]
        if snapshot.user_instruction:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "<operator_instruction>\n"
                        f"{snapshot.user_instruction}\n"
                        "</operator_instruction>"
                    ),
                }
            )
        return messages

    def _parse_tool_call(self, response: Any) -> ToolCallPlan:
        choice = response.choices[0]
        message = choice.message
        finish_reason = str(getattr(choice, "finish_reason", "") or "").strip().lower()
        tool_calls = getattr(message, "tool_calls", None) or []
        if not tool_calls:
            if finish_reason == "stop":
                return ToolCallPlan(tool_name="report_gaps_and_stop", arguments={})
            raise OpenAIAgentPlannerError("OpenAI response missing tool_calls")
        call = tool_calls[0]
        fn = call.function
        name = str(fn.name or "").strip()
        raw_args = str(fn.arguments or "{}")
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        return ToolCallPlan(tool_name=name, arguments=args)


def _call_llm_with_timeout(
    *,
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    provider_name: str,
    reasoning_effort: str = "",
    thinking_enabled: bool = False,
) -> Any:
    """Execute one bounded endpoint attempt, inside the planner's budget.

    Two deliberate properties:

    * **No wrapper thread.** The call runs on the caller's thread and is bounded by the HTTP
      timeout it hands the client, so nothing can be abandoned mid-flight. The previous
      ``ThreadPoolExecutor`` + ``future.result(timeout=45)`` never cancelled anything; the
      request kept running and kept holding one of two shared worker slots.
    * **``max_retries=0``.** The SDK's own retry loop is an *invisible* second retry layer whose
      worst case (3 attempts x per-attempt timeout + backoff) exceeded the wrapper that was
      supposed to bound it. Retry and fallback belong to the endpoint loop in
      ``plan_next_tool``, which is visible, budgeted and logged; the SDK does exactly one
      attempt per endpoint.

    ``extra_body`` carries DeepSeek's thinking-mode toggle/effort as raw top-level request
    fields — the openai SDK merges it into the JSON body regardless of whether the pinned SDK
    version recognizes ``reasoning_effort`` as a named parameter, so it's never passed as a
    direct kwarg (would risk a TypeError on stricter SDK versions).
    """
    attempt_timeout = attempt_timeout_sec(LLM_CLIENT_TIMEOUT_SEC)
    if attempt_timeout <= 0:
        raise LLMTimeoutError(
            "Planner budget exhausted before the endpoint attempt could start",
            context={"provider": provider_name, "model": model},
        )
    bounded_client = _bounded_client(client, attempt_timeout)

    create_kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        tools=tools,
        timeout=attempt_timeout,
    )
    if thinking_enabled:
        create_kwargs["extra_body"] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort or "high",
        }
    else:
        create_kwargs["tool_choice"] = "auto"
        create_kwargs["temperature"] = 0.2
    try:
        return bounded_client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        if _is_timeout_error(exc):
            raise LLMTimeoutError(
                f"LLM endpoint attempt timed out after {attempt_timeout:.1f}s "
                f"(configured per-attempt {LLM_CLIENT_TIMEOUT_SEC}s)",
                context={"provider": provider_name, "model": model},
            ) from exc
        raise


def _bounded_client(client: Any, attempt_timeout: float) -> Any:
    """Re-option a real SDK client so one attempt means one HTTP request.

    Only genuine ``openai`` clients are re-optioned. Injected doubles are returned untouched:
    a ``MagicMock`` would happily auto-create ``with_options()`` and hand back a *different*
    mock, silently detaching the call from whatever the caller configured.
    """
    with_options = getattr(client, "with_options", None)
    if not callable(with_options):
        return client
    if not type(client).__module__.startswith("openai"):
        return client
    return with_options(max_retries=0, timeout=attempt_timeout)


def _is_timeout_error(exc: Exception) -> bool:
    """Recognize a transport timeout from the SDK or from httpx underneath it."""
    if isinstance(exc, TimeoutError):
        return True
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


def _is_retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status is not None:
        try:
            s = int(status)
            if s in _PERMANENT_FAILURE_STATUS:
                return False
            if s in _RETRYABLE_STATUS or s in {404, 408}:
                return True
            if s == 400 and "tool call validation failed" in str(exc).lower():
                return True
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__.lower()
    if "timeout" in name or "connection" in name or "rate" in name:
        return True
    text = str(exc).lower()
    if "requires more credits" in text or ("insufficient" in text and "credit" in text):
        return True
    return "timeout" in text or "429" in text or "rate limit" in text


def _deepseek_should_fallback(exc: Exception) -> bool:
    """DeepSeek fallback is allowed for operational/provider failures, not adapter bugs."""
    text = str(exc).lower()
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status == 400:
        return False
    if (
        "invalid request" in text
        or "unsupported parameter" in text
        or "tool_choice" in text
    ):
        return False
    if status in {401, 403} or "invalid api key" in text or "unauthorized" in text or "forbidden" in text:
        return True
    if "requires more credits" in text or ("insufficient" in text and "credit" in text):
        return True
    return _is_retryable(exc)


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect rate limit errors from exception attributes or string patterns."""
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status is not None:
        try:
            return int(status) == 429
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__.lower()
    if "rate" in name:
        return True
    return "429" in str(exc) or "rate limit" in str(exc).lower()


def _extract_token_usage(response: Any) -> int:
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0
    total = getattr(usage, "total_tokens", None)
    if total is not None:
        try:
            return int(total)
        except (TypeError, ValueError):
            pass
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return prompt + completion
