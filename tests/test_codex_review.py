from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from c_orch.codex_review import run_codex_uncommitted_review


class CodexReviewTests(unittest.TestCase):
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
