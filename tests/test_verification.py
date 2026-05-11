from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch.verification import run_verification_commands


class VerificationTests(unittest.TestCase):
    def test_run_verification_commands_records_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_verification_commands(
                ["printf ok"],
                cwd=root,
                evidence_dir=root / "evidence",
            )

            self.assertEqual(report.summary, "All 1 verification command(s) passed.")
            self.assertEqual(report.results[0].status, "passed")
            self.assertTrue(report.output_path.exists())
            self.assertIn("stdout:\nok", report.output_path.read_text(encoding="utf-8"))

    def test_failed_command_marks_report_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_verification_commands(
                ["sh -c 'echo nope >&2; exit 7'"],
                cwd=root,
                evidence_dir=root / "evidence",
            )

            self.assertEqual(report.summary, "1 of 1 verification command(s) failed.")
            self.assertEqual(report.results[0].status, "failed")
            self.assertEqual(report.results[0].returncode, 7)
            self.assertIn("stderr:\nnope", report.output_path.read_text(encoding="utf-8"))

    def test_empty_commands_writes_noop_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = run_verification_commands(
                [""],
                cwd=root,
                evidence_dir=root / "evidence",
            )

            self.assertEqual(report.summary, "No verification commands configured.")
            self.assertEqual(report.results, [])


if __name__ == "__main__":
    unittest.main()
