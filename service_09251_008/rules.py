"""分级规则领域模型与评估引擎。

一条规则版本（RuleVersion）定义：
  * 每个指标的观察期窗口（天）与每档阈值；
  * 每个等级的触发表达式（满足哪些指标条件即落入该档）；
  * 人工例外在评估时如何叠加。

评估产物保留 *触发痕迹*（每个指标的窗口值、命中文本、例外来源），
供内部人员回答"这次调整是哪些指标触发的"，公开版本则只取简化结论。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .constants import (
    EXCEPTION_EXEMPT,
    EXCEPTION_FORCE,
    EXCEPTION_HOLD,
    LEVEL_BUSY,
    LEVEL_EXTREME,
    LEVEL_HEAVY,
    LEVEL_ORDER,
)
from .errors import ValidationError


@dataclass(frozen=True)
class MetricSpec:
    """单个指标的定义：观察期窗口（天）与三档阈值（含）。"""

    key: str
    label: str
    window_days: int
    busy_at: float
    heavy_at: float
    extreme_at: float
    higher_is_busier: bool = True

    def level_for(self, value: float | None) -> str | None:
        """窗口聚合值落入哪一档；无数据返回 None。"""
        if value is None:
            return None
        if self.higher_is_busier:
            if value >= self.extreme_at:
                return LEVEL_EXTREME
            if value >= self.heavy_at:
                return LEVEL_HEAVY
            if value >= self.busy_at:
                return LEVEL_BUSY
        else:  # 越低越繁忙（如平均车速）
            if value <= self.extreme_at:
                return LEVEL_EXTREME
            if value <= self.heavy_at:
                return LEVEL_HEAVY
            if value <= self.busy_at:
                return LEVEL_BUSY
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "window_days": self.window_days,
            "busy_at": self.busy_at,
            "heavy_at": self.heavy_at,
            "extreme_at": self.extreme_at,
            "higher_is_busier": self.higher_is_busier,
        }


@dataclass(frozen=True)
class RuleVersion:
    """一个不可变的规则版本。

    ``require_all`` 为真时，某档要求 ``required`` 中列出的指标全部达到该档；
    否则只要任一指标达到即落入该档（取最高）。指标缺数据视为不满足。
    """

    version: str
    metrics: tuple[MetricSpec, ...]
    require_all: bool = False
    required: tuple[str, ...] = ()
    note: str = ""

    def metric(self, key: str) -> MetricSpec | None:
        for m in self.metrics:
            if m.key == key:
                return m
        return None

    def max_window_days(self) -> int:
        return max((m.window_days for m in self.metrics), default=0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "require_all": self.require_all,
            "required": list(self.required),
            "note": self.note,
            "metrics": [m.to_dict() for m in self.metrics],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RuleVersion":
        metrics = tuple(MetricSpec(**m) for m in data["metrics"])
        keys = {m.key for m in metrics}
        unknown = [k for k in data.get("required", []) if k not in keys]
        if unknown:
            raise ValidationError(f"required 引用了未定义的指标: {unknown}")
        for m in metrics:
            if m.window_days <= 0:
                raise ValidationError(f"指标 {m.key} 观察期必须为正整数天")
            if m.higher_is_busier and not (
                m.busy_at <= m.heavy_at <= m.extreme_at
            ):
                raise ValidationError(f"指标 {m.key} 阈值需满足 busy<=heavy<=extreme")
            if not m.higher_is_busier and not (
                m.busy_at >= m.heavy_at >= m.extreme_at
            ):
                raise ValidationError(f"指标 {m.key} 阈值需满足 busy>=heavy>=extreme")
        return RuleVersion(
            version=data["version"],
            metrics=metrics,
            require_all=bool(data.get("require_all", False)),
            required=tuple(data.get("required", ())),
            note=data.get("note", ""),
        )


def default_rule_version() -> RuleVersion:
    """内置默认规则版本（v1），供全新部署与示例使用。"""
    return RuleVersion(
        version="v1",
        metrics=(
            MetricSpec("flow_saturation", "流量饱和度", 3, 0.80, 0.90, 0.97),
            MetricSpec("queue_minutes", "平均排队时长(分)", 2, 20.0, 40.0, 60.0),
            MetricSpec("avg_speed_kmh", "平均车速(km/h)", 3, 40.0, 25.0, 15.0,
                       higher_is_busier=False),
        ),
        require_all=False,
        note="节假日默认规则：任一指标达档即定级，取最高档",
    )


@dataclass(frozen=True)
class MetricTrace:
    """单个指标在评估时点的完整痕迹。"""

    key: str
    label: str
    window_days: int
    window_start: str
    window_end: str
    sample_count: int
    value: float | None
    reached: str | None  # busy/heavy/extreme/None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "window_days": self.window_days,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "sample_count": self.sample_count,
            "value": self.value,
            "reached": self.reached,
        }


@dataclass(frozen=True)
class ExceptionTrace:
    """人工例外在评估中的作用痕迹。"""

    kind: str
    level: str | None
    reason: str
    author: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "level": self.level,
                "reason": self.reason, "author": self.author}


@dataclass(frozen=True)
class Evaluation:
    """单个服务区的评估结论。"""

    service_area_id: str
    level: str | None
    automatic_level: str | None
    triggered_by: tuple[str, ...]
    traces: tuple[MetricTrace, ...]
    exception: ExceptionTrace | None = None
    excluded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "service_area_id": self.service_area_id,
            "level": self.level,
            "automatic_level": self.automatic_level,
            "triggered_by": list(self.triggered_by),
            "traces": [t.to_dict() for t in self.traces],
            "exception": self.exception.to_dict() if self.exception else None,
            "excluded": self.excluded,
        }


@dataclass
class MetricReading:
    """一条指标读数：(服务区, 指标, 日期, 值)。"""

    area_id: str
    metric_key: str
    day: str
    value: float


@dataclass
class ManualException:
    """人工例外：在 [valid_from, valid_to] 日期区间内对某服务区生效。

    区间为闭区间；``valid_to`` 为 None 表示长期有效直到撤销。
    """

    area_id: str
    kind: str
    level: str | None
    reason: str
    author: str
    valid_from: str
    valid_to: str | None = None

    def active_on(self, day: str) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


class RuleEngine:
    """无状态评估器：规则版本 + 读数 + 例外 => 评估结论。"""

    def __init__(self, rule: RuleVersion) -> None:
        self.rule = rule

    # -- 观察期聚合 -------------------------------------------------------

    @staticmethod
    def _window_end(on_day: date, window_days: int) -> date:
        # 观察期覆盖评估日及之前 window_days-1 天，跨日时窗口随评估日滑动
        return on_day - timedelta(days=window_days - 1)

    def aggregate(
        self,
        readings: Iterable[MetricReading],
        metric: MetricSpec,
        on_day: date,
    ) -> tuple[float | None, int, date, date]:
        """对窗口内读数取均值，返回 (值, 样本数, 起, 止)。"""
        start = self._window_end(on_day, metric.window_days)
        total = 0.0
        count = 0
        for r in readings:
            if r.metric_key != metric.key:
                continue
            rd = date.fromisoformat(r.day)
            if start <= rd <= on_day:
                total += r.value
                count += 1
        value = total / count if count else None
        return value, count, start, on_day

    # -- 单区评估 ---------------------------------------------------------

    def evaluate_area(
        self,
        area_id: str,
        readings_by_area: dict[str, list[MetricReading]],
        exception: ManualException | None,
        on_day: date,
    ) -> Evaluation:
        if exception is not None and exception.kind == EXCEPTION_EXEMPT:
            return Evaluation(
                service_area_id=area_id,
                level=None,
                automatic_level=None,
                triggered_by=(),
                traces=(),
                exception=ExceptionTrace(
                    EXCEPTION_EXEMPT, None, exception.reason, exception.author
                ),
                excluded=True,
            )

        readings = readings_by_area.get(area_id, [])
        traces: list[MetricTrace] = []
        reached: dict[str, str] = {}
        for spec in self.rule.metrics:
            value, count, start, end = self.aggregate(readings, spec, on_day)
            level = spec.level_for(value)
            traces.append(
                MetricTrace(
                    key=spec.key,
                    label=spec.label,
                    window_days=spec.window_days,
                    window_start=start.isoformat(),
                    window_end=end.isoformat(),
                    sample_count=count,
                    value=value,
                    reached=level,
                )
            )
            if level is not None:
                reached[spec.key] = level

        automatic = self._combine(reached)
        triggered = tuple(
            k for k, lv in reached.items()
            if automatic is not None and LEVEL_ORDER[lv] >= LEVEL_ORDER[automatic]
        )

        level, exc_trace = automatic, None
        if exception is not None and exception.active_on(on_day.isoformat()):
            exc_trace = ExceptionTrace(
                exception.kind, exception.level, exception.reason, exception.author
            )
            if exception.kind == EXCEPTION_FORCE and exception.level:
                level = exception.level
            elif exception.kind == EXCEPTION_HOLD:
                # 压低到指定档；未指定档位则不入选
                target = exception.level
                if target is None:
                    level = None
                elif automatic is None:
                    level = None
                else:
                    level = automatic if LEVEL_ORDER[automatic] < LEVEL_ORDER[target] \
                        else target

        return Evaluation(
            service_area_id=area_id,
            level=level,
            automatic_level=automatic,
            triggered_by=triggered,
            traces=tuple(traces),
            exception=exc_trace,
        )

    def _combine(self, reached: dict[str, str]) -> str | None:
        """按规则版本的组合策略（任一/全部）求自动等级。"""
        if not reached:
            return None
        if not self.rule.require_all:
            return max(reached.values(), key=lambda lv: LEVEL_ORDER[lv])

        required = self.rule.required or tuple(m.key for m in self.rule.metrics)
        present = [reached[k] for k in required if k in reached]
        if len(present) != len(required):
            return None
        # 全部指标达到的共同最高档：取各指标可达档中的最小值
        return min(present, key=lambda lv: LEVEL_ORDER[lv])

    # -- 批量评估 ---------------------------------------------------------

    def evaluate_all(
        self,
        area_ids: Iterable[str],
        readings_by_area: dict[str, list[MetricReading]],
        exceptions: Iterable[ManualException],
        on_day: date,
    ) -> list[Evaluation]:
        exc_by_area: dict[str, ManualException] = {}
        for e in exceptions:
            if e.active_on(on_day.isoformat()):
                # 同区多条例外：force_up 优先，其次 hold_down，最后 exempt
                # （exempt 在入口已直接处理；这里按稳定优先级保留一条）
                old = exc_by_area.get(e.area_id)
                if old is None or _EXC_PRIORITY[e.kind] > _EXC_PRIORITY[old.kind]:
                    exc_by_area[e.area_id] = e
        results = [
            self.evaluate_area(a, readings_by_area, exc_by_area.get(a), on_day)
            for a in area_ids
        ]
        # 候选清单只保留有等级的服务区，按等级降序、编号升序排列
        results.sort(
            key=lambda e: (
                -LEVEL_ORDER[e.level] if e.level is not None else -1,
                e.service_area_id,
            )
        )
        return results


_EXC_PRIORITY = {
    EXCEPTION_FORCE: 3,
    EXCEPTION_HOLD: 2,
    EXCEPTION_EXEMPT: 1,
}
