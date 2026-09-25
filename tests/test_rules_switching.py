"""规则切换：规则版本启用/退役、求值结果变化与历史清单的可追溯性。"""
import tempfile
import unittest

from service_09251_008.errors import DomainError

from support import make_service, observe, seed_metrics_and_rules, seed_period


class RuleSwitchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name)
        self.metric_version, self.rule_v1 = seed_metrics_and_rules(self.service)
        self.period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_evaluation_records_triggered_metrics(self) -> None:
        observe(self.service, self.period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")
        observe(self.service, self.period["id"], "G60-嘉兴", 50, "2026-10-01T10:00:00Z")
        candidate = self.service.generate_candidate_list("ops", self.period["id"])

        self.assertEqual(candidate["rule_version_id"], self.rule_v1["id"])
        by_area = {entry["area_code"]: entry for entry in candidate["entries"]}
        self.assertIn("G60-杭州湾", by_area)
        self.assertNotIn("G60-嘉兴", by_area)  # 未命中任何等级
        entry = by_area["G60-杭州湾"]
        self.assertEqual(entry["grade"], "L2")
        self.assertEqual(entry["source"], "AUTO")
        # 地方管理人员需要看到本次分级由哪些指标触发
        self.assertEqual(
            entry["triggered"],
            [{"metric": "occupancy", "op": ">=", "threshold": 75.0, "value": 80.0}],
        )

    def test_rule_switch_changes_future_lists_but_keeps_history(self) -> None:
        observe(self.service, self.period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")
        first = self.service.generate_candidate_list("ops", self.period["id"])
        self.assertEqual(len(first["entries"]), 1)

        # 切换为更严格的规则版本：L2 需要占用率 >= 85
        stricter = [
            {"grade": "L3", "all": [{"metric": "occupancy", "op": ">=", "value": 95}]},
            {"grade": "L2", "all": [{"metric": "occupancy", "op": ">=", "value": 85}]},
        ]
        rule_v2 = self.service.create_rule_version("ops", self.metric_version["id"], stricter)
        self.service.activate_rule_version("ops", rule_v2["id"])

        versions = {row["id"]: row["status"] for row in self.service.list_rule_versions("ops")}
        self.assertEqual(versions[self.rule_v1["id"]], "RETIRED")
        self.assertEqual(versions[rule_v2["id"]], "ACTIVE")

        second = self.service.generate_candidate_list("ops", self.period["id"])
        self.assertEqual(second["rule_version_id"], rule_v2["id"])
        self.assertEqual(second["entries"], [])  # 80 < 85，不再命中

        # 旧清单被取代，但仍完整保留其规则版本与触发指标，供历史追溯
        old = self.service.get_candidate_list("ops", first["id"])
        self.assertEqual(old["status"], "SUPERSEDED")
        self.assertEqual(old["rule_version_id"], self.rule_v1["id"])
        self.assertEqual(old["entries"][0]["grade"], "L2")

    def test_rule_validation(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.create_rule_version(
                "ops",
                self.metric_version["id"],
                [{"grade": "L2", "all": [{"metric": "unknown", "op": ">=", "value": 1}]}],
            )
        self.assertEqual(ctx.exception.code, "VALIDATION")

        with self.assertRaises(DomainError):
            self.service.create_rule_version(
                "ops",
                self.metric_version["id"],
                [
                    {"grade": "L2", "all": [{"metric": "occupancy", "op": ">=", "value": 1}]},
                    {"grade": "L2", "all": [{"metric": "occupancy", "op": ">=", "value": 2}]},
                ],
            )

    def test_generate_without_active_rule_fails(self) -> None:
        service, _ = make_service(tempfile.mkdtemp())
        period = seed_period(service, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        with self.assertRaises(DomainError) as ctx:
            service.generate_candidate_list("ops", period["id"])
        self.assertEqual(ctx.exception.code, "NO_ACTIVE_RULE_VERSION")


if __name__ == "__main__":
    unittest.main()
