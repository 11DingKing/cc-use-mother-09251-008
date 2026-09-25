"""权限隔离：内部/公众两套 API 的令牌边界与公众输出的简化。"""
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from service_09251_008.config import Config
from service_09251_008.domain import parse_ts
from service_09251_008.httpapi import start_test_server

from support import make_service, seed_metrics_and_rules

INTERNAL_ALICE = "dev-internal-alice"
INTERNAL_BOB = "dev-internal-bob"
PUBLIC = "dev-public"


def call(port, method, path, token=None, body=None):
    url = f"http://127.0.0.1:{port}{urllib.parse.quote(path, safe='/?=&')}"
    request = urllib.request.Request(url, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode("utf-8")
    try:
        with urllib.request.urlopen(request, data=data, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read().decode("utf-8"))


class HttpAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.service, cls.clock = make_service(cls.tmp.name)
        config = Config(data_dir=Path(cls.tmp.name))
        cls.server, cls.thread, cls.port = start_test_server(config, cls.service)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def test_missing_or_unknown_token(self) -> None:
        status, body = call(self.port, "GET", "/internal/metric-versions")
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "UNAUTHORIZED")

        status, _ = call(self.port, "GET", "/internal/metric-versions", token="wrong-token")
        self.assertEqual(status, 401)

        status, _ = call(self.port, "GET", "/public/current")
        self.assertEqual(status, 401)

    def test_public_token_cannot_reach_internal_api(self) -> None:
        for path in (
            "/internal/metric-versions",
            "/internal/candidate-lists",
            "/internal/emergency-upgrades",
            "/internal/audit/verify",
        ):
            status, body = call(self.port, "GET", path, token=PUBLIC)
            self.assertEqual(status, 403, path)
            self.assertEqual(body["error"]["code"], "FORBIDDEN")

        status, _ = call(self.port, "POST", "/internal/candidate-lists/generate", token=PUBLIC, body={})
        self.assertEqual(status, 403)

    def test_internal_token_can_read_public_api(self) -> None:
        status, _ = call(self.port, "GET", "/public/current", token=INTERNAL_ALICE)
        self.assertEqual(status, 200)

    def test_full_flow_and_public_simplification(self) -> None:
        # 把时钟拨到公开有效期内，/public/current 才能看到条目
        self.clock.set(parse_ts("2026-10-01T12:00:00Z"))
        # 通过内部 API 完成 指标→规则→观察期→观测→候选→双人复核→发布
        status, mv = call(self.port, "POST", "/internal/metric-versions", token=INTERNAL_ALICE,
                          body={"metrics": [{"code": "occupancy", "name": "泊位占用率", "unit": "%"}]})
        self.assertEqual(status, 200, mv)
        mv_id = mv["data"]["id"]
        call(self.port, "POST", f"/internal/metric-versions/{mv_id}/activate", token=INTERNAL_ALICE)

        status, rv = call(self.port, "POST", "/internal/rule-versions", token=INTERNAL_ALICE,
                          body={"metric_version_id": mv_id,
                                "rules": [{"grade": "L2", "all": [{"metric": "occupancy", "op": ">=", "value": 75}]}]})
        rv_id = rv["data"]["id"]
        call(self.port, "POST", f"/internal/rule-versions/{rv_id}/activate", token=INTERNAL_ALICE)

        status, period = call(self.port, "POST", "/internal/periods", token=INTERNAL_ALICE,
                              body={"name": "国庆", "start": "2026-10-01T00:00:00Z", "end": "2026-10-03T00:00:00Z"})
        period_id = period["data"]["id"]
        call(self.port, "POST", f"/internal/periods/{period_id}/observations", token=INTERNAL_ALICE,
             body={"items": [{"area_code": "G60-杭州湾", "metric": "occupancy", "value": 80,
                              "observed_at": "2026-10-01T10:00:00Z"}]})

        status, candidate = call(self.port, "POST", "/internal/candidate-lists/generate",
                                 token=INTERNAL_ALICE, body={"period_id": period_id})
        list_id = candidate["data"]["id"]
        # 内部视图包含触发指标
        self.assertEqual(candidate["data"]["entries"][0]["triggered"][0]["metric"], "occupancy")

        call(self.port, "POST", f"/internal/candidate-lists/{list_id}/submit", token=INTERNAL_ALICE)
        # 创建人 alice 不能自审；bob 与第三位复核人完成双人复核
        status, _ = call(self.port, "POST", f"/internal/candidate-lists/{list_id}/reviews",
                         token=INTERNAL_ALICE, body={"decision": "APPROVE"})
        self.assertEqual(status, 403)
        call(self.port, "POST", f"/internal/candidate-lists/{list_id}/reviews",
             token=INTERNAL_BOB, body={"decision": "APPROVE"})
        status, _ = call(self.port, "POST", f"/internal/candidate-lists/{list_id}/reviews",
                         token="dev-internal-carol", body={"decision": "APPROVE"})
        self.assertEqual(status, 200)

        status, published = call(self.port, "POST", f"/internal/candidate-lists/{list_id}/publish",
                                 token=INTERNAL_ALICE, body={})
        self.assertEqual(status, 200, published)
        version_no = published["data"]["version_no"]

        # 公众渠道只能看到简化结论
        status, current = call(self.port, "GET", "/public/current", token=PUBLIC)
        self.assertEqual(status, 200)
        entries = current["data"]["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["area_code"], "G60-杭州湾")
        self.assertEqual(entries[0]["label"], "中度繁忙")
        for field in ("triggered", "created_by", "revoked_at", "note", "source"):
            self.assertNotIn(field, entries[0], f"公众输出不应包含内部字段 {field}")

        status, public_version = call(self.port, "GET", f"/public/versions/{version_no}", token=PUBLIC)
        self.assertEqual(status, 200)
        self.assertNotIn("published_by", public_version["data"])
        self.assertIn("signature", public_version["data"])

        # 公众令牌无法撤销条目（越界写操作）
        status, _ = call(self.port, "POST",
                         f"/internal/public-versions/{version_no}/entries/G60-杭州湾/revoke",
                         token=PUBLIC, body={})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
