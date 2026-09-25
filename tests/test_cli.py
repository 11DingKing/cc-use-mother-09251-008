"""CLI：密钥签发、历史重建与签名校验命令。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from service_09251_008 import cli
from service_09251_008.storage import Repository

from support import approve_both, clock_at, make_app, seed_areas, window_days


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.db = str(self.base / "busy.db")
        self.secret = "cli-secret"
        self.clock = clock_at()
        self.app = make_app(self.base, self.clock, secret=self.secret)
        seed_areas(self.app)
        window_days(self.app, "SA001", "flow_saturation", "2026-09-25", 3, 0.95)
        bid = self.app.generate_candidates("planner")["batch_id"]
        approve_both(self.app, bid)
        self.bid = bid

    def _args(self, *extra: str) -> list[str]:
        return ["--db", self.db, "--secret", self.secret, *extra]

    def _run(self, *extra: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(self._args(*extra))
        return buf.getvalue()

    def test_issue_key_scopes(self) -> None:
        out = json.loads(self._run("issue-key", "--scope", "internal",
                                   "--label", "alice"))
        self.assertTrue(out["token"].startswith("internal_"))
        row = Repository(self.db).get_api_key(out["token"])
        self.assertEqual(row["scope"], "internal")
        self.assertEqual(row["label"], "alice")

        out = json.loads(self._run("issue-key", "--scope", "public",
                                   "--label", "media"))
        self.assertTrue(out["token"].startswith("public_"))

    def test_rebuild_snapshot_command(self) -> None:
        out = json.loads(self._run(
            "rebuild", "--day", "2026-09-25", "--mode", "snapshot"))
        self.assertEqual(out["effective_day"], "2026-09-25")
        self.assertEqual(out["items"][0]["level"], "heavy")

    def test_rebuild_candidate_command(self) -> None:
        out = json.loads(self._run(
            "rebuild", "--day", "2026-09-25"))
        self.assertEqual(out["rule_version"], "v1")
        self.assertTrue(out["items"][0]["traces"])

    def test_verify_command_on_valid_envelope(self) -> None:
        manifest = self.app.public_manifest(self.bid)
        env_file = self.base / "env.json"
        env_file.write_text(json.dumps(manifest, ensure_ascii=False),
                            encoding="utf-8")
        out = json.loads(self._run("verify", str(env_file)))
        self.assertTrue(out["valid_signature"])

    def test_verify_command_fails_on_tampered(self, ) -> None:
        manifest = self.app.public_manifest(self.bid)
        manifest["items"][0]["level"] = "extreme"
        env_file = self.base / "bad.json"
        env_file.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(SystemExit) as cm:
            self._run("verify", str(env_file))
        self.assertEqual(cm.exception.code, 1)

    def test_verify_with_wrong_secret(self) -> None:
        manifest = self.app.public_manifest(self.bid)
        env_file = self.base / "env2.json"
        env_file.write_text(json.dumps(manifest), encoding="utf-8")
        args = ["--db", self.db, "--secret", "other-secret",
                "verify", str(env_file)]
        with self.assertRaises(SystemExit) as cm:
            cli.main(args)
        self.assertEqual(cm.exception.code, 1)

    def test_sign_command(self) -> None:
        payload = {"batch_id": "x", "effective_day": "2026-09-25",
                   "items": []}
        src = self.base / "p.json"
        src.write_text(json.dumps(payload), encoding="utf-8")
        out = json.loads(self._run("sign", str(src)))
        self.assertIn("signature", out)
        from service_09251_008.signing import unseal

        self.assertTrue(unseal(out, self.secret))

    def test_demo_init(self) -> None:
        db2 = str(self.base / "demo.db")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["--db", db2, "--secret", self.secret, "demo-init"])
        out = json.loads(buf.getvalue())
        self.assertIn("internal", out["keys"])
        self.assertIn("public", out["keys"])
        # 演示库存在生效规则
        self.assertTrue(Repository(db2).active_rule_version())


if __name__ == "__main__":
    unittest.main()
