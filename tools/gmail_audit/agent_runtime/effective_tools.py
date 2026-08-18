"""Effective tool availability for one planner turn (PLANNER-EXEC-FIDELITY-01).

Constitution allowlist is necessary but not sufficient. Tools whose handlers
cannot succeed due to known missing configuration must not be offered to the
model as executable options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from agent_runtime.constitution import AgentConstitution
from agent_runtime.policy_guardrails import filter_planner_allowlist
from agent_runtime.settings import AgentRuntimeSettings

# Tools that require a configured endpoint / client before they are offered.
_CONFIG_GATED_TOOLS: dict[str, str] = {
    "call_kalk_top_quote": "kalk_top_base_url",
}


@dataclass(frozen=True)
class ToolAvailabilityDecision:
    tool_name: str
    offered: bool
    reason_code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class EffectiveToolAvailability:
    offered: tuple[str, ...]
    filtered: tuple[ToolAvailabilityDecision, ...] = field(default_factory=tuple)
    unavailable_notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offered": list(self.offered),
            "filtered": [
                {
                    "tool_name": item.tool_name,
                    "offered": item.offered,
                    "reason_code": item.reason_code,
                    "detail": item.detail,
                }
                for item in self.filtered
            ],
            "unavailable_notes": list(self.unavailable_notes),
        }


def _setting_present(settings: AgentRuntimeSettings | None, attr: str) -> bool:
    if settings is None:
        return False
    return bool(str(getattr(settings, attr, "") or "").strip())


def config_gate_reason(
    tool_name: str,
    *,
    settings: AgentRuntimeSettings | None,
) -> ToolAvailabilityDecision | None:
    """Return a filter decision when a tool is known-unexecutable from config."""
    name = str(tool_name or "").strip()
    required_attr = _CONFIG_GATED_TOOLS.get(name)
    if not required_attr:
        return None
    if _setting_present(settings, required_attr):
        return None
    env_hint = {
        "kalk_top_base_url": "KALK_TOP_BASE_URL",
    }.get(required_attr, required_attr.upper())
    return ToolAvailabilityDecision(
        tool_name=name,
        offered=False,
        reason_code="TOOL_CONFIGURATION_MISSING",
        detail=f"{env_hint} is not configured",
    )


def compute_effective_available_tools(
    available_tools: Iterable[str],
    *,
    constitution: AgentConstitution,
    settings: AgentRuntimeSettings | None = None,
    mutation_frozen: bool = True,
    send_frozen: bool = True,
    snapshot: Any | None = None,
    decision_context: dict[str, Any] | None = None,
) -> EffectiveToolAvailability:
    """Intersect constitution allowlist with runtime executability.

    Network health checks are intentionally out of scope — config presence is
    enough to avoid offering tools that cannot run.
    """
    base = filter_planner_allowlist(tuple(available_tools), constitution)
    offered: list[str] = []
    filtered: list[ToolAvailabilityDecision] = []
    notes: list[str] = []

    write_tools = frozenset({"propose_mutation", "propose_plan"})
    send_tools = frozenset({"send_email", "auto_send"})

    kalk_decision = None
    if snapshot is not None and "call_kalk_top_quote" in base:
        from agent_runtime.kalk_eligibility import decision_from_snapshot

        kalk_decision = decision_from_snapshot(
            snapshot,
            decision_context=decision_context,
        )

    for name in base:
        if name in send_tools and send_frozen:
            decision = ToolAvailabilityDecision(
                tool_name=name,
                offered=False,
                reason_code="SEND_FREEZE",
                detail="send freeze active",
            )
            filtered.append(decision)
            notes.append(f"{name}: SEND_FREEZE")
            continue
        if name in write_tools and mutation_frozen:
            # Mail constitution already excludes these; keep explicit for chat.
            decision = ToolAvailabilityDecision(
                tool_name=name,
                offered=False,
                reason_code="MUTATION_FREEZE",
                detail="mutation freeze active",
            )
            filtered.append(decision)
            notes.append(f"{name}: MUTATION_FREEZE")
            continue
        if name == "call_kalk_top_quote" and kalk_decision is not None:
            if not kalk_decision.offered:
                decision = ToolAvailabilityDecision(
                    tool_name=name,
                    offered=False,
                    reason_code="KALK_TOP_NOT_ELIGIBLE",
                    detail=";".join(kalk_decision.reasons) or "not eligible",
                )
                filtered.append(decision)
                notes.append(f"{name}: {decision.detail}")
                continue
            # Eligible: fall through to the config gate below so a missing
            # KALK_TOP_BASE_URL still removes the tool from the offered set.
        gated = config_gate_reason(name, settings=settings)
        if gated is not None:
            filtered.append(gated)
            notes.append(f"{name}: {gated.detail}")
            continue
        offered.append(name)
        filtered.append(
            ToolAvailabilityDecision(tool_name=name, offered=True, reason_code="AVAILABLE")
        )

    return EffectiveToolAvailability(
        offered=tuple(offered),
        filtered=tuple(filtered),
        unavailable_notes=tuple(notes),
    )


__all__ = [
    "EffectiveToolAvailability",
    "ToolAvailabilityDecision",
    "compute_effective_available_tools",
    "config_gate_reason",
]
