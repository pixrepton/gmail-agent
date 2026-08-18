
"""Agent graph engine â€” Python owns state; planner picks tools."""

from __future__ import annotations

from log_config import get_logger
import threading
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from agent_runtime.constitution import AgentConstitution
from agent_runtime.decision_divergence import evaluate_decision_divergence
from agent_runtime.effective_tools import compute_effective_available_tools
from agent_runtime.envelope_presence import (
    classify_envelope_presence,
    policy_path_requires_envelope,
)
from agent_runtime.failure_taxonomy import attribution, attach_attribution
from agent_runtime.known_fact_guard import guard_known_fact_reask
from agent_runtime.metrics import record_agent_turn
from agent_runtime.planner import ToolPlanner
from agent_runtime.planner_run_budget import PlannerRunBudget, build_planner_run_budget
from agent_runtime.policy_action_spine import (
    annotate_action_parent_refs,
    correlate_tool_plan,
    evaluate_semantic_policy_plan_consistency,
)
from agent_runtime.snapshot_delta import apply_snapshot_delta, decrement_steps
from agent_runtime.tool_context import ToolExecutionContext
from agent_runtime.sub_agents import (
    select_preplan_sub_agent,
    select_sub_agent,
    sub_agent_handoff_note,
    tools_for_sub_agent,
)
from agent_runtime.tool_result import ToolCallPlan, ToolResult
from agent_runtime.tools_registry import AgentToolRegistry, MockToolRegistry, ToolRegistry
from agent_runtime.turn_journal import AgentTurnJournal
from llm_contracts.engagement_snapshot_v2 import (
    EngagementSnapshotV2,
    HitlGate,
    ReasoningTraceItem,
    ToolCallItem,
)

_LOOP_TERMINAL_CODES = frozenset({"pending_operator", "node_a_error"})

# Globalny timeout na jednÄ… turÄ™ agenta (sekundy).
# JeĹ›li LLM lub narzÄ™dzie zablokuje siÄ™ na dĹ‚uĹĽej, tura zostaje przerwana.
# Kolejna tura (jeĹ›li sÄ… kroki) dziaĹ‚a normalnie â€” nie zabija caĹ‚ego runu.
AGENT_TURN_TIMEOUT_SECONDS = 45

# Maksymalna liczba tool calls na jednÄ… turÄ™.
# Chroni przed zapÄ™tleniem agenta (LLM wywoĹ‚uje to samo narzÄ™dzie wielokrotnie).
MAX_TOOL_CALLS_PER_TURN = 8

_READ_ONCE_AFTER_OK_TOOLS = frozenset({"search_gmail_thread"})

_RAG_QUERY_PREFIX = "RAG query="
_RAG_QUERY_STOPWORDS = frozenset(
    {
        "case",
        "recovery",
        "klient",
        "sprawa",
        "oferta",
        "pompa",
        "ciepla",
        "ciepła",
        "szczegoly",
        "szczegóły",
        "tresc",
        "treść",
        "sprawy",
        "wiadomosci",
        "wiadomości",
    }
)

logger = get_logger(__name__)


@dataclass
class AgentTurnRecord:
    tool_name: str
    tool_status: str
    turn_summary_pl: str
    snapshot_version: int


@dataclass
class AgentGraphRunResult:
    snapshot: EngagementSnapshotV2
    turns: list[AgentTurnRecord] = field(default_factory=list)
    planner_run_budget: dict[str, Any] = field(default_factory=dict)
    last_effective_tools: dict[str, Any] = field(default_factory=dict)


class AgentGraphEngine:
    def __init__(
        self,
        *,
        planner: ToolPlanner,
        constitution: AgentConstitution,
        tool_registry: ToolRegistry | None = None,
        turn_journal: AgentTurnJournal | None = None,
        checkpoint_store: Any | None = None,
        run_id: str = "",
    ) -> None:
        self._planner = planner
        self._constitution = constitution
        self._tools: ToolRegistry = tool_registry or AgentToolRegistry()
        self._turn_journal: AgentTurnJournal | None = turn_journal
        self._checkpoint_store = checkpoint_store
        self._run_id = str(run_id or "").strip()
        # #21: WspĂłĹ‚dzielony ThreadPoolExecutor â€” zamiast tworzyÄ‡ nowy na kaĹĽde narzÄ™dzie
        self._timeout_pool: Any | None = None
        # Faza 5a: Concurrency control â€” max 1 agent run na ten engine
        self._concurrency_semaphore = threading.Semaphore(1)
        # Krok 8: Agent checkpoint â€” licznik tur dla checkpointow
        self._checkpoint_turn_count: int = 0

    def run(
        self,
        snapshot: EngagementSnapshotV2,
        *,
        context: ToolExecutionContext | None = None,
        turn_journal: AgentTurnJournal | None = None,
        start_turn_idx: int = 0,
        operator_scope: str = "",
    ) -> AgentGraphRunResult:
        # Faza 5a: Concurrency control â€” tylko 1 agent run na ten engine
        acquired = self._concurrency_semaphore.acquire(blocking=False)
        if not acquired:
            from agent_runtime.exceptions import AgentConcurrencyError
            raise AgentConcurrencyError(
                f"Agent run already in progress for engagement {snapshot.engagement_id}"
            )
        try:
            return self._run(snapshot, context=context, turn_journal=turn_journal,
                             start_turn_idx=start_turn_idx, operator_scope=operator_scope)
        finally:
            self._concurrency_semaphore.release()

    def _run(
        self,
        snapshot: EngagementSnapshotV2,
        *,
        context: ToolExecutionContext | None = None,
        turn_journal: AgentTurnJournal | None = None,
        start_turn_idx: int = 0,
        operator_scope: str = "",
    ) -> AgentGraphRunResult:
        ctx = context or ToolExecutionContext.from_snapshot(snapshot)
        if ctx.constitution is None:
            ctx.constitution = self._constitution
        ctx.snapshot = snapshot
        journal = turn_journal or self._turn_journal
        current = _ground_current_signal(snapshot, ctx.signal_payload)
        turns: list[AgentTurnRecord] = []
        turn_idx = int(start_turn_idx)
        tool_call_count = 0
        completed_read_once_tools: set[str] = set()
        run_budget = build_planner_run_budget(
            max_rounds=int(getattr(getattr(ctx, "settings", None), "max_rounds", None) or 12),
            constitution_tool_budget=dict(self._constitution.tool_budget or {}),
        )
        # Align soft turn budget with remaining steps on the snapshot.
        run_budget.max_turns = min(
            run_budget.max_turns,
            max(1, int(current.operational_status.steps_remaining)),
        )
        known_fact_correction_used = False
        while int(current.operational_status.steps_remaining) > 0:
            if _is_loop_terminal(current):
                break
            budget_hit = run_budget.check_before_turn()
            if budget_hit:
                current = apply_snapshot_delta(
                    current,
                    {
                        "operational_status": {
                            "code": "pending_operator",
                            "blocking": True,
                            "steps_remaining": 0,
                        },
                        "hitl_gate": {
                            "required": True,
                            "reason": f"planner_budget_exceeded:{budget_hit}",
                        },
                        "agent_memory": {
                            "reasoning_trace": [
                                {
                                    "turn": turn_idx,
                                    "summary_pl": (
                                        f"Budżet planera wyczerpany ({budget_hit}). "
                                        "Zatrzymano bezpiecznie z dotychczasowym evidence. "
                                        "[PLANNER_BUDGET_EXCEEDED]"
                                    ),
                                }
                            ]
                        },
                    },
                )
                break
            # Limit liczby tool calls na turÄ™ (chroni przed zapÄ™tleniem LLM)
            if tool_call_count >= MAX_TOOL_CALLS_PER_TURN:
                logger.warning(
                    "agent_turn_limit engagement=%s calls=%d",
                    current.engagement_id,
                    tool_call_count,
                )
                current = _apply_snapshot_delta_blocking(
                    current,
                    "Przekroczono limit narzÄ™dzi w tej turze. Operatorze, doprecyzuj.",
                )
                break
            ctx.snapshot = current
            sub_agent = select_preplan_sub_agent(snapshot=current)
            scoped_tools = tools_for_sub_agent(sub_agent, self._constitution.tool_allowlist)
            available_tools = _filter_completed_read_once_tools(
                scoped_tools or list(self._constitution.tool_allowlist),
                completed_read_once_tools,
            )
            effective = compute_effective_available_tools(
                available_tools,
                constitution=self._constitution,
                settings=getattr(ctx, "settings", None),
                snapshot=current,
                decision_context=(
                    ctx.signal_payload.get("decision_comparison_inputs")
                    if isinstance(getattr(ctx, "signal_payload", None), dict)
                    else None
                ),
            )
            available_tools = effective.offered
            try:
                plan = self._planner.plan_next_tool(
                    snapshot=current,
                    available_tools=available_tools,
                    constitution=self._constitution,
                )
                plan, current = _observe_policy_plan(
                    current,
                    plan,
                    decision_inputs=ctx.signal_payload.get(
                        "decision_comparison_inputs"
                    ),
                )
                ctx.snapshot = current
            except Exception as exc:
                # Failure convergence (DELIVERY-1): a planner-call exception (ghost-tool
                # schema mismatch, network error, any provider-side bug) must never silently
                # unwind the whole run with zero recorded turn and zero hitl_gate change â€”
                # that is exactly how DEC-01-class must-escalate cases were lost (RC-2).
                # Converge to the same safe, escalated terminal state report_gaps_and_stop
                # already produces, through the SAME post-processing every other turn uses.
                logger.error(
                    "agent_planner_call_failed engagement=%s exc_type=%s exc=%s",
                    current.engagement_id,
                    type(exc).__name__,
                    exc,
                )
                plan = ToolCallPlan(tool_name="planner_error", arguments={})
                plan, current = _observe_policy_plan(
                    current,
                    plan,
                    decision_inputs=ctx.signal_payload.get(
                        "decision_comparison_inputs"
                    ),
                )
                ctx.snapshot = current
                result = ToolResult(
                    status="error",
                    turn_summary_pl=f"BĹ‚Ä…d planera ({type(exc).__name__}) â€” zatrzymano, wymaga operatora.",
                    snapshot_delta={
                        "operational_status": {"code": "pending_operator", "blocking": True},
                        "hitl_gate": {"required": True, "reason": f"planner_error:{type(exc).__name__}"},
                    },
                )
                sub_agent = "general"
            else:
                policy_block = _policy_enforcement_block(
                    current,
                    plan,
                    signal_payload=ctx.signal_payload if isinstance(ctx.signal_payload, dict) else {},
                )
                if policy_block is not None:
                    result = policy_block
                    sub_agent = "general"
                elif plan.tool_name not in available_tools:
                    result = attach_attribution(
                        ToolResult(
                            status="error",
                            turn_summary_pl=(
                                f"Narzędzie {plan.tool_name} nie było dostępne w tej turze."
                            ),
                            snapshot_delta={
                                "operational_status": {
                                    "code": "pending_operator",
                                    "blocking": True,
                                },
                                "hitl_gate": {
                                    "required": True,
                                    "reason": f"tool_not_offered:{plan.tool_name}",
                                },
                            },
                        ),
                        attribution(
                            failure_class="TOOL_UNAVAILABLE",
                            owner="infra",
                            stage="planner_offer",
                            retryable=False,
                            safe_next_step="use_offered_alternative",
                            detail=f"filtered_out={list(effective.unavailable_notes)[:4]}",
                        ),
                    )
                    sub_agent = "general"
                else:
                    reask = guard_known_fact_reask(
                        tool_name=plan.tool_name,
                        arguments=plan.arguments,
                        snapshot=current,
                    )
                    if reask is not None:
                        if not known_fact_correction_used:
                            known_fact_correction_used = True
                            result = attach_attribution(
                                ToolResult(
                                    status="error",
                                    turn_summary_pl=(
                                        "Zablokowano ponowne pytanie o znany fakt: "
                                        + ", ".join(reask.get("fact_keys") or [])
                                        + ". Wybierz inną akcję w ramach budżetu."
                                    ),
                                    next_tool_hint="generate_draft_reply",
                                    snapshot_delta={
                                        "agent_memory": {
                                            "reasoning_trace": [
                                                {
                                                    "turn": 0,
                                                    "summary_pl": (
                                                        "known_fact_reask_blocked: "
                                                        + ",".join(
                                                            reask.get("fact_keys") or []
                                                        )
                                                    ),
                                                }
                                            ]
                                        }
                                    },
                                ),
                                attribution(
                                    failure_class="PLANNER_KNOWN_FACT_REASK",
                                    owner="planner",
                                    stage="pre_tool_guard",
                                    retryable=True,
                                    safe_next_step="choose_non_reask_action",
                                    detail=",".join(reask.get("fact_keys") or []),
                                ),
                            )
                        else:
                            result = attach_attribution(
                                ToolResult(
                                    status="error",
                                    turn_summary_pl=(
                                        "Ponowne pytanie o znany fakt po korekcie — "
                                        "bezpieczna abstencja, wymaga operatora."
                                    ),
                                    snapshot_delta={
                                        "operational_status": {
                                            "code": "pending_operator",
                                            "blocking": True,
                                        },
                                        "hitl_gate": {
                                            "required": True,
                                            "reason": "known_fact_reask_blocked",
                                        },
                                    },
                                ),
                                attribution(
                                    failure_class="SAFE_ABSTENTION",
                                    owner="planner",
                                    stage="pre_tool_guard",
                                    retryable=False,
                                    safe_next_step="request_operator_clarification",
                                    detail=",".join(reask.get("fact_keys") or []),
                                ),
                            )
                        sub_agent = "general"
                    else:
                        duplicate_research = _duplicate_rag_research_result(current, plan)
                        if duplicate_research is not None:
                            result = duplicate_research
                            sub_agent = "general"
                        else:
                            sub_agent = select_sub_agent(
                                tool_name=plan.tool_name, snapshot=current
                            )
                            logger.info(
                                "agent_tool_shadow engagement=%s tool=%s sub_agent=%s",
                                current.engagement_id,
                                plan.tool_name,
                                sub_agent,
                            )
                            if plan.tool_name == "generate_draft_reply":
                                from agent_runtime.draft_lineage_transport import (
                                    resolve_generate_draft_reply,
                                )

                                transferred, allow_fallback = resolve_generate_draft_reply(
                                    ctx.signal_payload
                                )
                                if transferred is not None:
                                    result = transferred
                                elif allow_fallback:
                                    result = self._execute_tool_with_timeout(
                                        plan,
                                        ctx,
                                        sub_agent=sub_agent,
                                        operator_scope=operator_scope,
                                    )
                                else:
                                    result = ToolResult(
                                        status="error",
                                        turn_summary_pl=(
                                            "Brak upstream draft i fallback zablokowany."
                                        ),
                                    )
                            else:
                                result = self._execute_tool_with_timeout(
                                    plan,
                                    ctx,
                                    sub_agent=sub_agent,
                                    operator_scope=operator_scope,
                                )
            tool_call_count += 1
            run_budget.record_turn(
                tool_name=plan.tool_name,
                status=result.status,
                tokens=int(getattr(result, "tokens_used", 0) or 0)
                + self._planner_tokens(),
            )
            if result.status == "ok" and plan.tool_name in _READ_ONCE_AFTER_OK_TOOLS:
                completed_read_once_tools.add(plan.tool_name)
            # P7: Event Spine â€” agent.tool.invoked
            _db_url = str(getattr(ctx.settings, "mailbox_database_url", "") or "").strip()
            if _db_url:
                try:
                    from event_spine.emitter import publish_os_event
                    publish_os_event(
                        database_url=_db_url,
                        event_type="agent.tool.invoked",
                        engagement_id=current.engagement_id,
                        source_repo="gmail-agent",
                        severity="error" if result.status == "error" else "info",
                        success=result.status == "ok",
                        payload={"tool_name": plan.tool_name, "sub_agent": sub_agent, "status": result.status},
                    )
                except Exception as exc:
                    logger.warning("graph: sub_agent event publish failed tool=%s sub_agent=%s exc=%s", plan.tool_name, sub_agent, exc)
            if result.status == "ok" and sub_agent != "general":
                handoff = sub_agent_handoff_note(sub_agent, plan.tool_name)
                delta = dict(result.snapshot_delta or {})
                memory = dict(delta.get("agent_memory") or {})
                trace = list(memory.get("reasoning_trace") or [])
                trace.append({"turn": len(trace), "summary_pl": handoff})
                memory["reasoning_trace"] = trace
                delta["agent_memory"] = memory
                result = result.model_copy(update={"snapshot_delta": delta})
            annotated_delta = annotate_action_parent_refs(
                result.snapshot_delta,
                plan=plan,
                envelope=current.policy_action_envelope,
            )
            if annotated_delta is not result.snapshot_delta:
                result = result.model_copy(update={"snapshot_delta": annotated_delta})
            current = _apply_tool_result(current, plan, result)
            current = decrement_steps(current)
            turns.append(
                AgentTurnRecord(
                    tool_name=plan.tool_name,
                    tool_status=result.status,
                    turn_summary_pl=result.turn_summary_pl,
                    snapshot_version=current.version,
                )
            )
            # #43: Metryki
            record_agent_turn(engagement_id=current.engagement_id, tool=plan.tool_name)
            if journal is not None:
                journal.append_turn(
                    engagement_id=current.engagement_id,
                    snapshot_version=current.version,
                    trace_id=current.trace_id,
                    plan=plan,
                    result=result,
                )
            if self._checkpoint_store is not None and self._run_id:
                self._checkpoint_store.save_checkpoint(
                    run_id=self._run_id,
                    engagement_id=current.engagement_id,
                    turn_idx=turn_idx,
                    snapshot=current,
                    planner_state={"last_tool": plan.tool_name},
                    status="running",
                )
                turn_idx += 1
            if current.hitl_gate.required or _is_loop_terminal(current):
                break
        if (
            int(current.operational_status.steps_remaining) <= 0
            and not current.hitl_gate.required
            and not _is_loop_terminal(current)
        ):
            current = apply_snapshot_delta(
                current,
                {
                    "operational_status": {
                        "code": "pending_operator",
                        "blocking": True,
                    },
                },
            )
            current = current.model_copy(
                update={
                    "hitl_gate": HitlGate(required=True, reason="budget_exhausted"),
                }
            )
        return AgentGraphRunResult(
            snapshot=current,
            turns=turns,
            planner_run_budget=run_budget.as_dict(),
            last_effective_tools={},
        )

    def _planner_tokens(self) -> int:
        tokens = getattr(self._planner, "last_tokens_used", 0)
        try:
            return int(tokens or 0)
        except (TypeError, ValueError):
            return 0

    def _execute_tool(
        self,
        plan: ToolCallPlan,
        ctx: ToolExecutionContext,
        *,
        sub_agent: str = "general",
        planner_tokens: int = 0,
        operator_scope: str = "",
    ) -> ToolResult:
        # Runtime scope check (Fix #13): verify tool is allowed for this sub-agent
        allowed = tools_for_sub_agent(sub_agent, self._constitution.tool_allowlist)
        if plan.tool_name not in allowed:
            return ToolResult(
                status="error",
                turn_summary_pl=f"NarzÄ™dzie {plan.tool_name} niedozwolone dla sub-agenta {sub_agent}.",
            )
        # Runtime Authorization Gate (PR-4D): check operator permissions
        if operator_scope:
            from agent_runtime.authz import guard_tool_authz

            operation = ""
            if plan.arguments:
                operation = str(plan.arguments.get("operation") or "")
            authz_error = guard_tool_authz(plan.tool_name, scope=operator_scope, operation=operation or None)
            if authz_error is not None:
                return ToolResult(
                    status="error",
                    turn_summary_pl=authz_error,
                )
        if isinstance(self._tools, MockToolRegistry):
            result = self._tools.execute(plan, context=ctx)
        else:
            result = self._tools.execute(plan, context=ctx)
        if planner_tokens and not result.tokens_used:
            return result.model_copy(update={"tokens_used": planner_tokens})
        return result

    def _execute_tool_with_timeout(
        self,
        plan: ToolCallPlan,
        ctx: ToolExecutionContext,
        *,
        sub_agent: str = "general",
        operator_scope: str = "",
    ) -> ToolResult:
        """Execute tool with global timeout. Uses shared thread pool."""
        try:
            import concurrent.futures

            pool = self._get_timeout_pool()
            future = pool.submit(
                self._execute_tool,
                plan,
                ctx,
                sub_agent=sub_agent,
                planner_tokens=self._planner_tokens(),
                operator_scope=operator_scope,
            )
            try:
                return future.result(timeout=AGENT_TURN_TIMEOUT_SECONDS)
            except concurrent.futures.TimeoutError:
                logger.error(
                    "agent_turn_timeout engagement=%s tool=%s timeout=%ss",
                    ctx.snapshot.engagement_id,
                    plan.tool_name,
                    AGENT_TURN_TIMEOUT_SECONDS,
                )
                return ToolResult(
                    status="error",
                    turn_summary_pl=(
                        f"Przekroczono limit czasu ({AGENT_TURN_TIMEOUT_SECONDS}s) "
                        f"dla narzÄ™dzia {plan.tool_name}. SprĂłbuj ponownie."
                    ),
                )
        except ImportError:
            return self._execute_tool(
                plan,
                ctx,
                sub_agent=sub_agent,
                planner_tokens=self._planner_tokens(),
                operator_scope=operator_scope,
            )

    def _get_timeout_pool(self) -> Any:
        """Lazy-init wspĂłĹ‚dzielonego ThreadPoolExecutor."""
        if self._timeout_pool is None:
            import concurrent.futures
            self._timeout_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="agent_timeout",
            )
        return self._timeout_pool

    def _checkpoint(self, turn_number: int) -> None:
        """Zapisuje checkpoint po kazdej turze â€” wznowienie po crashu."""
        if self._checkpoint_store is None or not self._run_id:
            return
        try:
            self._checkpoint_store.save_checkpoint(
                run_id=self._run_id,
                engagement_id="",
                turn_idx=turn_number,
                snapshot=None,
                planner_state={},
                status="running",
            )
            logger.info("AGENT_CHECKPOINT turn=%d run_id=%s", turn_number, self._run_id)
        except Exception as exc:
            logger.warning("AGENT_CHECKPOINT_FAILED turn=%d: %s", turn_number, exc)

    def shutdown(self) -> None:
        """Zwolnij zasoby puli watkow."""
        if self._timeout_pool is not None:
            self._timeout_pool.shutdown(wait=False)
            self._timeout_pool = None


def _ground_current_signal(
    snapshot: EngagementSnapshotV2,
    signal_payload: dict,
) -> EngagementSnapshotV2:
    """Fold the real current-turn signal (and any already-computed case
    understanding) into agent_memory.reasoning_trace â€” the same existing seam
    already used for tool-handoff notes (see sub_agent_handoff_note usage
    above), which _compact_view already surfaces to the planner's LLM prompt
    as `recent_steps`. Without this, a follow-up turn's planner call only sees
    stale case-level state (hvac_profile/gaps/case_kind) and has no way to
    know what the customer's new message actually says.

    A1: also sets/clears the structured `case_understanding` field consumed
    by the operator-facing feed (daszek_engagement_feed). This runs exactly
    once per external signal (start of `_run`), so `case_understanding` is
    always either the freshly-correlated projection for THIS turn's signal or
    None â€” never a stale one from an earlier turn silently carried forward.
    """
    subject = str(signal_payload.get("subject") or "").strip()
    essence = str(signal_payload.get("snippet") or "").strip() or str(signal_payload.get("body_text") or "").strip()
    understanding = str(signal_payload.get("understanding_brief_pl") or "").strip()
    projection = signal_payload.get("case_understanding_projection")
    delta: dict[str, Any] = {}
    if subject or essence or understanding:
        parts = []
        if subject or essence:
            line = f'Biezaca wiadomosc: "{subject}"' if subject else "Biezaca wiadomosc:"
            if essence:
                line = f"{line} â€” {essence[:280]}"
            parts.append(line)
        if understanding:
            parts.append(f"Zrozumienie sprawy: {understanding[:400]}")
        trace = [item.model_dump(mode="python") for item in snapshot.agent_memory.reasoning_trace]
        trace.append({"turn": len(trace), "summary_pl": " ".join(parts)})
        delta["agent_memory"] = {"reasoning_trace": trace}
    provenance = signal_payload.get("case_understanding_provenance")
    policy_envelope = signal_payload.get("policy_action_envelope")
    if isinstance(projection, dict) and projection:
        delta["case_understanding"] = projection
    elif snapshot.case_understanding is not None:
        # No fresh, correlated Understanding for THIS turn's signal â€” never
        # let a previous turn's Understanding keep looking current.
        delta["case_understanding"] = None
    # SLICE-3A: provenance moves in LOCKSTEP with the Understanding it describes. It is cleared
    # whenever the Understanding is cleared, so the snapshot can never carry a status for a
    # projection that is no longer there, nor a status computed for an earlier signal.
    if isinstance(projection, dict) and projection and isinstance(provenance, dict) and provenance:
        delta["case_understanding_provenance"] = provenance
    elif snapshot.case_understanding_provenance is not None:
        delta["case_understanding_provenance"] = None
    # SLICE-2C: the derived status travels in the SAME lockstep as the provenance it comes from,
    # so it can never describe an Understanding that is gone or one from an earlier signal. It is
    # display/triage state only -- `feed_visibility` does not read it and membership is unaffected.
    if "case_understanding_provenance" in delta:
        from case_understanding_status import build_case_understanding_status

        status = (
            build_case_understanding_status(delta["case_understanding_provenance"])
            if isinstance(delta["case_understanding_provenance"], dict)
            else None
        )
        if status is not None or snapshot.case_understanding_status is not None:
            delta["case_understanding_status"] = status
    if isinstance(policy_envelope, dict) and policy_envelope:
        delta["policy_action_envelope"] = policy_envelope
    elif snapshot.policy_action_envelope is not None:
        delta["policy_action_envelope"] = None
    # Consistency is a per-plan observation. A new external signal must never
    # inherit the previous turn's planner result.
    if snapshot.semantic_policy_plan_consistency is not None:
        delta["semantic_policy_plan_consistency"] = None
    if snapshot.decision_divergence_observation is not None:
        delta["decision_divergence_observation"] = None
    if not delta:
        return snapshot
    return apply_snapshot_delta(snapshot, delta)


def _observe_policy_plan(
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
    *,
    decision_inputs: dict[str, Any] | None = None,
) -> tuple[ToolCallPlan, EngagementSnapshotV2]:
    """Correlate and observe a plan without changing its selected tool/arguments."""
    correlated = correlate_tool_plan(plan, snapshot.policy_action_envelope)
    telemetry = evaluate_semantic_policy_plan_consistency(
        snapshot.policy_action_envelope,
        correlated,
    )
    divergence = evaluate_decision_divergence(
        decision_inputs,
        case_kind=str(snapshot.case_kind or ""),
        plan=correlated,
    )
    return correlated, snapshot.model_copy(
        update={
            "semantic_policy_plan_consistency": telemetry,
            "decision_divergence_observation": divergence,
        }
    )


def _policy_enforcement_block(
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
    *,
    signal_payload: dict[str, Any] | None = None,
) -> ToolResult | None:
    """RP-30 + PLANNER-EXEC-FIDELITY-01: enforce policy/plan conflicts and required envelope."""
    payload = signal_payload if isinstance(signal_payload, dict) else {}
    consistency = snapshot.semantic_policy_plan_consistency
    envelope = snapshot.policy_action_envelope
    presence = classify_envelope_presence(
        envelope,
        case_understanding_present=snapshot.case_understanding is not None,
        policy_required=policy_path_requires_envelope(snapshot, payload),
        harness_mode=bool(payload.get("harness_mode")),
    )
    action_tools = frozenset({"generate_draft_reply", "propose_mutation"})
    if (
        presence.get("status") == "wiring_failure"
        and plan.tool_name in action_tools
    ):
        return attach_attribution(
            ToolResult(
                status="error",
                turn_summary_pl=(
                    "Brak wymaganego policy/action envelope po Brain 1 — "
                    "fail-closed, wymaga operatora (wiring failure)."
                ),
                snapshot_delta={
                    "operational_status": {"code": "pending_operator", "blocking": True},
                    "hitl_gate": {
                        "required": True,
                        "reason": "policy_envelope_wiring_failure",
                    },
                },
            ),
            attribution(
                failure_class="POLICY_ENVELOPE_MISSING",
                owner="policy",
                stage="policy_enforcement",
                retryable=False,
                safe_next_step="rebuild_policy_spine_or_escalate",
                detail=",".join(presence.get("reason_codes") or []),
            ),
        )

    if consistency is None:
        return None
    if str(consistency.status or "") != "conflicting":
        return None
    reasons = {str(item) for item in (consistency.reason_codes or [])}
    if "policy_blocks_actionable_tool" not in reasons:
        return None
    return attach_attribution(
        ToolResult(
            status="error",
            turn_summary_pl="Polityka blokuje wybrane narzedzie — wymaga decyzji operatora.",
            snapshot_delta={
                "operational_status": {"code": "pending_operator", "blocking": True},
                "hitl_gate": {
                    "required": True,
                    "reason": f"policy_blocks_actionable_tool:{plan.tool_name}",
                },
            },
        ),
        attribution(
            failure_class="POLICY_TOOL_MISMATCH",
            owner="policy",
            stage="policy_enforcement",
            retryable=False,
            safe_next_step="request_operator_clarification",
            correlation={
                "policy_decision_id": str(consistency.policy_decision_id or ""),
                "action_proposal_id": str(consistency.action_proposal_id or ""),
            },
            detail=plan.tool_name,
        ),
    )


def _filter_completed_read_once_tools(
    available_tools: list[str] | tuple[str, ...],
    completed_read_once_tools: set[str],
) -> tuple[str, ...]:
    return tuple(
        tool
        for tool in available_tools
        if not (tool in _READ_ONCE_AFTER_OK_TOOLS and tool in completed_read_once_tools)
    )


def _duplicate_rag_research_result(
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
) -> ToolResult | None:
    if plan.tool_name != "search_rag_knowledge":
        return None
    query = str(plan.arguments.get("query") or "").strip()
    objective = _rag_research_objective(query)
    if not objective:
        return None
    if objective not in _completed_rag_research_objectives(snapshot):
        return None
    return ToolResult(
        status="error",
        turn_summary_pl=(
            "Research RAG dla tego celu informacyjnego jest juz pokryty "
            f"w tym runie: {objective}."
        ),
        snapshot_delta={
            "operational_status": {"code": "pending_operator", "blocking": True},
            "hitl_gate": {"required": True, "reason": f"duplicate_rag_research:{objective}"},
            "agent_memory": {
                "reasoning_trace": [
                    {
                        "turn": 0,
                        "summary_pl": (
                            "RAG research stop: objective already covered; "
                            f"query={query[:120]}; objective={objective}"
                        ),
                    }
                ],
            },
        },
    )


def _completed_rag_research_objectives(snapshot: EngagementSnapshotV2) -> set[str]:
    objectives: set[str] = set()
    for item in snapshot.agent_memory.reasoning_trace:
        summary = str(item.summary_pl or "")
        if not summary.startswith(_RAG_QUERY_PREFIX):
            continue
        query = summary[len(_RAG_QUERY_PREFIX):].split(";", 1)[0].strip()
        objective = _rag_research_objective(query)
        if objective:
            objectives.add(objective)
    return objectives


def _rag_research_objective(query: str) -> str:
    normalized = _normalize_rag_query(query)
    if not normalized:
        return ""
    if _has_any(
        normalized,
        ("wywoz", "demontaz", "utyliz", "stary piec", "starego pieca", "starego kotla"),
    ):
        return "old_heater_removal_scope"
    if _has_any(normalized, ("rabat", "negocjac", "konkurenc", "taniej", "cen", "koszt", "kwot")):
        return "price_negotiation"
    if _has_any(normalized, ("zerwanie", "umowy", "opoznienie", "opoznienia", "harmonogram")):
        return "contract_delay"
    if _has_any(normalized, ("akceptuje", "akceptacja", "zaliczk", "faktur", "montaz")):
        return "offer_acceptance"
    if _has_any(normalized, ("awaria", "serwis", "ogrzewania", "pilne")):
        return "service_issue"
    if _has_any(normalized, ("delivery", "status", "notification", "dostarcz")):
        return "delivery_status"
    if _has_any(normalized, ("tresc", "wiadomosc", "wiadomosci", "oryginalna", "zapytanie", "temat")):
        return "source_message_content"
    if _has_any(normalized, ("metraz", "metraż", "lokalizacja", "budynek", "adres", "telefon", "dane")):
        return "case_details"
    if _has_any(normalized, ("kontekst", "sprawy")):
        return "case_context"
    tokens = [token for token in normalized.split() if token not in _RAG_QUERY_STOPWORDS]
    return " ".join(tokens[:6]) or "case_context"


def _normalize_rag_query(query: str) -> str:
    text = str(query or "").lower().replace("ł", "l")
    text = "".join(
        char for char in unicodedata.normalize("NFKD", text) if not unicodedata.combining(char)
    )
    text = re.sub(r"case_recovery_[a-z0-9-]+", "case", text)
    text = re.sub(r"[^0-9a-z\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _apply_snapshot_delta_blocking(
    snapshot: EngagementSnapshotV2,
    reason: str,
) -> EngagementSnapshotV2:
    """Apply a blocking snapshot delta with pending_operator status."""
    updated = apply_snapshot_delta(
        snapshot,
        {
            "operational_status": {
                "code": "pending_operator",
                "blocking": True,
            },
        },
    )
    return updated.model_copy(
        update={
            "hitl_gate": HitlGate(required=True, reason=reason),
        }
    )


def _is_loop_terminal(snapshot: EngagementSnapshotV2) -> bool:
    """Graph loop stops on HITL or hard terminal codes (not ready_for_quote alone)."""
    if snapshot.hitl_gate.required:
        return True
    return snapshot.operational_status.code in _LOOP_TERMINAL_CODES


#: Fields whose sole author is Brain 1 (`intake_shared_downstream` -> `understanding_output`),
#: projected into the snapshot by `_ground_current_signal`. Brain 2's tools own desk EXECUTION
#: state; they do not get to restate what the case means. No existing tool writes either key --
#: verified across `agent_runtime/tools/` and `tools_registry.py` -- so this guard forbids a
#: capability nobody legitimately uses today, rather than removing one.
_BRAIN1_OWNED_SNAPSHOT_FIELDS = (
    "case_understanding",
    "case_understanding_provenance",
    # SLICE-2C: derived from provenance around the planner, never authored by a tool.
    "case_understanding_status",
    "policy_action_envelope",
)
_RUNTIME_OWNED_SNAPSHOT_FIELDS = (
    "semantic_policy_plan_consistency",
    "decision_divergence_observation",
)
_PROTECTED_SNAPSHOT_FIELDS = (
    *_BRAIN1_OWNED_SNAPSHOT_FIELDS,
    *_RUNTIME_OWNED_SNAPSHOT_FIELDS,
    # Ephemeral attribution payload — lives on ToolResult, not EngagementSnapshotV2.
    "execution_attribution",
    "draft_sanity",
    "planner_run_budget",
    "effective_tools",
    "envelope_presence",
)


def _strip_protected_snapshot_fields(*, delta_source: Any, tool_name: str) -> Any:
    """Central authority guard: a tool delta may never write protected fields.

    One place, not one check per tool: `_apply_tool_result` is the single point at which every
    `ToolResult.snapshot_delta` reaches the snapshot, so a new tool cannot bypass
    this by forgetting to opt in. Brain 1 projections and runtime observations
    are authored around the planner, never by an individual tool.
    """
    if not isinstance(delta_source, dict):
        return delta_source
    blocked = [field for field in _PROTECTED_SNAPSHOT_FIELDS if field in delta_source]
    if not blocked:
        return delta_source
    logger.warning(
        "PROTECTED_SNAPSHOT_FIELD_WRITE_REJECTED tool=%s fields=%s",
        tool_name,
        ",".join(blocked),
    )
    return {key: value for key, value in delta_source.items() if key not in blocked}


def _apply_tool_result(
    snapshot: EngagementSnapshotV2,
    plan: ToolCallPlan,
    result: ToolResult,
) -> EngagementSnapshotV2:
    if result.status == "node_a_error":
        updated = apply_snapshot_delta(
            snapshot,
            {"operational_status": {"code": "node_a_error"}},
        )
        return updated.model_copy(
            update={"hitl_gate": HitlGate(required=True, reason="node_a_error")}
        )
    delta = _strip_protected_snapshot_fields(
        delta_source=result.snapshot_delta,
        tool_name=plan.tool_name,
    )
    incoming_trace = (
        ((delta or {}).get("agent_memory") or {}).get("reasoning_trace")
        if isinstance(delta, dict)
        else None
    )
    if isinstance(incoming_trace, list) and incoming_trace:
        delta = dict(delta or {})
        memory_delta = dict(delta.get("agent_memory") or {})
        existing_trace = [item.model_dump(mode="python") for item in snapshot.agent_memory.reasoning_trace]
        memory_delta["reasoning_trace"] = existing_trace + list(incoming_trace)
        delta["agent_memory"] = memory_delta
    try:
        updated = apply_snapshot_delta(snapshot, delta)
    except Exception as exc:  # noqa: BLE001 â€” niespĂłjna delta narzÄ™dzia nie moĹĽe wywaliÄ‡ caĹ‚ego runu
        logger.warning("apply_snapshot_delta_rejected tool=%s: %s", plan.tool_name, exc)
        updated = snapshot
        result = result.model_copy(
            update={
                "status": "error",
                "turn_summary_pl": f"Odrzucono niespĂłjnÄ… zmianÄ™ stanu narzÄ™dzia {plan.tool_name}.",
            }
        )
    trace = list(updated.agent_memory.reasoning_trace)
    if result.turn_summary_pl:
        trace.append(
            ReasoningTraceItem(turn=len(trace) + 1, summary_pl=result.turn_summary_pl)
        )
    calls = list(updated.agent_memory.tool_calls)
    calls.append(ToolCallItem(tool=plan.tool_name, status=result.status))
    memory = updated.agent_memory.model_copy(
        update={"reasoning_trace": trace, "tool_calls": calls},
    )
    return updated.model_copy(update={"agent_memory": memory})
