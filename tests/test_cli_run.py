from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from c_orch.cli import main
from c_orch.codex_discovery import CodexCandidateReport, CodexEnvironmentReport
from c_orch.run_store import PlanRecord, RunStore


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
                        "--planner-reasoning-effort",
                        "high",
                        "--worker-reasoning-effort",
                        "medium",
                        "--planner-service-tier",
                        "fast",
                        "--worker-service-tier",
                        "flex",
                        "Implement feature X",
                    ]
                )

            self.assertEqual(exit_code, 0)
            run_ids = [path.name for path in runs_dir.iterdir()]
            self.assertEqual(len(run_ids), 1)
            manifest = RunStore(runs_dir).load(run_ids[0])
            self.assertEqual(manifest.planner.model, "gpt-5.5")
            self.assertEqual(manifest.planner.reasoning_effort, "high")
            self.assertEqual(manifest.planner.service_tier, "fast")
            self.assertEqual(manifest.workers[0].model, "gpt-5.3-codex")
            self.assertEqual(manifest.workers[0].reasoning_effort, "medium")
            self.assertEqual(manifest.workers[0].service_tier, "flex")
            self.assertEqual(manifest.workers[0].worktree_path, str(worktree_path))
            self.assertEqual(manifest.status, "NEW")

    def test_run_prepare_only_uses_project_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            (cwd / ".c-orch.toml").write_text(
                """
[planner]
preferred_models = ["gpt-5.4", "gpt-5.5"]
reasoning_effort = "high"
service_tier = "fast"

[worker]
model = "gpt-5.3-codex-spark"
reasoning_effort = "medium"
service_tier = "flex"

[run]
runs_dir = "configured-runs"
worktrees_dir = "configured-worktrees"
max_attempts = 2
sandbox = "read-only"
approval_policy = "on-request"
""".strip(),
                encoding="utf-8",
            )
            worktree_path = cwd / "configured-worktrees" / "run" / "worker-1"
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.4", "gpt-5.5", "gpt-5.3-codex-spark"),
                    usable=True,
                ),
            )

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.worktrees.create_worker_worktree",
                return_value=worktree_path,
            ) as create_worktree, redirect_stdout(StringIO()):
                exit_code = main(
                    [
                        "run",
                        "--prepare-only",
                        "--cwd",
                        str(cwd),
                        "Implement feature X",
                    ]
                )

            self.assertEqual(exit_code, 0)
            create_worktree.assert_called_once()
            self.assertEqual(
                create_worktree.call_args.kwargs["worktrees_dir"],
                (cwd / "configured-worktrees").resolve(),
            )
            run_ids = [path.name for path in (cwd / "configured-runs").iterdir()]
            manifest = RunStore(cwd / "configured-runs").load(run_ids[0])
            self.assertEqual(manifest.planner.model, "gpt-5.4")
            self.assertEqual(manifest.planner.reasoning_effort, "high")
            self.assertEqual(manifest.planner.service_tier, "fast")
            self.assertEqual(manifest.workers[0].model, "gpt-5.3-codex-spark")
            self.assertEqual(manifest.workers[0].reasoning_effort, "medium")
            self.assertEqual(manifest.workers[0].service_tier, "flex")

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
                        "--planner-service-tier",
                        "fast",
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
            self.assertTrue(fake_run_single_worker.kwargs["require_plan_approval"])
            output = stdout.getvalue()
            self.assertIn("status: APPROVED", output)
            self.assertIn("planner_service_tier: fast", output)
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

    def test_resume_terminal_statuses_print_summary_without_orchestration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = cwd / "runs"
            store = RunStore(runs_dir)

            for status in ("APPROVED", "FAILED"):
                manifest = store.create_run(
                    cwd=cwd,
                    user_task=f"Task for {status}",
                    planner_model="gpt-5.5",
                    worker_model="gpt-5.3-codex",
                    codex_binary_path="/bin/codex",
                )
                manifest.status = status
                store.save(manifest)

                with self.subTest(status=status), mock.patch(
                    "c_orch.codex_discovery.inspect_codex_environment",
                ) as inspect_mock, mock.patch(
                    "c_orch.mcp_driver.McpCodexDriver",
                ) as driver_cls, mock.patch(
                    "c_orch.orchestrator.RunOrchestrator",
                ) as orchestrator_cls, redirect_stdout(StringIO()) as stdout:
                    exit_code = main(
                        [
                            "resume",
                            manifest.run_id,
                            "--cwd",
                            str(cwd),
                            "--runs-dir",
                            "runs",
                        ]
                    )

                self.assertEqual(exit_code, 0)
                inspect_mock.assert_not_called()
                driver_cls.assert_not_called()
                orchestrator_cls.assert_not_called()
                output = stdout.getvalue()
                self.assertIn(f"run_id: {manifest.run_id}", output)
                self.assertIn("manifest:", output)
                self.assertIn(f"status: {status}", output)

    def test_resume_new_manifest_runs_orchestrator_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = cwd / "runs"
            store = RunStore(runs_dir)
            manifest = store.create_run(
                cwd=cwd,
                user_task="Implement feature X",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                codex_binary_path="/custom/codex",
            )
            worker_worktree = root / "worktrees" / manifest.run_id / "worker-1"
            worker_worktree.mkdir(parents=True)
            manifest.workers[0].worktree_path = str(worker_worktree)
            store.save(manifest)

            driver = FakeDriver()

            def fake_orchestrator_run(loaded_manifest):
                loaded_manifest.status = "APPROVED"
                loaded_manifest.planner.thread_id = "planner-resume-thread"
                loaded_manifest.workers[0].thread_id = "worker-resume-thread"
                store.save(loaded_manifest)
                return loaded_manifest

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
            ) as inspect_mock, mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=driver,
            ) as driver_cls, mock.patch(
                "c_orch.orchestrator.RunOrchestrator",
            ) as orchestrator_cls, redirect_stdout(StringIO()) as stdout:
                orchestrator = orchestrator_cls.return_value
                orchestrator.run.side_effect = fake_orchestrator_run
                exit_code = main(
                    [
                        "resume",
                        manifest.run_id,
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        "runs",
                        "--max-attempts",
                        "2",
                        "--sandbox",
                        "read-only",
                        "--approval-policy",
                        "on-request",
                        "--planner-reasoning-effort",
                        "high",
                        "--worker-reasoning-effort",
                        "low",
                        "--planner-service-tier",
                        "fast",
                        "--worker-service-tier",
                        "flex",
                    ]
                )

            self.assertEqual(exit_code, 0)
            inspect_mock.assert_not_called()
            driver_cls.assert_called_once_with(codex_bin="/custom/codex")
            self.assertTrue(driver.entered)
            self.assertTrue(driver.exited)
            self.assertEqual(orchestrator.run.call_count, 1)
            resumed_manifest = orchestrator.run.call_args.args[0]
            self.assertEqual(resumed_manifest.run_id, manifest.run_id)
            self.assertEqual(resumed_manifest.planner.reasoning_effort, "high")
            self.assertEqual(resumed_manifest.planner.service_tier, "fast")
            self.assertEqual(resumed_manifest.workers[0].reasoning_effort, "low")
            self.assertEqual(resumed_manifest.workers[0].service_tier, "flex")
            config = orchestrator_cls.call_args.kwargs["config"]
            self.assertEqual(config.max_attempts, 2)
            self.assertEqual(config.sandbox, "read-only")
            self.assertEqual(config.approval_policy, "on-request")
            self.assertTrue(config.require_plan_approval)
            self.assertFalse(config.approve_plan)

            run_dirs = [path.name for path in runs_dir.iterdir()]
            self.assertEqual(run_dirs, [manifest.run_id])

            persisted = store.load(manifest.run_id)
            self.assertEqual(persisted.status, "APPROVED")
            self.assertEqual(persisted.planner.reasoning_effort, "high")
            self.assertEqual(persisted.workers[0].reasoning_effort, "low")
            self.assertEqual(persisted.planner.thread_id, "planner-resume-thread")
            self.assertEqual(persisted.workers[0].thread_id, "worker-resume-thread")
            output = stdout.getvalue()
            self.assertIn("status: APPROVED", output)

    def test_resume_plan_review_required_without_approval_prints_next_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = cwd / "runs"
            store = RunStore(runs_dir)
            manifest = store.create_run(
                cwd=cwd,
                user_task="Implement feature X",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/custom/codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.planner.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(
                summary="Plan summary",
                worker_prompt="Build the feature",
                approval_status="pending",
            )
            store.save(manifest)

            with mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
            ) as driver_cls, mock.patch(
                "c_orch.orchestrator.RunOrchestrator",
            ) as orchestrator_cls, redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "resume",
                        manifest.run_id,
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        "runs",
                    ]
                )

            self.assertEqual(exit_code, 0)
            driver_cls.assert_not_called()
            orchestrator_cls.assert_not_called()
            output = stdout.getvalue()
            self.assertIn("status: PLAN_REVIEW_REQUIRED", output)
            self.assertIn("plan_approval: pending", output)
            self.assertIn("next: rerun resume with --approve-plan or --revise-plan", output)

    def test_resume_approve_plan_passes_approval_to_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = cwd / "runs"
            store = RunStore(runs_dir)
            manifest = store.create_run(
                cwd=cwd,
                user_task="Implement feature X",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/custom/codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.planner.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(
                summary="Plan summary",
                worker_prompt="Build the feature",
                approval_status="pending",
            )
            store.save(manifest)

            driver = FakeDriver()

            def fake_orchestrator_run(loaded_manifest):
                loaded_manifest.status = "APPROVED"
                store.save(loaded_manifest)
                return loaded_manifest

            with mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=driver,
            ), mock.patch(
                "c_orch.orchestrator.RunOrchestrator",
            ) as orchestrator_cls, redirect_stdout(StringIO()):
                orchestrator = orchestrator_cls.return_value
                orchestrator.run.side_effect = fake_orchestrator_run
                exit_code = main(
                    [
                        "resume",
                        manifest.run_id,
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        "runs",
                        "--approve-plan",
                    ]
                )

            self.assertEqual(exit_code, 0)
            config = orchestrator_cls.call_args.kwargs["config"]
            self.assertTrue(config.require_plan_approval)
            self.assertTrue(config.approve_plan)

    def test_resume_revise_plan_calls_orchestrator_revise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            runs_dir = cwd / "runs"
            store = RunStore(runs_dir)
            manifest = store.create_run(
                cwd=cwd,
                user_task="Implement feature X",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/custom/codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.planner.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(
                summary="Plan summary",
                worker_prompt="Build the feature",
                approval_status="pending",
            )
            store.save(manifest)

            driver = FakeDriver()
            revised_manifest = store.load(manifest.run_id)
            revised_manifest.status = "PLAN_REVIEW_REQUIRED"

            with mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=driver,
            ), mock.patch(
                "c_orch.orchestrator.RunOrchestrator",
            ) as orchestrator_cls, redirect_stdout(StringIO()):
                orchestrator = orchestrator_cls.return_value
                orchestrator.revise_plan.return_value = revised_manifest
                exit_code = main(
                    [
                        "resume",
                        manifest.run_id,
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        "runs",
                        "--revise-plan",
                        "Please tighten acceptance criteria",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(orchestrator.revise_plan.call_count, 1)
            call = orchestrator.revise_plan.call_args
            self.assertEqual(call.args[0].run_id, manifest.run_id)
            self.assertEqual(call.args[1], "Please tighten acceptance criteria")

    def test_ui_serves_resolved_runs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()

            with mock.patch(
                "c_orch.ui.serve_dashboard",
            ) as serve_dashboard, redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "ui",
                        "--cwd",
                        str(cwd),
                        "--runs-dir",
                        "runs",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "9999",
                    ]
                )

            self.assertEqual(exit_code, 0)
            serve_dashboard.assert_called_once_with(
                runs_dir=cwd.resolve() / "runs",
                queue_path=cwd.resolve() / ".c-orch" / "tasks" / "queue.json",
                scheduler_config=None,
                host="127.0.0.1",
                port=9999,
            )
            self.assertIn("http://127.0.0.1:9999", stdout.getvalue())


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
