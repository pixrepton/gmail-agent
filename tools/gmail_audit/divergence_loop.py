"""Operator-agent divergence loop (Mechanism A) + learning rule candidates."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from log_config import get_logger
from _protocols import DatabaseConnection

logger = get_logger("divergence_loop")

RESPONSE_EXACT_MATCH = "EXACT_MATCH"
RESPONSE_EDITED_MATCH = "EDITED_MATCH"
RESPONSE_DIVERGENT_ACTION = "DIVERGENT_ACTION"
RESPONSE_IGNORED = "IGNORED"

CANDIDATE_PENDING = "pending_operator"
CANDIDATE_APPROVED = "approved"
CANDIDATE_REJECTED = "rejected"
CANDIDATE_STALE = "stale"
CANDIDATE_DORMANT = "dormant"
CANDIDATE_ARCHIVED = "archived"
CANDIDATE_PATTERN = "pattern_candidate"  # regex pattern z pattern_discovery

DIVERGENCE_WINDOW_HOURS = 24
CANDIDATE_THRESHOLD = 3  # Bazowy próg — używany gdy brak danych o rodzinie przypadków

# Adaptacyjny próg: env-overridable
ADAPTIVE_THRESHOLD_BASE = int(os.getenv("DIVERGENCE_THRESHOLD_BASE", "3"))
ADAPTIVE_THRESHOLD_MIN = int(os.getenv("DIVERGENCE_THRESHOLD_MIN", "2"))
ADAPTIVE_THRESHOLD_MAX = int(os.getenv("DIVERGENCE_THRESHOLD_MAX", "10"))
ADAPTIVE_THRESHOLD_DIVISOR = int(os.getenv("DIVERGENCE_THRESHOLD_DIVISOR", "10"))

# Time decay: env-overridable (dni, po których obserwacja traci połowę wagi)
TIME_DECAY_HALF_DAYS = int(os.getenv("DIVERGENCE_DECAY_DAYS", "90"))

# Auto-approve: gdy confidence >= 0.9 i supporting_count >= threshold
CONFIDENCE_AUTO_APPROVE_THRESHOLD = 0.9


# --- Krok 8: rodziny podobne --- cross-family learning
FAMILY_SIMILARITY: dict[str, list[str]] = {
    "lead_opportunity": ["quote_preparation", "marketing_performance_review"],
    "procurement_delivery": ["supplier_commercial_review", "logistics"],
    "finance_settlement": ["compliance_legal_review"],
    "service_warranty": ["service_installation", "service_repair"],
}


def _find_similar_candidates(cur, case_family, proposal_type):
    """Znajdz zatwierdzonych kandydatow z podobnych rodzin."""
    similar = FAMILY_SIMILARITY.get(case_family, [])
    if not similar:
        return []
    cur.execute("""
        SELECT candidate_id, pattern_key, supporting_count, status
        FROM learning_rule_candidates
        WHERE case_family = ANY(%s) AND proposal_type = %s AND status = %s
    """, (similar, proposal_type, CANDIDATE_APPROVED))
    return [dict(zip(["candidate_id", "pattern_key", "supporting_count", "status"], row))
            for row in cur.fetchall() or []]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def record_agent_proposal(
    conn: DatabaseConnection,
    *,
    engagement_id: str,
    case_id: str,
    proposal_type: str,
    proposal_content: dict[str, Any],
    proposal_reasoning_pl: str = "",
    source_pipeline: str = "",
    proposal_id: str = "",
) -> str:
    pid = str(proposal_id or _new_id("prop")).strip() or _new_id("prop")
    # Note: deliberately not `with conn:` -- this helper may be called with a connection it
    # does not own (e.g. process_operator_action passes down a connection several more
    # operations still need afterward). `with conn:` closes the connection on block exit in
    # this psycopg version, which would break every subsequent use by the caller.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO agent_proposal_records (
                proposal_id, engagement_id, case_id, created_at,
                proposal_type, proposal_content_json, proposal_reasoning_pl, source_pipeline
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (proposal_id) DO UPDATE SET
                proposal_content_json = EXCLUDED.proposal_content_json,
                proposal_reasoning_pl = EXCLUDED.proposal_reasoning_pl
            """,
            (
                pid,
                str(engagement_id or ""),
                str(case_id or ""),
                _utc_now(),
                str(proposal_type or ""),
                json.dumps(proposal_content or {}, ensure_ascii=False),
                str(proposal_reasoning_pl or ""),
                str(source_pipeline or ""),
            ),
        )
    conn.commit()
    logger.info("PROPOSAL_RECORDED", extra={"x": {
        "proposal_id": pid,
        "proposal_type": proposal_type,
        "engagement_id": engagement_id,
        "case_id": case_id,
    }})
    return pid


def fetch_open_proposals_for_case(conn: DatabaseConnection, *, case_id: str, within_hours: int = DIVERGENCE_WINDOW_HOURS) -> list[dict[str, Any]]:
    from psycopg.rows import dict_row

    cid = str(case_id or "").strip()
    if not cid:
        return []
    cutoff = _utc_now() - timedelta(hours=max(1, int(within_hours)))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.proposal_id, p.engagement_id, p.case_id, p.created_at,
                   p.proposal_type, p.proposal_content_json, p.proposal_reasoning_pl, p.source_pipeline
            FROM agent_proposal_records p
            LEFT JOIN operator_response_records r ON r.proposal_id = p.proposal_id
            WHERE p.case_id = %s AND p.created_at >= %s AND r.response_id IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM mailbox_memory_cases c
                WHERE c.case_id = p.case_id
                  AND (
                    c.case_family = 'reference_only'
                    OR COALESCE(c.metadata->>'export_case_type', '') = 'noise'
                  )
              )
            ORDER BY p.created_at DESC
            """,
            (cid, cutoff),
        )
        rows = cur.fetchall() or []
    return [_row_to_proposal(row) for row in rows]


def _row_to_proposal(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        content = row.get("proposal_content_json") or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                content = {}
        return {**row, "proposal_content_json": content}
    raise TypeError(
        "_row_to_proposal requires dict_row; tuple rows silently lose proposal_type/case_id and are no longer accepted"
    )


def classify_operator_response(
    *,
    proposal: dict[str, Any],
    operator_action_type: str,
    operator_payload: dict[str, Any] | None = None,
) -> tuple[str, float, str]:
    """Deterministic v1 classifier — no LLM."""
    prop_type = str(proposal.get("proposal_type") or "").strip().lower()
    action = str(operator_action_type or "").strip().lower()
    payload = operator_payload or {}

    if not action or action in {"ignored", "no_action", "skip"}:
        return RESPONSE_IGNORED, 0.6, "Brak działania operatora w oknie czasowym"

    prop_content = proposal.get("proposal_content_json") or {}
    if not isinstance(prop_content, dict):
        prop_content = {}

    if action == prop_type or action == str(prop_content.get("action_type") or "").strip().lower():
        return RESPONSE_EXACT_MATCH, 0.95, "Dokładne dopasowanie typu akcji"

    if action in {"hitl_approve", "approve", "approved"} and prop_type in {
        "prepare_reply_draft",
        "generate_draft_reply",
        "request_missing_info",
    }:
        return RESPONSE_EXACT_MATCH, 0.9, "Zatwierdzenie propozycji HITL"

    if action in {"hitl_edit", "edit", "edited"} or bool(payload.get("edited")):
        return RESPONSE_EDITED_MATCH, 0.85, "Operator zatwierdził po edycji"

    if action and action != prop_type:
        return RESPONSE_DIVERGENT_ACTION, 0.8, f"Operator wykonał inną akcję: {action}"

    return RESPONSE_IGNORED, 0.5, "Nie sklasyfikowano"


def record_operator_response(
    conn: DatabaseConnection,
    *,
    proposal_id: str,
    response_type: str,
    detection_confidence: float,
    evidence_event_ids: list[str] | None = None,
    diff_summary_pl: str = "",
) -> str:
    rid = _new_id("resp")
    # See record_agent_proposal's comment: not `with conn:` -- the caller may still need this
    # connection for further operations (process_operator_action does).
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_response_records (
                response_id, proposal_id, response_type, detected_at,
                detection_confidence, evidence_event_ids, diff_summary_pl
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                rid,
                str(proposal_id),
                str(response_type),
                _utc_now(),
                float(detection_confidence),
                json.dumps(list(evidence_event_ids or []), ensure_ascii=False),
                str(diff_summary_pl or ""),
            ),
        )
    conn.commit()
    logger.info("OPERATOR_RESPONSE", extra={"x": {
        "proposal_id": proposal_id,
        "response_type": response_type,
        "confidence": detection_confidence,
    }})
    return rid


def adaptive_threshold(case_family: str, family_observation_count: int | None = None) -> int:
    """Zwraca adaptacyjny próg dla danej rodziny przypadków.

    Dla małych rodzin (<10 obserwacji): niższy próg (więcej reguł).
    Dla dużych rodzin (100+ obserwacji): wyższy próg (mniej szumu).

    Wzór: base + log2(total / divisor), clamped do [min, max].
    """
    if family_observation_count is None or family_observation_count < 10:
        return ADAPTIVE_THRESHOLD_MIN  # Mało danych — więcej reguł

    import math
    offset = int(math.log2(max(1, family_observation_count) / ADAPTIVE_THRESHOLD_DIVISOR))
    return max(ADAPTIVE_THRESHOLD_MIN, min(ADAPTIVE_THRESHOLD_MAX, ADAPTIVE_THRESHOLD_BASE + offset))


def _apply_time_decay_weight(observation_age_days: float) -> float:
    """Zwraca wagę obserwacji w zależności od jej wieku.

    Stosuje wykładnicze wygaszanie: waga = max(0.1, 1.0 - (age_days / half_days)).
    Po TIME_DECAY_HALF_DAYS dniach waga = 0.5, po 2*half_days = 0.1 (minimum).
    """
    if observation_age_days <= 0:
        return 1.0
    weight = max(0.1, 1.0 - (observation_age_days / TIME_DECAY_HALF_DAYS))
    return weight


def update_rule_application(
    conn: DatabaseConnection,
    *,
    candidate_id: str,
) -> bool:
    """Aktualizuje last_applied_at i incrementuje application_count dla reguły."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE learning_rule_candidates
            SET last_applied_at = %s,
                application_count = COALESCE(application_count, 0) + 1
            WHERE candidate_id = %s
            """,
            (_utc_now(), str(candidate_id)),
        )
        return cur.rowcount > 0


def _auto_approve_candidate(conn: DatabaseConnection, candidate_id: str) -> bool:
    """Auto-approve candidate — ustawia status='approved', approved_at=now."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE learning_rule_candidates
            SET status = %s, approved_at = %s, approved_by = %s
            WHERE candidate_id = %s
            """,
            (CANDIDATE_APPROVED, _utc_now(), "auto_approve", str(candidate_id)),
        )
        ok = cur.rowcount > 0
    if ok:
        logger.info("CANDIDATE_AUTO_APPROVED", extra={"x": {"candidate_id": candidate_id}})
    return ok


def _run_pattern_learner(conn: DatabaseConnection, proposal_content: dict[str, Any]) -> None:
    """Wywołuje pattern_learner jeśli proposal_content zawiera dane faktów."""
    from pattern_learner import compare_llm_vs_regex, store_pattern_candidates

    llm_facts: dict[str, Any] | None = None
    regex_facts: dict[str, Any] | None = None

    # Sprawdź różne możliwe lokalizacje faktów w proposal_content
    raw_facts = proposal_content.get("facts") or proposal_content.get("extracted_facts")
    if raw_facts and isinstance(raw_facts, dict):
        llm_facts = raw_facts
    elif isinstance(proposal_content, dict):
        # Szukaj kluczy zawierających '_fact' lub 'fact_'
        fact_candidates = {
            k: v for k, v in proposal_content.items()
            if "fact" in k.lower()
        }
        if fact_candidates:
            llm_facts = fact_candidates

    # Sprawdź regex_facts jeśli dostępne
    raw_regex = proposal_content.get("regex_facts")
    if raw_regex and isinstance(raw_regex, dict):
        regex_facts = raw_regex

    if not llm_facts:
        return

    candidates = compare_llm_vs_regex(llm_facts, regex_facts or {})
    if candidates:
        count = store_pattern_candidates(conn, candidates)
        if count > 0:
            logger.info("PATTERN_LEARNER_CANDIDATES_STORED", extra={"x": {
                "count": count,
                "fact_keys": list(candidates[0].pattern_key) if candidates else [],
            }})


def maybe_create_learning_candidate(
    conn: DatabaseConnection,
    *,
    case_family: str,
    proposal_type: str,
    response_type: str,
    parent_observation_count: int | None = None,
    confidence: float | None = None,
    proposal_content: dict[str, Any] | None = None,
) -> str | None:
    if response_type not in {RESPONSE_DIVERGENT_ACTION, RESPONSE_EDITED_MATCH}:
        return None
    family = str(case_family or "unknown").strip() or "unknown"
    ptype = str(proposal_type or "").strip()
    pattern_key = f"{family}::{ptype}::{response_type}"
    threshold = adaptive_threshold(family, parent_observation_count)

    # Note: bare `with conn.cursor()`, not `with conn:` -- process_operator_action (the only
    # caller) still needs this connection afterward (see record_agent_proposal's comment).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT candidate_id, supporting_count, status
            FROM learning_rule_candidates
            WHERE pattern_key = %s AND status = %s
            LIMIT 1
            """,
            (pattern_key, CANDIDATE_PENDING),
        )
        row = cur.fetchone()
        if row:
            cid = row[0] if not isinstance(row, dict) else row.get("candidate_id")
            count = int((row[1] if not isinstance(row, dict) else row.get("supporting_count")) or 0) + 1
            cur.execute(
                """
                UPDATE learning_rule_candidates
                SET supporting_count = %s
                WHERE candidate_id = %s
                """,
                (count, cid),
            )
            conn.commit()
            return str(cid) if count < threshold else str(cid)

        cur.execute(
            """
            SELECT count(*) FROM operator_response_records r
            JOIN agent_proposal_records p ON p.proposal_id = r.proposal_id
            WHERE r.response_type IN (%s, %s)
              AND p.proposal_type = %s
            """,
            (RESPONSE_DIVERGENT_ACTION, RESPONSE_EDITED_MATCH, ptype),
        )
        count_row = cur.fetchone()
        total = int((count_row[0] if count_row and not isinstance(count_row, dict) else 0) or 0)

        if total + 1 < threshold:
            # Krok 8: cross-family — szukaj zatwierdzonych regul w podobnych rodzinach
            similar_candidates = _find_similar_candidates(cur, family, ptype)
            strong_similar = [s for s in similar_candidates if int(s.get("supporting_count") or 0) > 3]
            if strong_similar:
                lowered = max(ADAPTIVE_THRESHOLD_MIN, int(threshold * 0.7))
                logger.info("LEARNING_CROSS_FAMILY_BOOST", extra={"x": {
                    "case_family": family,
                    "proposal_type": ptype,
                    "similar_count": len(strong_similar),
                    "original_threshold": threshold,
                    "lowered_threshold": lowered,
                }})
                if total + 1 >= lowered:
                    pass  # proceed to create candidate below
                else:
                    return None
            else:
                return None

        cid = _new_id("cand")
        rule_text = (
            f"Gdy propozycja typu «{ptype}» w rodzinie «{family}» — "
            f"operator często reaguje inaczej ({response_type}). Rozważ korektę playbooka."
        )
        cur.execute(
            """
            INSERT INTO learning_rule_candidates (
                candidate_id, pattern_key, rule_text_pl, supporting_count,
                status, case_family, proposal_type, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (cid, pattern_key, rule_text, total + 1, CANDIDATE_PENDING, family, ptype, _utc_now()),
        )
        logger.info("LEARNING_CANDIDATE_CREATED", extra={"x": {
            "pattern_key": pattern_key,
            "supporting_count": total + 1,
            "candidate_id": cid,
            "threshold": threshold,
        }})

        # Faza 1: Auto-approve only when confidence AND observation threshold are both met.
        should_auto_approve = (
            confidence is not None
            and confidence >= CONFIDENCE_AUTO_APPROVE_THRESHOLD
            and (total + 1) >= threshold
        )
        if should_auto_approve:
            _auto_approve_candidate(conn, cid)

        # Faza 3: Pattern learner — gdy EDITED_MATCH dotyczy faktu
        if response_type == RESPONSE_EDITED_MATCH and proposal_content:
            _run_pattern_learner(conn, proposal_content)

        conn.commit()
        return cid


def process_operator_action(
    conn: DatabaseConnection,
    *,
    case_id: str,
    case_family: str,
    operator_action_type: str,
    operator_payload: dict[str, Any] | None = None,
    evidence_event_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Match latest open proposal and record divergence outcome.

    Enterprise: stosuje adaptacyjny próg (#25) i time-decay weight (#26)
    dla supporting_count. Wywołuje update_rule_application (#27) przy approve.
    """
    results: list[dict[str, Any]] = []
    proposals = fetch_open_proposals_for_case(conn, case_id=case_id)
    if not proposals:
        return results
    proposal = proposals[0]
    response_type, confidence, summary = classify_operator_response(
        proposal=proposal,
        operator_action_type=operator_action_type,
        operator_payload=operator_payload,
    )
    rid = record_operator_response(
        conn,
        proposal_id=str(proposal.get("proposal_id") or ""),
        response_type=response_type,
        detection_confidence=confidence,
        evidence_event_ids=evidence_event_ids,
        diff_summary_pl=summary,
    )

    # #26: Pobierz liczbę obserwacji w tej rodzinie dla adaptacyjnego progu
    parent_obs = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM operator_response_records r
                JOIN agent_proposal_records p ON p.proposal_id = r.proposal_id
                WHERE p.case_id = %s
                """,
                (case_id,),
            )
            row = cur.fetchone()
            if row:
                parent_obs = int(row[0] if not isinstance(row, dict) else (row.get("count") or 0))
    except Exception as exc:
        logger.warning("process_operator_action: parent_obs query failed: %s", exc)

    cand_id = maybe_create_learning_candidate(
        conn,
        case_family=case_family,
        proposal_type=str(proposal.get("proposal_type") or ""),
        response_type=response_type,
        parent_observation_count=parent_obs,
        confidence=confidence,
        proposal_content=proposal.get("proposal_content_json"),
    )

    # #27: Jeśli kandydat został zaakceptowany wcześniej, zaktualizuj application_count
    if response_type == RESPONSE_EXACT_MATCH:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT candidate_id FROM learning_rule_candidates
                    WHERE pattern_key LIKE %s AND status = 'approved'
                    LIMIT 1
                    """,
                    (f"{case_family}::{proposal.get('proposal_type', '')}::%",),
                )
                row = cur.fetchone()
                if row:
                    cid = row[0] if not isinstance(row, dict) else row.get("candidate_id")
                    update_rule_application(conn, candidate_id=cid)
        except Exception as exc:
            logger.warning("process_operator_action: rule_application update failed: %s", exc)

    results.append(
        {
            "response_id": rid,
            "proposal_id": proposal.get("proposal_id"),
            "response_type": response_type,
            "candidate_id": cand_id,
            "parent_observation_count": parent_obs,
        }
    )
    return results


def fetch_learning_candidates(conn: DatabaseConnection, *, limit: int = 50, status_filter: str | None = None) -> list[dict[str, Any]]:
    """Fetch learning rule candidates for operator review.

    Enterprise: wspiera time-decay weight dla supporting_count.
    """
    with conn.cursor() as cur:
        if status_filter:
            cur.execute(
                """
                SELECT candidate_id, pattern_key, rule_text_pl, supporting_count,
                       status, case_family, proposal_type, created_at, metadata
                FROM learning_rule_candidates
                WHERE status = %s
                ORDER BY supporting_count DESC, created_at DESC
                LIMIT %s
                """,
                (status_filter, max(1, int(limit))),
            )
        else:
            cur.execute(
                """
                SELECT candidate_id, pattern_key, rule_text_pl, supporting_count,
                       status, case_family, proposal_type, created_at, metadata
                FROM learning_rule_candidates
                ORDER BY supporting_count DESC, created_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
        rows = cur.fetchall() or []

    now = _utc_now()
    results = []
    for row in rows:
        if isinstance(row, dict):
            item = row
        else:
            item = {
                "candidate_id": row[0],
                "pattern_key": row[1],
                "rule_text_pl": row[2],
                "supporting_count": row[3],
                "status": row[4],
                "case_family": row[5],
                "proposal_type": row[6],
                "created_at": str(row[7]) if len(row) > 7 else "",
            }

        # #26: Time-decay weight — starsze reguły ważą mniej w raporcie
        created_raw = item.get("created_at") or item.get("created_at", "")
        if created_raw:
            try:
                if isinstance(created_raw, str):
                    created_dt = datetime.fromisoformat(created_raw)
                elif isinstance(created_raw, datetime):
                    created_dt = created_raw
                else:
                    created_dt = now
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                age_days = (now - created_dt).total_seconds() / 86400
                item["time_decay_weight"] = round(_apply_time_decay_weight(age_days), 3)
            except (ValueError, TypeError):
                item["time_decay_weight"] = 1.0
        else:
            item["time_decay_weight"] = 1.0

        results.append(item)
    return results


def update_candidate_status(
    conn: DatabaseConnection,
    *,
    candidate_id: str,
    status: str,
    approved_by: str = "operator",
    rule_text_pl: str = "",
) -> bool:
    if status not in {CANDIDATE_APPROVED, CANDIDATE_REJECTED}:
        return False
    with conn.cursor() as cur:
        if rule_text_pl:
            cur.execute(
                """
                UPDATE learning_rule_candidates
                SET status = %s, approved_at = %s, approved_by = %s, rule_text_pl = %s
                WHERE candidate_id = %s
                """,
                (status, _utc_now(), str(approved_by), str(rule_text_pl), str(candidate_id)),
            )
        else:
            cur.execute(
                """
                UPDATE learning_rule_candidates
                SET status = %s, approved_at = %s, approved_by = %s
                WHERE candidate_id = %s
                """,
                (status, _utc_now(), str(approved_by), str(candidate_id)),
            )
        return cur.rowcount > 0


def fetch_approved_rules_for_family(conn: DatabaseConnection, *, case_family: str, limit: int = 10) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT candidate_id, rule_text_pl, case_family, proposal_type, approved_at
            FROM learning_rule_candidates
            WHERE status = %s AND (case_family = %s OR case_family = 'unknown')
            ORDER BY approved_at DESC NULLS LAST
            LIMIT %s
            """,
            (CANDIDATE_APPROVED, str(case_family or "unknown"), max(1, int(limit))),
        )
        rows = cur.fetchall() or []
    return [
        {
            "candidate_id": r[0] if not isinstance(r, dict) else r.get("candidate_id"),
            "rule_text_pl": r[1] if not isinstance(r, dict) else r.get("rule_text_pl"),
            "case_family": r[2] if not isinstance(r, dict) else r.get("case_family"),
            "proposal_type": r[3] if not isinstance(r, dict) else r.get("proposal_type"),
        }
        for r in rows
    ]


def fetch_decision_queue(
    conn: DatabaseConnection,
    *,
    limit: int = 50,
    sla_warning_hours: int = 4,
    sla_critical_hours: int = 24,
) -> list[dict[str, Any]]:
    """Fetch all pending proposals for the operator decision queue, with SLA."""
    # Explicit dict_row: without it, plain-tuple rows make _row_to_proposal()
    # fall into its `{"proposal_id": str(row[0])}` fallback and every other
    # field (case_id, proposal_type, summary_pl, source_pipeline) goes silently
    # blank -- found via X1 v0 runtime proof against the already-live caller.
    from psycopg.rows import dict_row

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT p.proposal_id, p.engagement_id, p.case_id, p.created_at,
                   p.proposal_type, p.proposal_content_json, p.proposal_reasoning_pl,
                   p.source_pipeline
            FROM agent_proposal_records p
            LEFT JOIN operator_response_records r ON r.proposal_id = p.proposal_id
            WHERE r.response_id IS NULL
            ORDER BY p.created_at ASC
            LIMIT %s
            """,
            (max(1, int(limit)),),
        )
        rows = cur.fetchall() or []

    now = datetime.now(timezone.utc)
    queue = []
    for row in rows:
        proposal = _row_to_proposal(row)
        created_raw = proposal.get("created_at")
        if created_raw:
            if isinstance(created_raw, str):
                try:
                    created_dt = datetime.fromisoformat(created_raw)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    created_dt = now
            elif isinstance(created_raw, datetime):
                created_dt = created_raw
                if created_dt.tzinfo is None:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
            else:
                created_dt = now
        else:
            created_dt = now

        hours_waiting = (now - created_dt).total_seconds() / 3600
        if hours_waiting >= sla_critical_hours:
            priority = "critical"
        elif hours_waiting >= sla_warning_hours:
            priority = "high"
        else:
            priority = "normal"

        queue.append({
            "proposal_id": proposal.get("proposal_id", ""),
            "engagement_id": proposal.get("engagement_id", ""),
            "case_id": proposal.get("case_id", ""),
            "created_at": created_dt.isoformat(),
            "proposal_type": proposal.get("proposal_type", ""),
            "summary_pl": proposal.get("proposal_reasoning_pl", ""),
            "source_pipeline": proposal.get("source_pipeline", ""),
            "hours_waiting": round(hours_waiting, 1),
            "priority": priority,
        })

    return queue
