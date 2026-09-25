"""规则引擎：阈值、观察期滑动窗口、人工例外与规则版本切换。"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from service_09251_008.constants import (
    LEVEL_BUSY,
    LEVEL_EXTREME,
    LEVEL_HEAVY,
)
from service_09251_008.rules import (
    ManualException,
    MetricReading,
    MetricSpec,
    RuleEngine,
    RuleVersion,
    default_rule_version,
)

DAY = date(2026, 9, 25)


def readings(area, pairs):
    """pairs: [(metric, day_offset, value), ...]，offset 相对 DAY 向前。"""
    out = []
    for metric, offset, value in pairs:
        d = (DAY - timedelta(days=offset)).isoformat()
        out.append(MetricReading(area, metric, d, value))
    return out


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = default_rule_version()
        self.engine = RuleEngine(self.rule)

    def eval_one(self, rs, exc=None, on_day=DAY):
        by_area = {"A": rs}
        areas = ["A"]
        return self.engine.evaluate_all(areas, by_area, [exc] if exc else [], on_day)[0]

    def test_no_data_no_level(self) -> None:
        ev = self.eval_one([])
        self.assertIsNone(ev.level)
        self.assertEqual(ev.triggered_by, ())

    def test_single_metric_busy(self) -> None:
        rs = readings("A", [("flow_saturation", 0, 0.82),
                            ("flow_saturation", 1, 0.82),
                            ("flow_saturation", 2, 0.82)])
        ev = self.eval_one(rs)
        self.assertEqual(ev.level, LEVEL_BUSY)
        self.assertEqual(ev.triggered_by, ("flow_saturation",))
        trace = next(t for t in ev.traces if t.key == "flow_saturation")
        self.assertEqual(trace.sample_count, 3)
        self.assertAlmostEqual(trace.value, 0.82)
        self.assertEqual(trace.window_start, "2026-09-23")
        self.assertEqual(trace.window_end, "2026-09-25")

    def test_highest_level_wins(self) -> None:
        # 饱和度 heavy，但车速 extreme（低车速）=> 取 extreme
        rs = readings("A", [
            ("flow_saturation", 0, 0.91),
            ("flow_saturation", 1, 0.91),
            ("flow_saturation", 2, 0.91),
            ("avg_speed_kmh", 0, 12.0),
            ("avg_speed_kmh", 1, 12.0),
            ("avg_speed_kmh", 2, 12.0),
        ])
        ev = self.eval_one(rs)
        self.assertEqual(ev.level, LEVEL_EXTREME)
        self.assertIn("avg_speed_kmh", ev.triggered_by)
        self.assertNotIn("flow_saturation", ev.triggered_by)

    def test_window_average_and_stale_data(self) -> None:
        # 窗口 3 天：两天 0.95（heavy 区间），一天 0.60（窗口外，第 4 天）
        rs = readings("A", [
            ("flow_saturation", 0, 0.95),
            ("flow_saturation", 1, 0.95),
            ("flow_saturation", 2, 0.70),  # 均值 0.8667 => busy
            ("flow_saturation", 3, 0.99),  # 窗口外，忽略
        ])
        ev = self.eval_one(rs)
        self.assertEqual(ev.level, LEVEL_BUSY)
        trace = next(t for t in ev.traces if t.key == "flow_saturation")
        self.assertEqual(trace.sample_count, 3)
        self.assertAlmostEqual(trace.value, (0.95 + 0.95 + 0.70) / 3, places=6)

    def test_sliding_window_cross_day(self) -> None:
        rs = readings("A", [
            ("flow_saturation", 0, 0.95),  # DAY 当天
            ("flow_saturation", 2, 0.95),  # DAY-2
        ])
        # 在 DAY-1 评估：窗口 [DAY-3, DAY-1] 只含 DAY-2 一个样本
        ev = self.eval_one(rs, on_day=DAY - timedelta(days=1))
        trace = next(t for t in ev.traces if t.key == "flow_saturation")
        self.assertEqual(trace.sample_count, 1)
        self.assertEqual(ev.level, LEVEL_HEAVY)
        # 在 DAY 评估：DAY-2 滑出窗口？窗口3天=[DAY-2,DAY]，仍含两天
        ev2 = self.eval_one(rs, on_day=DAY)
        trace2 = next(t for t in ev2.traces if t.key == "flow_saturation")
        self.assertEqual(trace2.sample_count, 2)
        # DAY-3 评估：窗口内无数据
        ev3 = self.eval_one(rs, on_day=DAY - timedelta(days=3))
        self.assertEqual(ev3.level, None)

    def test_queue_uses_two_day_window(self) -> None:
        # queue_minutes 窗口为 2 天：一天 50、一天 30 => 均值 40 => heavy
        rs = readings("A", [
            ("queue_minutes", 0, 50.0),
            ("queue_minutes", 1, 30.0),
            ("queue_minutes", 2, 60.0),  # 窗口外
        ])
        ev = self.eval_one(rs)
        self.assertEqual(ev.level, LEVEL_HEAVY)

    def test_lower_is_busier_speed(self) -> None:
        rs = readings("A", [
            ("avg_speed_kmh", 0, 24.0),
            ("avg_speed_kmh", 1, 24.0),
            ("avg_speed_kmh", 2, 24.0),
        ])
        self.assertEqual(self.eval_one(rs).level, LEVEL_HEAVY)

    # -- 人工例外 ---------------------------------------------------------

    def test_force_up_exception(self) -> None:
        ev = self.eval_one(
            readings("A", [("flow_saturation", i, 0.82) for i in range(3)]),
            exc=ManualException("A", "force_up", LEVEL_EXTREME,
                                "重大活动保障", "boss", "2026-09-01"),
        )
        self.assertEqual(ev.level, LEVEL_EXTREME)
        self.assertEqual(ev.automatic_level, LEVEL_BUSY)
        self.assertEqual(ev.exception.kind, "force_up")

    def test_hold_down_exception(self) -> None:
        rs = readings("A", [("flow_saturation", i, 0.95) for i in range(3)])
        ev = self.eval_one(
            rs,
            exc=ManualException("A", "hold_down", LEVEL_BUSY,
                                "数据施工存疑", "analyst", "2026-09-01"),
        )
        self.assertEqual(ev.level, LEVEL_BUSY)
        self.assertEqual(ev.automatic_level, LEVEL_HEAVY)

    def test_hold_down_without_level_excludes(self) -> None:
        rs = readings("A", [("flow_saturation", i, 0.95) for i in range(3)])
        ev = self.eval_one(
            rs,
            exc=ManualException("A", "hold_down", None,
                                "传感器故障", "analyst", "2026-09-01"),
        )
        self.assertIsNone(ev.level)

    def test_exempt_excludes_entirely(self) -> None:
        rs = readings("A", [("flow_saturation", i, 0.99) for i in range(3)])
        ev = self.eval_one(
            rs,
            exc=ManualException("A", "exempt", None,
                                "封闭施工", "analyst", "2026-09-01"),
        )
        self.assertTrue(ev.excluded)
        self.assertIsNone(ev.level)
        self.assertEqual(ev.traces, ())

    def test_exception_date_bounds(self) -> None:
        rs = readings("A", [("flow_saturation", i, 0.85) for i in range(3)])
        # 尚未生效
        future = ManualException("A", "force_up", LEVEL_EXTREME, "x", "b",
                                 "2026-10-01")
        self.assertEqual(self.eval_one(rs, exc=future).level, LEVEL_BUSY)
        # 已过期
        past = ManualException("A", "force_up", LEVEL_EXTREME, "x", "b",
                               "2026-09-01", "2026-09-20")
        self.assertEqual(self.eval_one(rs, exc=past).level, LEVEL_BUSY)
        # 末日仍生效（闭区间）
        edge = ManualException("A", "force_up", LEVEL_EXTREME, "x", "b",
                               "2026-09-01", "2026-09-25")
        self.assertEqual(self.eval_one(rs, exc=edge).level, LEVEL_EXTREME)


class RuleSwitchTests(unittest.TestCase):
    def test_require_all_rule_version(self) -> None:
        v2 = RuleVersion(
            version="v2",
            metrics=(
                MetricSpec("flow_saturation", "流量饱和度", 3, 0.80, 0.90, 0.97),
                MetricSpec("queue_minutes", "排队时长", 2, 20.0, 40.0, 60.0),
            ),
            require_all=True,
            required=("flow_saturation", "queue_minutes"),
        )
        engine = RuleEngine(v2)
        rs = [
            MetricReading("A", "flow_saturation",
                          (DAY - timedelta(days=i)).isoformat(), 0.95)
            for i in range(3)
        ]
        # 只有饱和度 heavy，排队无数据 => require_all 下不定级
        only_flow = engine.evaluate_all(["A"], {"A": rs}, [], DAY)[0]
        self.assertIsNone(only_flow.level)
        # 补上排队（busy 档）：共同最高档取 min(heavy, busy)=busy
        rs += [
            MetricReading("A", "queue_minutes", DAY.isoformat(), 25.0),
            MetricReading("A", "queue_minutes",
                          (DAY - timedelta(days=1)).isoformat(), 25.0),
        ]
        both = engine.evaluate_all(["A"], {"A": rs}, [], DAY)[0]
        self.assertEqual(both.level, LEVEL_BUSY)

    def test_invalid_thresholds_rejected(self) -> None:
        with self.assertRaises(Exception):
            RuleVersion.from_dict({
                "version": "bad",
                "metrics": [{
                    "key": "x", "label": "x", "window_days": 3,
                    "busy_at": 0.9, "heavy_at": 0.8, "extreme_at": 0.97,
                }],
            })

    def test_invalid_required_reference(self) -> None:
        with self.assertRaises(Exception):
            RuleVersion.from_dict({
                "version": "bad",
                "metrics": [{
                    "key": "x", "label": "x", "window_days": 3,
                    "busy_at": 1, "heavy_at": 2, "extreme_at": 3,
                }],
                "required": ["missing"],
            })


if __name__ == "__main__":
    unittest.main()
