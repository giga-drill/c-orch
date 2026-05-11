from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from c_orch.cli import main
from c_orch.codex_discovery import CodexCandidateReport, CodexEnvironmentReport
from c_orch.run_store import RunStore


class CliRunTests(unittest.TestCase):
    def test_run_prepare_only_creates_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = root / "runs"
            worktree_path = root / "worktrees" / "run" / "worker-1"
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.5", "gpt-5.3-codex"),
                    usable=True,
                ),
            )

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.worktrees.create_worker_worktree",
                return_value=worktree_path,
            ), redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--prepare-only",
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        str(runs_dir),
                        "Implement feature X",
                    ]
                )

            self.assertEqual(exit_code, 0)
            run_ids = [path.name for path in runs_dir.iterdir()]
            self.assertEqual(len(run_ids), 1)
            manifest = RunStore(runs_dir).load(run_ids[0])
            self.assertEqual(manifest.planner.model, "gpt-5.5")
            self.assertEqual(manifest.workers[0].model, "gpt-5.3-codex")
            self.assertEqual(manifest.workers[0].worktree_path, str(worktree_path))
            self.assertEqual(manifest.status, "NEW")

    def test_run_executes_single_worker_with_mcp_driver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = root / "runs"
            worktree_path = root / "worktrees" / "run" / "worker-1"
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.5", "gpt-5.3-codex"),
                    usable=True,
                ),
            )
            driver = FakeDriver()

            def fake_run_single_worker(**kwargs):
                manifest = kwargs["manifest"]
                manifest.status = "APPROVED"
                manifest.planner.thread_id = "planner-thread"
                manifest.workers[0].thread_id = "worker-thread"
                kwargs["store"].save(manifest)
                fake_run_single_worker.kwargs = kwargs
                return manifest

            fake_run_single_worker.kwargs = {}

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.worktrees.create_worker_worktree",
                return_value=worktree_path,
            ), mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=driver,
            ), mock.patch(
                "c_orch.orchestrator.run_single_worker",
                side_effect=fake_run_single_worker,
            ), redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "run",
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        str(runs_dir),
                        "--max-attempts",
                        "2",
                        "--sandbox",
                        "read-only",
                        "Implement feature X",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue(driver.entered)
            self.assertTrue(driver.exited)
            self.assertEqual(fake_run_single_worker.kwargs["driver"], driver)
            self.assertEqual(fake_run_single_worker.kwargs["max_attempts"], 2)
            self.assertEqual(fake_run_single_worker.kwargs["sandbox"], "read-only")
            self.assertEqual(fake_run_single_worker.kwargs["approval_policy"], "never")
            output = stdout.getvalue()
            self.assertIn("status: APPROVED", output)
            self.assertIn("planner_thread: planner-thread", output)
            self.assertIn("worker_thread: worker-thread", output)

    def test_run_failure_prints_manifest_and_returns_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = root / "runs"
            worktree_path = root / "worktrees" / "run" / "worker-1"
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.5", "gpt-5.3-codex"),
                    usable=True,
                ),
            )

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.worktrees.create_worker_worktree",
                return_value=worktree_path,
            ), mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=FakeDriver(),
            ), mock.patch(
                "c_orch.orchestrator.run_single_worker",
                side_effect=RuntimeError("planner json failed"),
            ), redirect_stdout(StringIO()) as stdout, redirect_stderr(StringIO()) as stderr:
                exit_code = main(
                    [
                        "run",
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        str(runs_dir),
                        "Implement feature X",
                    ]
                )

            self.assertEqual(exit_code, 1)
            self.assertIn("status: FAILED", stdout.getvalue())
            self.assertIn("manifest:", stdout.getvalue())
            self.assertIn("planner json failed", stderr.getvalue())
            run_ids = [path.name for path in runs_dir.iterdir()]
            manifest = RunStore(runs_dir).load(run_ids[0])
            self.assertEqual(manifest.status, "FAILED")


class FakeDriver:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> "FakeDriver":
        self.entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exited = True


if __name__ == "__main__":
    unittest.main()
