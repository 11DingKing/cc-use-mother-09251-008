"""可替换端口：时间、标识与签名密钥。

应用服务只依赖这里定义的协议，测试可以用 ManualClock / SequentialIds
稳定复现状态变化，生产环境则注入系统实现。
"""
from __future__ import annotations

import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol


class Clock(Protocol):
    """时间端口。"""

    def now(self) -> datetime:
        """返回当前的带时区时间。"""
        ...


class SystemClock:
    """生产时钟：UTC 系统时间。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock:
    """测试时钟：可设置、可推进。"""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._current = start
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def set(self, moment: datetime) -> None:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        with self._lock:
            self._current = moment

    def advance(self, **kwargs) -> datetime:
        """按 timedelta 参数推进时钟并返回新时间。"""
        with self._lock:
            self._current = self._current + timedelta(**kwargs)
            return self._current


class IdGenerator(Protocol):
    """标识端口。"""

    def new_id(self, prefix: str) -> str:
        ...


class UuidIds:
    """生产标识：随机 UUID。"""

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"


class SequentialIds:
    """测试标识：确定性的递增序号。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            return f"{prefix}-{self._counters[prefix]:06d}"


class KeyProvider(Protocol):
    """签名密钥端口。"""

    def signing_key(self) -> bytes:
        ...


class FileKeyProvider:
    """从数据目录读取签名密钥；不存在时生成随机密钥并落盘（权限 600）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def signing_key(self) -> bytes:
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(secrets.token_hex(32), encoding="utf-8")
            self._path.chmod(0o600)
        return bytes.fromhex(self._path.read_text(encoding="utf-8").strip())


class StaticKeyProvider:
    """测试密钥：直接使用给定字节。"""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def signing_key(self) -> bytes:
        return self._key
