"""Projection-safe Decision View blocks for Daszek operational feed (Node B only).

Invariant: projection compresses structured inputs; it must not invent action or policy
semantics absent from UnderstandingOutput, DecisionCandidate, PolicyDecision, or
ActionProposal v2 (see ``derivation_tags`` on ``collapsed_operator_pl``).

**Wording:** ``PROJECTION_COPY_PL`` / ``V2_PROJECTION_LABEL_PL`` must stay aligned with
``docs/core/action_semantics_glossary.md`` (section *Projection surface copy*).
"""

from __future__ import annotations

from typing import Any, Final

from case_context_contract import operator_feed_plain_summary
from daszek_v3_operational_feed_contract import strip_forbidden_nested
from decision_candidate import sanitize_decision_candidate_for_projection
from evidence_ref import normalize_evidence_ref

# --- Operator-facing PL strings (sync with action_semantics_glossary.md §Projection surface copy) ---
PROJECTION_COPY_PL: Final[dict[str, str]] = {
    "mailbox_fallback_essence_pl": (
        "Podgląd z pamięci skrzynki — skrót sytuacji; pełny przebieg Decision Pipeline "
        "mógł nie być zapisany w tej migawce."
    ),
    "no_v2_proposals_pl": (
        "Brak propozycji ActionProposal v2 w tej migawce (powstają po PolicyDecision; tu nie ma wykonania)."
    ),
    "card_gap_title_pl": "Luka danych",
    "card_missing_title_pl": "Brakuje danych",
    "card_conflict_facts_title_pl": "Konflikt faktów",
    "card_conflict_generic_title_pl": "Konflikt",
    "card_blocking_title_pl": "Blokada",
    "headline_fallback_pl": "Widok przeglądowy — ocena operatora",
    "expand_hint_pl": (
        "Szczegóły i źródła po rozwinięciu. Tu nie ma automatycznego wykonania ani surowej treści maila."
    ),
    "why_playbook_prefix_pl": "Instrukcja playbooka (workflow):",
    "why_candidate_mode_prefix_pl": "Tryb kandydata (DecisionCandidate / pipeline):",
    "policy_mailbox_synthetic_status_pl": "Brak PolicyDecision (skrót z pamięci skrzynki)",
    "primary_review_label_pl": "Przejrzyj (tylko podgląd)",
    "secondary_wrong_case_label_pl": "Zła sprawa (adjudikacja)",
    "secondary_missing_data_label_pl": "Brakuje danych",
    "explanation_card_essence_title_pl": "Sedno sytuacji",
    "ribbon_situation_vs_decision_hint_pl": (
        "Rekomendacja NBA (sytuacja) ≠ decyzja formalna; decyzja wymaga DecisionCandidate + PolicyDecision + ActionProposal v2."
    ),
    "evidence_fallback_title_pl": "Źródło",
    "v2_action_policy_blocked_suffix_pl": " — tylko podgląd (brak zezwolenia polityki / wymaga decyzji)",
}

SUMMARY_SOURCE_LABEL_PL: Final[dict[str, str]] = {
    "operator_essence": "Sedno operatorskie",
    "decision_case_summary": "Skrót decyzji / sprawy",
    "situation_summary": "Skrót sytuacji",
    "context_pack_summary": "Skrót kontekstu sprawy",
    "mailbox_snapshot_summary": "Skrót z pamięci skrzynki (tylko podgląd)",
    "projection_fallback": "Neutralny fallback projekcji",
}

# v2_action_type → operator label PL (matches glossary projection_label_pl / v2 table wording)
V2_PROJECTION_LABEL_PL: Final[dict[str, str]] = {
    "prepare_reply_draft": "Przygotuj odpowiedź (draft)",
    "request_missing_info": "Poproś o brakujące dane",
    "mark_attention_required": "Wymaga uwagi operatora",
    "ask_for_operator_adjudication": "Wymaga decyzji operatora",
    "no_action": "Brak akcji",
}


def _v2_action_operator_label(action_type: str) -> str:
    key = str(action_type or "").strip()
    return V2_PROJECTION_LABEL_PL.get(key, key)


def _synthetic_case_intelligence_from_mailbox(mailbox_context: dict[str, Any]) -> dict[str, Any]:
    """When full case_intelligence blob is not persisted, derive a minimal read-only slice from vnext + pack."""
    vnext = mailbox_context.get("vnext") if isinstance(mailbox_context.get("vnext"), dict) else {}
    pack = mailbox_context.get("pack") if isinstance(mailbox_context.get("pack"), dict) else {}
    cs = vnext.get("case_summary") if isinstance(vnext.get("case_summary"), dict) else {}
    snap = pack.get("snapshot") if isinstance(pack.get("snapshot"), dict) else {}
    essence = str(cs.get("summary_text") or snap.get("summary_text") or snap.get("summary") or "").strip()[:800]
    if not essence:
        essence = PROJECTION_COPY_PL["mailbox_fallback_essence_pl"]

    gaps = [str(x) for x in (vnext.get("completeness_gaps") or []) if str(x).strip()][:8]
    conflicts = vnext.get("conflicting_facts") if isinstance(vnext.get("conflicting_facts"), list) else []
    cal = vnext.get("calendar_context") if isinstance(vnext.get("calendar_context"), list) else []
    rt = pack.get("runtime_state") if isinstance(pack.get("runtime_state"), dict) else {}
    last_sig = str(rt.get("latest_signal_at") or rt.get("last_projection_refresh_at") or "").strip()

    uo: dict[str, Any] = {
        "operator_explanation": {"essence_pl": essence},
        "conflicting_facts": conflicts[:6] if conflicts else [],
    }
    dp_stub: dict[str, Any] = {
        "schema_version": "decision_pipeline_run.v1",
        "projection_ready": False,
        "outputs": {"decision_candidate": {}},
    }
    return {
        "understanding_output": uo,
        "decision_pipeline": dp_stub,
        "policy_decision": {},
        "action_proposals_v2": [],
        "_mailbox_synthetic": True,
        "_gaps_preview": gaps,
        "_calendar_event_count": len(cal),
        "_last_signal_hint": last_sig[:64],
    }


def _summary_text(value: Any, *, limit: int = 700) -> str:
    text = operator_feed_plain_summary(str(value or ""), fallback="").strip()
    return text[:limit]


def _summary_candidate(value: Any, *, role: str, path: str, tag: str) -> dict[str, str]:
    text = _summary_text(value)
    if not text:
        return {}
    return {
        "essence_pl": text,
        "source_role": role,
        "source_path": path,
        "source_label_pl": SUMMARY_SOURCE_LABEL_PL.get(role, role),
        "derivation_tag": tag,
    }


def _select_operator_essence(
    *,
    case_intelligence: dict[str, Any],
    mailbox_context: dict[str, Any],
) -> dict[str, str]:
    """Deterministic precedence for the main operator-facing essence.

    This selects among existing fields only; it does not generate a new summary.
    """
    ci = case_intelligence if isinstance(case_intelligence, dict) else {}
    mc = mailbox_context if isinstance(mailbox_context, dict) else {}
    uo = ci.get("understanding_output") if isinstance(ci.get("understanding_output"), dict) else {}
    oe = uo.get("operator_explanation") if isinstance(uo.get("operator_explanation"), dict) else {}
    dp = ci.get("decision_pipeline") if isinstance(ci.get("decision_pipeline"), dict) else {}
    cand = (dp.get("outputs") or {}).get("decision_candidate") if isinstance(dp.get("outputs"), dict) else {}
    if not isinstance(cand, dict) and isinstance(ci.get("decision_candidate"), dict):
        cand = ci.get("decision_candidate")
    cand = cand if isinstance(cand, dict) else {}
    mailbox_synthetic = bool(ci.get("_mailbox_synthetic"))
    decision_summary = ci.get("decision_summary") if isinstance(ci.get("decision_summary"), dict) else {}
    candidate_decision_summary = (
        cand.get("decision_summary") if isinstance(cand.get("decision_summary"), dict) else {}
    )
    cu = ci.get("case_understanding") if isinstance(ci.get("case_understanding"), dict) else {}
    vnext = mc.get("vnext") if isinstance(mc.get("vnext"), dict) else {}
    pack = mc.get("pack") if isinstance(mc.get("pack"), dict) else {}
    cs = vnext.get("case_summary") if isinstance(vnext.get("case_summary"), dict) else {}
    snap = pack.get("snapshot") if isinstance(pack.get("snapshot"), dict) else {}
    hot_cs = snap.get("hot_case_summary") if isinstance(snap.get("hot_case_summary"), dict) else {}

    candidates = [
        _summary_candidate(
            (ci.get("collapsed_operator_pl") or {}).get("essence_pl")
            if isinstance(ci.get("collapsed_operator_pl"), dict) and not mailbox_synthetic
            else "",
            role="operator_essence",
            path="case_intelligence.collapsed_operator_pl.essence_pl",
            tag="essence<=collapsed_operator_pl",
        ),
        _summary_candidate(
            "" if mailbox_synthetic else oe.get("essence_pl"),
            role="operator_essence",
            path="understanding_output.operator_explanation.essence_pl",
            tag="essence<=understanding_output.operator_explanation",
        ),
        _summary_candidate(
            "" if mailbox_synthetic else decision_summary.get("essence_pl") or candidate_decision_summary.get("essence_pl"),
            role="decision_case_summary",
            path="decision_summary.essence_pl",
            tag="essence<=decision_summary",
        ),
        _summary_candidate(
            "" if mailbox_synthetic else cu.get("summary_operator") or cu.get("summary_short"),
            role="decision_case_summary",
            path="case_understanding.summary_operator",
            tag="essence<=case_understanding",
        ),
        _summary_candidate(
            "" if mailbox_synthetic else uo.get("situation_summary_pl") or uo.get("summary_pl"),
            role="situation_summary",
            path="understanding_output.situation_summary_pl",
            tag="essence<=understanding_output.situation_summary",
        ),
        _summary_candidate(
            cs.get("summary_text"),
            role="context_pack_summary",
            path="mailbox_context.vnext.case_summary.summary_text",
            tag="essence<=case_context_pack.case_summary",
        ),
        _summary_candidate(
            hot_cs.get("summary_text"),
            role="mailbox_snapshot_summary",
            path="mailbox_context.pack.snapshot.hot_case_summary.summary_text",
            tag="essence<=mailbox_snapshot.hot_case_summary",
        ),
        _summary_candidate(
            snap.get("summary_text") or snap.get("summary"),
            role="mailbox_snapshot_summary",
            path="mailbox_context.pack.snapshot.summary_text",
            tag="essence<=mailbox_snapshot",
        ),
    ]
    for candidate in candidates:
        if candidate:
            return candidate
    return {
        "essence_pl": PROJECTION_COPY_PL["mailbox_fallback_essence_pl"],
        "source_role": "projection_fallback",
        "source_path": "projection.neutral_fallback",
        "source_label_pl": SUMMARY_SOURCE_LABEL_PL["projection_fallback"],
        "derivation_tag": "essence<=projection_fallback",
    }


def build_decision_view_blocks(
    *,
    case_intelligence: dict[str, Any] | None = None,
    mailbox_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Summaries and IDs only — no raw mail bodies or prompts.

    If ``case_intelligence`` is empty and ``mailbox_context`` (vnext + pack) is provided,
    builds a degraded decision view from mailbox projection fields only.
    """
    ci = case_intelligence if isinstance(case_intelligence, dict) else {}
    if not ci and isinstance(mailbox_context, dict) and (mailbox_context.get("vnext") or mailbox_context.get("pack")):
        ci = _synthetic_case_intelligence_from_mailbox(mailbox_context)

    uo = ci.get("understanding_output") if isinstance(ci.get("understanding_output"), dict) else {}
    oe = uo.get("operator_explanation") if isinstance(uo.get("operator_explanation"), dict) else {}
    dp = ci.get("decision_pipeline") if isinstance(ci.get("decision_pipeline"), dict) else {}
    cand = (dp.get("outputs") or {}).get("decision_candidate") if isinstance(dp.get("outputs"), dict) else {}
    if not cand and isinstance(ci.get("decision_candidate"), dict):
        cand = ci.get("decision_candidate")
    cand = cand if isinstance(cand, dict) else {}
    cand = sanitize_decision_candidate_for_projection(cand)
    pd = ci.get("policy_decision") if isinstance(ci.get("policy_decision"), dict) else {}
    ap_list = ci.get("action_proposals_v2") if isinstance(ci.get("action_proposals_v2"), list) else []
    pb = (dp.get("outputs") or {}).get("service_request_playbook") if isinstance(dp.get("outputs"), dict) else None
    pb = pb if isinstance(pb, dict) else {}

    selected_essence = _select_operator_essence(case_intelligence=ci, mailbox_context=mailbox_context or {})
    essence = selected_essence["essence_pl"][:500]
    playbook_hint = str(pb.get("operator_instruction") or "").strip()[:400]

    missing_cards: list[dict[str, Any]] = []
    if isinstance(ci.get("_gaps_preview"), list) and ci.get("_gaps_preview"):
        for g in ci["_gaps_preview"][:8]:
            missing_cards.append({"title_pl": PROJECTION_COPY_PL["card_gap_title_pl"], "content_pl": str(g)[:240]})
    mg = uo.get("missing_critical_fields") if isinstance(uo.get("missing_critical_fields"), list) else []
    for m in mg[:6]:
        if isinstance(m, str) and m.strip():
            missing_cards.append({"title_pl": PROJECTION_COPY_PL["card_missing_title_pl"], "content_pl": m.strip()[:240]})

    risk_cards: list[dict[str, Any]] = []
    conf = uo.get("conflicting_facts") if isinstance(uo.get("conflicting_facts"), list) else []
    for c in conf[:5]:
        if isinstance(c, dict):
            risk_cards.append(
                {
                    "title_pl": PROJECTION_COPY_PL["card_conflict_facts_title_pl"],
                    "content_pl": str(c.get("summary_pl") or c.get("field_name") or c.get("conflict_type") or "konflikt")[
                        :280
                    ],
                }
            )
        elif isinstance(c, str) and c.strip():
            risk_cards.append(
                {"title_pl": PROJECTION_COPY_PL["card_conflict_generic_title_pl"], "content_pl": c.strip()[:280]}
            )
    if cand.get("blocking_gaps") and isinstance(cand.get("blocking_gaps"), list):
        for b in cand["blocking_gaps"][:4]:
            risk_cards.append({"title_pl": PROJECTION_COPY_PL["card_blocking_title_pl"], "content_pl": str(b)[:200]})

    proposal_lines: list[str] = []
    action_cards: list[dict[str, Any]] = []
    for x in ap_list:
        if not isinstance(x, dict):
            continue
        at = str(x.get("action_type") or "")
        label = _v2_action_operator_label(at)
        policy_ok = bool(x.get("allowed_by_policy")) and bool(str(x.get("policy_decision_id") or "").strip())
        if not policy_ok:
            label = f"{label}{PROJECTION_COPY_PL['v2_action_policy_blocked_suffix_pl']}"
        proposal_lines.append(
            f"{label}: {str(x.get('summary', ''))[:120]} (status={x.get('status', '')})".strip()
        )
        action_cards.append(
            {
                "proposal_id": str(x.get("proposal_id") or ""),
                "action_type": at,
                "action_type_label_pl": label,
                "status": str(x.get("status") or ""),
                "action_mode": str(x.get("action_mode") or ""),
                "blocked_reason": str(x.get("blocked_reason") or ""),
                "requires_operator_approval": bool(x.get("requires_operator_approval")),
                "allowed_by_policy": bool(x.get("allowed_by_policy")),
                "policy_spine_ok": policy_ok,
                "summary_pl": str(x.get("summary") or x.get("recommended_action") or "")[:260],
            }
        )
    proposal_pl = (
        "; ".join(proposal_lines)[:500] if proposal_lines else PROJECTION_COPY_PL["no_v2_proposals_pl"]
    )

    nba_rec = uo.get("next_best_action_recommendation") if isinstance(uo.get("next_best_action_recommendation"), dict) else {}
    recommendation_one_liner_pl = str(nba_rec.get("title_pl") or nba_rec.get("action_type") or "").strip()[:240]

    why_parts = [essence] if essence else []
    if playbook_hint:
        why_parts.append(f"{PROJECTION_COPY_PL['why_playbook_prefix_pl']} {playbook_hint}")
    if cand.get("recommended_mode"):
        why_parts.append(f"{PROJECTION_COPY_PL['why_candidate_mode_prefix_pl']} {cand.get('recommended_mode')}")
    why_pl = " ".join(why_parts)[:900]

    change_pl = ""
    if str(ci.get("_last_signal_hint") or "").strip():
        change_pl = f"Ostatni sygnał / odświeżenie: {ci['_last_signal_hint']}"
    elif dp.get("finished_at"):
        change_pl = f"Ostatni przebieg pipeline (meta): {dp.get('finished_at')}"

    policy_label = str(pd.get("status") or "unknown")
    if not pd and ci.get("_mailbox_synthetic"):
        policy_label = PROJECTION_COPY_PL["policy_mailbox_synthetic_status_pl"]

    evidence_cards: list[dict[str, Any]] = []
    evidence_refs: list[dict[str, Any]] = []
    for src in (cand.get("evidence_refs"), uo.get("evidence_refs")):
        if isinstance(src, list):
            evidence_refs.extend(normalize_evidence_ref(x) for x in src if isinstance(x, dict))
    seen_ev: set[tuple[str, str, str]] = set()
    for ref in evidence_refs[:16]:
        key = (
            str(ref.get("source_type") or ""),
            str(ref.get("source_id") or ref.get("message_id") or ""),
            str(ref.get("evidence_role") or ""),
        )
        if key in seen_ev:
            continue
        seen_ev.add(key)
        st = str(ref.get("source_type") or "").strip()
        evidence_cards.append(
            {
                "title_pl": st or PROJECTION_COPY_PL["evidence_fallback_title_pl"],
                "content_pl": str(ref.get("evidence_role") or "supports"),
                "source_id": str(ref.get("source_id") or ref.get("message_id") or ref.get("chunk_id") or "")[:120],
            }
        )

    decision_block = {
        "topic": str(cand.get("topic") or ""),
        "case_type": str(cand.get("case_type") or ""),
        "priority": str(cand.get("priority") or ""),
        "sla_risk": str(cand.get("sla_risk") or ""),
        "recommended_mode": str(cand.get("recommended_mode") or ""),
        "risk_class_candidate": str(cand.get("risk_class_candidate") or ""),
    }
    derivation_tags: list[str] = ["triage<=decision_candidate"]
    derivation_tags.append(selected_essence["derivation_tag"])
    if recommendation_one_liner_pl:
        derivation_tags.append("recommendation<=understanding_output.next_best_action_recommendation")
    if conf:
        derivation_tags.append("conflicts<=understanding_output.conflicting_facts")
    if missing_cards:
        derivation_tags.append("missing<=understanding_output|decision_candidate")
    if playbook_hint:
        derivation_tags.append("playbook<=decision_pipeline.outputs.service_request_playbook")
    if ap_list:
        derivation_tags.append("actions<=action_proposals_v2")
    if pd:
        derivation_tags.append("policy<=policy_decision")

    collapsed_operator_pl = {
        "essence_pl": (essence[:360] or "").strip(),
        "topic": decision_block["topic"],
        "priority": decision_block["priority"],
        "sla_risk": decision_block["sla_risk"],
        "risk_class_candidate": decision_block["risk_class_candidate"],
        "recommendation_one_liner_pl": recommendation_one_liner_pl,
        "situation_vs_decision_hint_pl": PROJECTION_COPY_PL["ribbon_situation_vs_decision_hint_pl"],
        "conflict_count": len(conf),
        "missing_data_count": len(missing_cards),
        "details_collapsed_by_default": True,
        "expand_hint_pl": PROJECTION_COPY_PL["expand_hint_pl"],
        "derivation_tags": derivation_tags,
        "essence_source_role": selected_essence["source_role"],
        "essence_source_path": selected_essence["source_path"],
        "essence_source_label_pl": selected_essence["source_label_pl"],
    }
    policy_block = {
        "status": policy_label,
        "risk_class": str(pd.get("risk_class") or ""),
        "failed_rules": [str(x)[:180] for x in (pd.get("failed_rules") or [])][:8],
        "blocked_actions": [str(x)[:80] for x in (pd.get("blocked_actions") or [])][:12],
        "warnings": [str(x)[:180] for x in (pd.get("warnings") or [])][:8],
        "requires_human_approval": bool(pd.get("requires_human_approval")),
        "dry_run_only": bool(pd.get("dry_run_only")),
    }

    blocks: dict[str, Any] = {
        "headline_co_pl": essence[:160] or PROJECTION_COPY_PL["headline_fallback_pl"],
        "change_summary_pl": change_pl[:400],
        "what_changed_since_pl": change_pl[:400],
        "missing_summary_pl": f"Liczba luk (podgląd): {len(missing_cards)}",
        "risk_summary_pl": f"Liczba alertów: {len(risk_cards)}",
        "proposal_summary_pl": proposal_pl,
        "why_pl": why_pl,
        "policy_status_pl": policy_label,
        "playbook_instruction_pl": playbook_hint,
        "decision_summary": {"essence_pl": essence},
        "decision_candidate_id": str(cand.get("decision_candidate_id") or ""),
        "policy_decision_id": str(pd.get("policy_decision_id") or ""),
        "pipeline_run_id": str(dp.get("pipeline_run_id") or ""),
        "projection_ready": bool(dp.get("projection_ready")),
        "action_proposal_ids": [str(x.get("proposal_id") or "") for x in ap_list if isinstance(x, dict)][:12],
        "decision": decision_block,
        "policy": policy_block,
        "action_proposals": action_cards[:8],
        "operator_required": True,
        "primary_button": {"id": "review", "label_pl": PROJECTION_COPY_PL["primary_review_label_pl"]},
        "secondary_buttons": [
            {"id": "wrong_case", "label_pl": PROJECTION_COPY_PL["secondary_wrong_case_label_pl"]},
            {"id": "missing_data", "label_pl": PROJECTION_COPY_PL["secondary_missing_data_label_pl"]},
        ],
        "explanation_cards": (
            [{"title_pl": PROJECTION_COPY_PL["explanation_card_essence_title_pl"], "content_pl": essence}] if essence else []
        ),
        "evidence_cards": evidence_cards,
        "missing_info_cards": missing_cards,
        "risk_cards": risk_cards,
        "status_badges": [{"label": policy_label, "variant": "neutral"}],
        "collapsed_operator_pl": collapsed_operator_pl,
    }
    return strip_forbidden_nested(blocks)


def merge_decision_view_into_case_detail(case_detail: dict[str, Any], decision_view: dict[str, Any]) -> dict[str, Any]:
    out = dict(case_detail)
    out["decision_view"] = strip_forbidden_nested(decision_view)
    return strip_forbidden_nested(out)
