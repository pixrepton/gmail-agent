"""Business Pulse — 9 narzędzi dla chat-agenta do odczytu stanu biznesu.

Kazde narzedzie czyta ISTNIEJACE dane z Node B — EngagementSnapshotV2, OfferDTO,
mailbox_memory, operator_memory, os_event, agent_runtime_turns.
NIE produkuje nowych danych — tylko agreguje i zwraca.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

# P1-C: In-memory cache for Business Pulse (TTL 300s = 5 min)
_BP_CACHE: dict[str, tuple[Any, float]] = {}
_BP_TTL = 300

from case_family_boundary import ACTIVE_CUSTOMER_CASES_SQL_WHERE as _ACTIVE_CASES_WHERE


# ── Redaction helper (Krok A3) ──────────────────────────────────────────────
_SENSITIVE_KEYS = frozenset({"customer_email", "email", "phone", "client_email"})


def _redact_for_logging(data: Any, keys_to_redact: frozenset[str] = _SENSITIVE_KEYS) -> Any:
    """Zastap wartosci podanych kluczy '[REDACTED]' — do logowania, nie do odpowiedzi."""
    if isinstance(data, dict):
        return {k: (_redact_for_logging(v, keys_to_redact) if k not in keys_to_redact else "[REDACTED]") for k, v in data.items()}
    if isinstance(data, list):
        return [_redact_for_logging(item, keys_to_redact) for item in data]
    return data


# ── BP metrics (Krok B2) ────────────────────────────────────────────────────
from log_config import get_logger

_bp_logger = get_logger("business_pulse")


def _log_bp_call(tool_name: str, store: Any, result: dict, duration_ms: float = 0) -> None:
    """Loguj wywolanie narzedzia Business Pulse z czasem i wynikiem."""
    db_url = str(getattr(store, "database_url", "") if store else "").strip()
    _bp_logger.info("BP_EXECUTED", extra={"x": {
        "tool": tool_name,
        "ok": result.get("ok", False),
        "duration_ms": round(duration_ms, 1),
        "result_size": len(json.dumps(_redact_for_logging(result))),
    }})


def _bpcache_key(name: str) -> str:
    return name


def _bpcache_get(name: str) -> Any | None:
    key = _bpcache_key(name)
    entry = _BP_CACHE.get(key)
    if entry and time.monotonic() < entry[1]:
        return entry[0]
    return None


def _bpcache_set(name: str, value: Any) -> None:
    _BP_CACHE[_bpcache_key(name)] = (value, time.monotonic() + _BP_TTL)


def _bpcache_clear() -> None:
    _BP_CACHE.clear()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Pipeline ───────────────────────────────────────────────────────────────

def get_pipeline_summary(store: Any, settings: Any) -> dict[str, Any]:
    """Agreguje pipeline: aktywne sprawy, oferty w toku, wartosc."""
    _t = time.monotonic()
    cached = _bpcache_get("get_pipeline_summary")
    if cached:
        return cached
    result: dict[str, Any] = {"ok": True, "pipeline": {}}

    try:
        # Read cases from mailbox memory
        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()
        elif hasattr(store, "conn"):
            cursor = store.conn.cursor()

        if cursor is None:
            r2 = _fallback_from_dash_projection(settings)
            return r2 if r2 else {"ok": False, "error": "No store access"}

        cursor.execute(
            f"SELECT COUNT(*) FROM mailbox_memory_cases WHERE {_ACTIVE_CASES_WHERE}"
        )
        total = (cursor.fetchone() or [0])[0] or 0

        cursor.execute(
            f"""SELECT c.status, COUNT(*)
               FROM mailbox_memory_cases c
               WHERE {_ACTIVE_CASES_WHERE}
               GROUP BY c.status"""
        )
        by_status = dict(cursor.fetchall() or [])

        cursor.execute(
            f"""SELECT c.case_id, c.customer_name, c.status,
                      c.created_at, COALESCE(c.metadata->>'stage', c.status) AS stage
               FROM mailbox_memory_cases c
               WHERE {_ACTIVE_CASES_WHERE}
               ORDER BY c.created_at DESC LIMIT 3"""
        )
        top_rows = cursor.fetchall() or []
        top_items = []
        for r in top_rows:
            top_items.append({
                "case_id": r[0] if not isinstance(r, dict) else r.get("case_id", ""),
                "client": str(r[1] if not isinstance(r, dict) else r.get("customer_name", "") or ""),
                "value_pln": None,  # no per-case monetary column exists yet (see pipeline.value_tracking)
                "stage": str(r[4] if not isinstance(r, dict) else r.get("stage", "") or ""),
            })

        cursor.execute(
            f"""SELECT COUNT(*) FROM mailbox_memory_cases
               WHERE {_ACTIVE_CASES_WHERE}
               AND case_family IN ('lead_opportunity', 'sales')
               AND lower(COALESCE(status, '')) NOT IN ('closed', 'done', 'archived', 'resolved', 'cancelled')"""
        )
        offers_in_progress = int((cursor.fetchone() or [0])[0] or 0)

        pipeline = {
            "active_cases": int(total),
            "offers_in_progress": offers_in_progress,
            # No table persists a per-case monetary value today (pricing/OfferDTO is
            # kalk-top's SoT, not gmail-agent's) -- untracked, not zero.
            "total_value_pln": None,
            "value_tracking": "not_implemented",
            "cases_by_stage": by_status,
            "top_3_by_value": top_items,
        }

        result["pipeline"] = pipeline
    except Exception as exc:
        fallback = _fallback_from_dash_projection(settings)
        if fallback:
            _bpcache_set("get_pipeline_summary", fallback)
            return fallback
        result = {"ok": False, "error": str(exc)}

    _bpcache_set("get_pipeline_summary", result)
    _log_bp_call("get_pipeline_summary", store, result, (time.monotonic() - _t) * 1000)
    return result


def _fallback_from_dash_projection(settings: Any) -> dict[str, Any] | None:
    """Fallback: read operational feed projection from mailbox memory store."""
    try:
        from daszek_v3_operational_feed import build_operational_feed_from_mailbox_store
        from mailbox_memory_runtime import build_mailbox_memory_runtime

        runtime = build_mailbox_memory_runtime(settings)
        store = runtime.store
        if store is None:
            return None

        snap = build_operational_feed_from_mailbox_store(store, case_limit=50)
        if not snap:
            return None

        feed = snap.get("feed") if isinstance(snap.get("feed"), dict) else snap
        cases = feed.get("cases", []) if isinstance(feed, dict) else []
        desk = feed.get("desk", []) if isinstance(feed, dict) else []
        cases_count = len(cases) if isinstance(cases, list) else 0
        desk_count = len(desk) if isinstance(desk, list) else 0
        offers_count = 0
        if isinstance(cases, list):
            for item in cases:
                if not isinstance(item, dict):
                    continue
                family = str(item.get("case_family") or "").strip()
                status = str(item.get("status") or "").strip().lower()
                if family in ("lead_opportunity", "sales") and status not in (
                    "closed",
                    "done",
                    "archived",
                    "resolved",
                    "cancelled",
                ):
                    offers_count += 1
        return {
            "ok": True,
            "pipeline": {
                "active_cases": cases_count,
                "offers_in_progress": offers_count,
                "desk_active_count": desk_count,
                "total_value_pln": None,
                "value_tracking": "not_implemented",
                "cases_by_stage": {},
                "top_3_by_value": [],
                "source": "operational_feed_projection",
            },
        }
    except Exception:
        return None


# ── Client Health ───────────────────────────────────────────────────────────

def get_client_health(store: Any, settings: Any) -> dict[str, Any]:
    """Ktorzy klienci czekaja za dlugo — SLA checker."""
    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "health": {}}
    try:
        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()

        if cursor is None:
            return {"ok": False, "error": "No store access"}

        now = _utc_now()
        cursor.execute(
            f"""SELECT case_id, customer_name, created_at, status
               FROM mailbox_memory_cases WHERE {_ACTIVE_CASES_WHERE}
               ORDER BY created_at DESC LIMIT 50"""
        )
        rows = cursor.fetchall() or []

        at_risk = []
        critical_count = 0
        high_count = 0

        for r in rows:
            eid = r[0] if not isinstance(r, dict) else r.get("case_id", "")
            name = str(r[1] if not isinstance(r, dict) else r.get("customer_name", "") or "")
            created_raw = r[2] if not isinstance(r, dict) else r.get("created_at")
            status = str(r[3] if not isinstance(r, dict) else r.get("status", "") or "")

            if not created_raw:
                continue

            if isinstance(created_raw, str):
                try:
                    created_dt = datetime.fromisoformat(created_raw)
                    if created_dt.tzinfo is None:
                        created_dt = created_dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    created_dt = now
            else:
                created_dt = created_raw

            days = (now - created_dt).days
            if days >= 7:
                priority = "critical"
                critical_count += 1
            elif days >= 3:
                priority = "high"
                high_count += 1
            else:
                continue

            at_risk.append({
                "case_id": str(eid),
                "client_name": name,
                "days_waiting": days,
                "priority": priority,
                "last_contact": str(created_raw),
                "reason": f"Oczekuje {days} dni — status: {status}",
            })

        result["health"] = {
            "total_clients": len(rows),
            "at_risk": len(at_risk),
            "critical": critical_count,
            "high": high_count,
            "clients": at_risk[:10],
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _log_bp_call("get_client_health", store, result, (time.monotonic() - _t) * 1000)
    return result


# ── Daily Delta ─────────────────────────────────────────────────────────────

def get_daily_delta(store: Any, settings: Any) -> dict[str, Any]:
    """Co sie zmienilo od wczoraj."""
    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "delta": {}}
    try:
        if not hasattr(store, "_connect"):
            return {"ok": False, "error": "No store access"}

        yesterday = (_utc_now() - timedelta(days=1)).isoformat()
        # Own the connection lifecycle explicitly (`with`) -- this used to be
        # opened without ever being closed, which is safe for a one-off call
        # but leaks a connection per call once wired into a path invoked on
        # every feed build (X1 day card).
        with store._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"SELECT COUNT(*) FROM mailbox_memory_cases WHERE created_at >= %s AND {_ACTIVE_CASES_WHERE}",
                    (yesterday,)
                )
                new_count = (cursor.fetchone() or [0])[0] or 0

                cursor.execute(
                    f"""SELECT case_id, customer_name FROM mailbox_memory_cases
                       WHERE created_at >= %s AND {_ACTIVE_CASES_WHERE}
                       ORDER BY created_at DESC LIMIT 5""",
                    (yesterday,)
                )
                new_rows = cursor.fetchall() or []

        new_list = [
            {"case_id": r[0], "client": str(r[1] or "")}
            for r in new_rows
        ]

        result["delta"] = {
            "new_cases": int(new_count),
            "new_cases_list": new_list,
            "since": yesterday,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _log_bp_call("get_daily_delta", store, result, (time.monotonic() - _t) * 1000)
    return result


# ── Win Rate ────────────────────────────────────────────────────────────────

def get_win_rate(store: Any, settings: Any) -> dict[str, Any]:
    """Procent wygranych ofert (lifecycle completed vs lost, not fictional won status)."""
    from business_outcome import classify_case_outcome

    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "win_rate": {}}
    try:
        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()

        if cursor is None:
            return {"ok": False, "error": "No store access"}

        cursor.execute(
            f"""SELECT lower(COALESCE(status, '')) AS status,
                       lower(COALESCE(metadata->>'resolution_outcome', '')) AS resolution_outcome
               FROM mailbox_memory_cases
               WHERE {_ACTIVE_CASES_WHERE}"""
        )
        rows = cursor.fetchall() or []

        won = lost = in_progress = 0
        for row in rows:
            if isinstance(row, dict):
                status = str(row.get("status") or "")
                resolution_outcome = str(row.get("resolution_outcome") or "")
            else:
                status = str(row[0] or "")
                resolution_outcome = str(row[1] or "") if len(row) > 1 else ""
            bucket = classify_case_outcome(status=status, resolution_outcome=resolution_outcome)
            if bucket == "won":
                won += 1
            elif bucket == "lost":
                lost += 1
            else:
                in_progress += 1

        total = won + lost
        rate = round((won / total * 100), 1) if total > 0 else 0.0
        result["win_rate"] = {
            "period_days": 999,
            "total": total,
            "won": won,
            "lost": lost,
            "in_progress": in_progress,
            "rate_pct": rate,
            "status_basis": "lifecycle_completed_vs_lost",
            "trend": None,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _bpcache_set("get_win_rate", result)
    _log_bp_call("get_win_rate", store, result, (time.monotonic() - _t) * 1000)
    return result


# ── Top Clients ─────────────────────────────────────────────────────────────

def get_top_clients(store: Any, settings: Any) -> dict[str, Any]:
    """Top 10 klientow wg pipeline."""
    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "top_clients": []}
    try:
        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()

        if cursor is None:
            return {"ok": False, "error": "No store access"}

        cursor.execute(
            f"""SELECT case_id, customer_name, created_at, status
               FROM mailbox_memory_cases WHERE {_ACTIVE_CASES_WHERE}
               ORDER BY created_at DESC LIMIT 10"""
        )
        rows = cursor.fetchall() or []

        now = _utc_now()
        for r in rows:
            eid = r[0] if not isinstance(r, dict) else r.get("case_id", "")
            name = str(r[1] if not isinstance(r, dict) else r.get("customer_name", "") or "")
            created_raw = r[2] if not isinstance(r, dict) else r.get("created_at")
            status = str(r[3] if not isinstance(r, dict) else r.get("status", "") or "")

            days = 0
            if created_raw:
                if isinstance(created_raw, str):
                    try:
                        cd = datetime.fromisoformat(created_raw)
                    except ValueError:
                        cd = now
                else:
                    cd = created_raw
                days = (now - cd).days

            result["top_clients"].append({
                "client_name": name,
                # No monetary column and no per-client offer count are computed
                # yet -- untracked, not a fabricated value_pln=0/active_offers=1.
                "pipeline_value_pln": None,
                "active_offers": None,
                "status": status,
                "days_since_contact": max(0, days),
            })
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _bpcache_set("get_top_clients", result)
    _log_bp_call("get_top_clients", store, result, (time.monotonic() - _t) * 1000)
    return result


# ── Revenue Forecast ────────────────────────────────────────────────────────

def get_revenue_forecast(store: Any, settings: Any) -> dict[str, Any]:
    """Prognoza przychodu: pipeline x win_rate."""
    _t = time.monotonic()
    win = get_win_rate(store, settings)
    win_payload = win.get("win_rate", {}) if win.get("ok") else {}
    total_decided = int(win_payload.get("total") or 0)
    rate = float(win_payload.get("rate_pct") or 0.0) if total_decided > 0 else None

    pipeline_val = None
    try:
        pipe = get_pipeline_summary(store, settings)
        if pipe.get("ok"):
            pipeline_val = pipe.get("pipeline", {}).get("total_value_pln")
    except Exception:
        pipeline_val = None

    if pipeline_val is None or rate is None:
        return {
            "ok": True,
            "forecast": {
                "pipe_pln": pipeline_val,
                "confident_pln": None,
                "probable_pln": None,
                "potential_pln": None,
                "total_forecast_pln": None,
                "method": (
                    "pipeline x win_rate"
                    if rate is not None
                    else "win_rate_unavailable_without_decided_cases"
                ),
                "value_tracking": "not_implemented",
                "win_rate_basis": win_payload.get("status_basis"),
            },
        }

    forecast = pipeline_val * rate / 100

    return {
        "ok": True,
        "forecast": {
            "pipe_pln": pipeline_val,
            "confident_pln": round(forecast * 0.2),
            "probable_pln": round(forecast * 0.5),
            "potential_pln": round(forecast * 0.3),
            "total_forecast_pln": round(forecast),
            "method": f"pipeline x {rate}% win_rate",
            "win_rate_basis": win_payload.get("status_basis"),
        },
    }


# ── System Health ───────────────────────────────────────────────────────────

def get_system_health_snapshot(store: Any, settings: Any) -> dict[str, Any]:
    """Stan techniczny wszystkich komponentow."""
    _t = time.monotonic()
    try:
        from event_spine.health_monitor import build_health_status
        from event_spine.query import fetch_recent_os_events

        db_url = str(getattr(settings, "mailbox_memory_database_url", "") or "")
        events = fetch_recent_os_events(db_url, limit=50) if db_url else []
        health = build_health_status(events, mailbox_store=store)

        return {
            "ok": True,
            "health": health,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    _log_bp_call("get_system_health_snapshot", store, {"ok": True}, (time.monotonic() - _t) * 1000)
    return result


# ── Business Signals ────────────────────────────────────────────────────────

def get_business_signals(store: Any, settings: Any) -> dict[str, Any]:
    """Sygnaly: serwisowe, marketingowe, alerty, decyzje."""
    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "signals": {}}
    try:
        from divergence_loop import fetch_decision_queue, fetch_learning_candidates

        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()

        service_signals = []
        marketing_signals = []
        alerts = []

        if cursor is not None:
            cursor.execute(
                """SELECT engagement_id, status, service_signals_count,
                          marketing_signals_count
                   FROM case_states
                   WHERE service_signals_count > 0 OR marketing_signals_count > 0
                   LIMIT 20"""
            )
            srows = cursor.fetchall() or []
            for r in srows:
                if (r[2] or 0) > 0:
                    service_signals.append({
                        "case_id": r[0],
                        "type": "serwis",
                        "count": r[2],
                        "status": r[1],
                    })
                if (r[3] or 0) > 0:
                    marketing_signals.append({
                        "case_id": r[0],
                        "type": "marketing",
                        "count": r[3],
                        "status": r[1],
                    })

        try:
            if cursor is not None:
                pending = fetch_decision_queue(cursor.connection if hasattr(cursor, "connection") else conn)
            else:
                pending = []
            candidates = fetch_learning_candidates(
                cursor.connection if cursor and hasattr(cursor, "connection") else (
                    conn if cursor is None else None
                ),
                status_filter="pending_operator",
                limit=10,
            )

            for p in (pending or []):
                h = p.get("hours_waiting", 0) if isinstance(p, dict) else 0
                if h and h > 4:
                    alerts.append({
                        "type": "sla_breached",
                        "case_id": p.get("case_id", ""),
                        "waiting_hours": h,
                        "severity": "critical" if h >= 24 else "high" if h >= 4 else "normal",
                    })

            result["signals"] = {
                "service": service_signals,
                "marketing": marketing_signals,
                "alerts": alerts,
                "pending_decisions": len(pending) if isinstance(pending, list) else 0,
                "rule_candidates": len(candidates) if isinstance(candidates, list) else 0,
            }
        except Exception:
            result["signals"] = {
                "service": service_signals,
                "marketing": marketing_signals,
                "alerts": alerts,
                "pending_decisions": 0,
                "rule_candidates": 0,
            }

    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _log_bp_call("get_business_signals", store, result, (time.monotonic() - _t) * 1000)
    return result


# ── Agent Activity ──────────────────────────────────────────────────────────

def get_agent_activity_summary(store: Any, settings: Any) -> dict[str, Any]:
    """Co robili agenci — statystyki aktywnosci."""
    _t = time.monotonic()
    result: dict[str, Any] = {"ok": True, "activity": {}}
    try:
        cursor = None
        if hasattr(store, "_connect"):
            conn = store._connect()
            cursor = conn.cursor()

        chat_turns = 0
        mail_signals = 0
        if cursor is not None:
            try:
                cursor.execute(
                    """SELECT COUNT(*) FROM agent_runtime_turns
                       WHERE created_at >= NOW() - INTERVAL '1 day'"""
                )
                chat_turns = int((cursor.fetchone() or [0])[0] or 0)
            except Exception:
                chat_turns = 0

            try:
                cursor.execute(
                    """SELECT COUNT(*) FROM operator_memory
                       WHERE memory_type = 'conversation'
                         AND created_at >= NOW() - INTERVAL '1 day'"""
                )
                mail_signals = int((cursor.fetchone() or [0])[0] or 0)
            except Exception:
                mail_signals = 0

        result["activity"] = {
            "chat_agent": {
                "turns_today": chat_turns,
                "last_turn": "unknown",
                "avg_response_ms": 0,
            },
            "mail_agent": {
                "signals_processed_today": mail_signals,
                "cases_updated": 0,
                "last_activity": "unknown",
            },
            "total_proposals": 0,
            "decisions_pending": 0,
        }
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}

    _log_bp_call("get_agent_activity_summary", store, result, (time.monotonic() - _t) * 1000)
    return result


def get_agent_health(store: Any, settings: Any) -> dict[str, Any]:
    """Sprawdz zdrowie agenta czatowego: LLM, store, konfiguracja."""
    checks: list[dict[str, Any]] = []
    try:
        from agent_runtime.openai_agent_client import OpenAIToolPlanner
        checks.append({"check": "llm_planner_import", "ok": True})
    except Exception as exc:
        checks.append({"check": "llm_planner_import", "ok": False, "error": str(exc)[:100]})
    try:
        if store and hasattr(store, "fetch_cases"):
            store.fetch_cases(limit=1)
            checks.append({"check": "mailbox_store", "ok": True})
    except Exception as exc:
        checks.append({"check": "mailbox_store", "ok": False, "error": str(exc)[:100]})
    try:
        get_constitution_for_signal("operator_command")
        checks.append({"check": "constitution", "ok": True})
    except Exception as exc:
        checks.append({"check": "constitution", "ok": False, "error": str(exc)[:100]})
    return {"ok": all(c["ok"] for c in checks), "checks": checks}
