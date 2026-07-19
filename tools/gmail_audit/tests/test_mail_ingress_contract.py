"""Contract tests: mail-ingress HTTP seam (local stub, no external services).

Primary Gate B evidence for the mail-ingress row is this in-repo matrix.
Optional live staging checks are documented in CROSS_REPO_LIVE_SMOKE_D1.md.
"""

from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from unittest import mock

import pytest

from mail_ingress_contract import AGENT_KEY_HEADER, post_mail_ingress_json

GOOD_KEY = "test-agent-key-ok"
WRONG_KEY = "wrong-key"


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    n = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(n) if n else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def _send_json(handler: BaseHTTPRequestHandler, code: int, body: dict[str, Any]) -> None:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def make_handler(
    *,
    good_key: str = GOOD_KEY,
    route: Callable[[BaseHTTPRequestHandler, str, dict[str, Any], str | None], None] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return  # quiet tests

        def do_POST(self) -> None:  # noqa: N802
            key = self.headers.get(AGENT_KEY_HEADER) or self.headers.get(AGENT_KEY_HEADER.lower())
            path = self.path.split("?", 1)[0]
            body = _read_json_body(self)
            if route is not None:
                route(self, path, body, key)
                return
            if key != good_key:
                _send_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if path == "/happy":
                _send_json(self, HTTPStatus.OK, {"ok": True, "path": "happy"})
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "not found")

    return Handler


@pytest.fixture
def server_happy() -> str:
    handler = make_handler()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)

    def serve() -> None:
        httpd.serve_forever(poll_interval=0.1)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    port = httpd.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/happy"
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def test_mail_ingress_happy_path(server_happy: str) -> None:
    r = post_mail_ingress_json(server_happy, agent_key=GOOD_KEY, payload={"smoke": "d1", "ts": "2026-01-01T00:00:00Z"})
    assert r.status_code == 200
    assert "ok" in r.body_preview


def test_mail_ingress_unauthorized_wrong_key() -> None:
    handler = make_handler()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.timeout = 1

    def serve() -> None:
        httpd.serve_forever(poll_interval=0.1)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/happy"
    try:
        r = post_mail_ingress_json(url, agent_key=WRONG_KEY, payload={"smoke": "d1"})
        assert r.status_code == 401
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def test_mail_ingress_duplicate_idempotency() -> None:
    seen: set[str] = set()

    def route(
        self: BaseHTTPRequestHandler,
        path: str,
        body: dict[str, Any],
        key: str | None,
    ) -> None:
        if key != GOOD_KEY:
            _send_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        idem = self.headers.get("X-Idempotency-Key")
        if path != "/dup":
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not idem:
            _send_json(self, HTTPStatus.BAD_REQUEST, {"error": "missing_idempotency"})
            return
        if idem in seen:
            _send_json(self, HTTPStatus.OK, {"deduped": True, "idempotency_key": idem})
            return
        seen.add(idem)
        _send_json(self, HTTPStatus.CREATED, {"created": True, "idempotency_key": idem})

    Handler = make_handler(route=route)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def serve() -> None:
        httpd.serve_forever(poll_interval=0.1)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/dup"
    try:
        idem = "idem-proof-001"
        r1 = post_mail_ingress_json(url, agent_key=GOOD_KEY, payload={"x": 1}, idempotency_key=idem)
        r2 = post_mail_ingress_json(url, agent_key=GOOD_KEY, payload={"x": 1}, idempotency_key=idem)
        assert r1.status_code == 201
        assert r2.status_code == 200
        assert "deduped" in r2.body_preview
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def test_mail_ingress_transient_then_success() -> None:
    attempts = {"n": 0}

    def route(
        self: BaseHTTPRequestHandler,
        path: str,
        body: dict[str, Any],
        key: str | None,
    ) -> None:
        if key != GOOD_KEY:
            _send_json(self, HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        if path != "/transient":
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        attempts["n"] += 1
        if attempts["n"] == 1:
            self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"retry":true}')
            return
        _send_json(self, HTTPStatus.OK, {"ok": True, "after_retry": True})

    Handler = make_handler(route=route)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)

    def serve() -> None:
        httpd.serve_forever(poll_interval=0.1)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/transient"
    try:
        r1 = post_mail_ingress_json(url, agent_key=GOOD_KEY, payload={"smoke": "d1"})
        assert r1.status_code == 503
        r2 = post_mail_ingress_json(url, agent_key=GOOD_KEY, payload={"smoke": "d1"})
        assert r2.status_code == 200
        assert "after_retry" in r2.body_preview
    finally:
        httpd.shutdown()
        t.join(timeout=2)


def test_post_mail_ingress_json_uses_requests_post() -> None:
    with mock.patch("mail_ingress_contract.requests.post") as m:
        m.return_value.status_code = 201
        m.return_value.text = '{"created":true}'
        m.return_value.headers = {"Content-Type": "application/json"}
        r = post_mail_ingress_json(
            "http://example.test/ingress",
            agent_key="k",
            payload={"a": 1},
            idempotency_key="ik",
        )
        assert r.status_code == 201
        args, kwargs = m.call_args
        assert args[0] == "http://example.test/ingress"
        assert kwargs["headers"]["X-Idempotency-Key"] == "ik"
        assert kwargs["headers"][AGENT_KEY_HEADER] == "k"
