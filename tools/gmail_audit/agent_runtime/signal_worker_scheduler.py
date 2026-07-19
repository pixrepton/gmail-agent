"""Predictive poll scheduler — dostosowuje czestotliwosc pollowania do wzorcow ruchu."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any


class PredictiveScheduler:
    """Uczy sie wzorcow volume per hour-of-day i dostosowuje poll interval.

    Przy srednim volume = 1, sleep = base. Przy volume = 5, sleep = base/5.
    Przy volume = 0.2 (cisza), sleep = base*5.
    """

    def __init__(self, base_interval: int = 30) -> None:
        self.base_interval = base_interval
        self._volume_by_hour: dict[int, float] = defaultdict(float)
        self._total_observations = 0

    def record_volume(self, count: int) -> None:
        hour = datetime.now().hour
        # Wygładzanie wykładnicze
        alpha = 0.3
        old = self._volume_by_hour.get(hour, 1.0)
        self._volume_by_hour[hour] = old * (1 - alpha) + count * alpha
        self._total_observations += 1

    def get_sleep_seconds(self) -> int:
        hour = datetime.now().hour
        avg = self._volume_by_hour.get(hour, 1.0)
        if avg <= 0:
            return self.base_interval
        ratio = max(0.1, min(5.0, 1.0 / avg))
        return max(1, int(self.base_interval * ratio))

    def get_stats(self) -> dict[str, Any]:
        return {
            "volume_by_hour": dict(self._volume_by_hour),
            "total_observations": self._total_observations,
        }
