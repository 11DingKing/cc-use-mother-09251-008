"""进程恢复：重启后期限追踪补做、在途流程可继续、审计链保持完整。"""
import tempfile
import unittest

from support import (
    drive_to_approved,
    make_service,
    observe,
    reopen_service,
    seed_metrics_and_rules,
    seed_period,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name, start="2026-10-01T00:00:00Z")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_restart_tracks_overdue_and_continues_inflight_work(self) -> None:
        seed_metrics_and_rules(self.service)
        period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
        observe(self.service, period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")

        # 在途流程：候选清单已提交、一人已复核；另有一笔紧急升级等待补审
        candidate = self.service.generate_candidate_list("ops", period["id"])
        self.service.submit_candidate_list("ops", candidate["id"])
        self.service.review_candidate_list("alice", candidate["id"], "APPROVE")
        emergency = self.service.emergency_upgrade(
            "ops",
            "突发大流量",
            [{"area_code": "G15-宁波北", "grade": "L3",
              "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-03T00:00:00Z"}],
        )

        # 进程"崩溃"：时钟推进 25 小时，超过补审期限
        self.clock.advance(hours=25)

        # 重启：新实例在同一数据库上恢复
        recovered = reopen_service(self.service, self.tmp.name, self.clock)
        report = recovered.recover()
        self.assertEqual(report["overdue_marked"], [emergency["id"]])
        self.assertTrue(report["audit_valid"])
        self.assertEqual(recovered.get_emergency("ops", emergency["id"])["status"], "OVERDUE")

        # 在途的复核流程可以从断点继续，最终完成发布
        still_in_review = recovered.get_candidate_list("ops", candidate["id"])
        self.assertEqual(still_in_review["status"], "IN_REVIEW")
        self.assertEqual(len(still_in_review["reviews"]), 1)
        recovered.review_candidate_list("bob", candidate["id"], "APPROVE")
        published = recovered.publish_candidate_list("ops", candidate["id"])
        self.assertEqual(published["version_no"], 2)  # 1 号是紧急升级版本

        # 恢复后补做的状态迁移也记录在审计链中，链仍然完整
        chain = recovered.verify_audit_chain()
        self.assertTrue(chain["valid"])
        actions = [event["action"] for event in recovered.repo.audit_events()]
        self.assertIn("emergency.overdue", actions)
        self.assertIn("recovery.completed", actions)

        # 恢复是幂等的：再次恢复不再重复标记
        second = recovered.recover()
        self.assertEqual(second["overdue_marked"], [])

    def test_recover_on_fresh_database(self) -> None:
        report = self.service.recover()
        self.assertTrue(report["audit_valid"])
        self.assertEqual(report["overdue_marked"], [])


if __name__ == "__main__":
    unittest.main()
