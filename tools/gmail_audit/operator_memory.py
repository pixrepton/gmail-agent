"""Operator Memory (L0) — persistent conversation memory for chat-agent.

Stores operator chat history, preferences, client context, and ongoing threads.
Uses PostgreSQL for persistence, injected as context into agent-chat calls.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from log_config import get_logger
from _protocols import DatabaseConnection

logger = get_logger("operator_memory")

OPERATOR_MEMORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS operator_memory (
    id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL DEFAULT 'default',
    memory_type TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    value_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_opmem_type ON operator_memory(memory_type);
CREATE INDEX IF NOT EXISTS idx_opmem_key ON operator_memory(key);
CREATE INDEX IF NOT EXISTS idx_opmem_session ON operator_memory(session_id);
CREATE INDEX IF NOT EXISTS idx_opmem_operator ON operator_memory(operator_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_opmem_operator_type_key ON operator_memory(operator_id, memory_type, key);
"""

MEMORY_TYPES = {
    "conversation": "Chat history turn",
    "preference": "Operator preference (style, notification, language)",
    "client_context": "Client-specific notes from operator",
    "thread": "Active topic/thread state",
    "decision": "Business decision recorded from chat",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return f"opmem_{uuid.uuid4().hex[:16]}"


def ensure_operator_memory_table(conn: DatabaseConnection) -> None:
    with conn.cursor() as cur:
        cur.execute(OPERATOR_MEMORY_TABLE_SQL)
        cur.execute(
            "ALTER TABLE operator_memory ADD COLUMN IF NOT EXISTS operator_id TEXT NOT NULL DEFAULT 'default'"
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_opmem_operator ON operator_memory(operator_id)")
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_opmem_operator_type_key "
            "ON operator_memory(operator_id, memory_type, key)"
        )
    conn.commit()


def save_conversation_turn(
    conn: DatabaseConnection,
    *,
    session_id: str,
    user_input: str,
    agent_response: str,
    case_id: str = "",
    engagement_id: str = "",
    token_count: int = 0,
    operator_id: str = "default",
) -> str:
    """Save a single conversation turn (user + agent) with token tracking."""
    mid = _new_id()
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_memory (id, operator_id, memory_type, session_id, key, value_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                mid,
                operator_id,
                "conversation",
                session_id,
                f"turn_{now}",
                json.dumps({
                    "user_input": user_input[:500],
                    "agent_response": agent_response[:1000],
                    "case_id": case_id,
                    "engagement_id": engagement_id,
                    "timestamp": now,
                    "token_count": token_count,
                }, ensure_ascii=False),
                now,
                now,
            ),
        )
    conn.commit()
    logger.info("MEMORY_STORED", extra={"x": {
        "memory_type": "conversation",
        "case_id": case_id if case_id else "",
        "engagement_id": engagement_id if engagement_id else "",
        "session_id": session_id,
        "token_count": token_count,
        "key_preview": f"turn_{now}",
    }})
    return mid


def save_preference(conn: DatabaseConnection, *, key: str, value: Any, session_id: str = "", operator_id: str = "default") -> bool:
    """Save or update an operator preference."""
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_memory (id, operator_id, memory_type, session_id, key, value_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (operator_id, memory_type, key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                updated_at = EXCLUDED.updated_at
            """,
            (
                _new_id(),
                operator_id,
                "preference",
                session_id or "global",
                key.strip(),
                json.dumps({"value": value}, ensure_ascii=False),
                now,
                now,
            ),
        )
    conn.commit()
    logger.info("MEMORY_STORED", extra={"x": {
        "memory_type": "preference",
        "key": key.strip(),
        "session_id": session_id or "global",
    }})
    return True


def save_client_context(conn: DatabaseConnection, *, client_name: str, note: str, session_id: str = "", operator_id: str = "default") -> str:
    """Save a note about a client from conversation context."""
    mid = _new_id()
    now = _utc_now()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO operator_memory (id, operator_id, memory_type, session_id, key, value_json, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (operator_id, memory_type, key) DO UPDATE SET
                value_json = EXCLUDED.value_json,
                updated_at = EXCLUDED.updated_at
            """,
            (
                mid,
                operator_id,
                "client_context",
                session_id or "global",
                client_name.strip().lower(),
                json.dumps({"client_name": client_name, "note": note, "timestamp": now}, ensure_ascii=False),
                now,
                now,
            ),
        )
    conn.commit()
    return mid


def get_recent_conversation(conn: DatabaseConnection, *, session_id: str = "", limit: int = 12) -> list[dict[str, Any]]:
    """Get recent conversation history for context injection.

    Zwiększono z 5 do 12 tur — więcej kontekstu dla długich rozmów.
    Starsze tury można skompresować przez summarize_older_turns().
    """
    with conn.cursor() as cur:
        if session_id:
            cur.execute(
                """
                SELECT value_json, created_at
                FROM operator_memory
                WHERE memory_type = 'conversation' AND session_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (session_id, max(1, int(limit))),
            )
        else:
            cur.execute(
                """
                SELECT value_json, created_at
                FROM operator_memory
                WHERE memory_type = 'conversation'
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
        rows = cur.fetchall() or []
    results = []
    for row in rows:
        val = row[0] if not isinstance(row, dict) else row.get("value_json", {})
        ts = row[1] if not isinstance(row, dict) else row.get("created_at", "")
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = {}
        results.append({
            "user_input": val.get("user_input", ""),
            "agent_response": val.get("agent_response", ""),
            "case_id": val.get("case_id", ""),
            "timestamp": str(ts) if ts else val.get("timestamp", ""),
        })
    return results


def get_preferences(conn: DatabaseConnection) -> dict[str, Any]:
    """Get all operator preferences."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT key, value_json FROM operator_memory WHERE memory_type = 'preference' ORDER BY key"
        )
        rows = cur.fetchall() or []
    prefs = {}
    for row in rows:
        key = row[0] if not isinstance(row, dict) else row.get("key", "")
        val = row[1] if not isinstance(row, dict) else row.get("value_json", {})
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = {}
        prefs[key] = val.get("value", val)
    return prefs


def get_client_context(conn: DatabaseConnection, *, client_name: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """Get saved client context notes."""
    with conn.cursor() as cur:
        if client_name:
            pattern = f"%{client_name.strip().lower()}%"
            cur.execute(
                """
                SELECT key, value_json, updated_at
                FROM operator_memory
                WHERE memory_type = 'client_context' AND (key ILIKE %s OR value_json::text ILIKE %s)
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (pattern, pattern, max(1, int(limit))),
            )
        else:
            cur.execute(
                """
                SELECT key, value_json, updated_at
                FROM operator_memory
                WHERE memory_type = 'client_context'
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (max(1, int(limit)),),
            )
        rows = cur.fetchall() or []
    results = []
    for row in rows:
        key = row[0] if not isinstance(row, dict) else row.get("key", "")
        val = row[1] if not isinstance(row, dict) else row.get("value_json", {})
        ts = row[2] if not isinstance(row, dict) else row.get("updated_at", "")
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = {}
        results.append({
            "key": key,
            "client_name": val.get("client_name", key),
            "note": val.get("note", ""),
            "timestamp": str(ts) if ts else val.get("timestamp", ""),
        })
    return results


def _truncate_to_token_budget(parts: list[str], max_chars: int = 8000) -> str:
    """Concatenate parts, truncating oldest first if over budget. Returns truncated string."""
    total = "\n\n".join(parts)
    if len(total) <= max_chars:
        return total
    # Keep preferences (first parts), truncate from the end
    truncated_parts = []
    running = 0
    for part in parts:
        part_len = len(part) + (2 if truncated_parts else 0)  # +2 for \n\n separators
        if running + part_len > max_chars:
            remaining = max_chars - running
            if remaining > 50:
                truncated_parts.append(part[:remaining])
            truncated_parts.append("\n[TRUNCATED: context budget exceeded]")
            break
        truncated_parts.append(part)
        running += part_len
    return "\n\n".join(truncated_parts)


def _summarize_older_turns(turns: list[dict[str, Any]]) -> str:
    """Kompresuje starsze tury rozmowy do krótkiego podsumowania.

    Łączy tematy, klientów, decyzje w 2-3 zdania.
    Nie używa LLM — deterministyczna agregacja.
    """
    if not turns:
        return ""

    # Wyciągnij unikalnych klientów, tematy i decyzje
    clients: set[str] = set()
    topics: list[str] = []
    decisions: list[str] = []
    turn_count = len(turns)

    for t in turns:
        inp = str(t.get("user_input") or "").strip()
        resp = str(t.get("agent_response") or "").strip()
        case_id = str(t.get("case_id") or "").strip()

        # Klienci
        if case_id:
            clients.add(case_id)

        # Tematy (pierwsze 50 znaków każdej tury)
        if inp and len(inp) > 5:
            topic = inp[:60].rstrip(".,!?")
            if topic not in topics:
                topics.append(topic)

        # Decyzje (tury gdzie agent coś proponował)
        if any(kw in resp.lower() for kw in ("proponuj", "propozycja", "zatwierdź", "wysłać", "utworzyć")):
            decisions.append(resp[:80])

    parts: list[str] = [f"Wczesniej w rozmowie ({turn_count} tur):"]

    if clients:
        c_list = ", ".join(sorted(clients)[:5])
        parts.append(f"- Klienci: {c_list}")

    if topics:
        # Weź pierwsze 3 tematy
        for t in topics[:3]:
            parts.append(f"- Temat: {t}…")

    if decisions:
        for d in decisions[:3]:
            parts.append(f"- Decyzja: {d}…")

    if not clients and not topics and not decisions:
        parts.append("- Kontynuacja poprzedniej rozmowy")

    return "\n".join(parts)


def _build_personality_section() -> str:
    """Zwraca sekcje osobowosci dla prompta — laduje z personality.yaml."""
    path = Path(__file__).resolve().parent / "personality.yaml"
    if not path.is_file():
        return ""
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            p = yaml.safe_load(f)
        lines = [
            f"Twoja tozsamosc: {p['identity']['role']}.",
            f"Ton wypowiedzi: {p['identity']['tone']}.",
            f"Jezyk: {p['identity']['language']}, forma: {p['identity']['form']}.",
        ]
        unc = p.get("uncertainty", {})
        if unc:
            lines.append(f"Zasady wyrazania niepewnosci:")
            lines.append(f"  - Jak jestes pewien: '{unc.get('low', '')}'")
            lines.append(f"  - Jak masz czesciowe info: '{unc.get('medium', '')}'")
            lines.append(f"  - Jak nie wiesz: '{unc.get('high', '')}'")
            lines.append(f"  - Jak nie mozesz obsluzyc: '{unc.get('fallback', '')}'")
        fb = p.get("fallback_rules", [])
        if fb:
            lines.append(f"Gdy nie mozesz wykonac zadania:")
            for rule in fb:
                lines.append(f"  - {rule['action']}")
        return "\n".join(lines)
    except Exception:
        return ""


def build_operator_context_prompt(conn: DatabaseConnection, *, session_id: str = "") -> str:
    """Build a natural language context prompt from operator memory for LLM injection.
    Enforces token budget to prevent silent truncation by LLM.
    """
    parts = []

    # 0. Personality (E1 — najwyzszy priorytet, zawsze pierwszy)
    personality = _build_personality_section()
    if personality:
        parts.append(personality)

    # 1. Preferences (highest priority — never truncated)
    prefs = get_preferences(conn)
    if prefs:
        pref_lines = [f"- {k}: {v}" for k, v in prefs.items()]
        parts.append("Preferencje operatora:\n" + "\n".join(pref_lines))

    # 2. Client context (second priority)
    clients = get_client_context(conn, limit=5)
    if clients:
        client_lines = [f"- {c['client_name']}: {c['note'][:200]}" for c in clients if c.get("note")]
        parts.append("Kontekst klientow:\n" + "\n".join(client_lines))

    # 3. Recent conversation (lowest priority — truncated first if over budget)
    recent = get_recent_conversation(conn, session_id=session_id, limit=12)
    if recent:
        conv_lines = []
        for turn in reversed(recent):
            inp = (turn.get("user_input") or "")[:200]
            resp = (turn.get("agent_response") or "")[:200]
            if inp:
                conv_lines.append(f"Operator: {inp}")
            if resp:
                conv_lines.append(f"Agent: {resp}")

        # Jeśli jest więcej niż 12 tur, wygeneruj podsumowanie starszej części
        older_turns = get_recent_conversation(conn, session_id=session_id, limit=50)
        if len(older_turns) > 15:
            older_summary = _summarize_older_turns(older_turns[12:])
            if older_summary:
                parts.append(f"Starsza czesc rozmowy (przed ostatnimi 12 turami):\n{older_summary}")

        parts.append("Ostatnia rozmowa:\n" + "\n".join(conv_lines))

    return _truncate_to_token_budget(parts, max_chars=8000)


def get_cost_summary(conn: DatabaseConnection, *, period_days: int = 30) -> dict[str, Any]:
    """Agreguje koszty tokenow z operator_memory conversation turns."""



def cleanup_operator_memory(db_url: str, ttl_days: int = 90) -> int:
    """Usun stare sesje operator_memory (starsze niz N dni)."""
    import psycopg
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM operator_memory WHERE created_at < %s", (cutoff,))
            return cur.rowcount


def get_decisions(conn: DatabaseConnection, *, days: int = 30) -> list[str]:
    """Pobierz decyzje z ostatnich N dni."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT value_json FROM operator_memory
               WHERE memory_type = 'decision' AND created_at >= %s
               ORDER BY created_at DESC LIMIT 10""",
            (cutoff,),
        )
        rows = cur.fetchall() or []
    results = []
    for row in rows:
        val = row[0] if not isinstance(row, dict) else row.get("value_json", {})
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except json.JSONDecodeError:
                val = {}
        if isinstance(val, dict) and val.get("decision"):
            results.append(val["decision"][:200])
    return results


def build_global_operator_context(conn: DatabaseConnection) -> str:
    """Zbuduj globalny kontekst ze wszystkich sesji — klienci, decyzje, preferencje."""
    parts = []
    clients = get_client_context(conn, limit=20)
    if clients:
        active = [c for c in clients if c.get("note")]
        if active:
            parts.append("Kontekst klientow z poprzednich rozmow:")
            for c in active[:5]:
                parts.append(f"- {c['client_name']}: {c['note'][:150]}")
    decisions = get_decisions(conn, days=30)
    if decisions:
        parts.append("Decyzje z ostatnich 30 dni:")
        for d in decisions[:3]:
            parts.append(f"- {d}")
    prefs = get_preferences(conn)
    if prefs:
        parts.append("Preferencje operatora:")
        for k, v in prefs.items():
            parts.append(f"- {k}: {v}")
    return "\n".join(parts)


def get_cost_summary(conn: DatabaseConnection, *, period_days: int = 30) -> dict[str, Any]:
    """Agreguje koszty tokenow z operator_memory conversation turns."""
    rates = {"gpt-4o-mini": "$0.15/1M input, $0.60/1M output"}
    now_dt = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        # Today total
        today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        cur.execute(
            """SELECT COUNT(*) FROM operator_memory
               WHERE memory_type = 'conversation' AND created_at >= %s""",
            (today_start,),
        )
        today_turns = (cur.fetchone() or [0])[0] or 0

        cur.execute(
            """SELECT COALESCE(SUM((value_json->>'token_count')::int), 0)
               FROM operator_memory
               WHERE memory_type = 'conversation' AND created_at >= %s""",
            (today_start,),
        )
        today_tokens = (cur.fetchone() or [0])[0] or 0

        # This week
        week_start_iso = (now_dt - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        cur.execute(
            """SELECT COALESCE(SUM((value_json->>'token_count')::int), 0),
                      COUNT(*)
               FROM operator_memory
               WHERE memory_type = 'conversation' AND created_at >= %s""",
            (week_start_iso,),
        )
        row = cur.fetchone()
        week_tokens = int((row[0] if not isinstance(row, dict) else row.get("sum", 0)) or 0) if row else 0
        week_turns = int((row[1] if not isinstance(row, dict) else row.get("count", 0)) or 0) if row else 0

        # By session
        cur.execute(
            """SELECT session_id, COUNT(*) as turns,
                      COALESCE(SUM((value_json->>'token_count')::int), 0) as tokens,
                      MAX(created_at) as last_turn
               FROM operator_memory
               WHERE memory_type = 'conversation'
               GROUP BY session_id
               ORDER BY MAX(created_at) DESC
               LIMIT 10"""
        )
        srows = cur.fetchall() or []

        by_session = []
        for r in srows:
            by_session.append({
                "session_id": str(r[0] if not isinstance(r, dict) else r.get("session_id", "")),
                "turns": int(r[1] if not isinstance(r, dict) else r.get("turns", 0) or 0),
                "tokens": int(r[2] if not isinstance(r, dict) else r.get("tokens", 0) or 0),
                "last_turn": str(r[3] if not isinstance(r, dict) else r.get("last_turn", "")),
            })

    ip_cost = 0.15 / 1_000_000
    op_cost = 0.60 / 1_000_000
    avg_token_cost = (ip_cost + op_cost) / 2

    usd_to_pln = 4.0

    return {
        "today": {
            "tokens": int(today_tokens),
            "turns": int(today_turns),
            "estimated_cost_pln": round(int(today_tokens) * avg_token_cost * usd_to_pln, 4),
        },
        "this_week": {
            "tokens": int(week_tokens),
            "turns": int(week_turns),
            "estimated_cost_pln": round(int(week_tokens) * avg_token_cost * usd_to_pln, 4),
        },
        "by_session": by_session,
        "rates": rates,
    }
