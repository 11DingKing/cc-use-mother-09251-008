"""HTTP API：两套密钥的权限隔离、越界访问、端到端发布与签名信封。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

from service_09251_008.api import ApiServer
from service_09251_008.services import AppService
from service_09251_008.storage import Repository
from service_09251_008.timeutil import parse_iso

from support import clock_at, http_request, seed_areas, window_days


class ApiServerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = clock_at()
        self.db_path = str(Path(self.tmp.name) / "busy.db")
        repo = Repository(self.db_path)
        # sweep_interval=0 关闭后台线程，测试中手动控制时钟
        self.server = ApiServer(
            repo, signing_secret="api-secret", clock=self.clock,
            sweep_interval_seconds=0,
        )
        httpd = self.server.start("127.0.0.1", 0)
        self.port = httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

        self.internal_keys: dict[str, str] = {}
        self.public_keys: dict[str, str] = {}
        self._issue("internal", "planner")
        self._issue("internal", "reviewer_a")
        self._issue("internal", "reviewer_b")
        self._issue("public", "channel-x")

    def _stop(self) -> None:
        self.server.shutdown()

    def _issue(self, scope: str, label: str) -> None:
        import secrets

        token = f"{scope}_{secrets.token_urlsafe(16)}"
        Repository(self.db_path).insert_api_key(
            token, scope, label, "2026-09-25T10:00:00Z"
        )
        (self.internal_keys if scope == "internal" else self.public_keys)[
            label
        ] = token

    def req(self, method, path, token=None, body=None):
        return http_request(self.port, method, path, token, body)


class AuthIsolationTests(ApiServerTestBase):
    def test_health_is_open(self) -> None:
        status, _ = self.req("GET", "/health")
        self.assertEqual(status, 200)

    def test_missing_token_unauthorized(self) -> None:
        status, body = self.req("GET", "/internal/batches")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_bad_token_unauthorized(self) -> None:
        status, _ = self.req("GET", "/internal/batches", token="garbage")
        self.assertEqual(status, 401)

    def test_public_key_cannot_reach_internal(self) -> None:
        token = self.public_keys["channel-x"]
        status, body = self.req("POST", "/internal/candidates",
                                token=token, body={})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        status, _ = self.req("GET", "/internal/audit", token=token)
        self.assertEqual(status, 403)

    def test_internal_key_cannot_reach_public(self) -> None:
        token = self.internal_keys["planner"]
        status, body = self.req("GET", "/public/current", token=token)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")

    def test_unknown_route_returns_404(self) -> None:
        token = self.internal_keys["planner"]
        status, _ = self.req("GET", "/internal/nope", token=token)
        self.assertEqual(status, 404)


class EndToEndApiTests(ApiServerTestBase):
    def _seed(self) -> None:
        app = AppService(Repository(self.db_path), clock=self.clock,
                         signing_secret="api-secret")
        seed_areas(app)
        window_days(app, "SA001", "flow_saturation", "2026-09-25", 3, 0.95)

    def test_full_publish_flow_over_http(self) -> None:
        self._seed()
        planner = self.internal_keys["planner"]
        ra = self.internal_keys["reviewer_a"]
        rb = self.internal_keys["reviewer_b"]
        pub = self.public_keys["channel-x"]

        status, body = self.req("POST", "/internal/areas", token=planner,
                                body={"area_id": "SA001", "name": "阳澄湖"})
        # 区域已由 seed 写入，upsert 幂等
        self.assertIn(status, (200, 201))

        status, batch = self.req("POST", "/internal/candidates",
                                 token=planner, body={})
        self.assertEqual(status, 201)
        bid = batch["batch_id"]

        # 公众在发布前看不到
        status, current = self.req("GET", "/public/current", token=pub)
        self.assertEqual(current["items"], [])

        status, _ = self.req(
            "POST", f"/internal/batches/{bid}/review", token=ra,
            body={"stage": "first", "decision": "approve"})
        self.assertEqual(status, 200)
        status, _ = self.req(
            "POST", f"/internal/batches/{bid}/review", token=rb,
            body={"stage": "second", "decision": "approve"})
        self.assertEqual(status, 200)

        # 公开渠道拿到当日结论
        status, current = self.req("GET", "/public/current", token=pub)
        self.assertEqual(status, 200)
        self.assertEqual(current["items"][0]["area_id"], "SA001")
        self.assertEqual(current["items"][0]["level"], "heavy")
        # 简化结论不含触发痕迹
        self.assertNotIn("traces", json.dumps(current, ensure_ascii=False))

        # 公开批次信封可验签；篡改后失败
        status, envelope = self.req(
            "GET", f"/public/batches/{bid}", token=pub)
        self.assertEqual(status, 200)
        self.assertIn("signature", envelope)
        tampered = dict(envelope)
        tampered["items"] = [{"area_id": "SA001", "name": "x",
                              "level": "extreme"}]
        from service_09251_008.signing import unseal

        self.assertTrue(unseal(envelope, "api-secret"))
        self.assertFalse(unseal(tampered, "api-secret"))

    def test_concurrent_reviews_over_http(self) -> None:
        self._seed()
        planner = self.internal_keys["planner"]
        ra = self.internal_keys["reviewer_a"]
        rb = self.internal_keys["reviewer_b"]
        _, batch = self.req("POST", "/internal/candidates", token=planner,
                            body={})
        bid = batch["batch_id"]
        self.req("POST", f"/internal/batches/{bid}/review", token=ra,
                 body={"stage": "first", "decision": "approve"})

        results: list[tuple] = []

        def second(token: str) -> None:
            results.append(self.req(
                "POST", f"/internal/batches/{bid}/review", token=token,
                body={"stage": "second", "decision": "approve"}))

        t1 = threading.Thread(target=second, args=(rb,))
        t2 = threading.Thread(target=second, args=(planner,))
        t1.start(); t2.start(); t1.join(); t2.join()
        statuses = sorted(s for s, _ in results)
        # planner 是提单人（409）；rb 成功（200）—— 顺序无关
        self.assertEqual(statuses[0], 200)
        self.assertEqual(statuses[1], 409)

        repo = Repository(self.db_path)
        self.assertEqual(len(repo.all_intervals()), 1)

    def test_emergency_then_recovery_sweep(self) -> None:
        self._seed()
        duty = self.internal_keys["planner"]
        pub = self.public_keys["channel-x"]
        status, batch = self.req(
            "POST", "/internal/emergency-publish", token=duty,
            body={"reason": "突增车流"})
        self.assertEqual(status, 201)
        bid = batch["batch_id"]
        _, current = self.req("GET", "/public/current", token=pub)
        self.assertEqual(len(current["items"]), 1)

        # 超过补审期限后，重启服务（进程恢复）自动失效
        self.clock.set(parse_iso("2026-09-26T11:30:00+00:00"))
        self.server.shutdown()
        repo = Repository(self.db_path)
        self.server = ApiServer(
            repo, signing_secret="api-secret", clock=self.clock,
            sweep_interval_seconds=0,
        )
        httpd = self.server.start("127.0.0.1", 0)
        self.port = httpd.server_address[1]
        self.thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self.thread.start()

        _, current = self.req("GET", "/public/current",
                              token=self.public_keys["channel-x"])
        self.assertEqual(current["items"], [])
        _, detail = self.req("GET", f"/internal/batches/{bid}",
                             token=self.internal_keys["reviewer_a"])
        self.assertEqual(detail["status"], "expired")


if __name__ == "__main__":
    unittest.main()
