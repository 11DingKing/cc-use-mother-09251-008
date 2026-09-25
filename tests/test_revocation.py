"""撤销：只影响尚未结束的有效区间，历史部分保持不变。"""
import tempfile
import unittest

from service_09251_008.domain import parse_ts
from service_09251_008.errors import DomainError

from support import drive_to_published, make_service, observe, seed_metrics_and_rules, seed_period

# 撤销动作发生的"当前"时间
NOW = "2026-10-02T00:00:00Z"


class RevocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        # 发布发生在有效期开始之前，保证历史时刻可重建
        self.service, self.clock = make_service(self.tmp.name, start="2026-09-27T00:00:00Z")
        seed_metrics_and_rules(self.service)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _published(self, area="G60-杭州湾", valid_from="2026-10-01T00:00:00Z", valid_to="2026-10-03T00:00:00Z"):
        period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
        observe(self.service, period["id"], area, 80, "2026-10-01T10:00:00Z")
        version = drive_to_published(self.service, period["id"], valid_from=valid_from, valid_to=valid_to)
        self.clock.set(parse_ts(NOW))  # 推进到撤销动作发生的时刻
        return version

    def test_revoke_active_interval_truncates_from_now(self) -> None:
        version = self._published()
        revoked = self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")
        entry = revoked["entries"][0]
        self.assertEqual(entry["revoked_at"], "2026-10-02T00:00:00.000+00:00")
        self.assertEqual(entry["revoked_by"], "ops")
        # 原始区间不被改写，撤销作为事件记录
        self.assertEqual(entry["valid_from"], "2026-10-01T00:00:00.000+00:00")
        self.assertEqual(entry["valid_to"], "2026-10-03T00:00:00.000+00:00")

        # 撤销前的时间点仍能看到条目（历史不变），撤销后不可见
        before = self.service.reconstruct("2026-10-01T12:00:00Z")
        self.assertEqual([e["area_code"] for e in before["entries"]], ["G60-杭州湾"])
        after = self.service.reconstruct("2026-10-02T12:00:00Z")
        self.assertEqual(after["entries"], [])

    def test_revoke_future_interval_cancels_it(self) -> None:
        version = self._published(valid_from="2026-10-05T00:00:00Z", valid_to="2026-10-06T00:00:00Z")
        self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")
        future = self.service.reconstruct("2026-10-05T12:00:00Z")
        self.assertEqual(future["entries"], [])

    def test_revoke_closed_interval_is_rejected(self) -> None:
        version = self._published(valid_from="2026-09-28T00:00:00Z", valid_to="2026-09-30T00:00:00Z")
        with self.assertRaises(DomainError) as ctx:
            self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")
        self.assertEqual(ctx.exception.code, "INTERVAL_CLOSED")
        # 已结束区间的历史视图不受撤销尝试影响
        history = self.service.reconstruct("2026-09-29T00:00:00Z")
        self.assertEqual([e["area_code"] for e in history["entries"]], ["G60-杭州湾"])

    def test_double_revoke_is_rejected(self) -> None:
        version = self._published()
        self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")
        with self.assertRaises(DomainError) as ctx:
            self.service.revoke_public_entry("ops", version["version_no"], "G60-杭州湾")
        self.assertEqual(ctx.exception.code, "ALREADY_REVOKED")

    def test_revoke_missing_entry(self) -> None:
        version = self._published()
        with self.assertRaises(DomainError) as ctx:
            self.service.revoke_public_entry("ops", version["version_no"], "不存在的服务区")
        self.assertEqual(ctx.exception.code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
