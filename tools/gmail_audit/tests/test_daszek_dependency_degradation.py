"""FIX-RT02: a Daszek projection outage must degrade the worker, never kill it.

Observed defect: `gmail-agent-vps-gmail-agent-worker-1` had accumulated **627 restarts** with
exit code 1 while `daszek-local-wordpress` had been down for 42 hours. Two faults combined:

1. `signal_worker.run_signal_loop` called `attach_daszek_client()` as a *startup precondition*
   whenever `DASZEK_OPERATIONAL_FEED_AUTO_PUSH=1`, before the poll loop was ever entered.
2. `DaszekClient` let raw `requests.ConnectionError` escape. That subclasses `OSError`, so
   `gmail_intake.main()`'s `except OSError` caught it, logged "File/OS error in intake", and
   returned 1 -- so Docker's `restart: unless-stopped` looped forever.

Architecturally Daszek is the operator *projection* surface; Node B owns operational truth.
Background projection push is therefore an optional dependency, and these tests pin that
contract: degrade, stay alive, retry with bounded backoff, expose the degradation. An explicit
operator `--push-daszek` request is the one case that must still fail fast.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import signal_worker  # noqa: E402
from daszek_client import DaszekClientError, _DaszekSession  # noqa: E402


class _Settings:
    daszek_base_url = "http://host.docker.internal:8090"
    daszek_login = "worker"
    daszek_password = "secret"  # noqa: S105 - test double, never a real credential
    http_timeout = 5


def _run_state() -> dict:
    return {"manifest": {}, "summary": {}}


# ── Transport translation ─────────────────────────────────────────────────────────────


def test_connection_error_is_reported_as_daszek_error_not_os_error(monkeypatch):
    """The exact mislabeling that reached `except OSError` and exited the process."""
    session = _DaszekSession()

    def _boom(*_args, **_kwargs):
        raise requests.ConnectionError(
            "HTTPConnectionPool(host='host.docker.internal', port=8090): "
            "Failed to establish a new connection: [Errno 111] Connection refused"
        )

    monkeypatch.setattr(requests.Session, "request", _boom)

    with pytest.raises(DaszekClientError) as caught:
        session.get("http://host.docker.internal:8090/wp-json/daszek/v1/login")

    assert "Daszek transport failure" in str(caught.value)


def test_daszek_error_is_not_an_os_error():
    """If this ever regresses, `except OSError` at the CLI boundary silently kills the worker."""
    assert not issubclass(DaszekClientError, OSError)
    assert issubclass(requests.ConnectionError, OSError), (
        "requests.ConnectionError subclasses OSError - this is precisely why the raw exception "
        "must never escape DaszekClient"
    )


# ── Optional dependency: degrade, do not die ──────────────────────────────────────────


def test_optional_daszek_outage_degrades_and_keeps_the_worker_alive(monkeypatch):
    import gmail_intake

    def _refused(_run_state, _settings):
        raise DaszekClientError("Daszek transport failure (POST /login): Connection refused")

    monkeypatch.setattr(gmail_intake, "attach_daszek_client", _refused)
    state = _run_state()

    attached = signal_worker.attach_daszek_projection_client(state, _Settings(), mandatory=False)

    assert attached is False, "an optional projection outage must not abort startup"
    dependency = state["manifest"]["daszek_dependency"]
    assert dependency["status"] == "degraded"
    assert dependency["required"] is False
    assert "Connection refused" in dependency["reason"]
    assert "canonical Node B processing continues" in dependency["impact"]
    assert state["summary"]["daszek_degraded"] is True
    assert state["daszek_client"] is None


def test_mandatory_daszek_push_still_fails_fast(monkeypatch):
    """An explicit operator --push-daszek must not silently pretend to push."""
    import gmail_intake

    def _refused(_run_state, _settings):
        raise DaszekClientError("Daszek transport failure (POST /login): Connection refused")

    monkeypatch.setattr(gmail_intake, "attach_daszek_client", _refused)

    with pytest.raises(DaszekClientError):
        signal_worker.attach_daszek_projection_client(_run_state(), _Settings(), mandatory=True)


def test_available_daszek_is_recorded_as_available(monkeypatch):
    import gmail_intake

    monkeypatch.setattr(
        gmail_intake,
        "attach_daszek_client",
        lambda run_state, _settings: run_state.__setitem__("daszek_client", object()),
    )
    state = _run_state()

    assert signal_worker.attach_daszek_projection_client(state, _Settings(), mandatory=False) is True
    assert state["manifest"]["daszek_dependency"]["status"] == "available"
    assert state["summary"]["daszek_degraded"] is False


# ── Bounded retry / backoff / recovery ────────────────────────────────────────────────


def test_reattach_is_skipped_entirely_when_not_degraded(monkeypatch):
    import gmail_intake

    calls: list[int] = []
    monkeypatch.setattr(gmail_intake, "attach_daszek_client", lambda *_a: calls.append(1))
    state = _run_state()
    state["summary"]["daszek_degraded"] = False

    signal_worker._maybe_reattach_daszek(run_state=state, settings=_Settings())

    assert calls == [], "a healthy dependency must not be re-probed every idle tick"


def test_reattach_backoff_doubles_and_is_capped(monkeypatch):
    import gmail_intake

    attempts: list[int] = []

    def _refused(_run_state, _settings):
        attempts.append(1)
        raise DaszekClientError("Connection refused")

    monkeypatch.setattr(gmail_intake, "attach_daszek_client", _refused)
    state = _run_state()
    state["summary"]["daszek_degraded"] = True

    signal_worker._maybe_reattach_daszek(run_state=state, settings=_Settings())
    assert len(attempts) == 1
    first_backoff = state["summary"]["daszek_reattach_backoff_sec"]
    assert first_backoff == signal_worker.DASZEK_REATTACH_MIN_BACKOFF_SEC * 2

    # An immediate second tick must be suppressed by the backoff window.
    signal_worker._maybe_reattach_daszek(run_state=state, settings=_Settings())
    assert len(attempts) == 1, "backoff must throttle reattach, not hammer a dead host"

    # Force the window open repeatedly and confirm the backoff saturates rather than growing.
    for _ in range(12):
        state["summary"]["daszek_reattach_next_monotonic"] = 0.0
        signal_worker._maybe_reattach_daszek(run_state=state, settings=_Settings())
    assert state["summary"]["daszek_reattach_backoff_sec"] == signal_worker.DASZEK_REATTACH_MAX_BACKOFF_SEC
    assert state["summary"]["daszek_degraded"] is True


def test_worker_recovers_automatically_when_daszek_returns(monkeypatch):
    import gmail_intake

    state = _run_state()
    state["summary"]["daszek_degraded"] = True
    state["summary"]["daszek_reattach_next_monotonic"] = 0.0

    monkeypatch.setattr(
        gmail_intake,
        "attach_daszek_client",
        lambda run_state, _settings: run_state.__setitem__("daszek_client", object()),
    )
    signal_worker._maybe_reattach_daszek(run_state=state, settings=_Settings())

    assert state["summary"]["daszek_degraded"] is False
    assert state["manifest"]["daszek_dependency"]["status"] == "available"
    assert state["summary"]["daszek_reattach_backoff_sec"] == signal_worker.DASZEK_REATTACH_MIN_BACKOFF_SEC


def test_idle_maintenance_drives_the_reattach(monkeypatch):
    """The recovery path must actually be wired into the loop, not merely exist."""
    seen: list[str] = []
    monkeypatch.setattr(
        signal_worker,
        "_maybe_reattach_daszek",
        lambda **_kwargs: seen.append("reattach"),
    )
    monkeypatch.setattr(signal_worker, "_maybe_run_sla_watcher_tick", lambda **_kwargs: None)
    monkeypatch.setattr(signal_worker, "_maybe_run_follow_up_guardian_tick", lambda **_kwargs: None)

    signal_worker._run_worker_idle_maintenance(
        run_state=_run_state(),
        settings=_Settings(),
        mailbox_runtime=None,
        iteration=1,
    )

    assert seen == ["reattach"]
