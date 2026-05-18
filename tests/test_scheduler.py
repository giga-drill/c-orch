from __future__ import annotations

import tempfile
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from typing import List, Tuple

from c_orch.run_store import ReviewAttemptRecord, ReviewRecord, RunStore
from c_orch.scheduler import SchedulerConfig, TaskScheduler
from c_orch.states import REVIEW_ATTEMPT_FAILED_RETRYABLE, RUN_WORK_DONE
from c_orch.task_store import (
    QUEUE_APPROVED,
    QUEUE_FAILED,
    QUEUE_RESTART_REQUIRED,
    TASK_APPROVED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_WAITING,
    TaskStore,
)
from c_orch.workspace_lanes import workspace_slug


class _FakeOrchestrator:
    def __init__(
        self,
        run_store: RunStore,
        outcomes: List[Tuple[str, bool]],
        retry_outcomes: List[Tuple[str, bool]] | None = None,
    ) -> None:
        self.run_store = run_store
        self.outcomes = list(outcomes)
        self.retry_outcomes = list(retry_outcomes or [])
        self.run_ids: List[str] = []
        self.retry_run_ids: List[str] = []
        self.run_calls = 0
        self.retry_review_calls = 0

    def run(self, manifest):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        status, requires_restart = self.outcomes.pop(0)
        manifest.status = status
        manifest.requires_restart = requires_restart
        self.run_store.save(manifest)
        self.run_ids.append(manifest.run_id)
        return manifest

    def retry_review(self, manifest):  # type: ignore[no-untyped-def]
        self.retry_review_calls += 1
        status, requires_restart = self.retry_outcomes.pop(0)
        manifest.status = status
        manifest.requires_restart = requires_restart
        self.run_store.save(manifest)
        self.retry_run_ids.append(manifest.run_id)
        return manifest


class SchedulerTests(unittest.TestCase):
    def test_active_run_waiting_plan_review_fails_queue_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_PENDING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_FAILED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_FAILED)
            self.assertEqual(loaded.tasks[0].reason, "plan_review_outside_proposal_pool")
            self.assertEqual(loaded.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(fake.run_ids, [])
            self.assertEqual(fake.run_calls, 0)
            self.assertEqual(fake.retry_review_calls, 0)

    def test_new_run_waiting_plan_review_fails_queue_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("PLAN_REVIEW_REQUIRED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_FAILED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_FAILED)
            self.assertEqual(loaded.tasks[0].reason, "plan_review_outside_proposal_pool")
            self.assertEqual(loaded.tasks[0].active_run_id, fake.run_ids[0])
            self.assertEqual(loaded.tasks[0].run_ids, fake.run_ids)
            self.assertEqual(fake.retry_review_calls, 0)

    def test_active_retryable_review_failure_auto_retries_and_approves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            _mark_retryable_review_failure(manifest)
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_WAITING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
                reason="planner_review_retry",
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, outcomes=[], retry_outcomes=[("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded_manifest = run_store.load(manifest.run_id)
            loaded_queue = task_store.load()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(loaded_queue.tasks[0].status, TASK_APPROVED)
            self.assertEqual(fake.run_calls, 0)
            self.assertEqual(fake.retry_review_calls, 1)
            self.assertEqual(fake.retry_run_ids, [manifest.run_id])
            self.assertEqual(loaded_manifest.review.evidence_files, ["evidence/review.patch"])  # type: ignore[union-attr]
            events = run_store.load_events(manifest.run_id)
            recovery_events = [
                event for event in events if event.get("type") == "recovery_decision_recorded"
            ]
            self.assertTrue(recovery_events)
            self.assertEqual(recovery_events[-1]["source"], "queue_scheduler")
            self.assertEqual(recovery_events[-1]["recovery_action"], "retry_review")
            self.assertTrue(recovery_events[-1]["automatic"])

    def test_active_retryable_review_failure_auto_retries_and_sets_restart_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            _mark_retryable_review_failure(manifest)
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_WAITING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
                reason="planner_review_retry",
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, outcomes=[], retry_outcomes=[("APPROVED", True)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()

            self.assertEqual(queue.status, QUEUE_RESTART_REQUIRED)
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[1].status, TASK_PENDING)
            self.assertEqual(fake.run_calls, 0)
            self.assertEqual(fake.retry_review_calls, 1)

    def test_runs_two_pending_tasks_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False), ("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            loaded = task_store.load()
            self.assertEqual([task.status for task in loaded.tasks], [TASK_APPROVED, TASK_APPROVED])
            self.assertEqual(len(loaded.tasks[0].run_ids), 1)
            self.assertEqual(len(loaded.tasks[1].run_ids), 1)
            self.assertEqual(loaded.tasks[0].active_run_id, loaded.tasks[0].run_ids[-1])
            self.assertEqual(loaded.tasks[1].active_run_id, loaded.tasks[1].run_ids[-1])
            self.assertEqual(len(fake.run_ids), 2)

    def test_max_tasks_marks_queue_approved_when_last_task_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=replace(_config(root), max_tasks=1),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(loaded.status, QUEUE_APPROVED)
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(len(fake.run_ids), 1)

    def test_max_tasks_keeps_queue_pending_when_more_tasks_remain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=replace(_config(root), max_tasks=1),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()

            self.assertEqual(queue.status, "PENDING")
            self.assertEqual(loaded.status, "PENDING")
            self.assertEqual([task.status for task in loaded.tasks], [TASK_APPROVED, TASK_PENDING])
            self.assertEqual(len(fake.run_ids), 1)

    def test_mixed_task_cwd_creates_runs_and_worktrees_per_task_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_one = root / "repo-one"
            repo_two = root / "repo-two"
            _init_git_repo(repo_one)
            _init_git_repo(repo_two)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {
                        "task_id": "task-001",
                        "title": "Task 1",
                        "prompt": "Do task 1",
                        "cwd": str(repo_one),
                    },
                    {
                        "task_id": "task-002",
                        "title": "Task 2",
                        "prompt": "Do task 2",
                        "cwd": str(repo_two),
                    },
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False), ("APPROVED", False)])
            repo_paths: List[Path] = []

            def worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
                repo_paths.append(repo_path)
                worktree = worktrees_dir / run_id / worker_id
                worktree.mkdir(parents=True, exist_ok=True)
                return worktree

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()
            first_manifest = run_store.load(loaded.tasks[0].run_ids[-1])
            second_manifest = run_store.load(loaded.tasks[1].run_ids[-1])

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(Path(first_manifest.cwd).resolve(), repo_one.resolve())
            self.assertEqual(Path(second_manifest.cwd).resolve(), repo_two.resolve())
            self.assertEqual([path.resolve() for path in repo_paths], [repo_one.resolve(), repo_two.resolve()])

    def test_default_worktrees_dir_rebinds_under_each_task_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime_cwd = root / "runtime-repo"
            _init_git_repo(runtime_cwd)
            repo_one = root / "repo-one"
            _init_git_repo(repo_one)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {
                        "task_id": "task-001",
                        "title": "Task 1",
                        "prompt": "Do task 1",
                        "cwd": str(repo_one),
                    }
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])
            seen_worktrees_dirs: List[Path] = []

            def worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
                _ = repo_path
                seen_worktrees_dirs.append(worktrees_dir)
                worktree = worktrees_dir / run_id / worker_id
                worktree.mkdir(parents=True, exist_ok=True)
                return worktree

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=SchedulerConfig(
                    cwd=runtime_cwd,
                    runs_dir=root / "runs",
                    worktrees_dir=runtime_cwd / ".c-orch" / "worktrees",
                    planner_model="gpt-5.5",
                    worker_model="gpt-5.3-codex-spark",
                    codex_binary_path="/bin/codex",
                    max_attempts=2,
                    sandbox="workspace-write",
                    approval_policy="never",
                ),
                worktree_factory=worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(
                [path.resolve() for path in seen_worktrees_dirs],
                [(runtime_cwd / ".c-orch" / "worktrees" / workspace_slug(repo_one)).resolve()],
            )

    def test_retry_preserves_task_cwd_for_new_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_target = root / "target-repo"
            _init_git_repo(repo_target)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {
                        "task_id": "task-001",
                        "title": "Task 1",
                        "prompt": "Do task 1",
                        "cwd": str(repo_target),
                    }
                ]
            )
            task_store.update_task(queue, "task-001", status=TASK_FAILED, reason="active_run_failed")
            task_store.save(queue)
            queue = task_store.load()
            task_store.update_task(queue, "task-001", status=TASK_PENDING, reason=None)
            task_store.save(queue)

            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])
            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()
            manifest = run_store.load(loaded.tasks[0].active_run_id or loaded.tasks[0].run_ids[-1])

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(Path(loaded.tasks[0].cwd or "").resolve(), repo_target.resolve())
            self.assertEqual(Path(manifest.cwd).resolve(), repo_target.resolve())

    def test_pending_task_with_approved_plan_run_continues_existing_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            manifest.status = "PLAN_APPROVED"
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_PENDING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(loaded.tasks[0].run_ids, [manifest.run_id])
            self.assertEqual(fake.run_ids, [manifest.run_id])
            self.assertEqual(Path(run_store.load(manifest.run_id).cwd).resolve(), (root / "repo").resolve())

    def test_active_run_keeps_manifest_cwd_when_scheduler_default_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_bound = root / "repo-bound"
            _init_git_repo(repo_bound)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=repo_bound,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            manifest.status = "PLAN_APPROVED"
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_PENDING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])
            changed_default = replace(_config(root), cwd=root / "different-default-repo")

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=changed_default,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            self.assertEqual(fake.run_ids, [manifest.run_id])
            self.assertEqual(Path(run_store.load(manifest.run_id).cwd).resolve(), repo_bound.resolve())

    def test_restart_required_stops_after_current_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", True)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_RESTART_REQUIRED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[1].status, TASK_PENDING)
            self.assertEqual(len(fake.run_ids), 1)

    def test_restart_required_resume_hard_stops_before_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            manifest.status = "APPROVED"
            manifest.requires_restart = True
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_RUNNING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, outcomes=[("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()

            self.assertEqual(queue.status, QUEUE_RESTART_REQUIRED)
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[1].status, TASK_PENDING)
            self.assertEqual(fake.run_calls, 0)
            self.assertEqual(fake.retry_review_calls, 0)

    def test_retry_review_failure_keeps_evidence_and_waiting_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root / "repo",
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            _mark_retryable_review_failure(manifest)
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_WAITING,
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
                reason="planner_review_retry",
            )
            task_store.save(queue)
            fake = _FakeOrchestrator(run_store, outcomes=[], retry_outcomes=[(RUN_WORK_DONE, False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()
            loaded = task_store.load()
            loaded_manifest = run_store.load(manifest.run_id)

            self.assertEqual(queue.status, "RUNNING")
            self.assertEqual(loaded.tasks[0].status, TASK_WAITING)
            self.assertEqual(loaded.tasks[0].reason, "planner_review_retry")
            self.assertEqual(fake.retry_review_calls, 1)
            self.assertEqual(loaded_manifest.status, RUN_WORK_DONE)
            self.assertEqual(loaded_manifest.review.evidence_files, ["evidence/review.patch"])  # type: ignore[union-attr]
            self.assertEqual(loaded_manifest.review_attempts[-1].status, REVIEW_ATTEMPT_FAILED_RETRYABLE)

    def test_resume_skips_approved_and_runs_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_APPROVED,
                active_run_id="run-prev",
                run_ids=["run-prev"],
            )
            task_store.save(queue)
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_APPROVED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].run_ids, ["run-prev"])
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[1].status, TASK_APPROVED)
            self.assertEqual(len(fake.run_ids), 1)

    def test_failure_stops_queue_and_preserves_remaining_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("FAILED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_FAILED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, "FAILED")
            self.assertEqual(loaded.tasks[1].status, TASK_PENDING)
            self.assertEqual(len(fake.run_ids), 1)

    def test_resume_does_not_skip_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                ]
            )
            task_store.update_task(queue, "task-001", status=TASK_FAILED, reason="FAILED")
            task_store.save(queue)
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, QUEUE_FAILED)
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_FAILED)
            self.assertEqual(loaded.tasks[1].status, TASK_PENDING)
            self.assertEqual(fake.run_ids, [])

    def test_resume_does_not_mark_running_task_queue_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                ]
            )
            task_store.update_task(queue, "task-001", status=TASK_RUNNING, active_run_id="run-1")
            task_store.save(queue)
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, [("APPROVED", False)])

            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=object(),  # type: ignore[arg-type]
                config=_config(root),
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )
            queue = scheduler.run()

            self.assertEqual(queue.status, "RUNNING")
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_RUNNING)
            self.assertEqual(fake.run_ids, [])


def _config(root: Path) -> SchedulerConfig:
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


def _fake_worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
    _ = repo_path
    worktree = worktrees_dir / run_id / worker_id
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _mark_retryable_review_failure(manifest) -> None:  # type: ignore[no-untyped-def]
    manifest.status = RUN_WORK_DONE
    manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
    manifest.review_attempts = [
        ReviewAttemptRecord(
            id="review-1",
            worker_id="worker-1",
            status=REVIEW_ATTEMPT_FAILED_RETRYABLE,
            started_at="2026-05-13T00:00:00+00:00",
            completed_at="2026-05-13T00:01:00+00:00",
            evidence_files=["evidence/review.patch"],
            error="Timed out waiting for MCP server output",
        )
    ]


if __name__ == "__main__":
    unittest.main()
