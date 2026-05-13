from __future__ import annotations

import tempfile
import unittest
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
