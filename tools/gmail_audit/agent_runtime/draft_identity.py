"""AI-OS-CANONICAL-DRAFT-IDENTITY-01: shared identity primitives for operator-facing
draft actions (`llm_contracts.engagement_snapshot_v2.ActionItem`).

`draft_id` names the opportunity/slot a draft occupies (stable across re-runs and
operator edits of the same case+signal+action-kind); `body_hash` versions its content.
Both are pure functions of already-known values -- no randomness, no invention. Follows
the existing `sha256(...)[:N]` convention used elsewhere in this codebase for stable,
inspectable ids (`agent_hitl_bridge._stable_bridge_key`, `decision_candidate._candidate_id`,
`action_proposal_v2`'s `apv2_` builder).
"""

from __future__ import annotations

import hashlib


def compute_draft_id(*, case_id: str, source_signal_id: str, action_id: str) -> str:
    """Stable identity for the draft "slot": same case+signal+action always yields the
    same draft_id, so re-running a turn or editing the body never mints a new one."""
    raw = "|".join([str(case_id or ""), str(source_signal_id or ""), str(action_id or "")])
    return "draft_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def compute_body_hash(body: str) -> str:
    """Deterministic content version. Empty body hashes to "" (not a valid draft)."""
    text = str(body or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def apply_operator_draft_edit(
    item: dict,
    *,
    draft_text: str,
    case_id: str,
    source_signal_id: str,
    action_id: str,
    case_kind: str = "",
    snapshot: object | None = None,
    intent: str = "",
) -> dict:
    """Apply an operator body onto an ActionItem-shaped dict with honest revisioning.

    - `draft_id` is minted once (kept forever for the same case+signal+action slot).
    - Content change bumps `revision` and replaces `body_hash`.
    - Identical content keeps revision/hash (idempotent re-approve of the same text).
    - Never fabricates parent lineage refs; preserves whatever was already present.
    - PF-01: re-run `evaluate_draft_sanity` before `enabled=True` (fail-closed).
    """
    from agent_runtime.draft_sanity import evaluate_draft_sanity

    out = dict(item)
    old_body = str(out.get("payload_pl") or "")
    old_hash = str(out.get("body_hash") or "") or compute_body_hash(old_body)
    new_hash = compute_body_hash(draft_text)
    resolved_case = str(out.get("case_id") or case_id or "")
    resolved_signal = str(out.get("source_signal_id") or source_signal_id or "")
    draft_id = str(out.get("draft_id") or "") or compute_draft_id(
        case_id=resolved_case,
        source_signal_id=resolved_signal,
        action_id=action_id,
    )
    try:
        prev_revision = int(out.get("revision") or 1)
    except (TypeError, ValueError):
        prev_revision = 1
    if new_hash and old_hash and new_hash == old_hash:
        revision = max(prev_revision, 1)
    elif old_body.strip() or old_hash:
        revision = max(prev_revision, 1) + 1
    else:
        revision = max(prev_revision, 1)
    out["payload_pl"] = draft_text
    out["enabled"] = True
    out["disabled_reason_pl"] = None
    out["draft_id"] = draft_id
    out["revision"] = revision
    out["body_hash"] = new_hash
    out["case_id"] = resolved_case
    if not str(out.get("source_signal_id") or ""):
        out["source_signal_id"] = resolved_signal
    if not str(out.get("identity_state") or ""):
        out["identity_state"] = "identity_incomplete"

    sanity = evaluate_draft_sanity(
        body=draft_text,
        case_kind=str(case_kind or ""),
        intent=str(intent or ""),
        snapshot=snapshot,
    )
    if not sanity.get("ok"):
        reasons = ",".join(sanity.get("reason_codes") or [])
        out["enabled"] = False
        out["disabled_reason_pl"] = f"DRAFT_SANITY_FAILED: {reasons}"
    return out


def mint_gap_only_draft_action(
    *,
    action_id: str,
    draft_text: str,
    case_id: str,
    source_signal_id: str,
    case_kind: str = "",
    snapshot: object | None = None,
    intent: str = "",
) -> dict:
    """Identity for a brand-new gap-only draft created at HITL approve time.

    PF-01: run `evaluate_draft_sanity` before `enabled=True` (fail-closed).
    """
    from agent_runtime.draft_sanity import evaluate_draft_sanity

    body_hash = compute_body_hash(draft_text)
    action = {
        "id": action_id or "draft_reply",
        "enabled": True,
        "payload_pl": draft_text,
        "disabled_reason_pl": None,
        "draft_id": compute_draft_id(
            case_id=case_id,
            source_signal_id=source_signal_id,
            action_id=action_id or "draft_reply",
        ),
        "revision": 1,
        "body_hash": body_hash,
        "case_id": case_id,
        "source_signal_id": source_signal_id,
        "identity_state": "identity_incomplete",
        "parent_policy_decision_id": "",
        "parent_action_proposal_v2_id": "",
        "parent_decision_candidate_id": "",
    }
    sanity = evaluate_draft_sanity(
        body=draft_text,
        case_kind=str(case_kind or ""),
        intent=str(intent or ""),
        snapshot=snapshot,
    )
    if not sanity.get("ok"):
        reasons = ",".join(sanity.get("reason_codes") or [])
        action["enabled"] = False
        action["disabled_reason_pl"] = f"DRAFT_SANITY_FAILED: {reasons}"
    return action
