"""签名校验与审计链：含命令行入口的退出码。"""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from service_09251_008 import cli
from service_09251_008.config import Config, build_service
from service_09251_008.domain import parse_ts
from service_09251_008.ports import ManualClock, SequentialIds

from support import drive_to_published, observe, seed_metrics_and_rules, seed_period


def run_cli(*args) -> tuple[int, str]:
    """运行 CLI 并捕获标准输出。"""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.main(list(args))
    return code, buffer.getvalue()


class SignatureAndAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(data_dir=Path(self.tmp.name))
        self.clock = ManualClock(parse_ts("2026-10-01T08:00:00Z"))
        self.service = build_service(self.config, clock=self.clock, ids=SequentialIds())
        seed_metrics_and_rules(self.service)
        period = seed_period(self.service, "2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")
        observe(self.service, period["id"], "G60-杭州湾", 80, "2026-10-01T10:00:00Z")
        self.published = drive_to_published(self.service, period["id"])
        self.db_path = str(self.config.db_path)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _tamper(self, sql: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def test_signature_verifies_and_detects_tampering(self) -> None:
        result = self.service.verify_version_signature(self.published["version_no"])
        self.assertTrue(result["valid"])

        self._tamper("UPDATE public_entries SET message='被篡改的结论' WHERE version_no=1")
        result = self.service.verify_version_signature(self.published["version_no"])
        self.assertFalse(result["valid"])

    def test_audit_chain_verifies_and_detects_tampering(self) -> None:
        self.assertTrue(self.service.verify_audit_chain()["valid"])

        self._tamper("UPDATE audit_events SET actor='mallory' WHERE seq=1")
        result = self.service.verify_audit_chain()
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid_seq"], 1)

    def test_cli_verify_signature_exit_codes(self) -> None:
        code, out = run_cli("--data-dir", self.tmp.name, "verify-signature", "--version", "1")
        self.assertEqual(code, 0)
        self.assertIn('"valid": true', out)

        self._tamper("UPDATE public_entries SET label='被篡改' WHERE version_no=1")
        code, out = run_cli("--data-dir", self.tmp.name, "verify-signature", "--version", "1")
        self.assertEqual(code, 1)
        self.assertIn('"valid": false', out)

    def test_cli_verify_audit_exit_codes(self) -> None:
        code, _ = run_cli("--data-dir", self.tmp.name, "verify-audit")
        self.assertEqual(code, 0)

        self._tamper("UPDATE audit_events SET payload='{}' WHERE seq=2")
        code, out = run_cli("--data-dir", self.tmp.name, "verify-audit")
        self.assertEqual(code, 1)
        self.assertIn('"valid": false', out)

    def test_cli_reconstruct(self) -> None:
        code, out = run_cli("--data-dir", self.tmp.name, "reconstruct", "--at", "2026-10-01T12:00:00Z")
        self.assertEqual(code, 0)
        self.assertIn("G60-杭州湾", out)

    def test_cli_sweep_and_recover(self) -> None:
        code, out = run_cli("--data-dir", self.tmp.name, "recover")
        self.assertEqual(code, 0)
        self.assertIn('"audit_valid": true', out)
        code, out = run_cli("--data-dir", self.tmp.name, "sweep")
        self.assertEqual(code, 0)
        self.assertIn('"marked_overdue": []', out)


if __name__ == "__main__":
    unittest.main()
