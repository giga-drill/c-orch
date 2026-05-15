from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch.run_store import RunStore
from c_orch.task_lifecycle import (
    REASON_PLAN_REVIEW_OUTSIDE_PROPOSAL_POOL,
    WAITING_RESTART,
    WAITING_RETRY_TASK,
    WAITING_SKIPPED,
    derive_run_waiting_for,
    mark_task_handled_skipped,
    mark_task_for_retry,
    reconcile_queue,
)
from c_orch.task_store import (
    QUEUE_FAILED,
    QUEUE_RESTART_REQUIRED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_SKIPPED,
    TaskStore,
)


class TaskLifecycleTests(unittest.TestCase):
    def test_active_failed_run_marks_task_failed_and_queue_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
            )
            manifest.status = "FAILED"
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-001",
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )

            changed = reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)

            self.assertTrue(changed)
            self.assertEqual(queue.status, QUEUE_FAILED)
            self.assertEqual(queue.tasks[0].status, TASK_FAILED)
            self.assertEqual(queue.tasks[0].reason, "active_run_failed")

    def test_active_plan_review_run_marks_queue_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
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

            changed = reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)

            self.assertTrue(changed)
            self.assertEqual(queue.status, QUEUE_FAILED)
            self.assertEqual(queue.tasks[0].status, TASK_FAILED)
            self.assertEqual(queue.tasks[0].reason, REASON_PLAN_REVIEW_OUTSIDE_PROPOSAL_POOL)

    def test_approved_run_requiring_restart_marks_queue_restart_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
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
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )

            changed = reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)

            self.assertTrue(changed)
            self.assertEqual(queue.status, QUEUE_RESTART_REQUIRED)
            self.assertEqual(queue.tasks[0].status, "APPROVED")
            self.assertEqual(derive_run_waiting_for(manifest), WAITING_RESTART)

    def test_failed_task_waits_for_retry_without_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            task_store.update_task(queue, "task-001", status=TASK_FAILED, reason="active_run_failed")

            changed = reconcile_queue(queue, run_loader=lambda _run_id: None, now_iso=lambda: "now")

            self.assertTrue(changed)
            self.assertEqual(queue.status, QUEUE_FAILED)
            self.assertEqual(queue.tasks[0].status, TASK_FAILED)
            self.assertEqual(WAITING_RETRY_TASK, "retry_task")

    def test_mark_task_for_retry_preserves_prior_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            task_store.update_task(
                queue,
                "task-001",
                status=TASK_FAILED,
                active_run_id="run-1",
                run_ids=["run-1"],
                reason="active_run_failed",
                error="apply failed",
            )

            task = mark_task_for_retry(queue, task_id="task-001")

            self.assertEqual(task.status, TASK_PENDING)
            self.assertIsNone(task.active_run_id)
            self.assertEqual(task.run_ids, ["run-1"])
            self.assertIsNone(task.reason)
            self.assertIsNone(task.error)

    def test_mark_task_handled_skipped_stops_blocking_queue(self) -> None:
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
                status=TASK_FAILED,
                active_run_id="run-1",
                run_ids=["run-1"],
                reason="active_run_failed",
                error="apply failed",
            )

            task = mark_task_handled_skipped(queue, task_id="task-001", completed_at="now")

            self.assertEqual(task.status, TASK_SKIPPED)
            self.assertEqual(task.completed_at, "now")
            self.assertEqual(task.run_ids, ["run-1"])
            self.assertEqual(queue.status, "PENDING")

            changed = reconcile_queue(queue, run_loader=lambda _run_id: None, now_iso=lambda: "later")

            self.assertFalse(changed)
            self.assertEqual(queue.tasks[0].status, TASK_SKIPPED)
            self.assertEqual(WAITING_SKIPPED, "skipped")


if __name__ == "__main__":
    unittest.main()
