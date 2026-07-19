"""Lekkie metryki systemowe — structlog + agregacja do JSON.

Zamiast Prometheusa/Grafany (overengineering dla lokalnego stacka),
ten moduł zbiera kluczowe metryki i zapisuje je do pliku JSON.

Metryki:
  - Liczba żądań na endpoint
  - Czas odpowiedzi (średni, p95, max)
  - Liczba błędów (5xx, timeouty)
  - Liczba tur agenta
  - Liczba HITL approve/reject

Użycie:
    from agent_runtime.metrics import MetricsCollector
    collector = MetricsCollector()
    collector.record_request("/agent-chat", status=200, duration_ms=1500)
    collector.record_agent_turn(engagement_id="...", tool="propose_mutation")
    collector.record_hitl(engagement_id="...", action="approve")
    report = collector.report()  # dict z agregacjami

Plik metrics JSON jest zapisywany do METRICS_PATH (domyślnie /tmp/topinstal-metrics.json)
za każdym razem, gdy licznik przekroczy próg FLUSH_INTERVAL.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRICS_PATH = os.environ.get(
    "TOPINSTAL_METRICS_PATH",
    str(Path("/tmp/topinstal-metrics.json").resolve()),
)
FLUSH_INTERVAL = 100  # Zrzuć do pliku co 100 zdarzeń

_SECONDS_TO_MS = 1000


class MetricsCollector:
    """Thread-safe kolektor metryk z flushingiem do pliku JSON."""

    def __init__(self, metrics_path: str = "", flush_interval: int = FLUSH_INTERVAL) -> None:
        self._path = Path(metrics_path or METRICS_PATH)
        self._flush_interval = int(flush_interval)
        self._lock = threading.Lock()
        self._event_count = 0

        # Liczniki
        self._request_count: dict[str, int] = defaultdict(int)
        self._error_count: dict[str, int] = defaultdict(int)
        self._request_durations: dict[str, list[float]] = defaultdict(list)
        self._agent_turns: int = 0
        self._agent_tool_counts: dict[str, int] = defaultdict(int)
        self._hitl_approves: int = 0
        self._hitl_rejects: int = 0
        self._engagement_count: int = 0

        # Timestamp startu
        self._started_at = datetime.now(timezone.utc).isoformat()

    def record_request(self, endpoint: str, *, status: int, duration_ms: float) -> None:
        """Zarejestruj żądanie HTTP."""
        with self._lock:
            self._request_count[endpoint] += 1
            self._request_durations[endpoint].append(duration_ms)
            if status >= 500:
                self._error_count[endpoint] += 1
            self._maybe_flush()

    def record_agent_turn(self, *, engagement_id: str = "", tool: str = "") -> None:
        """Zarejestruj turę agenta."""
        with self._lock:
            self._agent_turns += 1
            if tool:
                self._agent_tool_counts[tool] += 1
            if engagement_id:
                self._engagement_count += 1
            self._maybe_flush()

    def record_hitl(self, *, engagement_id: str = "", action: str = "") -> None:
        """Zarejestruj akcję HITL (approve/reject)."""
        with self._lock:
            if action in ("approve", "approved", "hitl_approve"):
                self._hitl_approves += 1
            elif action in ("reject", "rejected", "hitl_reject"):
                self._hitl_rejects += 1
            self._maybe_flush()

    def _maybe_flush(self) -> None:
        self._event_count += 1
        if self._event_count % self._flush_interval == 0:
            self._flush()

    def report(self) -> dict[str, Any]:
        """Zwróć zagregowane metryki."""
        with self._lock:
            req_stats: dict[str, dict[str, Any]] = {}
            for endpoint, durations in self._request_durations.items():
                if not durations:
                    continue
                sorted_d = sorted(durations)
                p95_idx = max(0, int(len(sorted_d) * 0.95))
                req_stats[endpoint] = {
                    "count": len(durations),
                    "avg_ms": round(sum(durations) / len(durations), 1),
                    "p95_ms": round(sorted_d[p95_idx], 1),
                    "max_ms": round(max(durations), 1),
                    "errors": self._error_count.get(endpoint, 0),
                }

            return {
                "started_at": self._started_at,
                "collected_at": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(
                    (datetime.now(timezone.utc) - datetime.fromisoformat(self._started_at)).total_seconds()
                ),
                "requests": dict(self._request_count),
                "errors": dict(self._error_count),
                "request_stats": req_stats,
                "agent_turns": self._agent_turns,
                "agent_tool_counts": dict(self._agent_tool_counts),
                "engagements": self._engagement_count,
                "hitl": {
                    "approves": self._hitl_approves,
                    "rejects": self._hitl_rejects,
                },
            }

    def _flush(self) -> None:
        """Zrzuć metryki do pliku JSON."""
        try:
            data = self.report()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("metrics_flush_failed: %s", exc)

    def flush(self) -> None:
        """Wymuś zrzut do pliku."""
        self._flush()


# Singleton dla całego procesu
_collector: MetricsCollector | None = None
_collector_lock = threading.Lock()


def get_metrics_collector() -> MetricsCollector:
    """Zwraca singleton MetricsCollector."""
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = MetricsCollector()
    return _collector


def record_request(endpoint: str, *, status: int, duration_ms: float) -> None:
    """Wygodna funkcja — rejestruje żądanie na singletonie."""
    get_metrics_collector().record_request(endpoint, status=status, duration_ms=duration_ms)


def record_agent_turn(*, engagement_id: str = "", tool: str = "") -> None:
    """Wygodna funkcja — rejestruje turę agenta."""
    get_metrics_collector().record_agent_turn(engagement_id=engagement_id, tool=tool)


def record_hitl(*, engagement_id: str = "", action: str = "") -> None:
    """Wygodna funkcja — rejestruje HITL."""
    get_metrics_collector().record_hitl(engagement_id=engagement_id, action=action)


def get_report() -> dict[str, Any]:
    """Zwraca raport metryk."""
    return get_metrics_collector().report()
