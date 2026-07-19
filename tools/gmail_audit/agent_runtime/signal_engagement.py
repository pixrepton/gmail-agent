"""Link mailbox signals to operator engagement_id (PR-A spine)."""

from __future__ import annotations

from typing import Any, Protocol


class SignalEngagementWriter(Protocol):
    def patch_signal_engagement_id(self, signal_id: str, engagement_id: str) -> bool: ...


def patch_signal_engagement(
    writer: SignalEngagementWriter,
    *,
    signal_id: str,
    engagement_id: str,
) -> bool:
    sid = str(signal_id or "").strip()
    eid = str(engagement_id or "").strip()
    if not sid or not eid:
        return False
    return bool(writer.patch_signal_engagement_id(sid, eid))
