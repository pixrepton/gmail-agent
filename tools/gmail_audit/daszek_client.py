"""Opt-in client for live Daszek writes from Gmail intake runs."""

from __future__ import annotations

from dataclasses import dataclass
import json
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

import requests

from config import Settings
from redaction import sanitize_text


class DaszekClientError(RuntimeError):
    """Raised when Daszek authentication or API calls fail."""


class _DaszekSession(requests.Session):
    """A session that reports transport failures as ``DaszekClientError``.

    ``requests.ConnectionError`` subclasses ``OSError``. An unreachable Daszek therefore used
    to surface at the CLI boundary as ``except OSError`` -> "File/OS error in intake" -> exit
    code 1, which turned an outage of an *optional projection* dependency into a container
    crash loop (627 restarts observed) and mislabeled a network problem as local file I/O.
    Translating once here keeps every call site honest: a Daszek failure is a Daszek failure.
    """

    def request(self, method, url, *args, **kwargs):  # type: ignore[override]
        try:
            return super().request(method, url, *args, **kwargs)
        except requests.RequestException as exc:
            raise DaszekClientError(
                f"Daszek transport failure ({method} {sanitize_text(str(url))}): {sanitize_text(str(exc))}"
            ) from exc


@dataclass(slots=True)
class DaszekPushResult:
    request_id: str
    status: str
    object_type: str
    message_id: str
    task_id: str | None
    details: dict[str, Any]


@dataclass(slots=True)
class DaszekV2PushResult:
    status: str
    message_id: str
    signal_id: str | None
    trace_id: str | None
    details: dict[str, Any]


class DaszekCircuitBreaker:
    """Per-endpoint circuit breaker dla Daszek HTTP calli.

    Po `failure_threshold` kolejnych błędów otwiera obwód na `recovery_timeout` sekund.
    W stanie otwartym wszystkie zapytania są odrzucane bez próby HTTP.
    """

    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 120.0) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._failures = 0
        self._open_until: float = 0.0

    @property
    def is_open(self) -> bool:
        if self._open_until and time.monotonic() < self._open_until:
            return True
        if self._open_until and time.monotonic() >= self._open_until:
            self._failures = 0
            self._open_until = 0.0  # half-open: następny call to próbnik
        return False

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._open_until = time.monotonic() + self._recovery_timeout

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0


class DaszekClient:
    """Session-based Daszek client using the plugin's login + CSRF flow."""

    def __init__(self, settings: Settings, observability_runtime: Any | None = None) -> None:
        if not settings.daszek_base_url:
            raise DaszekClientError("Missing DASZEK_BASE_URL for live Daszek push.")
        if not settings.daszek_login:
            raise DaszekClientError("Missing DASZEK_LOGIN for live Daszek push.")
        if not settings.daszek_password:
            raise DaszekClientError("Missing DASZEK_PASSWORD for live Daszek push.")

        self.settings = settings
        self.session = _DaszekSession()
        # Connection pool: 20 connections, 10 per-host — enterprise grade
        from requests.adapters import HTTPAdapter
        adapter = HTTPAdapter(pool_maxsize=20, pool_connections=10, max_retries=0)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self.csrf_token = ""
        self.bootstrap_csrf_token = ""
        self.base_url = settings.daszek_base_url.rstrip("/")
        self.api_base = f"{self.base_url}/wp-json/daszek/v1"
        self.api_v2_base = f"{self.base_url}/wp-json/daszek/v2"
        self.api_v3_base = f"{self.base_url}/wp-json/daszek/v3"
        self.tasks_cache: list[dict[str, Any]] | None = None
        self.observability_runtime = observability_runtime
        # Circuit breaker per-endpoint — chroni przed floodem gdy Daszek down
        self._feed_circuit_breaker = DaszekCircuitBreaker(failure_threshold=3, recovery_timeout=120.0)
        self._ingest_circuit_breaker = DaszekCircuitBreaker(failure_threshold=3, recovery_timeout=120.0)

    def __enter__(self) -> DaszekClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        """Zamknij sesję HTTP — zwolnij connection pool."""
        if hasattr(self, "session"):
            self.session.close()

    def _http_post_with_retries(self, url: str, *, json_body: Any, headers: dict[str, str], timeout: float,
                                 circuit_breaker: DaszekCircuitBreaker | None = None) -> requests.Response:
        """POST with limited retries for transient upstream failures.

        Jeśli `circuit_breaker` jest podany i jest otwarty — raise bez próby HTTP.
        """
        if circuit_breaker is not None and circuit_breaker.is_open:
            raise DaszekClientError(f"Circuit breaker open for {url} — skipping push (cooldown active)")

        max_attempts = 4
        response: requests.Response | None = None
        for attempt in range(max_attempts):
            response = self.session.post(url, json=json_body, headers=headers, timeout=timeout)
            if response.status_code in {429, 502, 503, 504} and attempt < max_attempts - 1:
                time.sleep(0.35 * (2**attempt) + random.random() * 0.12)
                continue
            return response
        assert response is not None
        return response

    def login(self) -> None:
        self._bootstrap_session_base()
        headers = self._telemetry_headers({"Referer": f"{self.base_url}/daszek/"})
        if self.bootstrap_csrf_token:
            headers["X-CSRF-Token"] = self.bootstrap_csrf_token
        response = self.session.post(
            f"{self.api_base}/login",
            json={"login": self.settings.daszek_login, "password": self.settings.daszek_password},
            headers=headers,
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "login")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek login failed: {self._error_message(data, response)}")

        csrf_token = data.get("csrf_token")
        if not isinstance(csrf_token, str) or not csrf_token.strip():
            self.csrf_token = self.fetch_csrf_token()
            return
        self.csrf_token = csrf_token.strip()

    def _normalize_tasks_response(self, data: Any) -> list[dict[str, Any]]:
        items: list[Any]
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            items = data["tasks"]
        elif isinstance(data, list):
            items = data
        else:
            raise DaszekClientError("Daszek tasks endpoint did not return a list.")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _task_record_id(task: dict[str, Any]) -> str:
        return str(task.get("case_id") or task.get("id") or task.get("task_id") or "").strip()

    def list_tasks(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self.tasks_cache is not None and not refresh:
            return self.tasks_cache

        response = self.session.get(
            f"{self.api_v2_base}/tasks",
            params={"_runtime_ts": str(int(time.time() * 1000))},
            headers=self._telemetry_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "list_tasks")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek tasks fetch failed: {self._error_message(data, response)}")
        self.tasks_cache = self._normalize_tasks_response(data)
        return self.tasks_cache

    def push_preview(self, preview: dict[str, Any]) -> list[DaszekPushResult]:
        requests_to_push = preview.get("requests") or []
        if preview.get("ignored") or not requests_to_push:
            return []

        existing_tasks = self.list_tasks()
        results: list[DaszekPushResult] = []

        for request_item in requests_to_push:
            payload = request_item["payload"]
            existing = self._find_existing_task(existing_tasks, payload)
            if existing is not None:
                results.append(
                    DaszekPushResult(
                        request_id=str(request_item.get("request_id") or ""),
                        status="skipped_existing",
                        object_type=str(request_item.get("object_type") or payload.get("kind") or ""),
                        message_id=str(request_item.get("message_id") or ""),
                        task_id=self._task_record_id(existing) or None,
                        details={"matched_on": "source/kind/message_id/case_key"},
                    )
                )
                continue

            response = self.session.post(
                f"{self.api_v2_base}/tasks",
                json=payload,
                headers=self._mutation_headers(),
                timeout=self.settings.http_timeout,
            )
            data = self._read_json(response, "create_task")
            if response.status_code >= 400:
                raise DaszekClientError(f"Daszek create failed: {self._error_message(data, response)}")

            created_task = data.get("task")
            created_id = str(data.get("case_id") or "").strip() or None
            if isinstance(created_task, dict):
                if not created_id:
                    created_id = self._task_record_id(created_task) or None
                existing_tasks.append(created_task)

            results.append(
                DaszekPushResult(
                    request_id=str(request_item.get("request_id") or ""),
                    status="created",
                    object_type=str(request_item.get("object_type") or payload.get("kind") or ""),
                    message_id=str(request_item.get("message_id") or ""),
                    task_id=created_id,
                    details={"response_ok": bool(data.get("ok")), "created": data.get("created")},
                )
            )

        self.tasks_cache = existing_tasks
        return results

    def mark_done(self, task_id: str) -> dict[str, Any]:
        if not isinstance(task_id, str) or not task_id.strip():
            raise DaszekClientError("Daszek mark_done requires a non-empty task_id.")

        response = self.session.post(
            f"{self.api_v2_base}/tasks/{task_id.strip()}/done",
            headers=self._mutation_headers(),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "mark_done")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek mark_done failed: {self._error_message(data, response)}")

        task = data.get("task")
        if not isinstance(task, dict):
            raise DaszekClientError("Daszek mark_done succeeded without a task payload.")

        if self.tasks_cache is not None:
            replaced = False
            for index, item in enumerate(self.tasks_cache):
                if self._task_record_id(item) == self._task_record_id(task):
                    self.tasks_cache[index] = task
                    replaced = True
                    break
            if not replaced:
                self.tasks_cache.append(task)
        return task

    def push_v2_projection(self, payload: dict[str, Any]) -> DaszekV2PushResult:
        if not isinstance(payload, dict):
            raise DaszekClientError("Daszek v2 ingest requires a JSON object payload.")

        message_id = str(payload.get("message_key") or "").strip()
        signal_projection = payload.get("signal_projection")
        signal_id = str(signal_projection.get("signal_id") or "").strip() if isinstance(signal_projection, dict) else None
        case_patch = payload.get("case_patch")
        desk_note_patch = payload.get("desk_note_patch")
        case_id = ""
        if isinstance(case_patch, dict):
            case_id = str(case_patch.get("case_id") or "").strip()
        if not case_id and isinstance(desk_note_patch, dict):
            case_id = str(desk_note_patch.get("case_id") or "").strip()
        decision_trace = payload.get("decision_trace")
        trace_id_hint = str((decision_trace or {}).get("trace_id") or "").strip() if isinstance(decision_trace, dict) else ""

        response = self.session.post(
            f"{self.api_v2_base}/ingest",
            json=payload,
            headers=self._mutation_headers(case_id=case_id, signal_id=signal_id or "", trace_id=trace_id_hint),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "v2_ingest")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek v2 ingest failed: {self._error_message(data, response)}")
        if not isinstance(data, dict):
            raise DaszekClientError("Daszek v2 ingest returned an unexpected response shape.")

        persisted = data.get("persisted")
        trace_id = str(persisted.get("trace_id") or "").strip() if isinstance(persisted, dict) else None
        return DaszekV2PushResult(
            status="ingested",
            message_id=message_id,
            signal_id=signal_id or None,
            trace_id=trace_id or None,
            details={
                "ok": bool(data.get("ok")),
                "shadow_contract": str(data.get("shadow_contract") or ""),
                "persisted": persisted if isinstance(persisted, dict) else {},
            },
        )

    def readback_v2_projection(self, *, payload: dict[str, Any], ingest_details: dict[str, Any]) -> dict[str, Any]:
        """Best-effort GET after v2 ingest to prove the record exists in v2 read APIs."""
        _ = ingest_details

        case_patch = payload.get("case_patch") if isinstance(payload.get("case_patch"), dict) else {}
        desk_patch = payload.get("desk_note_patch") if isinstance(payload.get("desk_note_patch"), dict) else {}
        case_id = str(case_patch.get("case_id") or desk_patch.get("case_id") or "").strip()
        desk_note_id = str(desk_patch.get("desk_note_id") or "").strip()
        message_key = str(payload.get("message_key") or "").strip()
        signal_projection = payload.get("signal_projection") if isinstance(payload.get("signal_projection"), dict) else {}
        signal_id = str(signal_projection.get("signal_id") or "").strip()

        detail_preview: dict[str, Any] = {}
        store_readback = "not_checked"
        reason = ""
        readback_note_id = ""
        readback_case_id = ""
        readback_title = ""
        readback_source_message_id = ""
        readback_source_signal_ids: list[str] = []

        try:
            if desk_note_id:
                detail = self.get_v2_note_detail(desk_note_id)
                if isinstance(detail, dict) and detail:
                    store_readback = "found"
                    note = detail.get("note") if isinstance(detail.get("note"), dict) else detail
                    if isinstance(note, dict):
                        readback_note_id = str(note.get("note_id") or note.get("desk_note_id") or desk_note_id).strip()
                        readback_case_id = str(note.get("case_id") or "").strip()
                        readback_title = str(note.get("title") or note.get("title_pl") or "").strip()
                        readback_source_message_id = str(note.get("source_message_id") or message_key).strip()
                        raw_signal_ids = note.get("source_signal_ids")
                        if isinstance(raw_signal_ids, list):
                            readback_source_signal_ids = [
                                str(item).strip() for item in raw_signal_ids if str(item or "").strip()
                            ]
                    detail_preview = {"keys": sorted(detail.keys())[:24]}
                else:
                    store_readback = "not_found"
                    reason = "desk_note_empty_response"
            elif case_id:
                detail = self.get_v2_case_detail(case_id)
                if isinstance(detail, dict) and detail:
                    store_readback = "found"
                    if signal_id:
                        blob = json.dumps(detail, ensure_ascii=False)
                        if signal_id not in blob:
                            reason = "signal_id_not_found_in_case_detail_response"
                    detail_preview = {"keys": sorted(detail.keys())[:24]}
                else:
                    store_readback = "not_found"
                    reason = "case_empty_response"
            else:
                reason = "no_case_id_or_desk_note_id_in_payload"
        except DaszekClientError as exc:
            store_readback = "not_checked"
            reason = sanitize_text(str(exc))[:400]

        if store_readback == "found" and not readback_case_id and case_id:
            readback_case_id = case_id
        if store_readback == "found" and not readback_source_signal_ids and signal_id:
            readback_source_signal_ids = [signal_id]

        ui_visibility_expected = store_readback == "found" and bool(desk_note_id or case_id)
        operator_action_available = bool(readback_note_id and readback_case_id and readback_source_signal_ids)
        allowed_operator_actions = ["zla_sprawa"] if operator_action_available else []

        return {
            "store_readback": store_readback,
            "readback_reason": reason,
            "lookup_case_id": case_id,
            "lookup_desk_note_id": desk_note_id,
            "readback_note_id": readback_note_id,
            "readback_case_id": readback_case_id,
            "readback_title": readback_title,
            "readback_source_message_id": readback_source_message_id,
            "readback_source_signal_ids": readback_source_signal_ids,
            "operator_action_available": operator_action_available,
            "allowed_operator_actions": allowed_operator_actions,
            "expected_bridge_domain": "adjudication" if operator_action_available else "",
            "expected_adjudication_kind": "reject_same_case" if operator_action_available else "",
            "response_preview": detail_preview,
            "ui_visibility_expected": ui_visibility_expected,
        }

    def get_v2_json(self, subpath: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET a `daszek/v2/*` JSON endpoint using the authenticated session."""
        path = subpath.strip().lstrip("/")
        query = {"_runtime_ts": str(int(time.time() * 1000))}
        if isinstance(params, dict):
            for key, value in params.items():
                if value in (None, ""):
                    continue
                query[str(key)] = value
        response = self.session.get(
            f"{self.api_v2_base}/{path}",
            params=query,
            headers=self._telemetry_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, path)
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek v2 GET {path} failed: {self._error_message(data, response)}")
        if not isinstance(data, dict):
            raise DaszekClientError(f"Daszek v2 GET {path} returned an unexpected shape.")
        return data

    def get_v2_thread_memory(self, thread_id: str) -> dict[str, Any]:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return {}
        return self.get_v2_json(f"thread-memory/{thread_id}")

    def get_v2_calibration_profile(self) -> dict[str, Any]:
        return self.get_v2_json("calibration-profile")

    def get_v2_desk(self, *, include_subtle: bool = False) -> dict[str, Any]:
        params = {"include_subtle": 1} if include_subtle else None
        return self.get_v2_json("desk", params=params)

    def get_v2_day(self, *, include_subtle: bool = False) -> dict[str, Any]:
        params = {"include_subtle": 1} if include_subtle else None
        return self.get_v2_json("day", params=params)

    def get_v2_case_detail(self, case_id: str) -> dict[str, Any]:
        case_id = str(case_id or "").strip()
        if not case_id:
            return {}
        return self.get_v2_json(f"cases/{case_id}")

    def get_v2_note_detail(self, note_id: str) -> dict[str, Any]:
        note_id = str(note_id or "").strip()
        if not note_id:
            return {}
        return self.get_v2_json(f"desk-notes/{note_id}")

    def get_v2_bridge_queue(
        self,
        *,
        limit: int = 25,
        status: str = "pending",
        claim: bool = False,
        claimer: str = "node_b",
    ) -> dict[str, Any]:
        query = {
            "status": str(status or "pending"),
            "limit": max(1, int(limit)),
            "_runtime_ts": str(int(time.time() * 1000)),
        }
        if claim:
            query["claim"] = "1"
            query["claimer"] = str(claimer or "node_b")
        response = self.session.get(
            f"{self.api_v2_base}/bridge-queue",
            params=query,
            headers=self._bridge_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "bridge_queue")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek v2 bridge queue fetch failed: {self._error_message(data, response)}")
        if not isinstance(data, dict):
            raise DaszekClientError("Daszek v2 bridge queue returned an unexpected shape.")
        return data

    def _daszek_snapshot_write_token(self) -> str:
        service = str(getattr(self.settings, "daszek_node_b_service_token", "") or "").strip()
        if service:
            return service
        return str(getattr(self.settings, "daszek_bridge_token", "") or "").strip()

    def post_v3_operational_feed_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """POST operational feed snapshot to Daszek v3 (service/bridge token if set, else login + CSRF)."""
        url = f"{self.api_v3_base}/operational-feed-snapshots"
        token = self._daszek_snapshot_write_token()
        try:
            if self._feed_circuit_breaker.is_open:
                raise DaszekClientError("Circuit breaker open for operational feed — skipping push (cooldown active)")
            if token:
                headers = self._telemetry_headers(
                    {
                        "Content-Type": "application/json",
                        "X-Daszek-Bridge-Token": token,
                        "Referer": f"{self.base_url}/daszek/",
                    }
                )
                response = self._http_post_with_retries(
                    url, json_body=snapshot, headers=headers, timeout=float(self.settings.http_timeout),
                    circuit_breaker=self._feed_circuit_breaker,
                )
            else:
                self.login()
                headers = self._mutation_headers()
                headers["Content-Type"] = "application/json"
                response = self._http_post_with_retries(
                    url, json_body=snapshot, headers=headers, timeout=float(self.settings.http_timeout),
                    circuit_breaker=self._feed_circuit_breaker,
                )
            data = self._read_json(response, "v3_operational_feed_post")
            if response.status_code >= 400:
                self._feed_circuit_breaker.record_failure()
                raise DaszekClientError(
                    f"Daszek v3 operational feed ingest failed (HTTP {response.status_code}): {self._error_message(data, response)}"
                )
            self._feed_circuit_breaker.record_success()
            return data if isinstance(data, dict) else {}
        except (requests.ConnectionError, requests.Timeout) as exc:
            self._feed_circuit_breaker.record_failure()
            raise DaszekClientError(f"Daszek v3 operational feed connection failed: {exc}") from exc

    def get_v3_operational_feed_snapshot_latest(self) -> dict[str, Any]:
        """GET latest operational feed snapshot (session auth; feed-first doctor probe)."""
        self.login()
        url = f"{self.api_v3_base}/operational-feed-snapshots/latest"
        response = self.session.get(
            url,
            headers=self._telemetry_headers({"Cache-Control": "no-cache", "Pragma": "no-cache"}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "v3_operational_feed_latest")
        if response.status_code >= 400:
            raise DaszekClientError(
                f"Daszek v3 operational feed latest failed (HTTP {response.status_code}): {self._error_message(data, response)}"
            )
        return data if isinstance(data, dict) else {}

    def post_v3_ingress_quality_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """POST ingress quality snapshot to Daszek v3 (service/bridge token if set, else login + CSRF)."""

        url = f"{self.api_v3_base}/ingress-quality-snapshots"
        token = self._daszek_snapshot_write_token()
        if token:
            headers = self._telemetry_headers(
                {
                    "Content-Type": "application/json",
                    "X-Daszek-Bridge-Token": token,
                    "Referer": f"{self.base_url}/daszek/",
                }
            )
            response = self._http_post_with_retries(
                url, json_body=snapshot, headers=headers, timeout=float(self.settings.http_timeout)
            )
        else:
            self.login()
            headers = self._mutation_headers()
            headers["Content-Type"] = "application/json"
            response = self._http_post_with_retries(
                url, json_body=snapshot, headers=headers, timeout=float(self.settings.http_timeout)
            )
        data = self._read_json(response, "v3_ingress_quality_post")
        if response.status_code >= 400:
            raise DaszekClientError(
                f"Daszek v3 ingress quality ingest failed (HTTP {response.status_code}): {self._error_message(data, response)}"
            )
        return data if isinstance(data, dict) else {}

    def complete_v2_bridge_queue_item(self, queue_id: str, *, status: str = "completed", error: str = "") -> dict[str, Any]:
        queue_id = str(queue_id or "").strip()
        if not queue_id:
            raise DaszekClientError("Bridge queue completion requires queue_id.")
        payload = {
            "queue_id": queue_id,
            "bridge_status": str(status or "completed").strip() or "completed",
            "bridge_error": str(error or "")[:4000],
        }
        response = self.session.post(
            f"{self.api_v2_base}/bridge-queue/complete",
            json=payload,
            headers=self._bridge_headers({"Content-Type": "application/json"}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "bridge_queue_complete")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek v2 bridge queue completion failed: {self._error_message(data, response)}")
        if not isinstance(data, dict):
            raise DaszekClientError("Daszek v2 bridge queue completion returned an unexpected shape.")
        return data

    def fetch_csrf_token(self) -> str:
        response = self.session.get(
            f"{self.api_base}/csrf",
            headers=self._telemetry_headers({}),
            timeout=self.settings.http_timeout,
        )
        data = self._read_json(response, "csrf")
        if response.status_code >= 400:
            raise DaszekClientError(f"Daszek csrf fetch failed: {self._error_message(data, response)}")

        csrf_token = ""
        if isinstance(data, dict):
            csrf_token = str(data.get("csrf_token") or "").strip()
        if not csrf_token:
            raise DaszekClientError("Daszek csrf fetch succeeded without csrf_token.")
        return csrf_token

    def _mutation_headers(self, *, case_id: str = "", signal_id: str = "", trace_id: str = "") -> dict[str, str]:
        return self._telemetry_headers(
            {
            "X-CSRF-Token": self.csrf_token,
            "Referer": f"{self.base_url}/daszek/",
            },
            case_id=case_id,
            signal_id=signal_id,
            trace_id=trace_id,
        )

    def _bridge_headers(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        token = str(getattr(self.settings, "daszek_bridge_token", "") or "").strip()
        if not token:
            raise DaszekClientError("Missing DASZEK_BRIDGE_TOKEN for Daszek bridge queue API.")
        payload = dict(headers or {})
        payload["X-Daszek-Bridge-Token"] = token
        payload["Referer"] = f"{self.base_url}/daszek/"
        return self._telemetry_headers(payload)

    def _bootstrap_session_base(self) -> None:
        try:
            response = self.session.get(
                f"{self.base_url}/daszek/",
                headers=self._telemetry_headers({}),
                timeout=self.settings.http_timeout,
            )
        except (requests.RequestException, DaszekClientError):
            # Base-URL discovery is best-effort; an unreachable Daszek is handled by the caller.
            return

        final_url = str(getattr(response, "url", "") or "").strip()
        if not final_url:
            return

        parts = urlsplit(final_url)
        if not parts.scheme or not parts.netloc:
            return

        resolved_base = f"{parts.scheme}://{parts.netloc}".rstrip("/")
        if resolved_base == self.base_url:
            resolved_changed = False
        else:
            self.base_url = resolved_base
            self.api_base = f"{self.base_url}/wp-json/daszek/v1"
            self.api_v2_base = f"{self.base_url}/wp-json/daszek/v2"
            self.api_v3_base = f"{self.base_url}/wp-json/daszek/v3"
            resolved_changed = True

        page_text = str(getattr(response, "text", "") or "")
        match = re.search(r'<meta\s+name="csrf-token"\s+content="([^"]+)"', page_text)
        if match:
            self.bootstrap_csrf_token = match.group(1).strip()
        elif resolved_changed:
            self.bootstrap_csrf_token = ""

    def _telemetry_headers(
        self,
        headers: dict[str, str] | None,
        *,
        case_id: str = "",
        signal_id: str = "",
        trace_id: str = "",
    ) -> dict[str, str]:
        payload = {str(key): str(value) for key, value in (headers or {}).items()}
        runtime = self.observability_runtime
        if runtime is None:
            return payload
        try:
            return runtime.inject_headers(
                payload,
                case_id=case_id,
                signal_id=signal_id,
                trace_id=trace_id,
            )
        except Exception:
            return payload

    def _find_existing_task(self, tasks: list[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any] | None:
        expected = self._dedupe_signature(payload)

        for task in tasks:
            actual = self._dedupe_signature(task)

            if actual["kind"] != expected["kind"]:
                continue
            if actual["source"] != expected["source"]:
                continue
            if expected["message_id"] and expected["message_id"] != actual["message_id"]:
                continue
            if expected["case_key"] and actual["case_key"] and expected["case_key"] != actual["case_key"]:
                continue
            if expected["decision_action"] and actual["decision_action"] and expected["decision_action"] != actual["decision_action"]:
                continue
            if expected["priority"] and actual["priority"] and expected["priority"] != actual["priority"]:
                continue
            if expected["due_at"] and actual["due_at"] and expected["due_at"] != actual["due_at"]:
                continue
            if expected["note"] and actual["note"] and expected["note"] != actual["note"]:
                continue
            if expected["kind"] == "case_update":
                if not self._same_state_change(expected["state_change"], actual["state_change"]):
                    continue
            return task

        return None

    def _dedupe_signature(self, payload: dict[str, Any]) -> dict[str, Any]:
        external_ref = payload.get("external_ref") or {}
        intake = payload.get("intake") or {}
        return {
            "kind": str(payload.get("kind") or ""),
            "source": str(payload.get("source") or ""),
            "message_id": str(external_ref.get("message_id") or ""),
            "case_key": str(external_ref.get("case_key") or ""),
            "priority": str(payload.get("priority") or ""),
            "due_at": str(payload.get("due_at") or ""),
            "note": sanitize_text(str(payload.get("note") or "")).strip(),
            "decision_action": str(intake.get("decision_action") or ""),
            "state_change": intake.get("state_change") or {},
        }

    def _same_state_change(self, expected: Any, actual: Any) -> bool:
        if not isinstance(expected, dict) or not expected:
            return True
        if not isinstance(actual, dict):
            return False
        for field in ("detected", "from_state", "to_state"):
            expected_value = expected.get(field)
            actual_value = actual.get(field)
            if expected_value in (None, "", False):
                continue
            if expected_value != actual_value:
                return False
        return True

    def _read_json(self, response: requests.Response, operation: str) -> Any:
        try:
            return response.json()
        except ValueError:
            pass

        text = response.text
        if text.startswith("\ufeff"):
            text = text.lstrip("\ufeff")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            body_preview = sanitize_text(text[:500].strip())
            raise DaszekClientError(
                f"Daszek {operation} returned invalid JSON. HTTP {response.status_code}. Body: {body_preview!r}"
            ) from exc

    def _error_message(self, payload: dict[str, Any], response: requests.Response) -> str:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("message") or payload.get("code") or "").strip()
        if not message:
            message = sanitize_text(response.text[:300].strip()) or f"HTTP {response.status_code}"
        return sanitize_text(message)
