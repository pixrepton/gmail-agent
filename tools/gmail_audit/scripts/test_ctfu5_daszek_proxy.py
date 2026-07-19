#!/usr/bin/env python3
"""CT-FU-5: Daszek proxy materialize approve without operator_id in body."""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from http.cookiejar import CookieJar

BASE = "http://127.0.0.1:8090"
ENGAGEMENT_ID = sys.argv[1] if len(sys.argv) > 1 else "eng_ar_ct_fu5_1c716080"
PROPOSAL_ID = sys.argv[2] if len(sys.argv) > 2 else "prop_b50c86dc"


def request(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, str]:
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def main() -> int:
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    urllib.request.install_opener(opener)

    code, text = request("POST", "/wp-json/daszek/v1/login", {"login": "konrad", "password": "konrad123"})
    print("login", code, text[:200].encode('ascii', 'backslashreplace').decode())
    if code != 200:
        return 1

    code, text = request("GET", "/wp-json/daszek/v1/me")
    me = json.loads(text)
    csrf = me.get("csrf_token", "")
    print("me", code, "user=", me.get("username"), "csrf=", bool(csrf))

    code, text = request(
        "POST",
        f"/wp-json/daszek/v2/engagements/{ENGAGEMENT_ID}/materialize/approve",
        {"proposal_id": PROPOSAL_ID},
        headers={"X-CSRF-Token": csrf},
    )
    print("approve", code)
    print(text)
    if code == 200:
        payload = json.loads(text)
        op = payload.get("operator_id") or (payload.get("data") or {}).get("operator_id")
        print("operator_id_in_response=", op)
    return 0 if code == 200 else 2


if __name__ == "__main__":
    raise SystemExit(main())
