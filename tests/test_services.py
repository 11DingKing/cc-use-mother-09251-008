"""应用服务：双人复核流程、重复审批、紧急升级期限追踪、撤销与跨日有效期。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from service_09251_008.errors import (
    ConflictError,
    DeadlinePassedError,
    DuplicateApprovalError,
)
from service_09251_008.constants import (
    BATCH_CANDIDATE,
    BATCH_EMERGENCY,
    BATCH_EXPIRED,
    BATCH_PUBLISHED,
    BATCH_REJECTED,
    BATCH_REVOKED,
)

from support import (
    approve_both,
    clock_at,
    make_app,
    seed_areas,
    window_days,
)


def make_heavy_sa001(app, day="2026-09-25") -> None:
    window_days(app, "SA001", "flow_saturation", day, 3, 0.95)


class ReviewFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = clock_at()
        self.app = make_app(Path(self.tmp.name), self.clock)
        seed_areas(self.app)
        make_heavy_sa001(self.app)

    def test_candidate_carries_traces_but_public_is_simplified(self) -> None:
        batch = self.app.generate_candidates("planner")
        self.assertEqual(batch["status"], BATCH_CANDIDATE)
        item = batch["payload"]["items"][0]
        self.assertEqual(item["service_area_id"], "SA001")
        self.assertEqual(item["level"], "heavy")
        # 内部可见触发痕迹
        self.assertTrue(item["traces"])
        self.assertIn("flow_saturation", item["triggered_by"])

    def test_full_double_review_publishes(self) -> None:
        batch = self.app.generate_candidates("planner")
        bid = batch["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "approve")
        done = self.app.review(bid, "reviewer_b", "second", "approve")
        self.assertEqual(done["status"], BATCH_PUBLISHED)
        # 公开视图生效
        current = self.app.public_current("2026-09-25")
        self.assertEqual([i["area_id"] for i in current["items"]], ["SA001"])
        # 公开清单信封只有简化字段，无痕迹
        manifest = self.app.public_manifest(bid)
        self.assertIn("signature", manifest)
        self.assertNotIn("traces", json.dumps(manifest, ensure_ascii=False))
        self.assertTrue(self.app.verify_manifest(manifest))

    def test_review_order_enforced(self) -> None:
        bid = self.app.generate_candidates("planner")["batch_id"]
        with self.assertRaises(ConflictError):
            self.app.review(bid, "reviewer_b", "second", "approve")

    def test_two_distinct_reviewers_required(self) -> None:
        bid = self.app.generate_candidates("planner")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "approve")
        with self.assertRaises(ConflictError):
            self.app.review(bid, "reviewer_a", "second", "approve")

    def test_creator_cannot_review(self) -> None:
        bid = self.app.generate_candidates("planner")["batch_id"]
        with self.assertRaises(ConflictError):
            self.app.review(bid, "planner", "first", "approve")

    def test_duplicate_approval_rejected(self) -> None:
        bid = self.app.generate_candidates("planner")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "approve")
        with self.assertRaises(DuplicateApprovalError):
            self.app.review(bid, "reviewer_a", "first", "approve")

    def test_rejection_ends_flow(self) -> None:
        bid = self.app.generate_candidates("planner")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "reject",
                        comment="数据异常")
        view = self.app.batch_view(bid)
        self.assertEqual(view["status"], BATCH_REJECTED)
        # 驳回后任何审批都不再接受
        with self.assertRaises(ConflictError):
            self.app.review(bid, "reviewer_b", "second", "approve")
        # 未发布，公开视图无内容
        self.assertEqual(self.app.public_current("2026-09-25")["items"], [])

    def test_concurrent_final_approval_only_publishes_once(self) -> None:
        """两个第二阶段复核人并发提交：只有一人能成为第二审批，
        且区间合并只发生一次（不产生重复区间）。"""
        bid = self.app.generate_candidates("planner")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "approve")

        errors: list[Exception] = []

        def review_as(name: str) -> None:
            try:
                self.app.review(bid, name, "second", "approve")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t1 = threading.Thread(target=review_as, args=("reviewer_b",))
        t2 = threading.Thread(target=review_as, args=("reviewer_c",))
        t1.start(); t2.start(); t1.join(); t2.join()

        view = self.app.batch_view(bid)
        self.assertEqual(view["status"], BATCH_PUBLISHED)
        seconds = [a for a in view["approvals"] if a["stage"] == "second"]
        self.assertEqual(len(seconds), 1)
        # 恰好一人失败（重复/冲突），一人成功
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0],
                              (DuplicateApprovalError, ConflictError))
        intervals = self.app.repo.all_intervals()
        self.assertEqual(len(intervals), 1)


class EmergencyAndDeadlineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = clock_at("2026-09-25T10:00:00+00:00")
        self.app = make_app(Path(self.tmp.name), self.clock)
        seed_areas(self.app)
        make_heavy_sa001(self.app)

    def test_emergency_publishes_immediately_with_deadline(self) -> None:
        batch = self.app.emergency_publish("duty_officer", "突发大流量")
        self.assertEqual(batch["status"], BATCH_EMERGENCY)
        self.assertIsNotNone(batch["publish_deadline"])
        # 先发布：公开渠道立即可见
        current = self.app.public_current("2026-09-25")
        self.assertEqual(len(current["items"]), 1)

    def test_supplementary_review_completes_emergency(self) -> None:
        bid = self.app.emergency_publish("duty_officer", "x")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "approve")
        done = self.app.review(bid, "reviewer_b", "second", "approve")
        self.assertEqual(done["status"], BATCH_PUBLISHED)
        self.assertIsNotNone(done["reviewed_at"])

    def test_sweep_before_deadline_keeps(self) -> None:
        self.app.emergency_publish("duty_officer", "x")
        self.clock.advance(hours=23)
        self.assertEqual(self.app.run_deadline_sweep(), [])
        self.assertEqual(self.app.public_current("2026-09-26")["items"] != [],
                         True)

    def test_sweep_after_deadline_expires_and_retracts(self) -> None:
        bid = self.app.emergency_publish("duty_officer", "x")["batch_id"]
        self.clock.advance(hours=25)
        expired = self.app.run_deadline_sweep()
        self.assertEqual(expired, [bid])
        view = self.app.batch_view(bid)
        self.assertEqual(view["status"], BATCH_EXPIRED)
        # 其有效区间已撤回
        self.assertEqual(self.app.repo.open_intervals(), [])
        # 失效后不能再补审
        with self.assertRaises((DeadlinePassedError, ConflictError)):
            self.app.review(bid, "reviewer_a", "first", "approve")

    def test_process_recovery_runs_sweep_on_start(self) -> None:
        """模拟进程崩溃：库中留着超期紧急件，新服务对象启动 sweep 即收尾。"""
        bid = self.app.emergency_publish("duty_officer", "x")["batch_id"]
        # 推进真实时钟超过期限后，用新 AppService 指向同一数据库
        self.clock.advance(hours=25)
        recovered = make_app(Path(self.tmp.name), self.clock)
        expired = recovered.run_deadline_sweep()
        self.assertEqual(expired, [bid])
        self.assertEqual(recovered.public_current("2026-09-26")["items"], [])

    def test_emergency_rejection_retracts_intervals(self) -> None:
        bid = self.app.emergency_publish("duty_officer", "x")["batch_id"]
        self.app.review(bid, "reviewer_a", "first", "reject", comment="误报")
        self.assertEqual(self.app.batch_view(bid)["status"], BATCH_REVOKED)
        self.assertEqual(self.app.repo.open_intervals(), [])


class RevokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = clock_at("2026-09-25T10:00:00+00:00")
        self.app = make_app(Path(self.tmp.name), self.clock)
        seed_areas(self.app)

    def test_revoke_only_closes_open_intervals(self) -> None:
        # 9/25 发布 SA001 heavy
        make_heavy_sa001(self.app, "2026-09-25")
        bid1 = self.app.generate_candidates("planner")["batch_id"]
        approve_both(self.app, bid1)
        self.assertEqual(len(self.app.repo.open_intervals()), 1)

        # 9/26 SA001 指标回落到阈值之下 -> 常规发布关闭旧区间、不产生新区间
        from service_09251_008.timeutil import parse_iso
        self.clock.set(parse_iso("2026-09-26T09:00:00+00:00"))
        window_days(self.app, "SA001", "flow_saturation", "2026-09-26", 3, 0.50)
        bid2 = self.app.generate_candidates("planner", "2026-09-26")["batch_id"]
        approve_both(self.app, bid2)
        # 旧区间已自然结束（valid_to=2026-09-26），无开放区间
        self.assertEqual(self.app.repo.open_intervals(), [])

        # 撤销 9/25 的批次：只影响尚未结束的区间——已结束区间不受影响
        view = self.app.revoke(bid1, "chief", "信息更正")
        self.assertEqual(view["status"], BATCH_REVOKED)
        historical = self.app.rebuild_snapshot("2026-09-25")
        # 已结束区间仍然保留在历史中（撤销不抹除历史区间行）
        self.assertEqual(
            [i["area_id"] for i in historical["items"]], ["SA001"]
        )

    def test_revoke_current_publish_takes_effect_today(self) -> None:
        make_heavy_sa001(self.app, "2026-09-25")
        bid = self.app.generate_candidates("planner")["batch_id"]
        approve_both(self.app, bid)
        self.app.revoke(bid, "chief", "错误发布")
        self.assertEqual(self.app.public_current("2026-09-25")["items"], [])
        with self.assertRaises(ConflictError):
            self.app.revoke(bid, "chief", "再次撤销")


class CrossDayValidityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = clock_at("2026-09-25T08:00:00+00:00")
        self.app = make_app(Path(self.tmp.name), self.clock)
        seed_areas(self.app)

    def test_same_level_is_continuous_across_day(self) -> None:
        # 9/25 与 9/26 均为 heavy：按批次分段但公开有效期无缝连续
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.95)
        bid = self.app.generate_candidates("planner", "2026-09-25")["batch_id"]
        approve_both(self.app, bid)

        from service_09251_008.timeutil import parse_iso
        self.clock.set(parse_iso("2026-09-26T08:00:00+00:00"))
        window_days(self.app, "SA001", "flow_saturation", "2026-09-26", 3, 0.95)
        bid2 = self.app.generate_candidates("planner", "2026-09-26")["batch_id"]
        approve_both(self.app, bid2)

        intervals = sorted(self.app.repo.all_intervals(),
                           key=lambda r: r["valid_from"])
        self.assertEqual(len(intervals), 2)
        # 前一段在 9/26 关闭，后一段从 9/26 开放：半开区间无缝衔接
        self.assertEqual(intervals[0]["valid_to"], "2026-09-26")
        self.assertEqual(intervals[1]["valid_from"], "2026-09-26")
        self.assertIsNone(intervals[1]["valid_to"])
        # 跨日查询：9/25、9/26 均可见 heavy
        self.assertEqual(
            self.app.public_current("2026-09-25")["items"][0]["level"], "heavy"
        )
        self.assertEqual(
            self.app.public_current("2026-09-26")["items"][0]["level"], "heavy"
        )
        # 9/24 生效前不可见
        self.assertEqual(self.app.public_current("2026-09-24")["items"], [])

    def test_revoking_old_segment_keeps_later_segment(self) -> None:
        """撤销早期已分段的批次，不影响后续批次覆盖的时段。"""
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.95)
        bid = self.app.generate_candidates("planner", "2026-09-25")["batch_id"]
        approve_both(self.app, bid)

        from service_09251_008.timeutil import parse_iso
        self.clock.set(parse_iso("2026-09-26T08:00:00+00:00"))
        window_days(self.app, "SA001", "flow_saturation", "2026-09-26", 3, 0.95)
        bid2 = self.app.generate_candidates("planner", "2026-09-26")["batch_id"]
        approve_both(self.app, bid2)

        # bid1 的区间已结束，撤销它不改变任何当前/历史公开结论
        self.app.revoke(bid, "chief", "旧批次信息更正")
        self.assertEqual(
            self.app.public_current("2026-09-26")["items"][0]["level"], "heavy"
        )
        self.assertEqual(
            self.app.rebuild_snapshot("2026-09-25")["items"][0]["level"], "heavy"
        )

    def test_level_change_closes_and_opens(self) -> None:
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.95)
        bid = self.app.generate_candidates("planner", "2026-09-25")["batch_id"]
        approve_both(self.app, bid)

        from service_09251_008.timeutil import parse_iso
        self.clock.set(parse_iso("2026-09-26T08:00:00+00:00"))
        window_days(self.app, "SA001", "flow_saturation", "2026-09-26", 3, 0.82)
        bid2 = self.app.generate_candidates("planner", "2026-09-26")["batch_id"]
        approve_both(self.app, bid2)

        intervals = sorted(self.app.repo.all_intervals(),
                           key=lambda r: r["valid_from"])
        self.assertEqual(len(intervals), 2)
        self.assertEqual(intervals[0]["valid_to"], "2026-09-26")
        self.assertIsNone(intervals[1]["valid_to"])
        # 历史日重建看到旧档，当日看到新档
        snap25 = self.app.rebuild_snapshot("2026-09-25")
        self.assertEqual(snap25["items"][0]["level"], "heavy")
        snap26 = self.app.rebuild_snapshot("2026-09-26")
        self.assertEqual(snap26["items"][0]["level"], "busy")

    def test_history_rebuild_with_different_rule_version(self) -> None:
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.82)
        # 创建更严格的 v2：busy 阈值提高到 0.90，则 0.82 不再入选
        self.app.create_rule_version({
            "version": "v2",
            "require_all": False,
            "metrics": [{
                "key": "flow_saturation", "label": "流量饱和度",
                "window_days": 3, "busy_at": 0.90, "heavy_at": 0.95,
                "extreme_at": 0.99, "higher_is_busier": True,
            }],
        }, "planner")
        rebuilt_v1 = self.app.rebuild_candidate("2026-09-25", "v1")
        self.assertEqual(len(rebuilt_v1["items"]), 1)
        rebuilt_v2 = self.app.rebuild_candidate("2026-09-25", "v2")
        self.assertEqual(rebuilt_v2["items"], [])

    def test_activating_rule_version_changes_new_candidates(self) -> None:
        """规则切换：v1 下入选，激活更严格的 v2 后新候选不再入选，
        且任何时刻只有一个 active 版本。"""
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.82)
        before = self.app.generate_candidates("planner", "2026-09-25")
        self.assertEqual(before["rule_version"], "v1")
        self.assertEqual(len(before["payload"]["items"]), 1)

        self.app.create_rule_version({
            "version": "v2",
            "require_all": False,
            "metrics": [{
                "key": "flow_saturation", "label": "流量饱和度",
                "window_days": 3, "busy_at": 0.90, "heavy_at": 0.95,
                "extreme_at": 0.99, "higher_is_busier": True,
            }],
        }, "planner")
        self.app.activate_rule("v2", "admin")

        versions = {r["version"]: r["status"]
                    for r in self.app.list_rule_versions()}
        self.assertEqual(versions["v2"], "active")
        self.assertEqual(versions["v1"], "retired")

        after = self.app.generate_candidates("planner", "2026-09-25")
        self.assertEqual(after["rule_version"], "v2")
        self.assertEqual(after["payload"]["items"], [])


if __name__ == "__main__":
    unittest.main()
