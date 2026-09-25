"""应用服务：繁忙服务区分级发布的全部用例。

覆盖：指标版本、分级规则、观察期与观测、人工例外、内部候选清单、
双人复核、公开发布、紧急升级（先发布后补审 + 期限自动追踪）、
撤销（只影响尚未结束的有效区间）、历史重建、签名校验与进程恢复。

所有方法的第一参数 ``actor`` 是接口边界解析出的操作者身份；
时间、标识与签名密钥均通过构造注入的可替换端口获取。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from . import domain
from .domain import (
    GRADE_LABEL,
    GRADE_MESSAGE,
    GRADE_ORDER,
    REQUIRED_APPROVALS,
    SUBJECT_CANDIDATE_LIST,
    SUBJECT_EMERGENCY,
    fmt_ts,
    parse_ts,
)
from .errors import conflict, forbidden, not_found, validation
from .ports import Clock, IdGenerator
from .signatures import GENESIS_HASH, audit_hash, sign_payload, verify_payload
from .storage import Repository


class AppService:
    """应用服务门面：每个公开方法对应一个用例。"""

    def __init__(
        self,
        repo: Repository,
        clock: Clock,
        ids: IdGenerator,
        signing_key: bytes,
        emergency_review_hours: int = 24,
    ) -> None:
        self.repo = repo
        self.clock = clock
        self.ids = ids
        self.signing_key = signing_key
        self.emergency_review_hours = emergency_review_hours

    # ------------------------------------------------------------------
    # 通用辅助
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return fmt_ts(self.clock.now())

    @staticmethod
    def _require(value: Any, message: str) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            raise validation(message)
        return value

    def _reviews_for(self, subject_type: str, subject_id: str) -> list[dict]:
        return self.repo.query(
            "SELECT reviewer, decision, decided_at FROM reviews"
            " WHERE subject_type=? AND subject_id=? ORDER BY decided_at, reviewer",
            (subject_type, subject_id),
        )

    # ------------------------------------------------------------------
    # 指标版本
    # ------------------------------------------------------------------

    def create_metric_version(self, actor: str, metrics: Any) -> dict:
        normalized = domain.validate_metrics_payload(metrics)
        with self.repo.tx():
            version_no = self.repo.next_value("metric_versions")
            version_id = self.ids.new_id("mv")
            self.repo.execute(
                "INSERT INTO metric_versions(id, version_no, metrics_json, status, created_by, created_at)"
                " VALUES (?, ?, ?, 'DRAFT', ?, ?)",
                (version_id, version_no, json.dumps(normalized, ensure_ascii=False), actor, self._now()),
            )
            self.repo.audit(
                actor=actor,
                action="metric_version.created",
                subject=f"metric-version:{version_id}",
                payload={"version_no": version_no, "metrics": normalized},
                at=self._now(),
            )
        return self.get_metric_version(version_id)

    def activate_metric_version(self, actor: str, version_id: str) -> dict:
        with self.repo.tx():
            target = self.repo.one("SELECT * FROM metric_versions WHERE id=?", (version_id,))
            if target is None:
                raise not_found(f"指标版本不存在: {version_id}")
            if target["status"] == "ACTIVE":
                raise conflict("该指标版本已处于启用状态", "ALREADY_ACTIVE")
            current = self.repo.one("SELECT * FROM metric_versions WHERE status='ACTIVE'")
            if current is not None:
                self.repo.execute("UPDATE metric_versions SET status='RETIRED' WHERE id=?", (current["id"],))
            self.repo.execute("UPDATE metric_versions SET status='ACTIVE' WHERE id=?", (version_id,))
            self.repo.audit(
                actor=actor,
                action="metric_version.activated",
                subject=f"metric-version:{version_id}",
                payload={"version_no": target["version_no"], "retired": current["id"] if current else None},
                at=self._now(),
            )
        return self.get_metric_version(version_id)

    def get_metric_version(self, version_id: str) -> dict:
        row = self.repo.one("SELECT * FROM metric_versions WHERE id=?", (version_id,))
        if row is None:
            raise not_found(f"指标版本不存在: {version_id}")
        return self._metric_version_dict(row)

    def list_metric_versions(self, actor: str) -> list[dict]:
        rows = self.repo.query("SELECT * FROM metric_versions ORDER BY version_no")
        return [self._metric_version_dict(row) for row in rows]

    @staticmethod
    def _metric_version_dict(row: dict) -> dict:
        return {
            "id": row["id"],
            "version_no": row["version_no"],
            "status": row["status"],
            "metrics": json.loads(row["metrics_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # 分级规则版本（规则切换 = 启用另一版本，旧版本退役，历史版本仍可追溯）
    # ------------------------------------------------------------------

    def create_rule_version(self, actor: str, metric_version_id: str, rules: Any) -> dict:
        metric_version = self.repo.one("SELECT * FROM metric_versions WHERE id=?", (metric_version_id,))
        if metric_version is None:
            raise not_found(f"指标版本不存在: {metric_version_id}")
        metric_codes = {item["code"] for item in json.loads(metric_version["metrics_json"])}
        normalized = domain.validate_rules_payload(rules, metric_codes)
        with self.repo.tx():
            version_no = self.repo.next_value("rule_versions")
            version_id = self.ids.new_id("rv")
            self.repo.execute(
                "INSERT INTO rule_versions(id, version_no, metric_version_id, rules_json, status,"
                " created_by, created_at) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?)",
                (
                    version_id,
                    version_no,
                    metric_version_id,
                    json.dumps(normalized, ensure_ascii=False),
                    actor,
                    self._now(),
                ),
            )
            self.repo.audit(
                actor=actor,
                action="rule_version.created",
                subject=f"rule-version:{version_id}",
                payload={"version_no": version_no, "metric_version_id": metric_version_id, "rules": normalized},
                at=self._now(),
            )
        return self.get_rule_version(version_id)

    def activate_rule_version(self, actor: str, version_id: str) -> dict:
        with self.repo.tx():
            target = self.repo.one("SELECT * FROM rule_versions WHERE id=?", (version_id,))
            if target is None:
                raise not_found(f"规则版本不存在: {version_id}")
            if target["status"] == "ACTIVE":
                raise conflict("该规则版本已处于启用状态", "ALREADY_ACTIVE")
            current = self.repo.one("SELECT * FROM rule_versions WHERE status='ACTIVE'")
            if current is not None:
                self.repo.execute("UPDATE rule_versions SET status='RETIRED' WHERE id=?", (current["id"],))
            self.repo.execute("UPDATE rule_versions SET status='ACTIVE' WHERE id=?", (version_id,))
            self.repo.audit(
                actor=actor,
                action="rule_version.activated",
                subject=f"rule-version:{version_id}",
                payload={"version_no": target["version_no"], "retired": current["id"] if current else None},
                at=self._now(),
            )
        return self.get_rule_version(version_id)

    def get_rule_version(self, version_id: str) -> dict:
        row = self.repo.one("SELECT * FROM rule_versions WHERE id=?", (version_id,))
        if row is None:
            raise not_found(f"规则版本不存在: {version_id}")
        return self._rule_version_dict(row)

    def list_rule_versions(self, actor: str) -> list[dict]:
        rows = self.repo.query("SELECT * FROM rule_versions ORDER BY version_no")
        return [self._rule_version_dict(row) for row in rows]

    @staticmethod
    def _rule_version_dict(row: dict) -> dict:
        return {
            "id": row["id"],
            "version_no": row["version_no"],
            "metric_version_id": row["metric_version_id"],
            "status": row["status"],
            "rules": json.loads(row["rules_json"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    # ------------------------------------------------------------------
    # 观察期与指标观测
    # ------------------------------------------------------------------

    def create_period(self, actor: str, name: str, start: Any, end: Any) -> dict:
        self._require(name, "观察期名称不能为空")
        start_at = fmt_ts(parse_ts(start))
        end_at = fmt_ts(parse_ts(end))
        if not start_at < end_at:
            raise validation("观察期结束时间必须晚于开始时间")
        with self.repo.tx():
            period_id = self.ids.new_id("op")
            self.repo.execute(
                "INSERT INTO observation_periods(id, name, start_at, end_at, status, created_at)"
                " VALUES (?, ?, ?, ?, 'OPEN', ?)",
                (period_id, name.strip(), start_at, end_at, self._now()),
            )
            self.repo.audit(
                actor=actor,
                action="period.created",
                subject=f"period:{period_id}",
                payload={"name": name.strip(), "start_at": start_at, "end_at": end_at},
                at=self._now(),
            )
        return self.get_period(period_id)

    def get_period(self, period_id: str) -> dict:
        row = self.repo.one("SELECT * FROM observation_periods WHERE id=?", (period_id,))
        if row is None:
            raise not_found(f"观察期不存在: {period_id}")
        return row

    def list_periods(self, actor: str) -> list[dict]:
        return self.repo.query("SELECT * FROM observation_periods ORDER BY start_at")

    def record_observations(self, actor: str, period_id: str, items: Any) -> dict:
        """批量录入观测值；同一 (观察期, 服务区, 指标) 重复录入时后者覆盖前者。"""
        period = self.get_period(period_id)
        if not isinstance(items, list) or not items:
            raise validation("观测数据必须是非空列表")
        active_metric = self.repo.one("SELECT * FROM metric_versions WHERE status='ACTIVE'")
        if active_metric is None:
            raise conflict("没有已启用的指标版本，无法录入观测", "NO_ACTIVE_METRIC_VERSION")
        metric_codes = {item["code"] for item in json.loads(active_metric["metrics_json"])}
        normalized: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                raise validation("观测条目必须是对象")
            area_code = str(item.get("area_code") or "").strip()
            metric = str(item.get("metric") or "").strip()
            if not area_code:
                raise validation("观测条目缺少 area_code")
            if metric not in metric_codes:
                raise validation(f"观测指标未在启用的指标版本中定义: {metric!r}")
            try:
                value = float(item.get("value"))
            except (TypeError, ValueError):
                raise validation(f"观测值必须是数值: {item.get('value')!r}")
            observed_raw = item.get("observed_at")
            observed_at = fmt_ts(parse_ts(observed_raw)) if observed_raw else self._now()
            if not (period["start_at"] <= observed_at < period["end_at"]):
                raise validation(f"观测时间 {observed_at} 不在观察期 [{period['start_at']}, {period['end_at']}) 内")
            normalized.append(
                {"area_code": area_code, "metric": metric, "value": value, "observed_at": observed_at}
            )
        with self.repo.tx():
            for entry in normalized:
                self.repo.execute(
                    "INSERT INTO observations(period_id, area_code, metric_code, value, observed_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(period_id, area_code, metric_code)"
                    " DO UPDATE SET value=excluded.value, observed_at=excluded.observed_at",
                    (period_id, entry["area_code"], entry["metric"], entry["value"], entry["observed_at"]),
                )
            self.repo.audit(
                actor=actor,
                action="observations.recorded",
                subject=f"period:{period_id}",
                payload={"count": len(normalized)},
                at=self._now(),
            )
        return {"period_id": period_id, "recorded": len(normalized)}

    # ------------------------------------------------------------------
    # 人工例外
    # ------------------------------------------------------------------

    def create_exception(
        self,
        actor: str,
        area_code: str,
        action: str,
        grade: str | None,
        valid_from: Any,
        valid_to: Any,
        reason: str,
    ) -> dict:
        area_code = str(area_code or "").strip()
        if not area_code:
            raise validation("人工例外缺少 area_code")
        if action not in domain.EXCEPTION_ACTIONS:
            raise validation(f"未知的例外动作: {action!r}")
        if action == "FORCE_INCLUDE":
            if grade not in GRADE_ORDER:
                raise validation("强制纳入必须指定有效的繁忙等级")
        elif grade is not None:
            raise validation("强制排除不应携带等级")
        self._require(reason, "人工例外必须填写原因")
        valid_from_s = fmt_ts(parse_ts(valid_from))
        valid_to_s = fmt_ts(parse_ts(valid_to))
        if not valid_from_s < valid_to_s:
            raise validation("例外有效期结束时间必须晚于开始时间")
        with self.repo.tx():
            exception_id = self.ids.new_id("ex")
            self.repo.execute(
                "INSERT INTO manual_exceptions(id, area_code, action, grade, valid_from, valid_to,"
                " reason, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)",
                (exception_id, area_code, action, grade, valid_from_s, valid_to_s, reason.strip(), actor, self._now()),
            )
            self.repo.audit(
                actor=actor,
                action="exception.created",
                subject=f"exception:{exception_id}",
                payload={
                    "area_code": area_code,
                    "action": action,
                    "grade": grade,
                    "valid_from": valid_from_s,
                    "valid_to": valid_to_s,
                    "reason": reason.strip(),
                },
                at=self._now(),
            )
        return self.get_exception(exception_id)

    def revoke_exception(self, actor: str, exception_id: str) -> dict:
        with self.repo.tx():
            row = self.repo.one("SELECT * FROM manual_exceptions WHERE id=?", (exception_id,))
            if row is None:
                raise not_found(f"人工例外不存在: {exception_id}")
            if row["status"] != "ACTIVE":
                raise conflict("该人工例外已被撤销", "ALREADY_REVOKED")
            now = self._now()
            self.repo.execute(
                "UPDATE manual_exceptions SET status='REVOKED', revoked_at=?, revoked_by=? WHERE id=?",
                (now, actor, exception_id),
            )
            self.repo.audit(
                actor=actor,
                action="exception.revoked",
                subject=f"exception:{exception_id}",
                payload={"area_code": row["area_code"]},
                at=now,
            )
        return self.get_exception(exception_id)

    def get_exception(self, exception_id: str) -> dict:
        row = self.repo.one("SELECT * FROM manual_exceptions WHERE id=?", (exception_id,))
        if row is None:
            raise not_found(f"人工例外不存在: {exception_id}")
        return row

    def list_exceptions(self, actor: str) -> list[dict]:
        return self.repo.query("SELECT * FROM manual_exceptions ORDER BY created_at, id")

    # ------------------------------------------------------------------
    # 内部候选清单
    # ------------------------------------------------------------------

    def generate_candidate_list(self, actor: str, period_id: str) -> dict:
        """按当前启用的规则版本对观察期求值，生成内部候选清单。

        求值顺序：自动规则 → 人工例外（排除优先于纳入）。
        同一观察期重复生成时，旧的未发布清单被标记为 SUPERSEDED。
        """
        period = self.get_period(period_id)
        rule = self.repo.one("SELECT * FROM rule_versions WHERE status='ACTIVE' ORDER BY version_no DESC LIMIT 1")
        if rule is None:
            raise conflict("没有已启用的分级规则版本，无法生成候选清单", "NO_ACTIVE_RULE_VERSION")
        rules = json.loads(rule["rules_json"])
        observations = self.repo.query(
            "SELECT area_code, metric_code, value FROM observations WHERE period_id=?"
            " ORDER BY area_code, metric_code",
            (period_id,),
        )
        values: dict[str, dict[str, float]] = {}
        for row in observations:
            values.setdefault(row["area_code"], {})[row["metric_code"]] = row["value"]

        evaluated: dict[str, tuple[str, list[dict]]] = {}
        for area_code, area_values in values.items():
            result = domain.evaluate_rules(rules, area_values)
            if result is not None:
                evaluated[area_code] = result

        exceptions = self.repo.query(
            "SELECT * FROM manual_exceptions WHERE status='ACTIVE' AND valid_from < ? AND valid_to > ?",
            (period["end_at"], period["start_at"]),
        )
        excluded = {ex["area_code"] for ex in exceptions if ex["action"] == "FORCE_EXCLUDE"}
        included = {ex["area_code"]: ex for ex in exceptions if ex["action"] == "FORCE_INCLUDE"}

        entries: list[dict] = []
        for area_code, (grade, triggered) in evaluated.items():
            if area_code in excluded:
                continue
            entries.append(
                {"area_code": area_code, "grade": grade, "source": "AUTO", "triggered": triggered, "note": None}
            )
        for area_code, ex in included.items():
            if area_code in excluded:
                continue  # 排除优先于纳入
            entries = [entry for entry in entries if entry["area_code"] != area_code]
            entries.append(
                {
                    "area_code": area_code,
                    "grade": ex["grade"],
                    "source": "MANUAL_EXCEPTION",
                    "triggered": [],
                    "note": ex["reason"],
                }
            )
        entries.sort(key=lambda entry: entry["area_code"])

        with self.repo.tx():
            self.repo.execute(
                "UPDATE candidate_lists SET status='SUPERSEDED'"
                " WHERE period_id=? AND status IN ('DRAFT', 'IN_REVIEW', 'APPROVED')",
                (period_id,),
            )
            list_id = self.ids.new_id("cl")
            self.repo.execute(
                "INSERT INTO candidate_lists(id, period_id, rule_version_id, metric_version_id, status,"
                " created_by, created_at) VALUES (?, ?, ?, ?, 'DRAFT', ?, ?)",
                (list_id, period_id, rule["id"], rule["metric_version_id"], actor, self._now()),
            )
            for entry in entries:
                self.repo.execute(
                    "INSERT INTO candidate_entries(list_id, area_code, grade, source, triggered_json, note)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        list_id,
                        entry["area_code"],
                        entry["grade"],
                        entry["source"],
                        json.dumps(entry["triggered"], ensure_ascii=False),
                        entry["note"],
                    ),
                )
            self.repo.audit(
                actor=actor,
                action="candidate.generated",
                subject=f"candidate-list:{list_id}",
                payload={
                    "period_id": period_id,
                    "rule_version_id": rule["id"],
                    "rule_version_no": rule["version_no"],
                    "entries": len(entries),
                },
                at=self._now(),
            )
        return self.get_candidate_list(actor, list_id)

    def get_candidate_list(self, actor: str, list_id: str) -> dict:
        row = self.repo.one("SELECT * FROM candidate_lists WHERE id=?", (list_id,))
        if row is None:
            raise not_found(f"候选清单不存在: {list_id}")
        entries = self.repo.query(
            "SELECT * FROM candidate_entries WHERE list_id=? ORDER BY area_code", (list_id,)
        )
        return {
            "id": row["id"],
            "period_id": row["period_id"],
            "rule_version_id": row["rule_version_id"],
            "metric_version_id": row["metric_version_id"],
            "status": row["status"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "entries": [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": GRADE_LABEL[entry["grade"]],
                    "source": entry["source"],
                    "triggered": json.loads(entry["triggered_json"]),
                    "note": entry["note"],
                }
                for entry in entries
            ],
            "reviews": self._reviews_for(SUBJECT_CANDIDATE_LIST, list_id),
        }

    def list_candidate_lists(self, actor: str) -> list[dict]:
        rows = self.repo.query("SELECT id FROM candidate_lists ORDER BY created_at, id")
        return [self.get_candidate_list(actor, row["id"]) for row in rows]

    def submit_candidate_list(self, actor: str, list_id: str) -> dict:
        with self.repo.tx():
            row = self.repo.one("SELECT * FROM candidate_lists WHERE id=?", (list_id,))
            if row is None:
                raise not_found(f"候选清单不存在: {list_id}")
            if row["status"] != "DRAFT":
                raise conflict(f"清单当前状态为 {row['status']}，不能提交复核")
            self.repo.execute("UPDATE candidate_lists SET status='IN_REVIEW' WHERE id=?", (list_id,))
            self.repo.audit(
                actor=actor,
                action="candidate.submitted",
                subject=f"candidate-list:{list_id}",
                payload={},
                at=self._now(),
            )
        return self.get_candidate_list(actor, list_id)

    def review_candidate_list(self, reviewer: str, list_id: str, decision: str) -> dict:
        """双人复核：两名不同复核人赞成后清单进入 APPROVED。

        重复审批由 (主体, 复核人) 唯一约束兜底，并映射为 DUPLICATE_REVIEW；
        清单创建人不能复核本人清单。
        """
        if decision not in domain.REVIEW_DECISIONS:
            raise validation(f"未知的复核结论: {decision!r}")
        with self.repo.tx():
            row = self.repo.one("SELECT * FROM candidate_lists WHERE id=?", (list_id,))
            if row is None:
                raise not_found(f"候选清单不存在: {list_id}")
            if row["status"] != "IN_REVIEW":
                raise conflict(f"清单当前状态为 {row['status']}，不能复核")
            if reviewer == row["created_by"]:
                raise forbidden("不能复核本人创建的清单")
            existing = self.repo.one(
                "SELECT 1 AS x FROM reviews WHERE subject_type=? AND subject_id=? AND reviewer=?",
                (SUBJECT_CANDIDATE_LIST, list_id, reviewer),
            )
            if existing is not None:
                raise conflict("该复核人已提交过复核结论", "DUPLICATE_REVIEW")
            now = self._now()
            try:
                self.repo.execute(
                    "INSERT INTO reviews(subject_type, subject_id, reviewer, decision, decided_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (SUBJECT_CANDIDATE_LIST, list_id, reviewer, decision, now),
                )
            except sqlite3.IntegrityError:
                raise conflict("该复核人已提交过复核结论", "DUPLICATE_REVIEW")
            if decision == "REJECT":
                self.repo.execute("UPDATE candidate_lists SET status='REJECTED' WHERE id=?", (list_id,))
                action = "candidate.rejected"
            else:
                approvals = self.repo.one(
                    "SELECT COUNT(*) AS n FROM reviews WHERE subject_type=? AND subject_id=? AND decision='APPROVE'",
                    (SUBJECT_CANDIDATE_LIST, list_id),
                )["n"]
                if approvals >= REQUIRED_APPROVALS:
                    self.repo.execute("UPDATE candidate_lists SET status='APPROVED' WHERE id=?", (list_id,))
                    action = "candidate.approved"
                else:
                    action = "candidate.reviewed"
            self.repo.audit(
                actor=reviewer,
                action=action,
                subject=f"candidate-list:{list_id}",
                payload={"decision": decision},
                at=now,
            )
        return self.get_candidate_list(reviewer, list_id)

    # ------------------------------------------------------------------
    # 公开发布
    # ------------------------------------------------------------------

    def publish_candidate_list(
        self, actor: str, list_id: str, valid_from: Any = None, valid_to: Any = None
    ) -> dict:
        """把已通过双人复核的候选清单发布为公开版本。

        并发安全：整个发布在立即事务内完成，版本号在事务内分配；
        同一清单重复发布由状态检查与 source_list_id 唯一约束双重兜底。
        """
        with self.repo.tx():
            row = self.repo.one("SELECT * FROM candidate_lists WHERE id=?", (list_id,))
            if row is None:
                raise not_found(f"候选清单不存在: {list_id}")
            if row["status"] != "APPROVED":
                raise conflict(f"清单当前状态为 {row['status']}，未通过双人复核，不能发布")
            period = self.repo.one("SELECT * FROM observation_periods WHERE id=?", (row["period_id"],))
            valid_from_s = fmt_ts(parse_ts(valid_from)) if valid_from else period["start_at"]
            valid_to_s = fmt_ts(parse_ts(valid_to)) if valid_to else period["end_at"]
            if not valid_from_s < valid_to_s:
                raise validation("公开有效期结束时间必须晚于开始时间")
            already = self.repo.one(
                "SELECT id FROM candidate_lists WHERE period_id=? AND status='PUBLISHED'", (row["period_id"],)
            )
            if already is not None:
                raise conflict("该观察期已发布过公开版本", "ALREADY_PUBLISHED")
            entries = self.repo.query(
                "SELECT * FROM candidate_entries WHERE list_id=? ORDER BY area_code", (list_id,)
            )
            version_no = self.repo.next_value("public_versions")
            published_at = self._now()
            payload_entries = [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": GRADE_LABEL[entry["grade"]],
                    "message": GRADE_MESSAGE[entry["grade"]],
                    "valid_from": valid_from_s,
                    "valid_to": valid_to_s,
                }
                for entry in entries
            ]
            signature = sign_payload(
                self.signing_key,
                self._signature_payload(version_no, "NORMAL", list_id, None, published_at, payload_entries),
            )
            try:
                self.repo.execute(
                    "INSERT INTO public_versions(version_no, kind, source_list_id, emergency_id,"
                    " published_by, published_at, signature) VALUES (?, 'NORMAL', ?, NULL, ?, ?, ?)",
                    (version_no, list_id, actor, published_at, signature),
                )
            except sqlite3.IntegrityError:
                raise conflict("该清单已被发布", "ALREADY_PUBLISHED")
            for entry in payload_entries:
                self.repo.execute(
                    "INSERT INTO public_entries(version_no, area_code, grade, label, message,"
                    " valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_no,
                        entry["area_code"],
                        entry["grade"],
                        entry["label"],
                        entry["message"],
                        entry["valid_from"],
                        entry["valid_to"],
                    ),
                )
            self.repo.execute("UPDATE candidate_lists SET status='PUBLISHED' WHERE id=?", (list_id,))
            self.repo.audit(
                actor=actor,
                action="public.published",
                subject=f"public-version:{version_no}",
                payload={"version_no": version_no, "source_list_id": list_id, "entries": len(payload_entries)},
                at=published_at,
            )
        return self.get_public_version(actor, version_no)

    @staticmethod
    def _signature_payload(
        version_no: int,
        kind: str,
        source_list_id: str | None,
        emergency_id: str | None,
        published_at: str,
        entries: list[dict],
    ) -> dict:
        return {
            "version_no": version_no,
            "kind": kind,
            "source_list_id": source_list_id,
            "emergency_id": emergency_id,
            "published_at": published_at,
            "entries": sorted(entries, key=lambda entry: entry["area_code"]),
        }

    def get_public_version(self, actor: str, version_no: int) -> dict:
        """内部视角：包含触发指标等完整细节。"""
        row = self.repo.one("SELECT * FROM public_versions WHERE version_no=?", (version_no,))
        if row is None:
            raise not_found(f"公开版本不存在: {version_no}")
        entries = self.repo.query(
            "SELECT * FROM public_entries WHERE version_no=? ORDER BY area_code", (version_no,)
        )
        triggered_by_area: dict[str, list] = {}
        if row["source_list_id"]:
            for entry in self.repo.query(
                "SELECT area_code, triggered_json FROM candidate_entries WHERE list_id=?",
                (row["source_list_id"],),
            ):
                triggered_by_area[entry["area_code"]] = json.loads(entry["triggered_json"])
        return {
            "version_no": row["version_no"],
            "kind": row["kind"],
            "source_list_id": row["source_list_id"],
            "emergency_id": row["emergency_id"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "signature": row["signature"],
            "entries": [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": entry["label"],
                    "message": entry["message"],
                    "valid_from": entry["valid_from"],
                    "valid_to": entry["valid_to"],
                    "revoked_at": entry["revoked_at"],
                    "revoked_by": entry["revoked_by"],
                    "triggered": triggered_by_area.get(entry["area_code"], []),
                }
                for entry in entries
            ],
        }

    def list_public_versions(self, actor: str) -> list[dict]:
        rows = self.repo.query("SELECT version_no FROM public_versions ORDER BY version_no")
        return [self.get_public_version(actor, row["version_no"]) for row in rows]

    def public_version_view(self, version_no: int) -> dict:
        """公众视角：只含适合发布的简化结论，不含触发指标与内部身份。"""
        full = self.get_public_version("public", version_no)
        return {
            "version_no": full["version_no"],
            "kind": full["kind"],
            "published_at": full["published_at"],
            "signature": full["signature"],
            "entries": [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": entry["label"],
                    "message": entry["message"],
                    "valid_from": entry["valid_from"],
                    "valid_to": entry["valid_to"],
                }
                for entry in full["entries"]
            ],
        }

    # ------------------------------------------------------------------
    # 紧急升级：先发布后补审，期限自动追踪
    # ------------------------------------------------------------------

    def emergency_upgrade(self, actor: str, reason: str, entries: Any) -> dict:
        """紧急升级：立即形成公开版本，补审期限从发布时刻起算。"""
        self._require(reason, "紧急升级必须填写原因")
        if not isinstance(entries, list) or not entries:
            raise validation("紧急升级必须包含至少一个条目")
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                raise validation("紧急升级条目必须是对象")
            area_code = str(item.get("area_code") or "").strip()
            grade = str(item.get("grade") or "")
            if not area_code:
                raise validation("紧急升级条目缺少 area_code")
            if area_code in seen:
                raise validation(f"紧急升级条目重复: {area_code}")
            seen.add(area_code)
            if grade not in GRADE_ORDER:
                raise validation(f"未知的繁忙等级: {grade!r}")
            valid_from_s = fmt_ts(parse_ts(self._require(item.get("valid_from"), "条目缺少 valid_from")))
            valid_to_s = fmt_ts(parse_ts(self._require(item.get("valid_to"), "条目缺少 valid_to")))
            if not valid_from_s < valid_to_s:
                raise validation("条目有效期结束时间必须晚于开始时间")
            normalized.append(
                {
                    "area_code": area_code,
                    "grade": grade,
                    "label": GRADE_LABEL[grade],
                    "message": GRADE_MESSAGE[grade],
                    "valid_from": valid_from_s,
                    "valid_to": valid_to_s,
                }
            )
        normalized.sort(key=lambda entry: entry["area_code"])
        with self.repo.tx():
            version_no = self.repo.next_value("public_versions")
            emergency_id = self.ids.new_id("emg")
            published_at = self._now()
            deadline = fmt_ts(self.clock.now() + timedelta(hours=self.emergency_review_hours))
            signature = sign_payload(
                self.signing_key,
                self._signature_payload(version_no, "EMERGENCY", None, emergency_id, published_at, normalized),
            )
            self.repo.execute(
                "INSERT INTO public_versions(version_no, kind, source_list_id, emergency_id,"
                " published_by, published_at, signature) VALUES (?, 'EMERGENCY', NULL, ?, ?, ?, ?)",
                (version_no, emergency_id, actor, published_at, signature),
            )
            for entry in normalized:
                self.repo.execute(
                    "INSERT INTO public_entries(version_no, area_code, grade, label, message,"
                    " valid_from, valid_to) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        version_no,
                        entry["area_code"],
                        entry["grade"],
                        entry["label"],
                        entry["message"],
                        entry["valid_from"],
                        entry["valid_to"],
                    ),
                )
            self.repo.execute(
                "INSERT INTO emergency_upgrades(id, version_no, reason, status, review_deadline,"
                " created_by, created_at) VALUES (?, ?, ?, 'PENDING_REVIEW', ?, ?, ?)",
                (emergency_id, version_no, reason.strip(), deadline, actor, published_at),
            )
            self.repo.audit(
                actor=actor,
                action="emergency.created",
                subject=f"emergency:{emergency_id}",
                payload={"version_no": version_no, "reason": reason.strip(), "review_deadline": deadline},
                at=published_at,
            )
        return self.get_emergency(actor, emergency_id)

    def review_emergency(self, reviewer: str, emergency_id: str, decision: str) -> dict:
        """紧急升级补审：双人赞成则确认；任何一人否决则撤回未结束的条目。"""
        if decision not in domain.REVIEW_DECISIONS:
            raise validation(f"未知的复核结论: {decision!r}")
        with self.repo.tx():
            row = self.repo.one("SELECT * FROM emergency_upgrades WHERE id=?", (emergency_id,))
            if row is None:
                raise not_found(f"紧急升级不存在: {emergency_id}")
            if row["status"] not in ("PENDING_REVIEW", "OVERDUE"):
                raise conflict(f"紧急升级当前状态为 {row['status']}，补审已结束")
            if reviewer == row["created_by"]:
                raise forbidden("不能补审本人发起的紧急升级")
            existing = self.repo.one(
                "SELECT 1 AS x FROM reviews WHERE subject_type=? AND subject_id=? AND reviewer=?",
                (SUBJECT_EMERGENCY, emergency_id, reviewer),
            )
            if existing is not None:
                raise conflict("该复核人已提交过补审结论", "DUPLICATE_REVIEW")
            now = self._now()
            try:
                self.repo.execute(
                    "INSERT INTO reviews(subject_type, subject_id, reviewer, decision, decided_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (SUBJECT_EMERGENCY, emergency_id, reviewer, decision, now),
                )
            except sqlite3.IntegrityError:
                raise conflict("该复核人已提交过补审结论", "DUPLICATE_REVIEW")
            if decision == "REJECT":
                self.repo.execute(
                    "UPDATE emergency_upgrades SET status='RETRACTED', closed_at=? WHERE id=?",
                    (now, emergency_id),
                )
                truncated = self._truncate_open_entries(row["version_no"], now, reviewer)
                self.repo.audit(
                    actor=reviewer,
                    action="emergency.retracted",
                    subject=f"emergency:{emergency_id}",
                    payload={"decision": decision, "truncated_entries": truncated},
                    at=now,
                )
            else:
                approvals = self.repo.one(
                    "SELECT COUNT(*) AS n FROM reviews WHERE subject_type=? AND subject_id=? AND decision='APPROVE'",
                    (SUBJECT_EMERGENCY, emergency_id),
                )["n"]
                if approvals >= REQUIRED_APPROVALS:
                    self.repo.execute(
                        "UPDATE emergency_upgrades SET status='CONFIRMED', closed_at=? WHERE id=?",
                        (now, emergency_id),
                    )
                    action = "emergency.confirmed"
                else:
                    action = "emergency.reviewed"
                self.repo.audit(
                    actor=reviewer,
                    action=action,
                    subject=f"emergency:{emergency_id}",
                    payload={"decision": decision},
                    at=now,
                )
        return self.get_emergency(reviewer, emergency_id)

    def _truncate_open_entries(self, version_no: int, now: str, actor: str) -> int:
        """截断指定版本中尚未结束的条目（须在事务内调用）。"""
        rows = self.repo.query(
            "SELECT area_code FROM public_entries WHERE version_no=? AND revoked_at IS NULL AND valid_to > ?",
            (version_no, now),
        )
        for entry in rows:
            self.repo.execute(
                "UPDATE public_entries SET revoked_at=?, revoked_by=? WHERE version_no=? AND area_code=?",
                (now, actor, version_no, entry["area_code"]),
            )
        return len(rows)

    def sweep_emergencies(self, actor: str = "system") -> dict:
        """期限自动追踪：把已过补审期限的紧急升级标记为 OVERDUE。"""
        now = self._now()
        marked: list[str] = []
        with self.repo.tx():
            rows = self.repo.query(
                "SELECT id, review_deadline FROM emergency_upgrades"
                " WHERE status='PENDING_REVIEW' AND review_deadline < ?",
                (now,),
            )
            for row in rows:
                self.repo.execute(
                    "UPDATE emergency_upgrades SET status='OVERDUE' WHERE id=?", (row["id"],)
                )
                self.repo.audit(
                    actor=actor,
                    action="emergency.overdue",
                    subject=f"emergency:{row['id']}",
                    payload={"review_deadline": row["review_deadline"]},
                    at=now,
                )
                marked.append(row["id"])
        return {"swept_at": now, "marked_overdue": marked}

    def get_emergency(self, actor: str, emergency_id: str) -> dict:
        row = self.repo.one("SELECT * FROM emergency_upgrades WHERE id=?", (emergency_id,))
        if row is None:
            raise not_found(f"紧急升级不存在: {emergency_id}")
        return {
            **row,
            "reviews": self._reviews_for(SUBJECT_EMERGENCY, emergency_id),
        }

    def list_emergencies(self, actor: str) -> list[dict]:
        self.sweep_emergencies()  # 惰性追踪期限，保证读到的状态是最新的
        rows = self.repo.query("SELECT id FROM emergency_upgrades ORDER BY created_at, id")
        return [self.get_emergency(actor, row["id"]) for row in rows]

    # ------------------------------------------------------------------
    # 撤销：只影响尚未结束的有效区间
    # ------------------------------------------------------------------

    def revoke_public_entry(self, actor: str, version_no: int, area_code: str) -> dict:
        """撤销公开条目。

        - 区间已结束（valid_to <= now）：拒绝，撤销不改变历史；
        - 区间进行中：revoked_at=now，有效区间在 now 处截断；
        - 区间未开始：revoked_at=now < valid_from，有效区间整体失效。
        原始区间保持不变，历史重建按撤销事件还原。
        """
        with self.repo.tx():
            version = self.repo.one("SELECT * FROM public_versions WHERE version_no=?", (version_no,))
            if version is None:
                raise not_found(f"公开版本不存在: {version_no}")
            entry = self.repo.one(
                "SELECT * FROM public_entries WHERE version_no=? AND area_code=?",
                (version_no, area_code),
            )
            if entry is None:
                raise not_found(f"公开条目不存在: {version_no}/{area_code}")
            now = self._now()
            if entry["revoked_at"] is not None:
                raise conflict("该条目已被撤销", "ALREADY_REVOKED")
            if entry["valid_to"] <= now:
                raise conflict("有效区间已结束，撤销只影响尚未结束的区间", "INTERVAL_CLOSED")
            self.repo.execute(
                "UPDATE public_entries SET revoked_at=?, revoked_by=? WHERE version_no=? AND area_code=?",
                (now, actor, version_no, area_code),
            )
            self.repo.audit(
                actor=actor,
                action="entry.revoked",
                subject=f"public-version:{version_no}",
                payload={"area_code": area_code, "valid_from": entry["valid_from"], "valid_to": entry["valid_to"]},
                at=now,
            )
        return self.get_public_version(actor, version_no)

    # ------------------------------------------------------------------
    # 历史重建与当前公开视图
    # ------------------------------------------------------------------

    def reconstruct(self, at: Any) -> dict:
        """重建指定时刻的公开清单。

        语义：最近一次 NORMAL 版本构成基线，其后的 EMERGENCY 版本按
        版本号顺序叠加（同服务区后者覆盖前者）；再按有效区间与撤销
        事件过滤——条目在 at 可见当且仅当：
        版本已发布 且 valid_from <= at < valid_to 且 (未撤销 或 at < revoked_at)。
        """
        at_s = fmt_ts(parse_ts(at))
        baseline = self.repo.one(
            "SELECT * FROM public_versions WHERE kind='NORMAL' AND published_at<=?"
            " ORDER BY version_no DESC LIMIT 1",
            (at_s,),
        )
        merged: dict[str, dict] = {}
        basis_version_no = baseline["version_no"] if baseline else None
        if baseline is not None:
            for entry in self.repo.query(
                "SELECT * FROM public_entries WHERE version_no=?", (baseline["version_no"],)
            ):
                merged[entry["area_code"]] = entry
        overlays = self.repo.query(
            "SELECT * FROM public_versions WHERE kind='EMERGENCY' AND published_at<=? AND version_no>?"
            " ORDER BY version_no",
            (at_s, basis_version_no or 0),
        )
        for version in overlays:
            for entry in self.repo.query(
                "SELECT * FROM public_entries WHERE version_no=?", (version["version_no"],)
            ):
                merged[entry["area_code"]] = entry
        visible = [
            entry
            for entry in merged.values()
            if entry["valid_from"] <= at_s < entry["valid_to"]
            and (entry["revoked_at"] is None or at_s < entry["revoked_at"])
        ]
        visible.sort(key=lambda entry: entry["area_code"])
        return {
            "at": at_s,
            "basis_version_no": basis_version_no,
            "entries": [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": entry["label"],
                    "message": entry["message"],
                    "valid_from": entry["valid_from"],
                    "valid_to": entry["valid_to"],
                    "version_no": entry["version_no"],
                }
                for entry in visible
            ],
        }

    def public_current(self) -> dict:
        """公众渠道的当前清单（reconstruct(now) 的简化形式）。"""
        snapshot = self.reconstruct(self._now())
        return {"generated_at": snapshot["at"], "entries": snapshot["entries"]}

    # ------------------------------------------------------------------
    # 签名校验与审计链
    # ------------------------------------------------------------------

    def verify_version_signature(self, version_no: int) -> dict:
        row = self.repo.one("SELECT * FROM public_versions WHERE version_no=?", (version_no,))
        if row is None:
            raise not_found(f"公开版本不存在: {version_no}")
        entries = self.repo.query(
            "SELECT * FROM public_entries WHERE version_no=? ORDER BY area_code", (version_no,)
        )
        payload = self._signature_payload(
            row["version_no"],
            row["kind"],
            row["source_list_id"],
            row["emergency_id"],
            row["published_at"],
            [
                {
                    "area_code": entry["area_code"],
                    "grade": entry["grade"],
                    "label": entry["label"],
                    "message": entry["message"],
                    "valid_from": entry["valid_from"],
                    "valid_to": entry["valid_to"],
                }
                for entry in entries
            ],
        )
        valid = verify_payload(self.signing_key, payload, row["signature"])
        return {"version_no": version_no, "valid": valid, "signature": row["signature"]}

    def verify_audit_chain(self) -> dict:
        events = self.repo.audit_events()
        prev_hash = GENESIS_HASH
        for index, event in enumerate(events, start=1):
            if event["prev_hash"] != prev_hash:
                return {"valid": False, "checked": index, "first_invalid_seq": event["seq"]}
            digest = audit_hash(
                event["prev_hash"],
                event["at"],
                event["actor"],
                event["action"],
                event["subject"],
                event["payload"],
            )
            if digest != event["hash"]:
                return {"valid": False, "checked": index, "first_invalid_seq": event["seq"]}
            prev_hash = event["hash"]
        return {"valid": True, "checked": len(events)}

    # ------------------------------------------------------------------
    # 进程恢复
    # ------------------------------------------------------------------

    def recover(self) -> dict:
        """进程恢复：重放派生状态（紧急期限追踪）并校验审计链。

        所有业务状态都持久化在数据库中，重启后只需补做由时钟驱动的
        状态迁移；审计链校验失败时不再追加新事件，交由运维处理。
        """
        sweep = self.sweep_emergencies()
        chain = self.verify_audit_chain()
        report = {
            "recovered_at": self._now(),
            "overdue_marked": sweep["marked_overdue"],
            "audit_valid": chain["valid"],
            "audit_events_checked": chain["checked"],
        }
        if chain["valid"]:
            with self.repo.tx():
                self.repo.audit(
                    actor="system",
                    action="recovery.completed",
                    subject="service",
                    payload=report,
                    at=self._now(),
                )
        return report
