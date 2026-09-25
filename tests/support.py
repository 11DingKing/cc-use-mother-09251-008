"""测试公共夹具与工具。"""
from __future__ import annotations

import json
from datetime import date, timedelta
from http.client import HTTPConnection

from service_09251_008.services import AppService
from service_09251_008.storage import Repository
from service_09251_008.timeutil import FixedClock

SECRET = "unit-test-secret"
START = "2026-09-25T10:00:00+00:00"


def make_app(tmp_path, clock: FixedClock | None = None, secret: str = SECRET):
    clock = clock or clock_at()
    repo = Repository(str(tmp_path / "busy.db"))
    app = AppService(repo, clock=clock, signing_secret=secret)
    app.ensure_default_rule()
    return app


def clock_at(text: str = START) -> FixedClock:
    from service_09251_008.timeutil import parse_iso

    return FixedClock(parse_iso(text))


def seed_areas(app: AppService) -> None:
    app.register_area("SA001", "阳澄湖服务区")
    app.register_area("SA002", "梅村服务区")
    app.register_area("SA003", "芳茂山服务区")


def put_reading(app, area, metric, day, value) -> None:
    app.add_reading(area, metric, day, value)


def window_days(app, area: str, metric: str, day: str, n: int,
                value: float) -> None:
    """在 day 及之前 n-1 天写入同一读数，保证窗口均值即 value。"""
    d = date.fromisoformat(day)
    for i in range(n):
        put_reading(app, area, metric, (d - timedelta(days=i)).isoformat(), value)


def approve_both(app, batch_id: str, first="reviewer_a",
                 second="reviewer_b") -> dict:
    app.review(batch_id, first, "first", "approve")
    return app.review(batch_id, second, "second", "approve")


# -- HTTP 工具 -------------------------------------------------------------

def http_request(port: int, method: str, path: str, token: str | None = None,
                 body: dict | None = None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=payload, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8")
    conn.close()
    data = json.loads(raw) if raw else {}
    return resp.status, data
