"""时间端口。

领域与应用层不直接调用 ``datetime.now``，而是通过 :class:`Clock` 取当前时间，
便于在测试中固定/拨动时钟（跨日有效期、补审期限追踪）。
"""
from __future__ import annotations

from datetime import datetime, timezone


class Clock:
    """可替换的时间源。"""

    def now(self) -> datetime:
        """返回当前 UTC 时间（带时区）。"""
        return datetime.now(timezone.utc)

    def today(self) -> str:
        """返回当前 UTC 日期（``YYYY-MM-DD``）。"""
        return self.now().date().isoformat()


class FixedClock(Clock):
    """测试用固定时钟，可手动推进。"""

    def __init__(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment.astimezone(timezone.utc)

    def now(self) -> datetime:
        return self._moment

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        self._moment = moment.astimezone(timezone.utc)

    def advance(self, **delta) -> None:
        from datetime import timedelta

        self._moment += timedelta(**delta)


def parse_iso(value: str) -> datetime:
    """解析 ISO-8601 时间串，无时区时按 UTC 处理。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    """统一的 UTC ISO-8601 序列化形式。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
