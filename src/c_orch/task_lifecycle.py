from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .failure_policy import has_retryable_review_failure
from .run_store import RunManifest
from .states import (
    RUN_APPROVED,
    RUN_FAILED,
    RUN_NEW,
    RUN_PLAN_APPROVED,
    RUN_PLAN_READY,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_PLAN_REVISING,
    RUN_PLANNING,
    RUN_REVIEWING,
    RUN_REVISION_REQUESTED,
    RUN_WORK_DONE,
    RUN_WORKING,
)
from .task_store import (
    QUEUE_APPROVED,
    QUEUE_BLOCKED,
    QUEUE_FAILED,
    QUEUE_PENDING,
    QUEUE_RESTART_REQUIRED,
    QUEUE_RUNNING,
    TASK_APPROVED,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_WAITING,
    TaskQueue,
    TaskRecord,
)


WAITING_PLANNER = "planner"
WAITING_HUMAN_PLAN_REVIEW = "human_plan_review"
WAITING_PLANNER_REVISION = "planner_revision"
WAITING_WORKER = "worker"
WAITING_PLANNER_REVIEW = "planner_review"
WAITING_PLANNER_REVIEW_RETRY = "planner_review_retry"
WAITING_WORKER_REWORK = "worker_rework"
WAITING_RETRY_TASK = "retry_task"
WAITING_RESTART = "restart"
WAITING_DONE = "done"
WAITING_FAILED = "failed"


@dataclass(frozen=True)
class TaskProgress:
    status: str
    waiting_for: str
    reason: Optional[str] = None


def derive_run_waiting_for(run: RunManifest) -> str:
    if run.status == RUN_APPROVED:
        return WAITING_RESTART if run.requires_restart else WAITING_DONE
    if run.status == RUN_FAILED:
        return WAITING_FAILED
    if has_retryable_review_failure(run):
        return WAITING_PLANNER_REVIEW_RETRY
    if run.status in {RUN_NEW, RUN_PLANNING}:
        return WAITING_PLANNER
    if run.status in {RUN_PLAN_READY, RUN_PLAN_REVIEW_REQUIRED}:
        return WAITING_HUMAN_PLAN_REVIEW
    if run.status == RUN_PLAN_REVISING:
        return WAITING_PLANNER_REVISION
    if run.status in {RUN_PLAN_APPROVED, RUN_WORKING}:
        return WAITING_WORKER
    if run.status in {RUN_WORK_DONE, RUN_REVIEWING}:
        return WAITING_PLANNER_REVIEW
    if run.status == RUN_REVISION_REQUESTED:
        return WAITING_WORKER_REWORK
    return WAITING_PLANNER


def derive_task_progress(
    task: TaskRecord,
    *,
    active_run: Optional[RunManifest] = None,
) -> TaskProgress:
    if active_run is not None:
        waiting_for = derive_run_waiting_for(active_run)
        if active_run.status == RUN_APPROVED:
            return TaskProgress(status=TASK_APPROVED, waiting_for=waiting_for)
        if active_run.status == RUN_FAILED:
            return TaskProgress(
                status=TASK_FAILED,
                waiting_for=WAITING_RETRY_TASK,
                reason="active_run_failed",
            )
        if waiting_for in {WAITING_HUMAN_PLAN_REVIEW, WAITING_PLANNER_REVIEW_RETRY}:
            return TaskProgress(status=TASK_WAITING, waiting_for=waiting_for, reason=waiting_for)
        return TaskProgress(status=TASK_RUNNING, waiting_for=waiting_for, reason=waiting_for)

    if task.status == TASK_APPROVED:
        return TaskProgress(status=TASK_APPROVED, waiting_for=WAITING_DONE)
    if task.status == TASK_FAILED:
        return TaskProgress(status=TASK_FAILED, waiting_for=WAITING_RETRY_TASK, reason=task.reason)
    if task.status == TASK_BLOCKED:
        return TaskProgress(status=TASK_BLOCKED, waiting_for=WAITING_FAILED, reason=task.reason)
    if task.status == TASK_WAITING:
        return TaskProgress(status=TASK_WAITING, waiting_for=task.reason or WAITING_HUMAN_PLAN_REVIEW)
    if task.status == TASK_RUNNING:
        return TaskProgress(status=TASK_RUNNING, waiting_for=task.reason or WAITING_WORKER)
    return TaskProgress(status=TASK_PENDING, waiting_for=WAITING_PLANNER)


def reconcile_task_with_active_run(
    task: TaskRecord,
    *,
    active_run: Optional[RunManifest],
    completed_at: Optional[str],
) -> bool:
    progress = derive_task_progress(task, active_run=active_run)
    changed = False

    if task.status != progress.status:
        task.status = progress.status
        changed = True
    if task.reason != progress.reason:
        task.reason = progress.reason
        changed = True
    if task.status in {TASK_RUNNING, TASK_WAITING, TASK_PENDING} and task.error is not None:
        task.error = None
        changed = True
    if task.status == TASK_APPROVED and task.completed_at is None:
        task.completed_at = completed_at
        changed = True
    if task.status != TASK_APPROVED and task.completed_at is not None:
        task.completed_at = None
        changed = True
    return changed


def reconcile_queue(
    queue: TaskQueue,
    *,
    run_loader,
    now_iso,
) -> bool:
    changed = False
    restart_required = False
    for task in queue.tasks:
        active_run = _load_active_run(task, run_loader)
        if active_run is not None:
            if active_run.status == RUN_APPROVED and active_run.requires_restart:
                restart_required = True
            changed = reconcile_task_with_active_run(
                task,
                active_run=active_run,
                completed_at=now_iso(),
            ) or changed

    next_status = derive_queue_status(queue, restart_required=restart_required)
    if queue.status != next_status:
        queue.status = next_status
        changed = True
    return changed


def mark_task_for_retry(queue: TaskQueue, *, task_id: str) -> TaskRecord:
    task = _find_task(queue, task_id)
    if task.status != TASK_FAILED:
        raise ValueError(f"task is not failed: {task_id}")
    task.status = TASK_PENDING
    task.active_run_id = None
    task.reason = None
    task.error = None
    task.completed_at = None
    queue.status = derive_queue_status(queue)
    return task


def derive_queue_status(queue: TaskQueue, *, restart_required: bool = False) -> str:
    if any(task.status == TASK_FAILED for task in queue.tasks):
        return QUEUE_FAILED
    if any(task.status == TASK_BLOCKED for task in queue.tasks):
        return QUEUE_BLOCKED
    if restart_required:
        return QUEUE_RESTART_REQUIRED
    if queue.tasks and all(task.status == TASK_APPROVED for task in queue.tasks):
        return QUEUE_APPROVED
    if any(task.status in {TASK_RUNNING, TASK_WAITING} for task in queue.tasks):
        return QUEUE_RUNNING
    return QUEUE_PENDING


def _find_task(queue: TaskQueue, task_id: str) -> TaskRecord:
    for task in queue.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError(f"task not found: {task_id}")


def _load_active_run(task: TaskRecord, run_loader) -> Optional[RunManifest]:
    if not task.active_run_id:
        return None
    try:
        return run_loader(task.active_run_id)
    except OSError:
        return None
