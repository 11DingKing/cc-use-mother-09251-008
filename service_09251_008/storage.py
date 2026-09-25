"""持久化边界：SQLite 仓储。

职责：
- 持有数据库连接（每线程一条，WAL 模式，busy_timeout 保证写者排队）；
- 提供 ``tx()`` 立即事务（BEGIN IMMEDIATE），把并发写串行化，
  重复审批、重复发布等冲突由唯一约束兜底并映射为领域错误；
- 维护审计哈希链：所有写操作在同一事务内追加防篡改事件。

应用服务通过本类的原语组合业务事务，不直接操作连接。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .signatures import GENESIS_HASH, audit_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_versions (
  id            TEXT PRIMARY KEY,
  version_no    INTEGER NOT NULL UNIQUE,
  metrics_json  TEXT NOT NULL,
  status        TEXT NOT NULL,
  created_by    TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
  id                 TEXT PRIMARY KEY,
  version_no         INTEGER NOT NULL UNIQUE,
  metric_version_id  TEXT NOT NULL REFERENCES metric_versions(id),
  rules_json         TEXT NOT NULL,
  status             TEXT NOT NULL,
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observation_periods (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  start_at    TEXT NOT NULL,
  end_at      TEXT NOT NULL,
  status      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
  period_id   TEXT NOT NULL REFERENCES observation_periods(id),
  area_code   TEXT NOT NULL,
  metric_code TEXT NOT NULL,
  value       REAL NOT NULL,
  observed_at TEXT NOT NULL,
  PRIMARY KEY (period_id, area_code, metric_code)
);

CREATE TABLE IF NOT EXISTS manual_exceptions (
  id          TEXT PRIMARY KEY,
  area_code   TEXT NOT NULL,
  action      TEXT NOT NULL,
  grade       TEXT,
  valid_from  TEXT NOT NULL,
  valid_to    TEXT NOT NULL,
  reason      TEXT NOT NULL,
  status      TEXT NOT NULL,
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  revoked_at  TEXT,
  revoked_by  TEXT
);

CREATE TABLE IF NOT EXISTS candidate_lists (
  id                 TEXT PRIMARY KEY,
  period_id          TEXT NOT NULL REFERENCES observation_periods(id),
  rule_version_id    TEXT NOT NULL REFERENCES rule_versions(id),
  metric_version_id  TEXT NOT NULL REFERENCES metric_versions(id),
  status             TEXT NOT NULL,
  created_by         TEXT NOT NULL,
  created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_entries (
  list_id        TEXT NOT NULL REFERENCES candidate_lists(id),
  area_code      TEXT NOT NULL,
  grade          TEXT NOT NULL,
  source         TEXT NOT NULL,
  triggered_json TEXT NOT NULL,
  note           TEXT,
  PRIMARY KEY (list_id, area_code)
);

CREATE TABLE IF NOT EXISTS reviews (
  subject_type TEXT NOT NULL,
  subject_id   TEXT NOT NULL,
  reviewer     TEXT NOT NULL,
  decision     TEXT NOT NULL,
  decided_at   TEXT NOT NULL,
  PRIMARY KEY (subject_type, subject_id, reviewer)
);

CREATE TABLE IF NOT EXISTS public_versions (
  version_no     INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL,
  source_list_id TEXT UNIQUE REFERENCES candidate_lists(id),
  emergency_id   TEXT UNIQUE,
  published_by   TEXT NOT NULL,
  published_at   TEXT NOT NULL,
  signature      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS public_entries (
  version_no INTEGER NOT NULL REFERENCES public_versions(version_no),
  area_code  TEXT NOT NULL,
  grade      TEXT NOT NULL,
  label      TEXT NOT NULL,
  message    TEXT NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to   TEXT NOT NULL,
  revoked_at TEXT,
  revoked_by TEXT,
  PRIMARY KEY (version_no, area_code)
);

CREATE TABLE IF NOT EXISTS emergency_upgrades (
  id              TEXT PRIMARY KEY,
  version_no      INTEGER NOT NULL REFERENCES public_versions(version_no),
  reason          TEXT NOT NULL,
  status          TEXT NOT NULL,
  review_deadline TEXT NOT NULL,
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
  seq       INTEGER PRIMARY KEY AUTOINCREMENT,
  at        TEXT NOT NULL,
  actor     TEXT NOT NULL,
  action    TEXT NOT NULL,
  subject   TEXT NOT NULL,
  payload   TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash      TEXT NOT NULL
);
"""


class Repository:
    """SQLite 仓储：连接、事务与审计链。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        """当前线程的连接（懒创建）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """立即事务：写操作串行化，冲突在提交前即可被发现。"""
        conn = self.connection
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    # -- 基础原语 ---------------------------------------------------------

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.connection.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(row) for row in self.connection.execute(sql, params)]

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, params)

    # -- 并发敏感的序号分配（须在 tx 内调用） ------------------------------

    def next_value(self, table: str, column: str = "version_no") -> int:
        row = self.one(f"SELECT COALESCE(MAX({column}), 0) + 1 AS n FROM {table}")
        return int(row["n"])

    # -- 审计链 -------------------------------------------------------------

    def audit(self, *, actor: str, action: str, subject: str, payload: Any, at: str) -> str:
        """在当前事务内追加一条审计事件，返回事件哈希。"""
        last = self.one("SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1")
        prev_hash = last["hash"] if last else GENESIS_HASH
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = audit_hash(prev_hash, at, actor, action, subject, payload_json)
        self.execute(
            "INSERT INTO audit_events(at, actor, action, subject, payload, prev_hash, hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (at, actor, action, subject, payload_json, prev_hash, digest),
        )
        return digest

    def audit_events(self) -> list[dict]:
        return self.query("SELECT * FROM audit_events ORDER BY seq")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
