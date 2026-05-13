from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import List, Tuple

from c_orch.run_store import RunStore
from c_orch.scheduler import SchedulerConfig, TaskScheduler
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


class _FakeOrchestrator:
    def __init__(self, run_store: RunStore, outcomes: List[Tuple[str, bool]]) -> None:
        self.run_store = run_store
        self.outcomes = list(outcomes)
        self.run_ids: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        status, requires_restart = self.outcomes.pop(0)
        manifest.status = status
        manifest.requires_restart = requires_restart
        self.run_store.save(manifest)
        self.run_ids.append(manifest.run_id)
        return manifest


class SchedulerTests(unittest.TestCase):
    def test_active_run_waiting_plan_review_keeps_task_running(self) -> None:
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

            self.assertEqual(queue.status, "RUNNING")
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_WAITING)
            self.assertEqual(loaded.tasks[0].reason, "human_plan_review")
            self.assertEqual(loaded.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(fake.run_ids, [])

    def test_new_run_waiting_plan_review_keeps_task_running(self) -> None:
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

            self.assertEqual(queue.status, "RUNNING")
            loaded = task_store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_WAITING)
            self.assertEqual(loaded.tasks[0].reason, "human_plan_review")
            self.assertEqual(loaded.tasks[0].active_run_id, fake.run_ids[0])
            self.assertEqual(loaded.tasks[0].run_ids, fake.run_ids)

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


if __name__ == "__main__":
    unittest.main()
