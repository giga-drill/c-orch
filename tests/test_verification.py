from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_pnpm_frontend_setup_runs_before_web_package_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            (web / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (web / "package.json").write_text(
                '{"scripts":{"typecheck":"printf typeok"}}\n',
                encoding="utf-8",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            pnpm = bin_dir / "pnpm"
            pnpm.write_text(
                "#!/bin/sh\n"
                "printf '%s|%s\\n' \"$CI\" \"$*\" > pnpm-called.txt\n"
                "exit 0\n",
                encoding="utf-8",
            )
            pnpm.chmod(0o755)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
                report = run_verification_commands(
                    ["cd web && npm run typecheck"],
                    cwd=root,
                    evidence_dir=root / "evidence",
                )

            self.assertEqual(report.summary, "All 1 verification command(s) passed.")
            self.assertEqual(report.results[0].command, "cd web && npm run typecheck")
            self.assertEqual((root / "pnpm-called.txt").read_text(encoding="utf-8").strip(), "true|--dir web install --frozen-lockfile")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("$ CI=true pnpm --dir web install --frozen-lockfile", output)
            self.assertIn("$ cd web && npm run typecheck", output)
            self.assertIn("typeok", output)

    def test_pnpm_frontend_setup_failure_stops_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            (web / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
            (web / "package.json").write_text(
                '{"scripts":{"typecheck":"printf should-not-run"}}\n',
                encoding="utf-8",
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            pnpm = bin_dir / "pnpm"
            pnpm.write_text(
                "#!/bin/sh\n"
                "echo setup failed >&2\n"
                "exit 42\n",
                encoding="utf-8",
            )
            pnpm.chmod(0o755)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
                report = run_verification_commands(
                    ["cd web && npm run typecheck"],
                    cwd=root,
                    evidence_dir=root / "evidence",
                )

            self.assertEqual(report.summary, "Frontend dependency setup failed.")
            self.assertEqual(report.results[0].command, "CI=true pnpm --dir web install --frozen-lockfile")
            self.assertEqual(report.results[0].returncode, 42)
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("setup failed", output)
            self.assertNotIn("should-not-run", output)

    def test_pnpm_install_verification_command_runs_with_ci(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            web = root / "web"
            web.mkdir()
            (web / "package.json").write_text("{}\n", encoding="utf-8")
            bin_dir = root / "bin"
            bin_dir.mkdir()
            pnpm = bin_dir / "pnpm"
            pnpm.write_text(
                "#!/bin/sh\n"
                "printf '%s|%s\\n' \"$CI\" \"$*\" > pnpm-install-called.txt\n"
                "exit 0\n",
                encoding="utf-8",
            )
            pnpm.chmod(0o755)

            with patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}):
                report = run_verification_commands(
                    ["pnpm --dir web install --frozen-lockfile"],
                    cwd=root,
                    evidence_dir=root / "evidence",
                )

            self.assertEqual(report.summary, "All 1 verification command(s) passed.")
            self.assertEqual(report.results[0].command, "CI=true pnpm --dir web install --frozen-lockfile")
            self.assertEqual((root / "pnpm-install-called.txt").read_text(encoding="utf-8").strip(), "true|--dir web install --frozen-lockfile")


if __name__ == "__main__":
    unittest.main()
