"""应用服务层：编排领域引擎与持久化，承载全部业务用例。

关键流程：
  generate_candidates  -> 内部候选（含指标触发痕迹），状态 candidate
  review               -> 双人复核（两阶段、两不同复核人、不可与提单人相同），
                          两阶段通过后在 *同一把跨进程锁与事务* 内发布，
                          杜绝并发发布与重复审批；
  emergency_publish    -> 紧急升级先发布后补审，自动记录补审期限；
  run_deadline_sweep   -> 进程启动（进程恢复）与后台定时追踪补审期限，
                          超期自动失效并撤回其尚在生效的区间；
  revoke               -> 只关闭 valid_to 为空（尚未结束）的区间。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from .constants import (
    APPROVE,
    BATCH_CANDIDATE,
    BATCH_EMERGENCY,
    BATCH_EXPIRED,
    BATCH_PUBLISHED,
    BATCH_REJECTED,
    BATCH_REVOKED,
    EMERGENCY_REVIEW_DEADLINE_HOURS,
    LEVELS,
    new_id,
)
from .errors import (
    ConflictError,
    DeadlinePassedError,
    DuplicateApprovalError,
    NotFoundError,
    ValidationError,
)
from .rules import (
    ManualException,
    MetricReading,
    RuleEngine,
    RuleVersion,
    default_rule_version,
)
from .signing import seal, unseal
from .storage import Repository
from .timeutil import Clock, iso, parse_iso

STAGE_FIRST = "first"
STAGE_SECOND = "second"
STAGES = (STAGE_FIRST, STAGE_SECOND)


class AppService:
    def __init__(
        self,
        repo: Repository,
        clock: Clock | None = None,
        signing_secret: str = "",
    ) -> None:
        self.repo = repo
        self.clock = clock or Clock()
        self.signing_secret = signing_secret

    # -- 规则版本 ---------------------------------------------------------

    def ensure_default_rule(self) -> None:
        if self.repo.active_rule_version() is None:
            rule = default_rule_version()
            now = iso(self.clock.now())
            self.repo.insert_rule_version(rule.version, rule.to_dict(), "system", now)
            self.repo.activate_rule(rule.version, now)

    def create_rule_version(self, spec: dict, actor: str) -> str:
        rule = RuleVersion.from_dict(spec)  # 内含校验
        if self.repo.get_rule_version(rule.version) is not None:
            raise ConflictError(f"规则版本 {rule.version} 已存在")
        self.repo.insert_rule_version(
            rule.version, rule.to_dict(), actor, iso(self.clock.now())
        )
        return rule.version

    def activate_rule(self, version: str, actor: str) -> None:
        """切换生效规则版本：跨进程加锁，保证同一时刻只有一个 active。"""
        if self.repo.get_rule_version(version) is None:
            raise NotFoundError(f"规则版本 {version} 不存在")
        with self.repo.admin_lock():
            self.repo.activate_rule(version, iso(self.clock.now()))

    def list_rule_versions(self) -> list[dict]:
        out = []
        for r in self.repo.list_rule_versions():
            d = dict(r)
            d["spec"] = json.loads(d.pop("spec_json"))
            out.append(d)
        return out

    def _active_rule(self) -> RuleVersion:
        row = self.repo.active_rule_version()
        if row is None:
            raise ConflictError("尚无生效规则版本")
        return RuleVersion.from_dict(json.loads(row["spec_json"]))

    def _load_rule(self, version: str | None) -> RuleVersion:
        if version is None:
            return self._active_rule()
        row = self.repo.get_rule_version(version)
        if row is None:
            raise NotFoundError(f"规则版本 {version} 不存在")
        return RuleVersion.from_dict(json.loads(row["spec_json"]))

    # -- 基础数据 ---------------------------------------------------------

    def register_area(self, area_id: str, name: str) -> None:
        self.repo.upsert_area(area_id, name, iso(self.clock.now()))

    def add_reading(self, area_id: str, metric_key: str, day: str, value: float) -> None:
        date.fromisoformat(day)  # 校验
        self.repo.upsert_reading(
            area_id, metric_key, day, float(value), iso(self.clock.now())
        )

    def add_exception(self, data: dict, actor: str) -> str:
        kind = data.get("kind")
        if kind not in ("force_up", "hold_down", "exempt"):
            raise ValidationError("kind 必须为 force_up/hold_down/exempt")
        level = data.get("level")
        if kind == "force_up" and level not in LEVELS:
            raise ValidationError("force_up 必须指定合法 level")
        if kind == "hold_down" and level is not None and level not in LEVELS:
            raise ValidationError("hold_down 的 level 必须为空或合法等级")
        valid_from = data.get("valid_from")
        date.fromisoformat(valid_from)
        valid_to = data.get("valid_to")
        if valid_to is not None:
            date.fromisoformat(valid_to)
            if valid_to < valid_from:
                raise ValidationError("valid_to 不能早于 valid_from")
        if not data.get("reason"):
            raise ValidationError("reason 必填")
        exc_id = new_id("exc")
        self.repo.insert_exception(
            {
                "exc_id": exc_id,
                "area_id": data["area_id"],
                "kind": kind,
                "level": level,
                "reason": data["reason"],
                "author": actor,
                "valid_from": valid_from,
                "valid_to": valid_to,
            },
            iso(self.clock.now()),
        )
        return exc_id

    def revoke_exception(self, exc_id: str) -> None:
        if not self.repo.revoke_exception(exc_id):
            raise NotFoundError("例外不存在或已撤销")

    # -- 评估输入装载 -----------------------------------------------------

    def _load_inputs(
        self, rule: RuleVersion, day_str: str
    ) -> tuple[list[str], dict[str, list[MetricReading]], list[ManualException],
               dict[str, str]]:
        on_day = date.fromisoformat(day_str)
        since = (on_day - timedelta(days=rule.max_window_days())).isoformat()
        readings: dict[str, list[MetricReading]] = {}
        for r in self.repo.readings_since(since):
            readings.setdefault(r["area_id"], []).append(
                MetricReading(r["area_id"], r["metric_key"], r["day"], r["value"])
            )
        exceptions = [
            ManualException(
                area_id=r["area_id"], kind=r["kind"], level=r["level"],
                reason=r["reason"], author=r["author"],
                valid_from=r["valid_from"], valid_to=r["valid_to"],
            )
            for r in self.repo.list_active_exceptions()
        ]
        areas = self.repo.list_areas()
        area_ids = [r["area_id"] for r in areas]
        names = {r["area_id"]: r["name"] for r in areas}
        return area_ids, readings, exceptions, names

    def _evaluate(
        self, rule: RuleVersion, day_str: str
    ) -> tuple[list[dict], dict[str, str]]:
        area_ids, readings, exceptions, names = self._load_inputs(rule, day_str)
        engine = RuleEngine(rule)
        results = engine.evaluate_all(
            area_ids, readings, exceptions, date.fromisoformat(day_str)
        )
        items = [e.to_dict() for e in results if not e.excluded and e.level is not None]
        return items, names

    @staticmethod
    def _public_items(items: list[dict], names: dict[str, str]) -> list[dict]:
        """内部条目 -> 公众渠道的简化结论（无触发痕迹、无例外细节）。"""
        return [
            {
                "area_id": it["service_area_id"],
                "name": names.get(it["service_area_id"], it["service_area_id"]),
                "level": it["level"],
            }
            for it in items
        ]

    # -- 候选生成 ---------------------------------------------------------

    def generate_candidates(self, actor: str, day_str: str | None = None) -> dict:
        day_str = day_str or self.clock.today()
        date.fromisoformat(day_str)
        rule = self._active_rule()
        items, names = self._evaluate(rule, day_str)
        batch_id = new_id("bat")
        now = iso(self.clock.now())
        payload = {
            "rule_version": rule.version,
            "eval_day": day_str,
            "items": items,
            "names": names,
        }
        self.repo.insert_batch(
            {
                "batch_id": batch_id,
                "rule_version": rule.version,
                "eval_day": day_str,
                "status": BATCH_CANDIDATE,
                "payload": payload,
                "created_by": actor,
                "created_at": now,
            }
        )
        return self.batch_view(batch_id)

    # -- 紧急升级（先发布后补审） ----------------------------------------

    def emergency_publish(
        self, actor: str, reason: str, day_str: str | None = None
    ) -> dict:
        if not reason:
            raise ValidationError("紧急升级必须填写 reason")
        day_str = day_str or self.clock.today()
        with self.repo.admin_lock():
            rule = self._active_rule()
            items, names = self._evaluate(rule, day_str)
            now_dt = self.clock.now()
            now = iso(now_dt)
            deadline = now_dt + timedelta(hours=EMERGENCY_REVIEW_DEADLINE_HOURS)
            batch_id = new_id("bat")
            public_items = self._public_items(items, names)
            manifest = self._manifest(batch_id, day_str, public_items, now)
            payload = {
                "rule_version": rule.version,
                "eval_day": day_str,
                "items": items,
                "names": names,
                "emergency_reason": reason,
                "manifest": manifest,
            }
            self.repo.insert_batch(
                {
                    "batch_id": batch_id,
                    "rule_version": rule.version,
                    "eval_day": day_str,
                    "status": BATCH_EMERGENCY,
                    "emergency": True,
                    "payload": payload,
                    "created_by": actor,
                    "created_at": now,
                    "published_at": now,
                    "publish_deadline": iso(deadline),
                    "signature": manifest["signature"],
                }
            )
            with self.repo.transaction(exclusive=True) as conn:
                self._merge_intervals(conn, batch_id, public_items, day_str, now)
                self.repo.audit(conn, actor, "emergency_publish", "batch", batch_id,
                                {"reason": reason, "deadline": iso(deadline)})
            return self.batch_view(batch_id)

    # -- 双人复核 ---------------------------------------------------------

    def review(
        self,
        batch_id: str,
        reviewer: str,
        stage: str,
        decision: str,
        comment: str | None = None,
    ) -> dict:
        if stage not in STAGES:
            raise ValidationError("stage 必须为 first/second")
        if decision not in (APPROVE, "reject"):
            raise ValidationError("decision 必须为 approve/reject")
        row = self.repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError("候选批次不存在")
        status = row["status"]
        if status not in (BATCH_CANDIDATE, BATCH_EMERGENCY):
            raise ConflictError(f"批次状态 {status} 不可审批")
        if reviewer == row["created_by"]:
            raise ConflictError("提单人不能担任复核人")

        with self.repo.admin_lock():
            # 锁内重读，避免两个并发请求同时看到"还差一个审批"
            row = self.repo.get_batch(batch_id)
            status = row["status"]
            if status not in (BATCH_CANDIDATE, BATCH_EMERGENCY):
                raise ConflictError(f"批次状态 {status} 不可审批")

            # 紧急件补审：期限一过即不可审批（锁内判定，配合 sweep 追踪）
            if status == BATCH_EMERGENCY and row["publish_deadline"]:
                if self.clock.now() > parse_iso(row["publish_deadline"]):
                    raise DeadlinePassedError("补审期限已过，该紧急件已失效")

            prior = self.repo.decisions_of(batch_id, STAGE_FIRST)
            second_prior = self.repo.decisions_of(batch_id, STAGE_SECOND)
            if stage == STAGE_FIRST:
                if any(d["reviewer"] == reviewer for d in prior):
                    raise DuplicateApprovalError("你已提交过第一阶段审批")
                if prior and prior[0]["decision"] == "reject":
                    raise ConflictError("第一阶段已驳回，流程结束")
            else:
                approves_first = [d for d in prior if d["decision"] == APPROVE]
                if not approves_first:
                    raise ConflictError("须先完成第一阶段复核")
                if approves_first[0]["reviewer"] == reviewer:
                    raise ConflictError("第二阶段复核人必须与第一阶段不同")
                if any(d["reviewer"] == reviewer for d in second_prior):
                    raise DuplicateApprovalError("你已提交过第二阶段审批")

            now = iso(self.clock.now())
            try:
                self.repo.add_approval(
                    batch_id, stage, reviewer, decision, comment, now
                )
            except sqlite3.IntegrityError as exc:  # 并发下的重复审批
                raise DuplicateApprovalError("重复审批") from exc

            if decision == "reject":
                self._handle_rejection(row, reviewer, comment, now)
            else:
                approvals = self.repo.approvals_of(batch_id, STAGE_FIRST) + \
                    self.repo.approvals_of(batch_id, STAGE_SECOND)
                if len(approvals) == 2:
                    self._finalize_approval(row, now)
            return self.batch_view(batch_id)

    def _handle_rejection(
        self, row: sqlite3.Row, reviewer: str, comment: str | None, now: str
    ) -> None:
        batch_id = row["batch_id"]
        if row["status"] == BATCH_EMERGENCY:
            # 紧急件补审被驳回：立即撤回其尚在生效的区间（当日起失效）
            close_day = self.clock.today()
            with self.repo.transaction(exclusive=True) as conn:
                self._close_batch_intervals(conn, batch_id, close_day)
                conn.execute(
                    "UPDATE batches SET status=?, revoked_at=?, revoke_reason=? "
                    "WHERE batch_id=?",
                    (BATCH_REVOKED, now, f"补审驳回: {comment or ''}", batch_id),
                )
                self.repo.audit(conn, reviewer, "reject_emergency", "batch",
                                batch_id, {"comment": comment})
        else:
            self.repo.update_batch_status(batch_id, BATCH_REJECTED)

    def _finalize_approval(self, row: sqlite3.Row, now: str) -> None:
        batch_id = row["batch_id"]
        with self.repo.transaction(exclusive=True) as conn:
            if row["status"] == BATCH_CANDIDATE:
                payload = json.loads(row["payload_json"])
                public_items = self._public_items(
                    payload["items"], payload.get("names", {})
                )
                manifest = self._manifest(
                    batch_id, row["eval_day"], public_items, now
                )
                payload["manifest"] = manifest
                self._merge_intervals(
                    conn, batch_id, public_items, row["eval_day"], now
                )
                conn.execute(
                    "UPDATE batches SET status=?, published_at=?, reviewed_at=?, "
                    "payload_json=?, signature=? WHERE batch_id=?",
                    (BATCH_PUBLISHED, now, now,
                     json.dumps(payload, ensure_ascii=False),
                     manifest["signature"], batch_id),
                )
            else:  # 紧急件补齐双人复核 -> 转为正式发布
                conn.execute(
                    "UPDATE batches SET status=?, reviewed_at=? WHERE batch_id=?",
                    (BATCH_PUBLISHED, now, batch_id),
                )
            self.repo.audit(conn, "system", "review_complete", "batch", batch_id)

    # -- 区间合并 / 撤销 --------------------------------------------------

    def _merge_intervals(
        self,
        conn: sqlite3.Connection,
        batch_id: str,
        public_items: list[dict],
        day_str: str,
        now: str,
    ) -> None:
        """按本次发布重算公开区间。

        每次发布都为入选服务区建立 *归属本批次* 的新区间，并在评估日关闭
        其此前开放的区间（半开 [from,to)，相邻区间无缝连续）。这样：

        * 等级未变：公开有效期仍然连续，只是按批次分段，溯源清晰；
        * 撤销/紧急失效只关闭本批次名下尚未结束的区间，
          不会误伤其它已发布批次覆盖的时段；
        * 掉出清单：旧区间在评估日关闭，不产生新区间。
        """
        wanted = {it["area_id"]: it["level"] for it in public_items}
        existing = {
            r["area_id"]: r
            for r in conn.execute(
                "SELECT * FROM public_intervals WHERE valid_to IS NULL"
            ).fetchall()
        }
        for area_id, level in wanted.items():
            old = existing.pop(area_id, None)
            if old is not None:
                conn.execute(
                    "UPDATE public_intervals SET valid_to=? WHERE interval_id=? "
                    "AND valid_to IS NULL",
                    (day_str, old["interval_id"]),
                )
            conn.execute(
                "INSERT INTO public_intervals(interval_id, area_id, level, batch_id, "
                "valid_from, valid_to, created_at) VALUES(?,?,?,?,?,NULL,?)",
                (new_id("ivl"), area_id, level, batch_id, day_str, now),
            )
        for old in existing.values():
            conn.execute(
                "UPDATE public_intervals SET valid_to=? WHERE interval_id=? "
                "AND valid_to IS NULL",
                (day_str, old["interval_id"]),
            )

    def _close_batch_intervals(
        self, conn: sqlite3.Connection, batch_id: str, valid_to: str
    ) -> int:
        cur = conn.execute(
            "UPDATE public_intervals SET valid_to=? WHERE batch_id=? AND "
            "valid_to IS NULL",
            (valid_to, batch_id),
        )
        return cur.rowcount

    def revoke(self, batch_id: str, actor: str, reason: str) -> dict:
        """撤销发布：只影响尚未结束（valid_to IS NULL）的有效区间。"""
        if not reason:
            raise ValidationError("撤销必须填写 reason")
        row = self.repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError("批次不存在")
        if row["status"] in (BATCH_REVOKED, BATCH_EXPIRED, BATCH_REJECTED):
            raise ConflictError(f"批次状态 {row['status']} 不可撤销")
        with self.repo.admin_lock():
            now = iso(self.clock.now())
            close_day = self.clock.today()
            with self.repo.transaction(exclusive=True) as conn:
                closed = self._close_batch_intervals(conn, batch_id, close_day)
                conn.execute(
                    "UPDATE batches SET status=?, revoked_at=?, revoke_reason=? "
                    "WHERE batch_id=?",
                    (BATCH_REVOKED, now, reason, batch_id),
                )
                self.repo.audit(conn, actor, "revoke", "batch", batch_id,
                                {"reason": reason, "closed_intervals": closed})
            return self.batch_view(batch_id)

    # -- 紧急补审期限追踪（进程恢复 + 定时） ------------------------------

    def run_deadline_sweep(self) -> list[str]:
        """检查所有待补审紧急件；超期未补齐双人复核则失效并撤回其有效区间。

        服务启动时调用一次（进程恢复），之后由后台线程周期调用。
        """
        expired: list[str] = []
        now_dt = self.clock.now()
        for row in self.repo.open_emergency_batches():
            deadline = parse_iso(row["publish_deadline"])
            approvals = self.repo.approvals_of(row["batch_id"], STAGE_FIRST) + \
                self.repo.approvals_of(row["batch_id"], STAGE_SECOND)
            if len(approvals) >= 2:
                # 审批已齐但状态未推进（不应发生，防御性收尾）
                self.repo.update_batch_status(
                    row["batch_id"], BATCH_PUBLISHED, reviewed_at=iso(now_dt)
                )
                continue
            if now_dt > deadline:
                with self.repo.admin_lock():
                    now = iso(now_dt)
                    close_day = now_dt.date().isoformat()
                    with self.repo.transaction(exclusive=True) as conn:
                        closed = self._close_batch_intervals(
                            conn, row["batch_id"], close_day
                        )
                        conn.execute(
                            "UPDATE batches SET status=? WHERE batch_id=? AND "
                            "status='emergency'",
                            (BATCH_EXPIRED, row["batch_id"]),
                        )
                        self.repo.audit(conn, "system", "deadline_expired",
                                        "batch", row["batch_id"],
                                        {"closed_intervals": closed})
                    expired.append(row["batch_id"])
        return expired

    # -- 查询 / 公开视图 --------------------------------------------------

    def batch_view(self, batch_id: str) -> dict:
        row = self.repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError("批次不存在")
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        d["emergency"] = bool(d["emergency"])
        d["approvals"] = [dict(a) for a in
                          self.repo.decisions_of(batch_id, STAGE_FIRST) +
                          self.repo.decisions_of(batch_id, STAGE_SECOND)]
        d["manifest"] = d["payload"].get("manifest")
        return d

    def list_batches(self) -> list[dict]:
        return [
            {
                "batch_id": r["batch_id"],
                "rule_version": r["rule_version"],
                "eval_day": r["eval_day"],
                "status": r["status"],
                "emergency": bool(r["emergency"]),
                "created_by": r["created_by"],
                "created_at": r["created_at"],
                "published_at": r["published_at"],
                "publish_deadline": r["publish_deadline"],
            }
            for r in self.repo.list_batches()
        ]

    def public_manifest(self, batch_id: str) -> dict:
        """返回带签名、可供公众渠道核验的发布清单信封。"""
        view = self.batch_view(batch_id)
        manifest = view["payload"].get("manifest")
        if manifest is None:
            raise NotFoundError("该批次没有公开清单（未发布）")
        if not unseal(manifest, self.signing_secret):
            raise ConflictError("清单签名校验失败")
        return manifest

    def public_current(self, day_str: str | None = None) -> dict:
        """公众渠道：当日有效区间的简化结论。"""
        day_str = day_str or self.clock.today()
        date.fromisoformat(day_str)
        rows = self.repo.intervals_on(day_str)
        names = {r["area_id"]: r["name"] for r in self.repo.list_areas()}
        items = [
            {
                "area_id": r["area_id"],
                "name": names.get(r["area_id"], r["area_id"]),
                "level": r["level"],
                "valid_from": r["valid_from"],
            }
            for r in rows
        ]
        return {"effective_day": day_str, "items": items}

    # -- 历史重建 ---------------------------------------------------------

    def rebuild_candidate(
        self, day_str: str, rule_version: str | None = None
    ) -> dict:
        """按指定（通常是历史）规则版本与当日证据重新计算候选清单。

        用于回答"如果当时用 v2 规则，清单会是什么"以及规则切换影响分析；
        纯计算，不改变任何已发布状态。
        """
        date.fromisoformat(day_str)
        rule = self._load_rule(rule_version)
        items, names = self._evaluate(rule, day_str)
        return {
            "eval_day": day_str,
            "rule_version": rule.version,
            "rebuilt_at": iso(self.clock.now()),
            "items": items,
        }

    def rebuild_snapshot(self, day_str: str) -> dict:
        """从已发布区间重建某一天的公开版本（历史原貌）。"""
        date.fromisoformat(day_str)
        rows = self.repo.intervals_on(day_str)
        names = {r["area_id"]: r["name"] for r in self.repo.list_areas()}
        return {
            "effective_day": day_str,
            "items": [
                {
                    "area_id": r["area_id"],
                    "name": names.get(r["area_id"], r["area_id"]),
                    "level": r["level"],
                    "batch_id": r["batch_id"],
                    "valid_from": r["valid_from"],
                    "valid_to": r["valid_to"],
                }
                for r in rows
            ],
        }

    # -- 签名信封 ---------------------------------------------------------

    def _manifest(
        self, batch_id: str, day_str: str, public_items: list[dict], signed_at: str
    ) -> dict:
        body = {
            "batch_id": batch_id,
            "effective_day": day_str,
            "items": public_items,
        }
        return seal(body, self.signing_secret, signed_at)

    def verify_manifest(self, envelope: dict) -> bool:
        return unseal(envelope, self.signing_secret)

    # -- 审计 -------------------------------------------------------------

    def audit_tail(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self.repo.audit_tail(limit)]
