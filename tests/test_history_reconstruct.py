"""历史重建：基线版本 + 紧急叠加的合并语义，任意时刻可重放。"""
import tempfile
import unittest

from service_09251_008.domain import parse_ts

from support import drive_to_published, make_service, observe, seed_metrics_and_rules, seed_period


class HistoryReconstructTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name, start="2026-10-01T08:00:00Z")
        seed_metrics_and_rules(self.service)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _publish_normal(self, name, start, end, observations):
        period = seed_period(self.service, start, end, name=name)
        for area, occupancy, at in observations:
            observe(self.service, period["id"], area, occupancy, at)
        return drive_to_published(self.service, period["id"])

    def test_baseline_plus_emergency_overlay(self) -> None:
        # 基线：正常发布 A、B 两个繁忙服务区
        self._publish_normal(
            "第一观察期",
            "2026-10-01T00:00:00Z",
            "2026-10-05T00:00:00Z",
            [("G60-杭州湾", 80, "2026-10-01T10:00:00Z"), ("G15-宁波北", 70, "2026-10-01T10:00:00Z")],
        )
        # 紧急升级：叠加 C
        self.clock.set(parse_ts("2026-10-02T09:00:00Z"))
        self.service.emergency_upgrade(
            "ops",
            "突发事故",
            [{"area_code": "G25-湖州", "grade": "L3",
              "valid_from": "2026-10-02T09:00:00Z", "valid_to": "2026-10-04T00:00:00Z"}],
        )

        current = self.service.public_current()
        self.assertEqual(
            [e["area_code"] for e in current["entries"]],
            ["G15-宁波北", "G25-湖州", "G60-杭州湾"],
        )

        # 紧急升级之前的时刻只能看到基线条目
        earlier = self.service.reconstruct("2026-10-01T12:00:00Z")
        self.assertEqual([e["area_code"] for e in earlier["entries"]], ["G15-宁波北", "G60-杭州湾"])

    def test_new_normal_version_resets_baseline(self) -> None:
        self._publish_normal(
            "第一观察期",
            "2026-10-01T00:00:00Z",
            "2026-10-05T00:00:00Z",
            [("G60-杭州湾", 80, "2026-10-01T10:00:00Z"), ("G15-宁波北", 70, "2026-10-01T10:00:00Z")],
        )
        self.clock.set(parse_ts("2026-10-02T09:00:00Z"))
        self.service.emergency_upgrade(
            "ops",
            "突发事故",
            [{"area_code": "G25-湖州", "grade": "L3",
              "valid_from": "2026-10-02T09:00:00Z", "valid_to": "2026-10-04T00:00:00Z"}],
        )
        # 新的正常发布只包含 A：B 与紧急叠加的 C 都被新基线取代
        self.clock.set(parse_ts("2026-10-03T08:00:00Z"))
        self._publish_normal(
            "第二观察期",
            "2026-10-03T00:00:00Z",
            "2026-10-06T00:00:00Z",
            [("G60-杭州湾", 95, "2026-10-03T10:00:00Z")],
        )

        current = self.service.public_current()
        self.assertEqual([e["area_code"] for e in current["entries"]], ["G60-杭州湾"])
        self.assertEqual(current["entries"][0]["grade"], "L3")

        # 但历史时刻仍可完整重放
        before = self.service.reconstruct("2026-10-02T10:00:00Z")
        self.assertEqual(
            [e["area_code"] for e in before["entries"]],
            ["G15-宁波北", "G25-湖州", "G60-杭州湾"],
        )

    def test_retracted_emergency_disappears_from_current(self) -> None:
        self._publish_normal(
            "第一观察期",
            "2026-10-01T00:00:00Z",
            "2026-10-05T00:00:00Z",
            [("G60-杭州湾", 80, "2026-10-01T10:00:00Z")],
        )
        self.clock.set(parse_ts("2026-10-02T09:00:00Z"))
        emergency = self.service.emergency_upgrade(
            "ops",
            "突发事故",
            [{"area_code": "G25-湖州", "grade": "L3",
              "valid_from": "2026-10-02T09:00:00Z", "valid_to": "2026-10-04T00:00:00Z"}],
        )
        self.assertEqual(len(self.service.public_current()["entries"]), 2)

        self.clock.set(parse_ts("2026-10-02T10:00:00Z"))
        self.service.review_emergency("alice", emergency["id"], "REJECT")
        current = self.service.public_current()
        self.assertEqual([e["area_code"] for e in current["entries"]], ["G60-杭州湾"])

        # 撤回前的历史时刻仍能看到紧急条目
        before = self.service.reconstruct("2026-10-02T09:30:00Z")
        self.assertIn("G25-湖州", [e["area_code"] for e in before["entries"]])

    def test_reconstruct_before_any_publish_is_empty(self) -> None:
        snapshot = self.service.reconstruct("2026-09-01T00:00:00Z")
        self.assertEqual(snapshot["entries"], [])
        self.assertIsNone(snapshot["basis_version_no"])


if __name__ == "__main__":
    unittest.main()
