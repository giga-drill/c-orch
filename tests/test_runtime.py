from __future__ import annotations

from contextlib import contextmanager
import subprocess
import threading
import time
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional
from unittest import mock

from c_orch.proposal_store import (
    PROPOSAL_QUEUED,
    PROPOSAL_WAITING_WORKSPACE,
    ProposalPool,
    ProposalRecord,
    ProposalStore,
)
from c_orch.run_store import PlanRecord, ReviewAttemptRecord, ReviewRecord, RunStore
from c_orch.runtime import COrchRuntime, prune_queued_proposals
from c_orch.scheduler import SchedulerConfig
from c_orch.states import (
    REVIEW_ATTEMPT_FAILED_RETRYABLE,
    RUN_FAILED,
    RUN_PLAN_APPROVED,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_WORK_DONE,
)
from c_orch.task_store import TaskStore


class _FakeOrchestrator:
    def __init__(self, run_store, status: str) -> None:  # type: ignore[no-untyped-def]
        self.run_store = run_store
        self.status = status
        self.run_ids: List[str] = []
        self.run_calls = 0
        self.input_statuses: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        self.input_statuses.append(manifest.status)
        manifest.status = self.status
        self.run_store.save(manifest)
        self.run_ids.append(manifest.run_id)
        return manifest


class _BlockingQueueOrchestrator:
    def __init__(
        self,
        run_store: RunStore,
        *,
        release: threading.Event,
        statuses: Optional[Dict[str, str]] = None,
    ) -> None:
        self.run_store = run_store
        self.release = release
        self.statuses = statuses or {}
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.two_started = threading.Event()
        self.run_ids: List[str] = []
        self.user_tasks: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        with self.lock:
            self.run_ids.append(manifest.run_id)
            self.user_tasks.append(manifest.user_task)
            self.started.set()
            if len(self.run_ids) >= 2:
                self.two_started.set()
        self.release.wait(timeout=5)
        manifest.status = self.statuses.get(manifest.user_task, "APPROVED")
        self.run_store.save(manifest)
        return manifest

    def retry_review(self, manifest):  # type: ignore[no-untyped-def]
        return self.run(manifest)


class _RetryReviewQueueOrchestrator:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store
        self.run_calls = 0
        self.retry_review_calls = 0
        self.retry_run_ids: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        manifest.status = "APPROVED"
        self.run_store.save(manifest)
        return manifest

    def retry_review(self, manifest):  # type: ignore[no-untyped-def]
        self.retry_review_calls += 1
        self.retry_run_ids.append(manifest.run_id)
        manifest.status = "APPROVED"
        self.run_store.save(manifest)
        return manifest


class _FakeProposalPlanner:
    def __init__(
        self,
        run_store: RunStore,
        *,
        block_event: Optional[threading.Event] = None,
        block_first_call: bool = False,
        fail_by_run_id: Optional[Dict[str, str]] = None,
    ) -> None:
        self.run_store = run_store
        self.block_event = block_event
        self.block_first_call = block_first_call
        self.fail_by_run_id = fail_by_run_id or {}
        self.run_calls = 0
        self.run_ids: List[str] = []
        self.lock = threading.Lock()
        self.started = threading.Event()
        self.two_started = threading.Event()

    def run(self, manifest):  # type: ignore[no-untyped-def]
        with self.lock:
            self.run_calls += 1
            run_calls = self.run_calls
            self.run_ids.append(manifest.run_id)
            self.started.set()
            if len(self.run_ids) >= 2:
                self.two_started.set()
        should_block = self.block_event is not None and (not self.block_first_call or run_calls == 1)
        if should_block:
            self.block_event.wait(timeout=5)
        failure_message = self.fail_by_run_id.get(manifest.run_id)
        if failure_message:
            raise RuntimeError(failure_message)
        manifest.status = RUN_PLAN_REVIEW_REQUIRED
        manifest.plan = PlanRecord(
            summary=f"Plan for {manifest.run_id}",
            worker_prompt=f"Implement {manifest.user_task}",
            risk_notes=["risk-1"],
        )
        manifest.acceptance_criteria = [f"accept-{manifest.run_id}"]
        manifest.verification_commands = ["pytest -q"]
        self.run_store.save(manifest)
        return manifest


class _AutoQueueProposalOrchestrator:
    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store
        self.planner_run_ids: List[str] = []
        self.worker_run_ids: List[str] = []
        self.input_statuses: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        self.input_statuses.append(manifest.status)
        if manifest.status in {"NEW", "PLANNING"}:
            self.planner_run_ids.append(manifest.run_id)
            manifest.status = RUN_PLAN_REVIEW_REQUIRED
            manifest.plan = PlanRecord(
                summary=f"Plan for {manifest.run_id}",
                worker_prompt=f"Implement {manifest.user_task}",
                risk_notes=["risk-1"],
            )
            manifest.acceptance_criteria = [f"accept-{manifest.run_id}"]
            manifest.verification_commands = ["pytest -q"]
        elif manifest.status == RUN_PLAN_APPROVED:
            self.worker_run_ids.append(manifest.run_id)
            manifest.status = "APPROVED"
        self.run_store.save(manifest)
        return manifest


class RuntimeTests(unittest.TestCase):
    def test_runtime_reuses_cached_driver_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            driver = driver_cls.return_value
            driver.__enter__.return_value = driver
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with runtime._driver_context("/bin/codex") as first:
                self.assertIs(first, driver)
            with runtime._driver_context("/bin/codex") as second:
                self.assertIs(second, driver)

            driver_cls.assert_called_once_with(codex_bin="/bin/codex")
            driver.close.assert_not_called()

            runtime.close()

            driver.close.assert_called_once()

    def test_runtime_drops_cached_driver_after_action_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            first_driver = mock.Mock()
            first_driver.__enter__ = mock.Mock(return_value=first_driver)
            first_driver.__exit__ = mock.Mock(return_value=None)
            second_driver = mock.Mock()
            second_driver.__enter__ = mock.Mock(return_value=second_driver)
            second_driver.__exit__ = mock.Mock(return_value=None)
            driver_cls.side_effect = [first_driver, second_driver]
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with self.assertRaises(RuntimeError):
                with runtime._driver_context("/bin/codex"):
                    raise RuntimeError("driver pipe broke")

            first_driver.close.assert_called_once()
            with runtime._driver_context("/bin/codex") as recovered:
                self.assertIs(recovered, second_driver)

            self.assertEqual(driver_cls.call_count, 2)

    def test_retry_task_starts_background_queue_dispatch(self) -> None:
        from c_orch.run_store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            task_store.update_task(queue, "task-001", status="FAILED", reason="active_run_failed")
            task_store.save(queue)
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, "APPROVED")
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=_scheduler_config(root),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            status, _payload = runtime.task_action("task-001", "retry-task")

            self.assertEqual(int(status), 200)
            self.assertTrue(runtime.wait_for_dispatch(timeout=2))
            loaded = task_store.load()
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual(loaded.tasks[0].status, "APPROVED")
            self.assertIsNone(loaded.tasks[0].reason)
            self.assertEqual(loaded.tasks[0].run_ids, fake.run_ids)
            self.assertEqual(len(fake.run_ids), 1)

    def test_mark_handled_skipped_wakes_waiting_workspace_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {
                        "task_id": "failed-task",
                        "title": "Failed Task",
                        "prompt": "Failed task",
                        "cwd": str(repo),
                    }
                ]
            )
            task_store.update_task(
                queue,
                "failed-task",
                status="FAILED",
                reason="active_run_failed",
            )
            task_store.save(queue)
            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal = proposal_store.add_proposal(
                pool,
                title="Waiting Proposal",
                prompt="Plan next task",
                cwd=str(repo),
            )
            proposal_store.update_proposal(
                pool,
                proposal.proposal_id,
                status=PROPOSAL_WAITING_WORKSPACE,
                reason="workspace_lane",
            )
            proposal_store.save(pool)
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(
                    _scheduler_config(root),
                    cwd=repo,
                    require_proposal_plan_review=True,
                ),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=2))
            self.assertEqual(fake_planner.run_calls, 0)

            status, payload = runtime.task_action("failed-task", "mark-handled-skipped")

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["transition"]["type"], "task_marked_handled_skipped")
            self.assertTrue(fake_planner.started.wait(timeout=2))
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))
            loaded_queue = task_store.load()
            self.assertEqual(loaded_queue.tasks[0].status, "SKIPPED")
            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(proposals[0]["status"], RUN_PLAN_REVIEW_REQUIRED)
            self.assertEqual(fake_planner.run_calls, 1)

    def test_create_proposal_auto_queues_same_planner_run_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            fake = _AutoQueueProposalOrchestrator(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            status, payload = runtime.create_proposal("Task 1", "Do task 1")

            self.assertEqual(int(status), 200)
            run_id = payload["transition"]["run_id"]
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))
            self.assertEqual(fake.planner_run_ids, [run_id])
            self.assertEqual(fake.worker_run_ids, [run_id])
            self.assertEqual(fake.input_statuses, ["NEW", RUN_PLAN_APPROVED])

            proposals = ProposalStore(root / "proposals.json").load().proposals
            self.assertEqual(proposals, [])

            queue = TaskStore(root / "queue.json").load()
            self.assertEqual(queue.status, "APPROVED")
            self.assertEqual(len(queue.tasks), 1)
            task = queue.tasks[0]
            self.assertEqual(task.active_run_id, run_id)
            self.assertEqual(task.run_ids, [run_id])
            self.assertEqual(task.status, "APPROVED")

            loaded_manifest = run_store.load(run_id)
            self.assertEqual(loaded_manifest.status, "APPROVED")
            assert loaded_manifest.plan is not None
            self.assertEqual(loaded_manifest.plan.approval_status, "approved")
            self.assertEqual(loaded_manifest.plan.approved_by, "c-orch:auto")

    def test_create_proposal_auto_queue_waits_for_queue_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            fake = _AutoQueueProposalOrchestrator(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            with runtime._queue_lock:
                status, payload = runtime.create_proposal("Task 1", "Do task 1")
                self.assertEqual(int(status), 200)
                run_id = payload["transition"]["run_id"]

                deadline = time.monotonic() + 2.0
                planner_ready = False
                while time.monotonic() < deadline:
                    try:
                        manifest = run_store.load(run_id)
                    except OSError:
                        time.sleep(0.02)
                        continue
                    if manifest.status == RUN_PLAN_REVIEW_REQUIRED:
                        planner_ready = True
                        break
                    time.sleep(0.02)
                self.assertTrue(planner_ready)
                self.assertFalse(runtime.wait_for_proposal_dispatch(timeout=0.2))

                proposals = ProposalStore(root / "proposals.json").load().proposals
                self.assertEqual(len(proposals), 1)
                self.assertEqual(proposals[0].proposal_id, "task-1")
                self.assertIn(proposals[0].status, {"PLANNING", "PLAN_REVIEW_REQUIRED"})
                queue_file = root / "queue.json"
                if queue_file.exists():
                    self.assertEqual(TaskStore(queue_file).load().tasks, [])

            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))
            self.assertEqual(fake.planner_run_ids, [run_id])
            self.assertEqual(fake.worker_run_ids, [run_id])
            self.assertEqual(fake.input_statuses, ["NEW", RUN_PLAN_APPROVED])

            queue = TaskStore(root / "queue.json").load()
            self.assertEqual(len(queue.tasks), 1)
            self.assertEqual(queue.tasks[0].active_run_id, run_id)
            self.assertEqual(queue.tasks[0].run_ids, [run_id])

    def test_create_proposal_manual_review_mode_keeps_plan_review_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(
                    _scheduler_config(root),
                    cwd=repo,
                    require_proposal_plan_review=True,
                ),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, _payload = runtime.create_proposal("Task 1", "Do task 1")
            self.assertEqual(int(status), 200)
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(len(proposals), 1)
            self.assertEqual(proposals[0]["status"], "PLAN_REVIEW_REQUIRED")
            self.assertEqual(proposals[0]["allowed_actions"], ["approve-plan", "revise-plan"])
            self.assertEqual(fake_planner.run_calls, 1)

            queue_payload = runtime.build_queue_payload()
            self.assertEqual(queue_payload["tasks"], [])

    def test_create_proposal_dirty_workspace_not_auto_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, payload = runtime.create_proposal("Task dirty", "Do task dirty")

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["transition"]["type"], "proposal_created_waiting_workspace_clean")
            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(proposals[0]["status"], "WAITING_WORKSPACE_CLEAN")
            self.assertEqual(fake_planner.run_calls, 0)
            self.assertEqual(runtime.build_queue_payload()["tasks"], [])

    def test_create_proposal_planner_failure_not_auto_queued(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, payload = runtime.create_proposal("Task 1", "Do task 1")
            self.assertEqual(int(status), 200)
            run_id = payload["transition"]["run_id"]
            fake_planner.fail_by_run_id[run_id] = "planner exploded"
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(proposals[0]["status"], "FAILED")
            self.assertEqual(runtime.build_queue_payload()["tasks"], [])

    def test_queue_lane_retry_review_is_policy_gated_and_records_recovery_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1", "cwd": str(repo)}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=repo,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/bin/codex",
            )
            _mark_retryable_review_failure(manifest)
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status="WAITING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
                reason="planner_review_retry",
            )
            task_store.save(queue)
            fake = _RetryReviewQueueOrchestrator(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            self.assertTrue(runtime.dispatch_queue_async())
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))

            self.assertEqual(fake.run_calls, 0)
            self.assertEqual(fake.retry_review_calls, 1)
            self.assertEqual(fake.retry_run_ids, [manifest.run_id])
            events = run_store.load_events(manifest.run_id)
            recovery_events = [
                event for event in events if event.get("type") == "recovery_decision_recorded"
            ]
            self.assertTrue(recovery_events)
            self.assertEqual(recovery_events[-1]["source"], "queue_scheduler")
            self.assertEqual(recovery_events[-1]["recovery_action"], "retry_review")
            self.assertTrue(recovery_events[-1]["automatic"])

    def test_queue_dispatch_runs_different_git_workspaces_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            _init_git_repo(repo_a)
            _init_git_repo(repo_b)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-a", "title": "Task A", "prompt": "Do A", "cwd": str(repo_a)},
                    {"task_id": "task-b", "title": "Task B", "prompt": "Do B", "cwd": str(repo_b)},
                ]
            )
            run_store = RunStore(root / "runs")
            release = threading.Event()
            fake = _BlockingQueueOrchestrator(run_store, release=release)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=replace(_scheduler_config(root), max_parallel_workspaces=2),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            self.assertTrue(runtime.dispatch_queue_async())
            self.assertTrue(fake.two_started.wait(timeout=2))
            loaded = task_store.load()
            self.assertEqual([task.status for task in loaded.tasks], ["RUNNING", "RUNNING"])

            release.set()
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))
            loaded = task_store.load()
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual([task.status for task in loaded.tasks], ["APPROVED", "APPROVED"])
            self.assertEqual(set(fake.user_tasks), {"Do A", "Do B"})

    def test_queue_dispatch_keeps_same_git_workspace_serial(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo-a"
            _init_git_repo(repo)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-a", "title": "Task A", "prompt": "Do A", "cwd": str(repo)},
                    {"task_id": "task-b", "title": "Task B", "prompt": "Do B", "cwd": str(repo)},
                ]
            )
            run_store = RunStore(root / "runs")
            release = threading.Event()
            fake = _BlockingQueueOrchestrator(run_store, release=release)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=replace(_scheduler_config(root), max_parallel_workspaces=2),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            self.assertTrue(runtime.dispatch_queue_async())
            self.assertTrue(fake.started.wait(timeout=2))
            time.sleep(0.1)
            self.assertEqual(fake.user_tasks, ["Do A"])
            queue_payload = runtime.build_queue_payload()
            self.assertEqual(queue_payload["tasks"][1]["waiting_for"], "workspace_lane")

            release.set()
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))
            loaded = task_store.load()
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual([task.status for task in loaded.tasks], ["APPROVED", "APPROVED"])
            self.assertEqual(fake.user_tasks, ["Do A", "Do B"])

    def test_queue_dispatch_failed_lane_does_not_block_other_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            _init_git_repo(repo_a)
            _init_git_repo(repo_b)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-a", "title": "Task A", "prompt": "Do A", "cwd": str(repo_a)},
                    {"task_id": "task-b", "title": "Task B", "prompt": "Do B", "cwd": str(repo_b)},
                ]
            )
            run_store = RunStore(root / "runs")
            release = threading.Event()
            fake = _BlockingQueueOrchestrator(
                run_store,
                release=release,
                statuses={"Do A": "FAILED", "Do B": "APPROVED"},
            )
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=replace(_scheduler_config(root), max_parallel_workspaces=2),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            self.assertTrue(runtime.dispatch_queue_async())
            self.assertTrue(fake.two_started.wait(timeout=2))
            release.set()
            self.assertTrue(runtime.wait_for_dispatch(timeout=3))
            loaded = task_store.load()

            self.assertEqual(loaded.status, "FAILED")
            self.assertEqual([task.status for task in loaded.tasks], ["FAILED", "APPROVED"])
            self.assertEqual(set(fake.user_tasks), {"Do A", "Do B"})

    def test_proposal_approve_dispatches_existing_approved_run_to_worker(self) -> None:
        from c_orch.run_store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=repo,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/bin/codex",
            )
            manifest.status = RUN_PLAN_REVIEW_REQUIRED
            manifest.plan = PlanRecord(summary="Plan summary", worker_prompt="Do the work")
            run_store.save(manifest)

            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal = proposal_store.add_proposal(pool, title="Task 1", prompt="Do task 1")
            proposal_store.update_proposal(
                pool,
                proposal.proposal_id,
                run_id=manifest.run_id,
                status=RUN_PLAN_REVIEW_REQUIRED,
                cwd=str(repo),
            )
            proposal_store.save(pool)

            fake = _FakeOrchestrator(run_store, "APPROVED")
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=_scheduler_config(root),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            status, payload = runtime.proposal_action(proposal.proposal_id, "approve-plan")

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["transition"]["type"], "proposal_approved_and_queued")
            self.assertEqual(payload["transition"]["run_id"], manifest.run_id)
            self.assertEqual(payload["transition"]["task_id"], proposal.proposal_id)
            self.assertIn("state", payload)
            self.assertEqual(payload["state"]["selected_run"]["run"]["run_id"], manifest.run_id)
            self.assertIn("timing", payload["state"]["selected_run"]["run"])
            self.assertGreater(payload["state_version"], 0)
            self.assertTrue(runtime.wait_for_dispatch(timeout=2))

            loaded_manifest = run_store.load(manifest.run_id)
            self.assertEqual(loaded_manifest.status, "APPROVED")
            self.assertEqual(fake.input_statuses, [RUN_PLAN_APPROVED])
            self.assertEqual(fake.run_calls, 1)
            self.assertEqual(fake.run_ids, [manifest.run_id])

            loaded_pool = proposal_store.load()
            self.assertEqual(loaded_pool.proposals, [])

            queue = TaskStore(root / "queue.json").load()
            self.assertEqual(queue.status, "APPROVED")
            self.assertEqual(len(queue.tasks), 1)
            self.assertEqual(queue.tasks[0].task_id, proposal.proposal_id)
            self.assertEqual(queue.tasks[0].status, "APPROVED")
            self.assertEqual(queue.tasks[0].cwd, str(repo))
            self.assertEqual(queue.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(queue.tasks[0].run_ids, [manifest.run_id])

            manifests = list((root / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)

    def test_prune_queued_proposals_removes_legacy_plan_pool_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal_store = ProposalStore(root / "proposals.json")
            proposal_store.save(
                ProposalPool(
                    proposals=[
                        ProposalRecord(
                            proposal_id="queued",
                            title="Queued",
                            prompt="Already queued",
                            status=PROPOSAL_QUEUED,
                        ),
                        ProposalRecord(
                            proposal_id="review",
                            title="Review",
                            prompt="Needs review",
                            status=RUN_PLAN_REVIEW_REQUIRED,
                        ),
                    ],
                )
            )

            removed = prune_queued_proposals(root / "proposals.json")

            self.assertEqual(removed, 1)
            loaded = proposal_store.load()
            self.assertEqual([proposal.proposal_id for proposal in loaded.proposals], ["review"])

    def test_queue_action_requires_queue_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")
            status, payload = runtime.queue_action("confirm-runtime-restarted")
            self.assertEqual(int(status), 400)
            self.assertEqual(payload["error"], "missing queue file")

    def test_create_proposal_returns_immediately_and_dispatches_in_background(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            block_event = threading.Event()
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(
                run_store,
                block_event=block_event,
                block_first_call=True,
            )
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            start = time.monotonic()
            status, payload = runtime.create_proposal("Task 1", "Do task 1")
            elapsed = time.monotonic() - start

            self.assertEqual(int(status), 200)
            self.assertLess(elapsed, 0.5)
            self.assertEqual(payload["transition"]["type"], "proposal_created")
            proposal = ProposalStore(root / "proposals.json").load().proposals[0]
            self.assertEqual(proposal.status, "PLANNING")
            self.assertIsNotNone(proposal.run_id)
            self.assertTrue(fake_planner.started.wait(timeout=1.0))

            block_event.set()
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

    def test_create_proposal_dirty_workspace_waits_for_clean_without_planner_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, payload = runtime.create_proposal("Task dirty", "Do task dirty")

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["transition"]["type"], "proposal_created_waiting_workspace_clean")
            proposal = ProposalStore(root / "proposals.json").load().proposals[0]
            self.assertEqual(proposal.status, "WAITING_WORKSPACE_CLEAN")
            self.assertIsNone(proposal.run_id)
            self.assertIsNotNone(proposal.blocker)
            assert proposal.blocker is not None
            self.assertEqual(
                proposal.blocker["message"],
                "目标工作区存在未提交改动。建议先提交或处理这些改动，再生成计划。",
            )
            self.assertIn("dirty.txt", proposal.blocker["status_output"])
            self.assertEqual(fake_planner.run_calls, 0)
            self.assertEqual(list((root / "runs").glob("*/manifest.json")), [])

    def test_retry_plan_rechecks_workspace_and_runs_planner_after_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            dirty_path = repo / "dirty.txt"
            dirty_path.write_text("dirty\n", encoding="utf-8")
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            create_status, _create_payload = runtime.create_proposal("Task retry", "Do task retry")
            self.assertEqual(int(create_status), 200)
            proposal_id = ProposalStore(root / "proposals.json").load().proposals[0].proposal_id

            retry_dirty_status, retry_dirty_payload = runtime.proposal_action(proposal_id, "retry-plan")
            self.assertEqual(int(retry_dirty_status), 200)
            self.assertEqual(
                retry_dirty_payload["transition"]["type"],
                "proposal_retry_waiting_workspace_clean",
            )
            self.assertEqual(fake_planner.run_calls, 0)

            dirty_path.unlink()
            retry_clean_status, retry_clean_payload = runtime.proposal_action(proposal_id, "retry-plan")
            self.assertEqual(int(retry_clean_status), 200)
            self.assertEqual(retry_clean_payload["transition"]["type"], "proposal_retry_planning")
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(proposals[0]["status"], "PLAN_REVIEW_REQUIRED")
            self.assertEqual(proposals[0]["allowed_actions"], ["approve-plan", "revise-plan"])
            self.assertEqual(fake_planner.run_calls, 1)

    def test_proposal_background_planning_success_updates_plan_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, payload = runtime.create_proposal("Task 1", "Do task 1")
            self.assertEqual(int(status), 200)
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(len(proposals), 1)
            proposal = proposals[0]
            self.assertEqual(proposal["status"], "PLAN_REVIEW_REQUIRED")
            self.assertEqual(proposal["allowed_actions"], ["approve-plan", "revise-plan"])
            self.assertIsNotNone(proposal["plan_detail"])
            self.assertEqual(proposal["plan_detail"]["summary"], f"Plan for {proposal['run_id']}")
            self.assertEqual(proposal["plan_detail"]["worker_prompt"], "Implement Do task 1")
            self.assertEqual(proposal["plan_detail"]["acceptance_criteria"], [f"accept-{proposal['run_id']}"])
            self.assertEqual(proposal["plan_detail"]["verification_commands"], ["pytest -q"])

    def test_proposal_background_planning_failure_persists_failed_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            block_event = threading.Event()
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(
                run_store,
                block_event=block_event,
                block_first_call=True,
            )
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status, payload = runtime.create_proposal("Task 1", "Do task 1")
            self.assertEqual(int(status), 200)
            run_id = payload["transition"]["run_id"]
            self.assertTrue(fake_planner.started.wait(timeout=1.0))
            fake_planner.fail_by_run_id[run_id] = "planner exploded"
            block_event.set()
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))

            proposals_payload = runtime.build_proposals_payload()
            proposal = proposals_payload["proposals"][0]
            self.assertEqual(proposal["status"], "FAILED")
            self.assertIn("planner exploded", proposal["error"])
            self.assertEqual(proposal["reason"], "proposal_planning_failed")
            manifest = run_store.load(run_id)
            self.assertEqual(manifest.status, RUN_FAILED)

            manifest.status = "NEW"
            run_store.save(manifest)
            refreshed = runtime.build_proposals_payload()["proposals"][0]
            self.assertEqual(refreshed["status"], "FAILED")

    def test_create_multiple_proposals_keep_unique_run_ids_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            block_event = threading.Event()
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(
                run_store,
                block_event=block_event,
                block_first_call=True,
            )
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            runtime.create_proposal("Task 1", "Do task 1")
            self.assertTrue(fake_planner.started.wait(timeout=1.0))
            runtime.create_proposal("Task 2", "Do task 2")
            runtime.create_proposal("Task 3", "Do task 3")
            pool = ProposalStore(root / "proposals.json").load()
            self.assertEqual(len(pool.proposals), 3)
            self.assertEqual([proposal.status for proposal in pool.proposals], ["PLANNING", "WAITING_WORKSPACE", "WAITING_WORKSPACE"])
            run_ids_before = [proposal.run_id for proposal in pool.proposals]
            self.assertIsNotNone(run_ids_before[0])
            self.assertEqual(run_ids_before[1:], [None, None])

            block_event.set()
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=4))
            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(len(proposals), 3)
            self.assertEqual(
                [proposal["status"] for proposal in proposals],
                ["PLAN_REVIEW_REQUIRED", "WAITING_WORKSPACE", "WAITING_WORKSPACE"],
            )
            self.assertEqual(proposals[1]["waiting_for"], "workspace_lane")

    def test_proposal_planning_runs_different_workspaces_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_a = root / "repo-a"
            repo_b = root / "repo-b"
            _init_git_repo(repo_a)
            _init_git_repo(repo_b)
            block_event = threading.Event()
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store, block_event=block_event)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(
                    _scheduler_config(root),
                    cwd=repo_a,
                    max_parallel_workspaces=2,
                ),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )

            status_a, _payload_a = runtime.create_proposal("Task A", "Do task A", str(repo_a))
            self.assertEqual(int(status_a), 200)
            self.assertTrue(fake_planner.started.wait(timeout=1.0))
            status_b, _payload_b = runtime.create_proposal("Task B", "Do task B", str(repo_b))
            self.assertEqual(int(status_b), 200)

            self.assertTrue(fake_planner.two_started.wait(timeout=2.0))
            self.assertEqual(fake_planner.run_calls, 2)
            self.assertEqual(runtime._active_proposal_lane_count(), 2)

            block_event.set()
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))
            proposals = runtime.build_proposals_payload()["proposals"]
            self.assertEqual(
                [proposal["status"] for proposal in proposals],
                ["PLAN_REVIEW_REQUIRED", "PLAN_REVIEW_REQUIRED"],
            )

    def test_create_proposal_with_task_cwd_uses_target_repo_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard_repo = root / "dashboard-repo"
            _init_git_repo(dashboard_repo)
            target_repo = root / "target-repo"
            _init_git_repo(target_repo)
            worktree_repo_paths: List[Path] = []
            run_store = RunStore(root / "runs")
            fake_planner = _FakeProposalPlanner(run_store)

            def worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
                worktree_repo_paths.append(repo_path)
                worktree = worktrees_dir / run_id / worker_id
                worktree.mkdir(parents=True, exist_ok=True)
                return worktree

            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=dashboard_repo),
                driver_factory=_fake_driver_factory,
                worktree_factory=worktree_factory,
                orchestrator_factory=lambda: fake_planner,
            )
            status, _payload = runtime.create_proposal("Task 1", "Do task 1", "../target-repo")

            self.assertEqual(int(status), 200)
            self.assertTrue(runtime.wait_for_proposal_dispatch(timeout=3))
            proposal_store = ProposalStore(root / "proposals.json")
            proposal = proposal_store.load().proposals[0]
            manifest = run_store.load(proposal.run_id or "")

            self.assertEqual(Path(proposal.cwd or "").resolve(), target_repo.resolve())
            self.assertEqual(Path(manifest.cwd).resolve(), target_repo.resolve())
            self.assertEqual([path.resolve() for path in worktree_repo_paths], [target_repo.resolve()])

    def test_create_proposal_rejects_invalid_task_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dashboard_repo = root / "dashboard-repo"
            _init_git_repo(dashboard_repo)
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                proposals_path=root / "proposals.json",
                scheduler_config=replace(_scheduler_config(root), cwd=dashboard_repo),
                driver_factory=_fake_driver_factory,
            )

            status, payload = runtime.create_proposal("Task 1", "Do task 1", "./missing-repo")

            self.assertEqual(int(status), 400)
            self.assertIn("cwd does not exist", payload["error"])
            proposals = ProposalStore(root / "proposals.json").load_or_create()
            self.assertEqual(proposals.proposals, [])


def _scheduler_config(root: Path) -> SchedulerConfig:
    _init_git_repo(root / "repo")
    return SchedulerConfig(
        cwd=root / "repo",
        runs_dir=root / "runs",
        worktrees_dir=root / "worktrees",
        planner_model="gpt-5.5",
        worker_model="gpt-5.3-codex-spark",
        codex_binary_path="/bin/codex",
        max_attempts=2,
        sandbox="workspace-write",
        approval_policy="never",
    )


@contextmanager
def _fake_driver_factory(_codex_path: str):  # type: ignore[no-untyped-def]
    yield object()


def _fake_worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
    _ = repo_path
    worktree = worktrees_dir / run_id / worker_id
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _mark_retryable_review_failure(manifest) -> None:  # type: ignore[no-untyped-def]
    manifest.status = RUN_WORK_DONE
    manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
    manifest.review_attempts = [
        ReviewAttemptRecord(
            id="review-1",
            worker_id="worker-1",
            status=REVIEW_ATTEMPT_FAILED_RETRYABLE,
            started_at="2026-05-18T00:00:00+00:00",
            completed_at="2026-05-18T00:01:00+00:00",
            reason="planner_review_failed",
            error="Timed out waiting for MCP server output",
            evidence_files=["evidence/review.patch"],
        )
    ]


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
