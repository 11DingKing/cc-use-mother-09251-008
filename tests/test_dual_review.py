"""双人复核：赞成/否决、重复审批、自审禁止与发布门槛。"""
import tempfile
import unittest

from service_09251_008.errors import DomainError

from support import make_service, observe, seed_metrics_and_rules, seed_period


class DualReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.service, self.clock = make_service(self.tmp.name)
        seed_metrics_and_rules(self.service)
        self.period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-02T00:00:00Z")
        observe(self.service, self.period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")
        candidate = self.service.generate_candidate_list("ops", self.period["id"])
        self.service.submit_candidate_list("ops", candidate["id"])
        self.list_id = candidate["id"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_two_distinct_reviewers_required(self) -> None:
        after_first = self.service.review_candidate_list("alice", self.list_id, "APPROVE")
        self.assertEqual(after_first["status"], "IN_REVIEW")

        after_second = self.service.review_candidate_list("bob", self.list_id, "APPROVE")
        self.assertEqual(after_second["status"], "APPROVED")
        self.assertEqual([r["reviewer"] for r in after_second["reviews"]], ["alice", "bob"])

    def test_duplicate_review_is_rejected(self) -> None:
        self.service.review_candidate_list("alice", self.list_id, "APPROVE")
        with self.assertRaises(DomainError) as ctx:
            self.service.review_candidate_list("alice", self.list_id, "APPROVE")
        self.assertEqual(ctx.exception.code, "DUPLICATE_REVIEW")
        self.assertEqual(ctx.exception.http_status, 409)
        # 重复审批不影响已记录的复核结论
        candidate = self.service.get_candidate_list("ops", self.list_id)
        self.assertEqual(len(candidate["reviews"]), 1)

    def test_creator_cannot_review_own_list(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.review_candidate_list("ops", self.list_id, "APPROVE")
        self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_reject_is_terminal(self) -> None:
        rejected = self.service.review_candidate_list("alice", self.list_id, "REJECT")
        self.assertEqual(rejected["status"], "REJECTED")
        with self.assertRaises(DomainError) as ctx:
            self.service.review_candidate_list("bob", self.list_id, "APPROVE")
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

    def test_publish_requires_dual_approval(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.publish_candidate_list("ops", self.list_id)
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")

        self.service.review_candidate_list("alice", self.list_id, "APPROVE")
        with self.assertRaises(DomainError):
            self.service.publish_candidate_list("ops", self.list_id)

    def test_submit_twice_fails(self) -> None:
        with self.assertRaises(DomainError) as ctx:
            self.service.submit_candidate_list("ops", self.list_id)
        self.assertEqual(ctx.exception.code, "STATE_CONFLICT")


if __name__ == "__main__":
    unittest.main()
