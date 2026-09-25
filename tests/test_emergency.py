"""紧急升级：先发布后补审、期限自动追踪、否决撤回。"""
import tempfile
import unittest

from service_09251_008.errors import DomainError

from support import make_service


class EmergencyUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name, start="2026-10-01T00:00:00Z")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _upgrade(self, area="G60-杭州湾", valid_from="2026-10-01T00:00:00Z", valid_to="2026-10-03T00:00:00Z"):
        return self.service.emergency_upgrade(
            "ops",
            "突发大流量",
            [{"area_code": area, "grade": "L3", "valid_from": valid_from, "valid_to": valid_to}],
        )

    def test_publish_first_review_later(self) -> None:
        emergency = self._upgrade()
        self.assertEqual(emergency["status"], "PENDING_REVIEW")
        self.assertEqual(emergency["review_deadline"], "2026-10-02T00:00:00.000+00:00")

        # 发布即对公众可见，无需等待补审
        current = self.service.public_current()
        self.assertEqual([e["area_code"] for e in current["entries"]], ["G60-杭州湾"])
        self.assertEqual(current["entries"][0]["label"], "重度繁忙")

    def test_deadline_is_tracked_automatically(self) -> None:
        emergency = self._upgrade()
        self.clock.advance(hours=25)  # 超过 24 小时补审期限

        swept = self.service.sweep_emergencies()
        self.assertEqual(swept["marked_overdue"], [emergency["id"]])
        self.assertEqual(self.service.get_emergency("ops", emergency["id"])["status"], "OVERDUE")

        # 再次扫描是幂等的
        self.assertEqual(self.service.sweep_emergencies()["marked_overdue"], [])

    def test_listing_lazily_tracks_deadline(self) -> None:
        emergency = self._upgrade()
        self.clock.advance(hours=30)
        listed = self.service.list_emergencies("ops")
        self.assertEqual(listed[0]["status"], "OVERDUE")
        self.assertEqual(listed[0]["id"], emergency["id"])

    def test_dual_post_review_confirms(self) -> None:
        emergency = self._upgrade()
        self.service.review_emergency("alice", emergency["id"], "APPROVE")
        self.assertEqual(self.service.get_emergency("ops", emergency["id"])["status"], "PENDING_REVIEW")
        confirmed = self.service.review_emergency("bob", emergency["id"], "APPROVE")
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertIsNotNone(confirmed["closed_at"])

    def test_late_review_after_overdue_still_allowed(self) -> None:
        emergency = self._upgrade()
        self.clock.advance(hours=30)
        self.service.sweep_emergencies()
        self.service.review_emergency("alice", emergency["id"], "APPROVE")
        confirmed = self.service.review_emergency("bob", emergency["id"], "APPROVE")
        self.assertEqual(confirmed["status"], "CONFIRMED")

    def test_duplicate_post_review_rejected(self) -> None:
        emergency = self._upgrade()
        self.service.review_emergency("alice", emergency["id"], "APPROVE")
        with self.assertRaises(DomainError) as ctx:
            self.service.review_emergency("alice", emergency["id"], "APPROVE")
        self.assertEqual(ctx.exception.code, "DUPLICATE_REVIEW")

    def test_creator_cannot_post_review(self) -> None:
        emergency = self._upgrade()
        with self.assertRaises(DomainError) as ctx:
            self.service.review_emergency("ops", emergency["id"], "APPROVE")
        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_reject_retracts_open_entries_only(self) -> None:
        # 一个仍在有效期内的条目 + 一个已经结束的条目
        emergency = self.service.emergency_upgrade(
            "ops",
            "突发大流量",
            [
                {"area_code": "G60-杭州湾", "grade": "L3",
                 "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-10-03T00:00:00Z"},
                {"area_code": "G15-宁波北", "grade": "L2",
                 "valid_from": "2026-09-30T00:00:00Z", "valid_to": "2026-09-30T12:00:00Z"},
            ],
        )
        self.clock.advance(hours=2)
        retracted = self.service.review_emergency("alice", emergency["id"], "REJECT")
        self.assertEqual(retracted["status"], "RETRACTED")

        version = self.service.get_public_version("ops", emergency["version_no"])
        by_area = {e["area_code"]: e for e in version["entries"]}
        # 未结束的区间被截断；已结束的区间保持原样（撤销不影响历史）
        self.assertIsNotNone(by_area["G60-杭州湾"]["revoked_at"])
        self.assertIsNone(by_area["G15-宁波北"]["revoked_at"])

        current = self.service.public_current()
        self.assertNotIn("G60-杭州湾", [e["area_code"] for e in current["entries"]])


if __name__ == "__main__":
    unittest.main()
