from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from c_orch.codex_review import (
    DEFAULT_CODEX_REVIEW_TIMEOUT_SECONDS,
    run_codex_uncommitted_review,
)


class CodexReviewTests(unittest.TestCase):
    def test_review_timeout_uses_900s_default_and_records_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "c_orch.codex_review.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["codex", "review", "--uncommitted"],
                    timeout=DEFAULT_CODEX_REVIEW_TIMEOUT_SECONDS,
                ),
            ) as run_mock:
                report = run_codex_uncommitted_review(
                    cwd=root,
                    evidence_dir=root / "evidence",
                    codex_binary_path="codex",
                )

            self.assertEqual(report.status, "error")
            self.assertIsNone(report.returncode)
            self.assertIn("timed out after 900s", report.summary)
            self.assertEqual(
                run_mock.call_args.kwargs.get("timeout"),
                DEFAULT_CODEX_REVIEW_TIMEOUT_SECONDS,
            )
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertEqual(payload["error"], "timed out after 900s")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("timed out after 900s", output)

    def test_review_timeout_honors_explicit_timeout_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch(
                "c_orch.codex_review.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    cmd=["codex", "review", "--uncommitted"],
                    timeout=7,
                ),
            ) as run_mock:
                report = run_codex_uncommitted_review(
                    cwd=root,
                    evidence_dir=root / "evidence",
                    codex_binary_path="codex",
                    timeout_seconds=7,
                )

            self.assertEqual(report.status, "error")
            self.assertIn("timed out after 7s", report.summary)
            self.assertEqual(run_mock.call_args.kwargs.get("timeout"), 7)
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["error"], "timed out after 7s")

    def test_review_defaults_to_macos_app_embedded_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_root = root / "Codex.app" / "Contents" / "Resources"
            app_root.mkdir(parents=True)
            app_codex = app_root / "codex"
            app_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"review\" ] && [ \"$2\" = \"--uncommitted\" ]; then\n"
                "  printf 'app cli review ok\\n'\n"
                "  exit 0\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            app_codex.chmod(0o755)

            with mock.patch("c_orch.codex_review.MACOS_CODEX_PATH", str(app_codex)):
                report = run_codex_uncommitted_review(
                    cwd=root,
                    evidence_dir=root / "evidence",
                )

            self.assertEqual(report.status, "passed")
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"][0], str(app_codex))
            self.assertEqual(payload["command"][1:], ["review", "--uncommitted"])
            self.assertEqual(payload["codex_binary_source"], "macos_app")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn(f"$ {app_codex} review --uncommitted", output)
            self.assertIn("codex_binary_source: macos_app", output)

            with mock.patch("c_orch.codex_review.MACOS_CODEX_PATH", str(app_codex)):
                explicit_report = run_codex_uncommitted_review(
                    cwd=root,
                    evidence_dir=root / "explicit-evidence",
                    codex_binary_path=str(app_codex),
                )

            explicit_payload = json.loads(
                explicit_report.result_path.read_text(encoding="utf-8")
            )
            self.assertEqual(explicit_payload["codex_binary_source"], "macos_app")

    def test_run_codex_uncommitted_review_records_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_path = root / "codex"
            bin_path.write_text(
                "#!/bin/sh\n"
                "printf 'fake review ok\\n'\n",
                encoding="utf-8",
            )
            bin_path.chmod(0o755)

            report = run_codex_uncommitted_review(
                cwd=root,
                evidence_dir=root / "evidence",
                codex_binary_path=str(bin_path),
            )

            self.assertEqual(report.status, "passed")
            self.assertEqual(report.returncode, 0)
            self.assertEqual(report.summary, "Codex review completed successfully.")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("$", output)
            self.assertIn("review --uncommitted", output)
            self.assertIn("fake review ok", output)
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "passed")
            self.assertEqual(payload["returncode"], 0)
            self.assertEqual(payload["command"][1:], ["review", "--uncommitted"])
            self.assertIsNone(payload["service_tier"])

    def test_run_codex_uncommitted_review_includes_service_tier_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_path = root / "codex"
            bin_path.write_text(
                "#!/bin/sh\n"
                "printf 'fake review with flex\\n'\n",
                encoding="utf-8",
            )
            bin_path.chmod(0o755)

            report = run_codex_uncommitted_review(
                cwd=root,
                evidence_dir=root / "evidence",
                codex_binary_path=str(bin_path),
                service_tier="flex",
            )

            self.assertEqual(report.status, "passed")
            self.assertEqual(report.service_tier, "flex")
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["service_tier"], "flex")
            self.assertEqual(
                payload["command"],
                [str(bin_path), "review", "--uncommitted", "-c", "service_tier=flex"],
            )
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("-c service_tier=flex", output)
            self.assertIn("service_tier: flex", output)

    def test_run_codex_uncommitted_review_records_missing_binary_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            missing = root / "does-not-exist-codex"

            report = run_codex_uncommitted_review(
                cwd=root,
                evidence_dir=root / "evidence",
                codex_binary_path=str(missing),
            )

            self.assertEqual(report.status, "error")
            self.assertIsNone(report.returncode)
            self.assertIn("could not start", report.summary.lower())
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("binary not found", output)
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertIn("binary not found", payload["error"])

    def test_nonzero_review_exit_is_blocking_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_path = root / "codex"
            bin_path.write_text(
                "#!/bin/sh\n"
                "echo config failed >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
            bin_path.chmod(0o755)

            report = run_codex_uncommitted_review(
                cwd=root,
                evidence_dir=root / "evidence",
                codex_binary_path=str(bin_path),
            )

            self.assertEqual(report.status, "error")
            self.assertEqual(report.returncode, 7)
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("config failed", output)
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertIn("exited with code 7", payload["error"])

    def test_review_uses_isolated_patch_workspace_and_ignores_untracked_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
            _git(repo, ["init"])
            _git(repo, ["config", "user.email", "c-orch-test@example.com"])
            _git(repo, ["config", "user.name", "c-orch test"])
            _git(repo, ["add", "."])
            _git(repo, ["commit", "-m", "init"])
            (repo / "app.py").write_text("value = 2\n", encoding="utf-8")
            (repo / "verification-output.txt").write_text("artifact\n", encoding="utf-8")
            patch = _git(repo, ["diff", "--binary", "HEAD", "--"])
            patch_path = root / "worker.patch"
            patch_path.write_text(patch, encoding="utf-8")

            codex = root / "codex"
            codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = \"review\" ] && [ \"$2\" = \"--uncommitted\" ]; then\n"
                "  if [ -f verification-output.txt ]; then\n"
                "    echo artifact-present\n"
                "    exit 9\n"
                "  fi\n"
                "  if [ -f app.py ]; then\n"
                "    exit 0\n"
                "  fi\n"
                "fi\n"
                "exit 2\n",
                encoding="utf-8",
            )
            codex.chmod(0o755)

            report = run_codex_uncommitted_review(
                cwd=repo,
                evidence_dir=root / "evidence",
                codex_binary_path=str(codex),
                patch_path=patch_path,
            )

            self.assertEqual(report.status, "passed")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("review_workspace_mode: isolated_patch_worktree", output)
            payload = json.loads(report.result_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["review_workspace_mode"], "isolated_patch_worktree")


def _git(cwd: Path, args: list[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


if __name__ == "__main__":
    unittest.main()
