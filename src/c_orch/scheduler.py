from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Protocol

from .drivers import CodexDriver
from .failure_policy import has_retryable_review_failure
from .orchestrator import OrchestratorConfig, RunOrchestrator
from .run_store import RunManifest, RunStore
from .states import RUN_APPROVED, RUN_FAILED, RUN_PLAN_APPROVED, RUN_PLAN_REVIEW_REQUIRED
from .task_lifecycle import (
    REASON_PLAN_REVIEW_OUTSIDE_PROPOSAL_POOL,
    reconcile_queue,
    reconcile_task_with_active_run,
)
from .settings import DEFAULT_WORKTREES_DIR
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
    TASK_SKIPPED,
    TASK_WAITING,
    TaskRecord,
    TaskQueue,
    TaskStore,
)
from .worktrees import create_worker_worktree


class OrchestratorLike(Protocol):
    def run(self, manifest: RunManifest) -> RunManifest:
        ...

    def retry_review(self, manifest: RunManifest) -> RunManifest:
        ...


@dataclass(frozen=True)
class SchedulerConfig:
    cwd: Path
    runs_dir: Path
    worktrees_dir: Path
    planner_model: str
    worker_model: str
    codex_binary_path: str
    max_attempts: int
    sandbox: str
    approval_policy: str
    planner_reasoning_effort: Optional[str] = None
    worker_reasoning_effort: Optional[str] = None
    planner_service_tier: Optional[str] = None
    worker_service_tier: Optional[str] = None
    max_tasks: Optional[int] = None

    def resolve_task_cwd(self, task_cwd: Optional[str]) -> Path:
        if task_cwd is None or not task_cwd.strip():
            return self.cwd.expanduser().resolve()
        return Path(task_cwd).expanduser().resolve()

    def resolve_worktrees_dir(self, task_cwd: Path) -> Path:
        worktrees_dir = self.worktrees_dir.expanduser()
        default_under_runtime = (self.cwd.expanduser().resolve() / DEFAULT_WORKTREES_DIR).resolve()
        if worktrees_dir.is_absolute() and worktrees_dir.resolve() == default_under_runtime:
            return (task_cwd / DEFAULT_WORKTREES_DIR).resolve()
        if worktrees_dir.is_absolute():
            return worktrees_dir.resolve()
        return (task_cwd / worktrees_dir).resolve()


class TaskScheduler:
    def __init__(
        self,
        *,
        task_store: TaskStore,
        run_store: RunStore,
        driver: CodexDriver,
        config: SchedulerConfig,
        worktree_factory: Callable[..., Path] = create_worker_worktree,
        orchestrator_factory: Optional[Callable[[], OrchestratorLike]] = None,
    ) -> None:
        self.task_store = task_store
        self.run_store = run_store
        self.driver = driver
        self.config = config
        self.worktree_factory = worktree_factory
        self._orchestrator_factory = orchestrator_factory or self._default_orchestrator

    def run(self) -> TaskQueue:
        queue = self.task_store.load()
        if reconcile_queue(
            queue,
            run_loader=self.run_store.load,
            now_iso=self.run_store.now_iso,
        ):
            self.task_store.save(queue)
        orchestrator = self._orchestrator_factory()
        processed = 0
        attempted_retry_review_run_ids: set[str] = set()

        while True:
            if queue.status in {QUEUE_FAILED, QUEUE_BLOCKED, QUEUE_RESTART_REQUIRED}:
                self.task_store.save(queue)
                return queue
            if self.config.max_tasks is not None and processed >= self.config.max_tasks:
                if queue.status == QUEUE_RUNNING:
                    queue.status = QUEUE_PENDING
                    self.task_store.save(queue)
                return queue

            task = self._first_incomplete_task(queue)
            if task is None:
                queue.status = QUEUE_APPROVED
                self.task_store.save(queue)
                return queue
            if task.active_run_id:
                active = self._load_active_run(task.active_run_id)
                if active is not None and active.status == RUN_PLAN_APPROVED:
                    self.task_store.update_task(
                        queue,
                        task.task_id,
                        status=TASK_RUNNING,
                        error=None,
                        reason="worker",
                    )
                    self.task_store.save(queue)
                    try:
                        active = orchestrator.run(active)
                    except Exception as exc:
                        queue.status = QUEUE_FAILED
                        self.task_store.update_task(
                            queue,
                            task.task_id,
                            status=TASK_FAILED,
                            error=str(exc),
                            reason="orchestrator_exception",
                        )
                        self.task_store.save(queue)
                        raise
                    if self._handle_manifest_result(queue=queue, task=task, manifest=active):
                        processed += 1
                        continue
                    return queue
                if active is not None and has_retryable_review_failure(active):
                    if active.run_id in attempted_retry_review_run_ids:
                        queue.status = QUEUE_RUNNING
                        self.task_store.save(queue)
                        return queue
                    attempted_retry_review_run_ids.add(active.run_id)
                    self.run_store.append_event(
                        active.run_id,
                        "queue_auto_retry_review_started",
                        "Queue scheduler started automatic planner review retry",
                        source="queue_scheduler",
                        action="retry-review",
                    )
                    try:
                        active = orchestrator.retry_review(active)
                    except Exception as exc:
                        queue.status = QUEUE_FAILED
                        self.task_store.update_task(
                            queue,
                            task.task_id,
                            status=TASK_FAILED,
                            error=str(exc),
                            reason="orchestrator_exception",
                        )
                        self.task_store.save(queue)
                        raise
                    if self._handle_manifest_result(queue=queue, task=task, manifest=active):
                        processed += 1
                        continue
                    return queue
            if task.status == TASK_FAILED:
                queue.status = QUEUE_FAILED
                self.task_store.save(queue)
                return queue
            if task.status == TASK_BLOCKED:
                queue.status = QUEUE_BLOCKED
                self.task_store.save(queue)
                return queue
            if task.status == TASK_WAITING:
                queue.status = QUEUE_RUNNING
                self.task_store.save(queue)
                return queue
            if task.status == TASK_RUNNING:
                queue.status = QUEUE_RUNNING
                self.task_store.save(queue)
                return queue
            if task.status != TASK_PENDING:
                queue.status = QUEUE_FAILED
                self.task_store.update_task(
                    queue,
                    task.task_id,
                    status=TASK_FAILED,
                    reason=f"unsupported task status: {task.status}",
                )
                self.task_store.save(queue)
                return queue

            queue.status = QUEUE_RUNNING
            self.task_store.update_task(
                queue,
                task.task_id,
                status=TASK_RUNNING,
                error=None,
                reason=None,
            )
            self.task_store.save(queue)

            try:
                task_cwd = self.config.resolve_task_cwd(task.cwd)
                worktrees_dir = self.config.resolve_worktrees_dir(task_cwd)
                manifest = self.run_store.create_run(
                    cwd=task_cwd,
                    user_task=task.prompt,
                    planner_model=self.config.planner_model,
                    worker_model=self.config.worker_model,
                    codex_binary_path=self.config.codex_binary_path,
                    planner_reasoning_effort=self.config.planner_reasoning_effort,
                    worker_reasoning_effort=self.config.worker_reasoning_effort,
                    planner_service_tier=self.config.planner_service_tier,
                    worker_service_tier=self.config.worker_service_tier,
                )
                worker = manifest.workers[0]
                worker.worktree_path = str(
                    self.worktree_factory(
                        repo_path=task_cwd,
                        worktrees_dir=worktrees_dir,
                        run_id=manifest.run_id,
                        worker_id=worker.id,
                    )
                )
                self.run_store.save(manifest)
                run_ids = list(task.run_ids)
                run_ids.append(manifest.run_id)
                self.task_store.update_task(
                    queue,
                    task.task_id,
                    cwd=str(task_cwd),
                    active_run_id=manifest.run_id,
                    run_ids=run_ids,
                )
                self.task_store.save(queue)
            except Exception as exc:
                queue.status = QUEUE_FAILED
                self.task_store.update_task(
                    queue,
                    task.task_id,
                    status=TASK_FAILED,
                    error=str(exc),
                    reason="run_prepare_failed",
                )
                self.task_store.save(queue)
                raise

            try:
                manifest = orchestrator.run(manifest)
            except Exception as exc:
                queue.status = QUEUE_FAILED
                self.task_store.update_task(
                    queue,
                    task.task_id,
                    status=TASK_FAILED,
                    error=str(exc),
                    reason="orchestrator_exception",
                )
                self.task_store.save(queue)
                raise

            if self._handle_manifest_result(queue=queue, task=task, manifest=manifest):
                processed += 1
                continue
            return queue

    def _first_incomplete_task(self, queue: TaskQueue):
        for task in queue.tasks:
            if task.status not in {TASK_APPROVED, TASK_SKIPPED}:
                return task
        return None

    def _default_orchestrator(self) -> RunOrchestrator:
        return RunOrchestrator(
            store=self.run_store,
            driver=self.driver,
            config=OrchestratorConfig(
                sandbox=self.config.sandbox,
                approval_policy=self.config.approval_policy,
                max_attempts=self.config.max_attempts,
                require_plan_approval=True,
                approve_plan=True,
            ),
        )

    def _load_active_run(self, run_id: str) -> Optional[RunManifest]:
        try:
            return self.run_store.load(run_id)
        except OSError:
            return None

    def _handle_manifest_result(
        self,
        *,
        queue: TaskQueue,
        task: TaskRecord,
        manifest: RunManifest,
    ) -> bool:
        reconcile_task_with_active_run(
            task,
            active_run=manifest,
            completed_at=self.run_store.now_iso(),
        )

        if manifest.status == RUN_APPROVED:
            if manifest.requires_restart:
                queue.status = QUEUE_RESTART_REQUIRED
                self.task_store.save(queue)
                return False
            queue.status = QUEUE_PENDING
            self.task_store.save(queue)
            return True

        if has_retryable_review_failure(manifest):
            queue.status = QUEUE_RUNNING
            self.task_store.save(queue)
            return False
        if manifest.status == RUN_PLAN_REVIEW_REQUIRED:
            queue.status = QUEUE_FAILED
            self.task_store.update_task(
                queue,
                task.task_id,
                status=TASK_FAILED,
                reason=REASON_PLAN_REVIEW_OUTSIDE_PROPOSAL_POOL,
            )
            self.task_store.save(queue)
            return False

        queue.status = QUEUE_FAILED
        if task.status != TASK_FAILED:
            self.task_store.update_task(
                queue,
                task.task_id,
                status=TASK_FAILED,
                reason=manifest.status or RUN_FAILED,
            )
        self.task_store.save(queue)
        return False
