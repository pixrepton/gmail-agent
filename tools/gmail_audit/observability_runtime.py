"""Bounded observability runtime with optional OpenTelemetry export and local audit mirror."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from artifact_io import append_jsonl, read_jsonl


def _make_trace_id() -> str:
    return uuid4().hex + uuid4().hex


def _make_span_id() -> str:
    return uuid4().hex[:16]


@dataclass(slots=True)
class ObservabilityRuntime:
    run_id: str
    run_dir: Path
    command_name: str
    enabled: bool
    local_mirror_enabled: bool
    service_name: str
    otlp_endpoint: str = ""
    otlp_headers: str = ""
    local_events_path: Path = field(init=False, repr=False)
    _event_count: int = field(init=False, repr=False, default=0)
    _tracer: Any | None = field(init=False, repr=False, default=None)
    _provider: Any | None = field(init=False, repr=False, default=None)
    _otel_export_enabled: bool = field(init=False, repr=False, default=False)
    _otel_bootstrap_error: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        self.run_dir = Path(self.run_dir)
        self.local_events_path = self.run_dir / "telemetry_events.jsonl"
        self._event_count = 0
        self._tracer = None
        self._provider = None
        self._otel_export_enabled = False
        self._otel_bootstrap_error = ""
        if self.enabled:
            self._bootstrap_otel()

    @property
    def export_mode(self) -> str:
        if self._otel_export_enabled and self.local_mirror_enabled:
            return "otlp+local_mirror"
        if self._otel_export_enabled:
            return "otlp"
        if self.local_mirror_enabled:
            return "mirror_only"
        return "disabled"

    @property
    def event_count(self) -> int:
        return int(self._event_count)

    @property
    def mirror_path(self) -> str:
        return str(self.local_events_path)

    @property
    def otel_ready(self) -> bool:
        return bool(self._tracer is not None)

    @property
    def otel_bootstrap_error(self) -> str:
        return str(self._otel_bootstrap_error or "")

    def summary(self) -> dict[str, Any]:
        return {
            "telemetry_enabled": bool(self.enabled or self.local_mirror_enabled),
            "otel_export_mode": self.export_mode,
            "telemetry_event_count": self.event_count,
            "telemetry_mirror_path": self.mirror_path if self.local_mirror_enabled else "",
            "trace_coverage_summary": {
                "local_mirror_enabled": bool(self.local_mirror_enabled),
                "otel_ready": bool(self.otel_ready),
            },
        }

    @contextmanager
    def span(
        self,
        span_name: str,
        *,
        case_id: str = "",
        message_id: str = "",
        thread_id: str = "",
        signal_id: str = "",
        trace_id: str = "",
        stage_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Iterator[dict[str, str]]:
        started = time.monotonic()
        otel_trace_id = _make_trace_id()
        otel_span_id = _make_span_id()
        attrs = {
            "gmail_agent.run_id": self.run_id,
            "gmail_agent.command_name": self.command_name,
            "gmail_agent.case_id": case_id,
            "gmail_agent.message_id": message_id,
            "gmail_agent.thread_id": thread_id,
            "gmail_agent.signal_id": signal_id,
            "gmail_agent.trace_id": trace_id,
            "gmail_agent.stage_name": stage_name or span_name,
        }

        ctx = nullcontext()
        if self._tracer is not None:
            ctx = self._tracer.start_as_current_span(span_name, attributes={k: v for k, v in attrs.items() if v})
        try:
            with ctx:
                yield {
                    "otel_trace_id": otel_trace_id,
                    "otel_span_id": otel_span_id,
                }
        except Exception as exc:
            self.record_event(
                span_name=span_name,
                stage_name=stage_name or span_name,
                status="failed",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                signal_id=signal_id,
                trace_id=trace_id,
                otel_trace_id=otel_trace_id,
                otel_span_id=otel_span_id,
                extra={
                    **(extra or {}),
                    "error_class": type(exc).__name__,
                },
            )
            raise
        else:
            self.record_event(
                span_name=span_name,
                stage_name=stage_name or span_name,
                status="ok",
                duration_ms=round((time.monotonic() - started) * 1000, 3),
                case_id=case_id,
                message_id=message_id,
                thread_id=thread_id,
                signal_id=signal_id,
                trace_id=trace_id,
                otel_trace_id=otel_trace_id,
                otel_span_id=otel_span_id,
                extra=extra,
            )

    def record_event(
        self,
        *,
        span_name: str,
        stage_name: str,
        status: str,
        duration_ms: float,
        case_id: str = "",
        message_id: str = "",
        thread_id: str = "",
        signal_id: str = "",
        trace_id: str = "",
        otel_trace_id: str = "",
        otel_span_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "command_name": self.command_name,
            "case_id": str(case_id or ""),
            "message_id": str(message_id or ""),
            "thread_id": str(thread_id or ""),
            "signal_id": str(signal_id or ""),
            "trace_id": str(trace_id or ""),
            "otel_trace_id": str(otel_trace_id or _make_trace_id()),
            "otel_span_id": str(otel_span_id or _make_span_id()),
            "span_name": str(span_name or ""),
            "stage_name": str(stage_name or span_name or ""),
            "status": str(status or ""),
            "duration_ms": float(duration_ms or 0.0),
        }
        if extra:
            payload["details"] = {
                str(key): value
                for key, value in extra.items()
                if "secret" not in str(key).lower() and "authorization" not in str(key).lower()
            }
        if self.local_mirror_enabled:
            append_jsonl(self.local_events_path, payload)
        self._event_count += 1

    def inject_headers(
        self,
        headers: dict[str, str] | None,
        *,
        case_id: str = "",
        signal_id: str = "",
        trace_id: str = "",
    ) -> dict[str, str]:
        out = {str(key): str(value) for key, value in (headers or {}).items()}
        out["X-Gmail-Agent-Run-Id"] = self.run_id
        if case_id:
            out["X-Gmail-Agent-Case-Id"] = str(case_id)
        if signal_id:
            out["X-Gmail-Agent-Signal-Id"] = str(signal_id)
        if trace_id:
            out["X-Gmail-Agent-Trace-Id"] = str(trace_id)
        if self._tracer is not None:
            try:
                from opentelemetry.propagate import inject  # type: ignore[import-untyped]

                inject(out)
            except Exception as exc:
                import logging; logging.getLogger("observability_runtime").warning(
                    "otel: inject failed: %s", exc
                )
        return out

    def read_local_events(self) -> list[dict[str, Any]]:
        return [dict(item) for item in read_jsonl(self.local_events_path, allow_missing=True)]

    def _bootstrap_otel(self) -> None:
        if not self.otlp_endpoint:
            return
        try:
            from opentelemetry import trace  # type: ignore[import-untyped]
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # type: ignore[import-untyped]
            from opentelemetry.sdk.resources import Resource  # type: ignore[import-untyped]
            from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-untyped]
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore[import-untyped]
        except Exception as exc:  # noqa: BLE001
            self._otel_bootstrap_error = str(exc)
            return

        try:
            resource = Resource.create({"service.name": self.service_name or "gmail-agent"})
            self._provider = TracerProvider(resource=resource)
            headers = _parse_otlp_headers(self.otlp_headers)
            exporter = OTLPSpanExporter(endpoint=self.otlp_endpoint, headers=headers or None)
            processor = BatchSpanProcessor(exporter)
            self._provider.add_span_processor(processor)
            trace.set_tracer_provider(self._provider)
            self._tracer = trace.get_tracer(self.service_name or "gmail-agent")
            self._otel_export_enabled = True
        except Exception as exc:  # noqa: BLE001
            self._otel_bootstrap_error = str(exc)
            self._tracer = None
            self._provider = None
            self._otel_export_enabled = False


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for chunk in str(raw or "").split(","):
        piece = chunk.strip()
        if not piece or "=" not in piece:
            continue
        key, value = piece.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and value:
            headers[key] = value
    return headers


def build_otel_check(settings: Any) -> dict[str, Any]:
    enabled = bool(getattr(settings, "gmail_agent_otel_enabled", False))
    local_mirror_enabled = bool(getattr(settings, "gmail_agent_otel_local_mirror_enabled", True))
    endpoint = str(getattr(settings, "otel_exporter_otlp_endpoint", "") or "").strip()
    service_name = str(getattr(settings, "otel_service_name", "") or "gmail-agent")
    check = {
        "enabled": enabled,
        "local_mirror_enabled": local_mirror_enabled,
        "service_name": service_name,
        "export_mode": "disabled",
    }
    if not enabled:
        check["export_mode"] = "mirror_only" if local_mirror_enabled else "disabled"
        # Canonical production treats local JSONL mirror as sufficient; OTLP export stays optional.
        if local_mirror_enabled:
            check["status"] = "ok"
            check["reason"] = (
                "OTLP export disabled (GMAIL_AGENT_OTEL_ENABLED=0); "
                "local telemetry mirror remains active for bounded runs."
            )
            return check
        check["status"] = "skipped"
        check["reason"] = "OpenTelemetry export and local mirror are both off."
        return check
    runtime = ObservabilityRuntime(
        run_id="doctor",
        run_dir=Path(os.getenv("TEMP", ".")),
        command_name="doctor",
        enabled=enabled,
        local_mirror_enabled=local_mirror_enabled,
        service_name=service_name,
        otlp_endpoint=endpoint,
        otlp_headers=str(getattr(settings, "otel_exporter_otlp_headers", "") or ""),
    )
    check["export_mode"] = runtime.export_mode
    if endpoint and runtime.otel_ready:
        check["status"] = "ok"
    elif endpoint and runtime.otel_bootstrap_error:
        check["status"] = "failed"
        check["error"] = runtime.otel_bootstrap_error
    elif local_mirror_enabled:
        check["status"] = "ok"
    else:
        check["status"] = "failed"
        check["error"] = "OpenTelemetry enabled without OTLP endpoint or local mirror."
    return check
