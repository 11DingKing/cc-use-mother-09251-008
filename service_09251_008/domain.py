"""领域模型：等级、状态、时间规范与分级规则求值。

本模块只包含纯函数与常量，不依赖持久化与接口边界，
保证规则求值可以在任意时间端口下确定性地重放。
"""
from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Any

from .errors import validation

# ---------------------------------------------------------------------------
# 繁忙等级
# ---------------------------------------------------------------------------

#: 等级严重度排序（数值越大越严重）。
GRADE_ORDER: dict[str, int] = {"L1": 1, "L2": 2, "L3": 3}

#: 面向公众的等级文案。
GRADE_LABEL: dict[str, str] = {
    "L1": "轻度繁忙",
    "L2": "中度繁忙",
    "L3": "重度繁忙",
}

#: 面向公众的简化结论文案（公众渠道只能看到这一层）。
GRADE_MESSAGE: dict[str, str] = {
    "L1": "轻度繁忙，通行基本正常",
    "L2": "中度繁忙，建议错峰出行",
    "L3": "重度繁忙，建议绕行或推迟出行",
}

# ---------------------------------------------------------------------------
# 状态机常量
# ---------------------------------------------------------------------------

VERSION_STATUS = ("DRAFT", "ACTIVE", "RETIRED")
PERIOD_STATUS = ("OPEN", "CLOSED")
LIST_STATUS = ("DRAFT", "IN_REVIEW", "APPROVED", "REJECTED", "PUBLISHED", "SUPERSEDED")
EXCEPTION_ACTIONS = ("FORCE_INCLUDE", "FORCE_EXCLUDE")
REVIEW_DECISIONS = ("APPROVE", "REJECT")
EMERGENCY_STATUS = ("PENDING_REVIEW", "OVERDUE", "CONFIRMED", "RETRACTED")
PUBLIC_KIND = ("NORMAL", "EMERGENCY")

#: 复核主体类型（候选清单 / 紧急升级共用一个复核表）。
SUBJECT_CANDIDATE_LIST = "CANDIDATE_LIST"
SUBJECT_EMERGENCY = "EMERGENCY_UPGRADE"

#: 双人复核所需的最少赞成票。
REQUIRED_APPROVALS = 2

#: 规则条件支持的比较运算符。
_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}

# ---------------------------------------------------------------------------
# 时间规范：所有落库时间统一为 UTC、毫秒精度的 ISO 字符串，
# 这样字符串比较与时间比较等价，历史重建可以直接用 SQL 过滤。
# ---------------------------------------------------------------------------


def parse_ts(value: Any) -> datetime:
    """把外部输入解析为带时区的 UTC 时间。"""
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise validation(f"无效的时间格式: {value!r}")
    else:
        raise validation(f"无效的时间格式: {value!r}")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def fmt_ts(moment: datetime) -> str:
    """把时间格式化为统一的落库形式。"""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 规则载荷校验与求值
# ---------------------------------------------------------------------------


def validate_metrics_payload(metrics: Any) -> list[dict]:
    """校验指标版本载荷，返回规范化后的指标列表。"""
    if not isinstance(metrics, list) or not metrics:
        raise validation("指标版本必须包含至少一个指标定义")
    seen: set[str] = set()
    normalized: list[dict] = []
    for item in metrics:
        if not isinstance(item, dict):
            raise validation("指标定义必须是对象")
        code = str(item.get("code") or "").strip()
        name = str(item.get("name") or "").strip()
        unit = str(item.get("unit") or "").strip()
        if not code or not name:
            raise validation("指标定义必须包含 code 与 name")
        if code in seen:
            raise validation(f"指标编码重复: {code}")
        seen.add(code)
        normalized.append({"code": code, "name": name, "unit": unit})
    return normalized


def validate_rules_payload(rules: Any, metric_codes: set[str]) -> list[dict]:
    """校验分级规则载荷，返回规范化后的规则列表。

    每个等级至多一条规则；条件引用的指标必须存在于规则绑定的指标版本中。
    """
    if not isinstance(rules, list) or not rules:
        raise validation("分级规则必须包含至少一条规则")
    seen_grades: set[str] = set()
    normalized: list[dict] = []
    for rule in rules:
        if not isinstance(rule, dict):
            raise validation("规则必须是对象")
        grade = str(rule.get("grade") or "")
        if grade not in GRADE_ORDER:
            raise validation(f"未知的繁忙等级: {grade!r}")
        if grade in seen_grades:
            raise validation(f"等级 {grade} 存在重复规则")
        seen_grades.add(grade)
        conditions = rule.get("all")
        if not isinstance(conditions, list) or not conditions:
            raise validation(f"等级 {grade} 的规则必须包含 all 条件列表")
        normalized_conditions: list[dict] = []
        for condition in conditions:
            if not isinstance(condition, dict):
                raise validation("规则条件必须是对象")
            metric = str(condition.get("metric") or "")
            op = str(condition.get("op") or "")
            if metric not in metric_codes:
                raise validation(f"规则引用了未定义的指标: {metric!r}")
            if op not in _OPS:
                raise validation(f"不支持的比较运算符: {op!r}")
            try:
                threshold = float(condition.get("value"))
            except (TypeError, ValueError):
                raise validation(f"规则阈值必须是数值: {condition.get('value')!r}")
            normalized_conditions.append({"metric": metric, "op": op, "value": threshold})
        normalized.append({"grade": grade, "all": normalized_conditions})
    return normalized


def evaluate_rules(rules: list[dict], values: dict[str, float]) -> tuple[str, list[dict]] | None:
    """对单个服务区的指标值求值。

    按等级从重到轻匹配，第一条所有条件都成立的规则生效。
    返回 ``(等级, 触发指标列表)``；没有任何规则命中时返回 ``None``。
    触发指标列表记录了本次分级由哪些指标触发，供内部管理人员追溯。
    """
    for rule in sorted(rules, key=lambda item: -GRADE_ORDER[item["grade"]]):
        triggered: list[dict] = []
        matched = True
        for condition in rule["all"]:
            observed = values.get(condition["metric"])
            if observed is None or not _OPS[condition["op"]](observed, condition["value"]):
                matched = False
                break
            triggered.append(
                {
                    "metric": condition["metric"],
                    "op": condition["op"],
                    "threshold": condition["value"],
                    "value": observed,
                }
            )
        if matched:
            return rule["grade"], triggered
    return None


def intervals_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    """判断两个左闭右开区间是否相交（入参须为 fmt_ts 规范化字符串）。"""
    return a_start < b_end and b_start < a_end
