from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from c_orch.worktrees import (
    apply_diff_evidence_to_repo,
    collect_diff_evidence,
    create_worker_worktree,
    worker_worktree_path,
)


def _git(cwd: Path, args: Sequence[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


@unittest.skipIf(shutil.which("git") is None, "git is not available")
class WorktreeTests(unittest.TestCase):
    def test_create_worker_worktree_uses_per_worker_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            worktrees_dir = root / "worktrees"

            worktree = create_worker_worktree(repo, worktrees_dir, "run-1", "worker-1")

            self.assertEqual(
                worktree,
                (worktrees_dir / "run-1" / "worker-1").resolve(),
            )
            self.assertTrue((worktree / ".git").exists())
            self.assertEqual(
                (worktree / "README.md").read_text(encoding="utf-8"),
                "hello\n",
            )
            self.assertEqual(
                worker_worktree_path(worktrees_dir, "run-1", "worker-1"),
                worktrees_dir / "run-1" / "worker-1",
            )

    def test_collect_diff_evidence_writes_summary_and_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            worktree = create_worker_worktree(
                repo,
                root / "worktrees",
                "run-1",
                "worker-1",
            )
            (worktree / "README.md").write_text("hello\nchanged\n", encoding="utf-8")
            (worktree / "new.txt").write_text("new evidence\n", encoding="utf-8")

            evidence = collect_diff_evidence(
                worktree,
                root / "runs" / "run-1" / "evidence",
            )

            self.assertTrue(evidence.summary_path.exists())
            self.assertTrue(evidence.patch_path.exists())
            self.assertIn("README.md", evidence.summary)
            self.assertIn("new.txt", evidence.summary)
            self.assertIn("+changed", evidence.patch)
            self.assertIn("+new evidence", evidence.patch)
            self.assertIn("README.md", evidence.changed_paths)
            self.assertIn("new.txt", evidence.changed_paths)
            self.assertIn("?? new.txt", _git(worktree, ["status", "--short"]))

    def test_create_worker_worktree_requires_committed_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _git(repo, ["init"])

            with self.assertRaisesRegex(RuntimeError, "requires a committed base ref"):
                create_worker_worktree(
                    repo,
                    root / "worktrees",
                    "run-1",
                    "worker-1",
                )

    def test_apply_diff_evidence_to_repo_applies_tracked_and_untracked_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            worktree = create_worker_worktree(
                repo,
                root / "worktrees",
                "run-1",
                "worker-1",
            )
            (worktree / "README.md").write_text("hello from worker\n", encoding="utf-8")
            (worktree / "new.txt").write_text("new evidence\n", encoding="utf-8")
            diff_evidence = collect_diff_evidence(
                worktree,
                root / "runs" / "run-1" / "evidence",
            )

            report = apply_diff_evidence_to_repo(
                diff_evidence,
                repo,
                root / "runs" / "run-1" / "evidence",
            )

            self.assertTrue(report.applied)
            self.assertTrue(report.output_path.exists())
            self.assertEqual(
                (repo / "README.md").read_text(encoding="utf-8"),
                "hello from worker\n",
            )
            self.assertEqual((repo / "new.txt").read_text(encoding="utf-8"), "new evidence\n")
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("Summary: Patch applied successfully.", output)
            self.assertIn("apply --check --binary", output)
            self.assertIn("returncode: 0", output)

    def test_apply_diff_evidence_to_repo_failure_keeps_target_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            worktree = create_worker_worktree(
                repo,
                root / "worktrees",
                "run-1",
                "worker-1",
            )
            (worktree / "README.md").write_text("hello from worker\n", encoding="utf-8")
            diff_evidence = collect_diff_evidence(
                worktree,
                root / "runs" / "run-1" / "evidence",
            )
            (repo / "README.md").write_text("hello from target\n", encoding="utf-8")

            report = apply_diff_evidence_to_repo(
                diff_evidence,
                repo,
                root / "runs" / "run-1" / "evidence",
            )

            self.assertFalse(report.applied)
            self.assertEqual(
                (repo / "README.md").read_text(encoding="utf-8"),
                "hello from target\n",
            )
            output = report.output_path.read_text(encoding="utf-8")
            self.assertIn("Failed: git apply --check rejected the patch.", output)
            self.assertIn("apply --check --binary", output)
            self.assertIn("returncode:", output)
            self.assertIn("stderr:", output)

    def _create_repo(self, path: Path) -> Path:
        path.mkdir()
        _git(path, ["init"])
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        _git(path, ["add", "README.md"])
        _git(
            path,
            [
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-m",
                "initial",
            ],
        )
        return path


if __name__ == "__main__":
    unittest.main()
