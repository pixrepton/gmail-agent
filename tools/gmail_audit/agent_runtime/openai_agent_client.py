"""OpenAI gpt-4o-mini tool planner (PR-C) with OpenRouter fallback.

LLM Resilience features:
- Hard timeout (45s) per LLM call via ThreadPoolExecutor
- Provider-level timeout (30s) passed to OpenAI client
- Retryable vs permanent failure status codes
- Circuit breaker per provider (3 failures → 30s cooldown)
- Hallucination detection — no silent write fallback
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

import yaml

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

LLM_TIMEOUT_SEC = 45          # hard kill: ThreadPoolExecutor timeout
LLM_CLIENT_TIMEOUT_SEC = 30   # soft timeout: passed to OpenAI client

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


def _compact_view(snapshot: Any) -> dict[str, Any]:
    """Kompaktowy widok stanu dla plannera — bez pełnego dumpa (oszczędność tokenów)."""
    return {
        "case_id": snapshot.case_id or "(nowy lead — brak case_id)",
        "case_kind": snapshot.case_kind,
        "operational_status": snapshot.operational_status.model_dump(),
        "hvac_profile": snapshot.hvac_profile.model_dump(exclude_none=True),
        "gaps": [g.model_dump() for g in snapshot.gaps][:6],
        "actions": [a.model_dump() for a in snapshot.actions][:4],
        "recent_steps": [r.summary_pl for r in snapshot.agent_memory.reasoning_trace[-3:]],
    }


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
        # Shared thread pool for timeout-enforced LLM calls
        self._executor = ThreadPoolExecutor(max_workers=2)

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
        from agent_runtime.policy_guardrails import filter_planner_allowlist

        filtered = filter_planner_allowlist(available_tools, constitution)
        if not filtered:
            raise OpenAIAgentPlannerError("No tools available after policy filter")
        tools = openai_tool_definitions(filtered)
        messages = self._build_messages(snapshot=snapshot, constitution=constitution, available_tools=filtered)
        logger.info("LLM_TOOL_OFFER", extra={"x": {"tools": list(filtered)}})
        last_exc: Exception | None = None
        for index, endpoint in enumerate(endpoints):
            provider_name = endpoint.base_url or "openai"
            breaker = get_breaker(provider_name)
            if breaker.is_open:
                logger.warning("CIRCUIT_SKIP_PROVIDER", extra={"x": {"provider": provider_name}})
                continue
            client = self._client if self._client is not None else self._build_client(
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
            )
            try:
                logger.info("LLM_CALL_START", extra={"x": {
                    "provider": provider_name,
                    "model": endpoint.model,
                    "attempt": index + 1,
                    "total_endpoints": len(endpoints),
                }})
                response = _call_llm_with_timeout(
                    client=client,
                    model=endpoint.model,
                    messages=messages,
                    tools=tools,
                    executor=self._executor,
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
                return self._parse_tool_call(response)
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

                # Exponential backoff before next provider (rate limit / timeout)
                if is_rate_limit or is_timeout:
                    wait_sec = 2 ** index
                    logger.info("LLM_BACKOFF", extra={"x": {
                        "provider": provider_name,
                        "wait_sec": wait_sec,
                        "attempt": index + 1,
                    }})
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
        )

    def _build_messages(
        self,
        *,
        snapshot: Any,
        constitution: AgentConstitution,
        available_tools: tuple[str, ...] = (),
    ) -> list[dict[str, Any]]:
        sections = "\n\n".join(
            f"## {title}\n{body}"
            for title, body in constitution.sections.items()
            if body.strip() and "allowlist" not in title.lower()
        )
        goal = _GOAL_BY_KIND.get(snapshot.case_kind, _GOAL_BY_KIND["niezaklasyfikowane"])
        instruction_prefix = ""
        if snapshot.user_instruction:
            instruction_prefix = f"Instrukcja operatora: {snapshot.user_instruction}\n\n"

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
                "(update_case_status, schedule_visit, generate_draft). "
            )
        elif "generate_draft_reply" in available_tools:
            draft_instruction = (
                'Gdy masz wystarczające dane — przygotuj draft przez generate_draft_reply(intent="quote"|"missing_info") zamiast pytać operatora. '
                "intent to WYŁĄCZNIE etykieta klasyfikacji szablonu — NIE dodawaj żadnych innych argumentów ani treści draftu, "
                "treść generuje system automatycznie z profilu klienta. "
            )
            followup_instruction = (
                "NIE wywołuj extract_facts_from_text — sprawa już istnieje; użyj generate_draft_reply gdy masz dość danych, "
                "w przeciwnym razie request_operator_clarification lub report_gaps_and_stop. "
            )
        else:
            draft_instruction = ""
            followup_instruction = "NIE wywołuj extract_facts_from_text — sprawa już istnieje. "

        system = (
            f"{instruction_prefix}{sections}\n\n"
            f"Narzedzia dostepne w tej turze: {', '.join(available_tools)}.\n"
            f"Zakazane akcje: {', '.join(constitution.forbidden_actions)}.\n"
            f"Język odpowiedzi operatora: {constitution.language}.\n"
            f"Typ sprawy (case_kind): {snapshot.case_kind}. {goal}\n"
            "Wybierz dokładnie jedno narzędzie z allowlisty na tę turę. "
            f"{draft_instruction}"
            "o metraż pytaj tylko dla spraw ofertowych (wycena_oferta / zapytanie_klienta).\n"
            "WAŻNE: Jeśli case_id jest już ustawione (nie jest puste ani '(nowy lead)') — to jest FOLLOW-UP na istniejącej sprawie. "
            f"{followup_instruction}"
            "extract_facts_from_text używaj TYLKO gdy case_id jest puste (nowy lead)."
        )
        if constitution.company_context:
            system += f"\n\nKontekst firmy:\n{constitution.company_context[:8000]}"
        return [
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
    executor: ThreadPoolExecutor,
    provider_name: str,
    reasoning_effort: str = "",
    thinking_enabled: bool = False,
) -> Any:
    """Execute client.chat.completions.create with a hard timeout.

    ``extra_body`` carries DeepSeek's thinking-mode toggle/effort as raw top-level request
    fields — the openai SDK merges it into the JSON body regardless of whether the pinned SDK
    version recognizes ``reasoning_effort`` as a named parameter, so it's never passed as a
    direct kwarg (would risk a TypeError on stricter SDK versions).
    """
    create_kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        tools=tools,
        timeout=LLM_CLIENT_TIMEOUT_SEC,
    )
    if thinking_enabled:
        create_kwargs["extra_body"] = {
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort or "high",
        }
    else:
        create_kwargs["tool_choice"] = "auto"
        create_kwargs["temperature"] = 0.2
    future = executor.submit(
        client.chat.completions.create,
        **create_kwargs,
    )
    try:
        return future.result(timeout=LLM_TIMEOUT_SEC)
    except TimeoutError:
        raise LLMTimeoutError(
            f"LLM call timed out after {LLM_TIMEOUT_SEC}s",
            context={"provider": provider_name, "model": model},
        ) from None


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
