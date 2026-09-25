"""SQLite 持久化。

设计要点：
  * 每次操作从连接池取连接，写事务一律 ``BEGIN IMMEDIATE``，配合写串行化，
    解决 *并发发布* 与 *重复审批* 的竞争；
  * 进程级互斥锁文件（``.lock`` 经 ``fcntl.flock``）用于"需要跨进程串行"
    的管理操作（规则切换、发布），即使多进程部署也安全；
  * 所有状态落库，进程崩溃重启后可凭库中状态恢复（含紧急件补审期限追踪）。
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from .timeutil import iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS service_areas (
    area_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metric_readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    area_id TEXT NOT NULL,
    metric_key TEXT NOT NULL,
    day TEXT NOT NULL,
    value REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(area_id, metric_key, day)
);
CREATE INDEX IF NOT EXISTS idx_readings_lookup
    ON metric_readings(area_id, metric_key, day);

CREATE TABLE IF NOT EXISTS rule_versions (
    version TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    retired_at TEXT
);

CREATE TABLE IF NOT EXISTS manual_exceptions (
    exc_id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    level TEXT,
    reason TEXT NOT NULL,
    author TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exc_area ON manual_exceptions(area_id);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    rule_version TEXT NOT NULL,
    eval_day TEXT NOT NULL,
    status TEXT NOT NULL,
    emergency INTEGER NOT NULL DEFAULT 0,
    payload_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    published_at TEXT,
    publish_deadline TEXT,
    reviewed_at TEXT,
    revoked_at TEXT,
    revoke_reason TEXT,
    superseded_at TEXT,
    signature TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_status ON batches(status);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    decision TEXT NOT NULL,
    comment TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE(batch_id, stage, reviewer)
);

-- 公开视图的区间：每个服务区在 [valid_from, valid_to) 内的有效等级。
-- valid_to 为 NULL 表示当前有效；撤销只截断尚未结束的区间。
CREATE TABLE IF NOT EXISTS public_intervals (
    interval_id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL,
    level TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pi_lookup
    ON public_intervals(area_id, valid_from);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE IF NOT EXISTS api_keys (
    token TEXT PRIMARY KEY,
    scope TEXT NOT NULL,          -- internal / public
    label TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


class Repository:
    """数据访问对象。线程内为每次操作新建短连接（check_same_thread=False）。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._lock_path = os.path.abspath(db_path) + ".lock"
        self._init_schema()

    # -- 基础连接 / 事务 --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path, timeout=30, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_schema(self) -> None:
        with self._flock():
            conn = self._connect()
            try:
                conn.executescript(SCHEMA)
                conn.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                conn.commit()
            finally:
                conn.close()

    @contextmanager
    def _flock(self) -> Iterator[None]:
        """跨进程互斥锁（管理操作用）。"""
        import fcntl

        fh = open(self._lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()

    @contextmanager
    def transaction(self, exclusive: bool = False) -> Iterator[sqlite3.Connection]:
        """短事务。exclusive=True 使用 BEGIN IMMEDIATE 立即取写锁。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if exclusive else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def admin_lock(self) -> Iterator[None]:
        """跨进程串行化的管理事务入口。"""
        with self._flock():
            yield

    # -- 通用读取 ---------------------------------------------------------

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        conn = self._connect()
        try:
            return list(conn.execute(sql, params))
        finally:
            conn.close()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    # -- 审计 -------------------------------------------------------------

    def audit(
        self,
        conn: sqlite3.Connection,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str,
        detail: dict[str, Any] | None = None,
        at: datetime | None = None,
    ) -> None:
        from datetime import timezone

        stamp = iso(at) if at else iso(datetime.now(timezone.utc))
        conn.execute(
            "INSERT INTO audit_log(at, actor, action, entity_type, entity_id, "
            "detail_json) VALUES(?,?,?,?,?,?)",
            (
                stamp,
                actor, action, entity_type, entity_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True)
                if detail is not None else None,
            ),
        )

    # -- 服务区 -----------------------------------------------------------

    def upsert_area(self, area_id: str, name: str, now_iso: str) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO service_areas(area_id, name, created_at) "
                "VALUES(?,?,?) ON CONFLICT(area_id) DO UPDATE SET name=excluded.name",
                (area_id, name, now_iso),
            )

    def list_areas(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM service_areas WHERE active=1 ORDER BY area_id"
        )

    # -- 读数 -------------------------------------------------------------

    def upsert_reading(
        self, area_id: str, metric_key: str, day: str, value: float, now_iso: str
    ) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO metric_readings(area_id, metric_key, day, value, "
                "created_at) VALUES(?,?,?,?,?) ON CONFLICT(area_id, metric_key, day) "
                "DO UPDATE SET value=excluded.value",
                (area_id, metric_key, day, float(value), now_iso),
            )

    def readings_since(self, since_day: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT area_id, metric_key, day, value FROM metric_readings "
            "WHERE day >= ? ORDER BY day",
            (since_day,),
        )

    # -- 规则版本 ---------------------------------------------------------

    def insert_rule_version(
        self, version: str, spec: dict, created_by: str, now_iso: str
    ) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO rule_versions(version, spec_json, status, created_by, "
                "created_at) VALUES(?,?,?,?,?)",
                (version, json.dumps(spec, ensure_ascii=False), "draft",
                 created_by, now_iso),
            )

    def get_rule_version(self, version: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM rule_versions WHERE version=?", (version,)
        )

    def active_rule_version(self) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM rule_versions WHERE status='active' "
            "ORDER BY activated_at DESC LIMIT 1"
        )

    def list_rule_versions(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM rule_versions ORDER BY created_at DESC"
        )

    def activate_rule(self, version: str, now_iso: str) -> None:
        """在调用方持有 admin_lock 时执行：旧版本退役、新版本生效。"""
        with self.transaction(exclusive=True) as conn:
            row = conn.execute(
                "SELECT status FROM rule_versions WHERE version=?", (version,)
            ).fetchone()
            if row is None:
                raise KeyError(version)
            conn.execute(
                "UPDATE rule_versions SET status='retired', retired_at=? "
                "WHERE status='active'",
                (now_iso,),
            )
            conn.execute(
                "UPDATE rule_versions SET status='active', activated_at=? "
                "WHERE version=?",
                (now_iso, version),
            )

    # -- 人工例外 ---------------------------------------------------------

    def insert_exception(self, exc: dict, now_iso: str) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO manual_exceptions(exc_id, area_id, kind, level, reason, "
                "author, valid_from, valid_to, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (exc["exc_id"], exc["area_id"], exc["kind"], exc.get("level"),
                 exc["reason"], exc["author"], exc["valid_from"],
                 exc.get("valid_to"), now_iso),
            )

    def list_active_exceptions(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM manual_exceptions WHERE revoked=0"
        )

    def revoke_exception(self, exc_id: str) -> bool:
        with self.transaction(exclusive=True) as conn:
            cur = conn.execute(
                "UPDATE manual_exceptions SET revoked=1 WHERE exc_id=? AND revoked=0",
                (exc_id,),
            )
            return cur.rowcount > 0

    # -- 批次 / 审批 ------------------------------------------------------

    def insert_batch(self, b: dict) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO batches(batch_id, rule_version, eval_day, status, "
                "emergency, payload_json, created_by, created_at, published_at, "
                "publish_deadline, signature) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (b["batch_id"], b["rule_version"], b["eval_day"], b["status"],
                 1 if b.get("emergency") else 0,
                 json.dumps(b["payload"], ensure_ascii=False),
                 b["created_by"], b["created_at"],
                 b.get("published_at"), b.get("publish_deadline"),
                 b.get("signature")),
            )

    def get_batch(self, batch_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM batches WHERE batch_id=?", (batch_id,))

    def list_batches(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM batches ORDER BY created_at DESC LIMIT ?", (limit,)
        )

    def add_approval(
        self, batch_id: str, stage: str, reviewer: str, decision: str,
        comment: str | None, now_iso: str,
    ) -> None:
        """UNIQUE(batch_id, stage, reviewer) 会在重复审批时抛 IntegrityError。"""
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO approvals(batch_id, stage, reviewer, decision, comment, "
                "decided_at) VALUES(?,?,?,?,?,?)",
                (batch_id, stage, reviewer, decision, comment, now_iso),
            )

    def approvals_of(self, batch_id: str, stage: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM approvals WHERE batch_id=? AND stage=? AND decision='approve'"
            " ORDER BY decided_at",
            (batch_id, stage),
        )

    def decisions_of(self, batch_id: str, stage: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM approvals WHERE batch_id=? AND stage=? ORDER BY decided_at",
            (batch_id, stage),
        )

    def update_batch_status(
        self, batch_id: str, status: str, **fields: Any
    ) -> None:
        allowed = {
            "published_at", "reviewed_at", "revoked_at", "revoke_reason",
            "superseded_at", "signature", "publish_deadline",
        }
        sets = ["status=?"]
        params: list[Any] = [status]
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(f"非法字段 {k}")
            sets.append(f"{k}=?")
            params.append(v)
        params.append(batch_id)
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                f"UPDATE batches SET {', '.join(sets)} WHERE batch_id=?", params
            )

    def open_emergency_batches(self) -> list[sqlite3.Row]:
        """进程恢复/定期追踪用：所有已发布但补审期限未定结论的紧急件。"""
        return self.query(
            "SELECT * FROM batches WHERE emergency=1 AND status='emergency'"
        )

    # -- 公开区间 ---------------------------------------------------------

    def insert_interval(self, iv: dict) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO public_intervals(interval_id, area_id, level, batch_id, "
                "valid_from, valid_to, created_at) VALUES(?,?,?,?,?,?,?)",
                (iv["interval_id"], iv["area_id"], iv["level"], iv["batch_id"],
                 iv["valid_from"], iv.get("valid_to"), iv["created_at"]),
            )

    def open_intervals(self, area_ids: list[str] | None = None) -> list[sqlite3.Row]:
        if area_ids is None:
            return self.query(
                "SELECT * FROM public_intervals WHERE valid_to IS NULL"
            )
        if not area_ids:
            return []
        marks = ",".join("?" * len(area_ids))
        return self.query(
            f"SELECT * FROM public_intervals WHERE valid_to IS NULL "
            f"AND area_id IN ({marks})",
            tuple(area_ids),
        )

    def close_interval(
        self, conn: sqlite3.Connection, interval_id: str, valid_to: str
    ) -> None:
        conn.execute(
            "UPDATE public_intervals SET valid_to=? WHERE interval_id=? AND "
            "valid_to IS NULL",
            (valid_to, interval_id),
        )

    def intervals_on(self, day: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM public_intervals WHERE valid_from <= ? "
            "AND (valid_to IS NULL OR ? < valid_to) ORDER BY area_id",
            (day, day),
        )

    def all_intervals(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM public_intervals ORDER BY valid_from, area_id"
        )

    # -- API Key ----------------------------------------------------------

    def insert_api_key(self, token: str, scope: str, label: str, now_iso: str) -> None:
        with self.transaction(exclusive=True) as conn:
            conn.execute(
                "INSERT INTO api_keys(token, scope, label, created_at) "
                "VALUES(?,?,?,?)",
                (token, scope, label, now_iso),
            )

    def get_api_key(self, token: str) -> sqlite3.Row | None:
        return self.query_one(
            "SELECT * FROM api_keys WHERE token=? AND active=1", (token,)
        )

    # -- 审计读取 ---------------------------------------------------------

    def audit_tail(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        )
