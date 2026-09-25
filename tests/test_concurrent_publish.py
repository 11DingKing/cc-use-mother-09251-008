"""并发发布：同一清单只能发布一次，不同清单的版本号唯一且连续。"""
import tempfile
import threading
import unittest

from service_09251_008.errors import DomainError

from support import drive_to_approved, make_service, observe, seed_metrics_and_rules, seed_period


class ConcurrentPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name)
        seed_metrics_and_rules(self.service)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _approved_list(self, name: str, start: str, end: str, area: str, occupancy: float, at: str) -> dict:
        period = seed_period(self.service, start, end, name=name)
        observe(self.service, period["id"], area, occupancy, at)
        return drive_to_approved(self.service, period["id"])

    def _run_parallel(self, tasks):
        barrier = threading.Barrier(len(tasks))
        results: list = [None] * len(tasks)
        errors: list = [None] * len(tasks)

        def worker(index, fn):
            barrier.wait(timeout=10)
            try:
                results[index] = fn()
            except DomainError as err:  # 预期的业务冲突
                errors[index] = err

        threads = [threading.Thread(target=worker, args=(i, fn)) for i, fn in enumerate(tasks)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results, errors

    def test_same_list_published_once_under_concurrency(self) -> None:
        approved = self._approved_list(
            "同一清单", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "G60-杭州湾", 80, "2026-10-01T10:00:00Z"
        )
        tasks = [
            lambda: self.service.publish_candidate_list("ops", approved["id"]),
            lambda: self.service.publish_candidate_list("ops", approved["id"]),
        ]
        results, errors = self._run_parallel(tasks)

        successes = [r for r in results if r is not None]
        failures = [e for e in errors if e is not None]
        self.assertEqual(len(successes), 1, "并发发布同一清单只能成功一次")
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "STATE_CONFLICT")
        self.assertEqual(len(self.service.list_public_versions("ops")), 1)

    def test_distinct_lists_get_unique_version_numbers(self) -> None:
        first = self._approved_list(
            "清单A", "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z", "G60-杭州湾", 80, "2026-10-01T10:00:00Z"
        )
        second = self._approved_list(
            "清单B", "2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z", "G15-宁波北", 95, "2026-10-02T10:00:00Z"
        )
        tasks = [
            lambda: self.service.publish_candidate_list("ops", first["id"]),
            lambda: self.service.publish_candidate_list("ops", second["id"]),
        ]
        results, errors = self._run_parallel(tasks)

        self.assertTrue(all(e is None for e in errors), f"不同清单的并发发布不应冲突: {errors}")
        version_numbers = sorted(r["version_no"] for r in results)
        self.assertEqual(version_numbers, [1, 2], "版本号必须在并发下保持唯一且连续")

    def test_same_period_cannot_be_published_twice(self) -> None:
        period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        observe(self.service, period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")
        first = drive_to_approved(self.service, period["id"])
        self.service.publish_candidate_list("ops", first["id"])

        # 重新生成同观察期的清单并走完全部复核流程，仍不能重复发布
        second = drive_to_approved(self.service, period["id"])
        with self.assertRaises(DomainError) as ctx:
            self.service.publish_candidate_list("ops", second["id"])
        self.assertEqual(ctx.exception.code, "ALREADY_PUBLISHED")


if __name__ == "__main__":
    unittest.main()
