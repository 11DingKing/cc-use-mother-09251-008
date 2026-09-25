"""跨日有效期：观察期与公开有效期跨越午夜时的可见性。"""
import tempfile
import unittest

from service_09251_008.domain import parse_ts
from service_09251_008.errors import DomainError

from support import drive_to_published, make_service, observe, seed_metrics_and_rules, seed_period

# 跨日观察期：10 月 1 日晚 8 点 → 10 月 2 日凌晨 4 点
PERIOD_START = "2026-10-01T20:00:00Z"
PERIOD_END = "2026-10-02T04:00:00Z"


class CrossDayValidityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name, start="2026-10-01T21:00:00Z")
        seed_metrics_and_rules(self.service)
        self.period = seed_period(self.service, PERIOD_START, PERIOD_END, name="国庆跨日高峰")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_observation_window_spans_midnight(self) -> None:
        observe(self.service, self.period["id"], "G60-杭州湾", 92, "2026-10-01T23:30:00Z")
        observe(self.service, self.period["id"], "G15-宁波北", 80, "2026-10-02T02:30:00Z")
        candidate = self.service.generate_candidate_list("ops", self.period["id"])
        grades = {e["area_code"]: e["grade"] for e in candidate["entries"]}
        self.assertEqual(grades, {"G60-杭州湾": "L3", "G15-宁波北": "L2"})

    def test_observation_outside_window_rejected(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            observe(self.service, self.period["id"], "G60-杭州湾", 92, "2026-10-02T05:00:00Z")
        self.assertEqual(ctx.exception.code, "VALIDATION")

    def test_entry_visible_across_midnight(self) -> None:
        observe(self.service, self.period["id"], "G60-杭州湾", 92, "2026-10-01T23:30:00Z")
        drive_to_published(self.service, self.period["id"])  # 默认有效期 = 观察期窗口

        evening = self.service.reconstruct("2026-10-01T23:00:00Z")
        self.assertEqual([e["area_code"] for e in evening["entries"]], ["G60-杭州湾"])

        after_midnight = self.service.reconstruct("2026-10-02T02:00:00Z")
        self.assertEqual([e["area_code"] for e in after_midnight["entries"]], ["G60-杭州湾"])

        morning = self.service.reconstruct("2026-10-02T05:00:00Z")
        self.assertEqual(morning["entries"], [])

    def test_revocation_after_midnight_keeps_evening_history(self) -> None:
        observe(self.service, self.period["id"], "G60-杭州湾", 92, "2026-10-01T23:30:00Z")
        version = drive_to_published(self.service, self.period["id"])

        self.clock.set(parse_ts("2026-10-02T00:30:00Z"))
        self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")

        # 撤销前（跨日前的晚间）历史不变；撤销后（跨日后的凌晨）不再可见
        evening = self.service.reconstruct("2026-10-01T23:00:00Z")
        self.assertEqual([e["area_code"] for e in evening["entries"]], ["G60-杭州湾"])
        early = self.service.reconstruct("2026-10-02T01:00:00Z")
        self.assertEqual(early["entries"], [])


if __name__ == "__main__":
    unittest.main()
