from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from c_orch.worktrees import (
    apply_diff_evidence_to_repo,
    commit_applied_changes,
    collect_repo_changed_paths,
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

    def test_collect_repo_changed_paths_includes_tracked_and_untracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("hello changed\n", encoding="utf-8")
            (repo / "new.txt").write_text("new\n", encoding="utf-8")

            changed = collect_repo_changed_paths(repo)

            self.assertEqual(changed, ["README.md", "new.txt"])

    def test_commit_applied_changes_commits_only_expected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("hello from worker\n", encoding="utf-8")
            (repo / "new.txt").write_text("new evidence\n", encoding="utf-8")

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X\nSecond line",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md", "new.txt"],
                pre_apply_changed_paths=[],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=["README.md", "new.txt"],
            )

            self.assertTrue(report.committed)
            self.assertIsNotNone(report.commit_hash)
            self.assertTrue(report.output_path.exists())
            self.assertTrue(report.message_path.exists())
            self.assertTrue((root / "runs" / "run-1" / "evidence" / "git-commit-hash.txt").exists())
            message = report.message_path.read_text(encoding="utf-8")
            self.assertIn("Run-ID: run-1", message)
            self.assertIn("Worker-ID: worker-1", message)
            self.assertIn("Planner-Review: accepted", message)
            self.assertIn("Task: Implement feature X", message)
            self.assertEqual(_git(repo, ["status", "--short"]).strip(), "")
            self.assertIn("Ship worker patch", _git(repo, ["log", "-1", "--pretty=%B"]))

    def test_commit_applied_changes_skips_when_no_commit_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md"],
                pre_apply_changed_paths=[],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=[],
            )

            self.assertTrue(report.skipped)
            self.assertIn("Skipped:", report.summary)
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

    def test_commit_applied_changes_fails_closed_on_preexisting_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("preexisting\n", encoding="utf-8")

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md"],
                pre_apply_changed_paths=["README.md"],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=["README.md"],
            )

            self.assertTrue(report.failed)
            self.assertEqual(report.failure_reason, "preexisting_overlap")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

    def test_commit_applied_changes_fails_closed_on_unexpected_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("worker\n", encoding="utf-8")
            (repo / "other.txt").write_text("unexpected\n", encoding="utf-8")

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md"],
                pre_apply_changed_paths=[],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=["README.md", "other.txt"],
            )

            self.assertTrue(report.failed)
            self.assertEqual(report.failure_reason, "unexpected_paths_after_apply")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

    def test_commit_applied_changes_fails_with_preexisting_unrelated_staged_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("worker change\n", encoding="utf-8")
            (repo / "staged-only.txt").write_text("already staged\n", encoding="utf-8")
            _git(repo, ["add", "staged-only.txt"])

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md"],
                pre_apply_changed_paths=["README.md"],
                pre_apply_staged_paths=["staged-only.txt"],
                post_apply_changed_paths=["README.md"],
            )

            self.assertTrue(report.failed)
            self.assertEqual(report.failure_reason, "preexisting_staged_changes")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

    def test_commit_applied_changes_fails_when_git_add_returns_nonzero(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["missing.txt"],
                pre_apply_changed_paths=[],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=["missing.txt"],
            )

            self.assertTrue(report.failed)
            self.assertEqual(report.failure_reason, "git_add_failed")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

    def test_commit_applied_changes_fails_when_staged_paths_extend_beyond_planned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._create_repo(root / "repo")
            (repo / "README.md").write_text("worker change\n", encoding="utf-8")
            (repo / "other-staged.txt").write_text("staged outsider\n", encoding="utf-8")
            _git(repo, ["add", "other-staged.txt"])

            report = commit_applied_changes(
                target_repo_path=repo,
                evidence_dir=root / "runs" / "run-1" / "evidence",
                run_id="run-1",
                worker_id="worker-1",
                planner_review_decision="accepted",
                user_task="Implement feature X",
                plan_summary="Ship worker patch",
                review_reason="Looks good",
                changed_paths=["README.md"],
                pre_apply_changed_paths=[],
                pre_apply_staged_paths=[],
                post_apply_changed_paths=["README.md"],
            )

            self.assertTrue(report.failed)
            self.assertEqual(report.failure_reason, "staged_paths_outside_planned")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "1")

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
