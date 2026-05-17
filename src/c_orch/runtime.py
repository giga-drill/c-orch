from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence, Tuple, Union

from . import dashboard_payloads
from .drivers import CodexDriver
from .failure_policy import (
    has_retryable_review_failure,
    has_retryable_verification_failure,
)
from .phase_timing import record_run_status_transition
from .proposal_store import (
    PROPOSAL_APPROVED,
    PROPOSAL_FAILED,
    PROPOSAL_PLAN_REVIEW_REQUIRED,
    PROPOSAL_PLAN_REVISING,
    PROPOSAL_PLANNING,
    PROPOSAL_QUEUED,
    PROPOSAL_WAITING_WORKSPACE,
    ProposalRecord,
    ProposalStore,
)
from .run_store import RunManifest, RunStore
from .scheduler import OrchestratorLike, SchedulerConfig, TaskScheduler
from .states import (
    RUN_PLAN_APPROVED,
    RUN_PLAN_REVIEW_REQUIRED,
)
from .task_lifecycle import (
    derive_run_waiting_for,
    mark_task_handled_skipped,
    mark_task_for_retry,
    reconcile_queue,
    reconcile_task_with_active_run,
)
from .task_store import (
    TASK_APPROVED,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_PENDING,
    TASK_RUNNING,
    TASK_SKIPPED,
    TaskRecord,
    TaskQueue,
    TaskStore,
)
from .worktrees import create_worker_worktree
from .workspace_lanes import WorkspaceResolutionError, canonical_git_root


Pathish = Union[str, Path]
RunActionResponse = Optional[Tuple[HTTPStatus, Dict[str, Any]]]
DriverFactory = Callable[[str], ContextManager[CodexDriver]]


class _UnusedCodexDriver:
    def start_session(self, **_: Any) -> Any:
        raise RuntimeError("retry-verification does not start Codex sessions")

    def reply(self, **_: Any) -> Any:
        raise RuntimeError("retry-verification does not call Codex sessions")


class COrchRuntime:
    """Backend control plane for dashboard requests.

    The HTTP UI should express user intent and render returned data; this class
    owns run actions, driver creation, and the business state transition entrypoints.
    """

    def __init__(
        self,
        *,
        runs_dir: Pathish,
        queue_path: Optional[Pathish] = None,
        proposals_path: Optional[Pathish] = None,
        scheduler_config: Optional[SchedulerConfig] = None,
        driver_factory: Optional[DriverFactory] = None,
        worktree_factory: Optional[Callable[..., Path]] = None,
        orchestrator_factory: Optional[Callable[[], OrchestratorLike]] = None,
    ) -> None:
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.queue_path = Path(queue_path).expanduser().resolve() if queue_path is not None else None
        self.proposals_path = (
            Path(proposals_path).expanduser().resolve()
            if proposals_path is not None
            else None
        )
        self._scheduler_config = scheduler_config
        self._external_driver_factory = driver_factory
        self._worktree_factory = worktree_factory
        self._orchestrator_factory = orchestrator_factory
        self._action_lock = threading.RLock()
        self._queue_lock = threading.RLock()
        self._proposal_lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._proposal_dispatch_lock = threading.RLock()
        self._queue_lane_threads: Dict[str, threading.Thread] = {}
        self._queue_lane_threads_lock = threading.RLock()
        self._proposal_lane_threads: Dict[str, threading.Thread] = {}
        self._proposal_lane_threads_lock = threading.RLock()
        self._drivers: Dict[str, Any] = {}
        self._dispatch_thread: Optional[threading.Thread] = None
        self._proposal_dispatch_thread: Optional[threading.Thread] = None
        self._last_dispatch_error: Optional[str] = None
        self._last_proposal_dispatch_error: Optional[str] = None
        self._runtime_generation = f"{datetime.now().astimezone().isoformat(timespec='seconds')}:{os.getpid()}"
        if self.proposals_path is not None:
            prune_queued_proposals(self.proposals_path)
        if self.proposals_path is not None and self._scheduler_config is not None:
            self.dispatch_proposals_async()

    def build_runs_payload(self) -> Dict[str, Any]:
        return dashboard_payloads.build_runs_payload(self.runs_dir)

    def build_queue_payload(self) -> Dict[str, Any]:
        return dashboard_payloads.build_queue_payload(self.queue_path, runs_dir=self.runs_dir)

    def build_proposals_payload(self) -> Dict[str, Any]:
        return dashboard_payloads.build_proposals_payload(self.proposals_path, runs_dir=self.runs_dir)

    def build_run_payload(self, run_id: str) -> Optional[Dict[str, Any]]:
        return dashboard_payloads.build_run_payload(self.runs_dir, run_id)

    def build_state_payload(self, selected_run_id: Optional[str] = None) -> Dict[str, Any]:
        queue_dispatch_running = self._dispatch_thread is not None and self._dispatch_thread.is_alive()
        queue_dispatch_running = queue_dispatch_running or self._active_queue_lane_count() > 0
        proposal_dispatch_running = (
            self._proposal_dispatch_thread is not None and self._proposal_dispatch_thread.is_alive()
        )
        proposal_dispatch_running = proposal_dispatch_running or self._active_proposal_lane_count() > 0
        return dashboard_payloads.build_state_payload(
            runs_dir=self.runs_dir,
            queue_path=self.queue_path,
            proposals_path=self.proposals_path,
            runtime_generation=self._runtime_generation,
            dispatch_running=queue_dispatch_running or proposal_dispatch_running,
            queue_dispatch_running=queue_dispatch_running,
            proposal_dispatch_running=proposal_dispatch_running,
            last_dispatch_error=self._last_dispatch_error,
            last_proposal_dispatch_error=self._last_proposal_dispatch_error,
            selected_run_id=selected_run_id,
        )

    def _active_queue_lane_count(self) -> int:
        with self._queue_lane_threads_lock:
            self._queue_lane_threads = {
                lane: thread for lane, thread in self._queue_lane_threads.items() if thread.is_alive()
            }
            return len(self._queue_lane_threads)

    def _active_proposal_lane_count(self) -> int:
        with self._proposal_lane_threads_lock:
            self._proposal_lane_threads = {
                lane: thread for lane, thread in self._proposal_lane_threads.items() if thread.is_alive()
            }
            return len(self._proposal_lane_threads)

    def run_action(
        self,
        run_id: str,
        action: Any,
        feedback: Any = None,
    ) -> RunActionResponse:
        with self._action_lock:
            result = run_action(
                self.runs_dir,
                run_id,
                action,
                feedback,
                driver_factory=self._driver_context,
            )
            if self.queue_path is not None:
                reconcile_queue_file(queue_path=self.queue_path, runs_dir=self.runs_dir)
                self.dispatch_queue_async()
            return self._attach_state(result, selected_run_id=run_id)

    def task_action(self, task_id: str, action: Any) -> RunActionResponse:
        with self._action_lock, self._queue_lock:
            if self.queue_path is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing queue file"}
            result = task_action(
                self.queue_path,
                task_id,
                action,
                runs_dir=self.runs_dir,
            )
            if result is not None and int(result[0]) < 400:
                self.dispatch_queue_async()
            return self._attach_state(result)

    def queue_action(self, action: Any, *, confirmed_by: str = "dashboard") -> RunActionResponse:
        with self._action_lock, self._queue_lock:
            if self.queue_path is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing queue file"}
            result = queue_action(
                self.queue_path,
                action,
                runs_dir=self.runs_dir,
                confirmed_by=confirmed_by,
            )
            if result is not None and int(result[0]) < 400:
                self.dispatch_queue_async()
            return self._attach_state(result)

    def create_proposal(self, title: Any, prompt: Any, cwd: Any = None) -> RunActionResponse:
        with self._action_lock, self._proposal_lock, self._queue_lock:
            if self.proposals_path is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing proposals file"}
            if self._scheduler_config is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing execution config"}
            result = create_proposal(
                self.proposals_path,
                title,
                prompt,
                cwd,
                runs_dir=self.runs_dir,
                queue_path=self.queue_path,
                config=self._scheduler_config,
                worktree_factory=self._worktree_factory,
            )
            if result is not None and int(result[0]) < 400:
                self.dispatch_proposals_async()
            return self._attach_state(result)

    def proposal_action(
        self,
        proposal_id: str,
        action: Any,
        feedback: Any = None,
    ) -> RunActionResponse:
        with self._action_lock, self._proposal_lock, self._queue_lock:
            if self.proposals_path is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing proposals file"}
            if self.queue_path is None:
                return HTTPStatus.BAD_REQUEST, {"error": "missing queue file"}
            result = proposal_action(
                self.proposals_path,
                proposal_id,
                action,
                feedback,
                runs_dir=self.runs_dir,
                queue_path=self.queue_path,
                driver_factory=self._driver_context,
            )
            if result is not None and int(result[0]) < 400:
                self.dispatch_queue_async()
            selected_run_id = _selected_run_from_payload(result[1]) if result is not None else None
            return self._attach_state(result, selected_run_id=selected_run_id)

    def dispatch_queue_async(self) -> bool:
        """Start one background queue scheduler pass when execution is configured."""
        if self.queue_path is None or self._scheduler_config is None:
            return False
        with self._dispatch_lock:
            if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
                return False
            self._last_dispatch_error = None
            thread = threading.Thread(
                target=self._dispatch_queue_worker,
                name="c-orch-queue-dispatch",
                daemon=True,
            )
            self._dispatch_thread = thread
            thread.start()
            return True

    def wait_for_dispatch(self, timeout: Optional[float] = None) -> bool:
        """Wait for the current background scheduler pass; mainly used by tests."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._dispatch_lock:
            thread = self._dispatch_thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        while True:
            with self._dispatch_lock:
                dispatch_thread = self._dispatch_thread
            if dispatch_thread is not None and dispatch_thread.is_alive():
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                dispatch_thread.join(timeout=remaining)
                if deadline is not None and time.monotonic() >= deadline and dispatch_thread.is_alive():
                    return False
                continue
            with self._queue_lane_threads_lock:
                threads = [thread for thread in self._queue_lane_threads.values() if thread.is_alive()]
            if not threads:
                time.sleep(0.05)
                with self._dispatch_lock:
                    dispatch_thread = self._dispatch_thread
                with self._queue_lane_threads_lock:
                    threads = [thread for thread in self._queue_lane_threads.values() if thread.is_alive()]
                if (dispatch_thread is None or not dispatch_thread.is_alive()) and not threads:
                    return True
                continue
            for lane_thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                lane_thread.join(timeout=remaining)
                if deadline is not None and time.monotonic() >= deadline and lane_thread.is_alive():
                    return False

    def dispatch_proposals_async(self) -> bool:
        """Start one background proposal planning dispatcher pass."""
        if self.proposals_path is None or self._scheduler_config is None:
            return False
        with self._proposal_dispatch_lock:
            if self._proposal_dispatch_thread is not None and self._proposal_dispatch_thread.is_alive():
                return False
            self._last_proposal_dispatch_error = None
            thread = threading.Thread(
                target=self._dispatch_proposals_worker,
                name="c-orch-proposal-dispatch",
                daemon=True,
            )
            self._proposal_dispatch_thread = thread
            thread.start()
            return True

    def wait_for_proposal_dispatch(self, timeout: Optional[float] = None) -> bool:
        """Wait for the current background proposal dispatcher pass; mainly used by tests."""
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._proposal_dispatch_lock:
            thread = self._proposal_dispatch_thread
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                return False
        while True:
            with self._proposal_dispatch_lock:
                dispatch_thread = self._proposal_dispatch_thread
            if dispatch_thread is not None and dispatch_thread.is_alive():
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                dispatch_thread.join(timeout=remaining)
                if deadline is not None and time.monotonic() >= deadline and dispatch_thread.is_alive():
                    return False
                continue
            with self._proposal_lane_threads_lock:
                threads = [thread for thread in self._proposal_lane_threads.values() if thread.is_alive()]
            if not threads:
                time.sleep(0.05)
                with self._proposal_dispatch_lock:
                    dispatch_thread = self._proposal_dispatch_thread
                with self._proposal_lane_threads_lock:
                    threads = [thread for thread in self._proposal_lane_threads.values() if thread.is_alive()]
                if (dispatch_thread is None or not dispatch_thread.is_alive()) and not threads:
                    return True
                continue
            for lane_thread in threads:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                lane_thread.join(timeout=remaining)
                if deadline is not None and time.monotonic() >= deadline and lane_thread.is_alive():
                    return False

    def close(self) -> None:
        with self._driver_lock:
            drivers = list(self._drivers.values())
            self._drivers.clear()
        for driver in drivers:
            driver.close()

    @contextmanager
    def _driver_context(self, codex_path: str):
        if self._external_driver_factory is not None:
            with self._external_driver_factory(codex_path) as driver:
                yield driver
            return
        driver = self._get_or_create_driver(codex_path)
        try:
            yield driver
        except Exception:
            self._drop_driver(codex_path)
            raise

    def _get_or_create_driver(self, codex_path: str) -> CodexDriver:
        with self._driver_lock:
            driver = self._drivers.get(codex_path)
            if driver is None:
                from .mcp_driver import McpCodexDriver

                driver = McpCodexDriver(codex_bin=codex_path)
                driver.__enter__()
                self._drivers[codex_path] = driver
            return driver

    def _drop_driver(self, codex_path: str) -> None:
        with self._driver_lock:
            driver = self._drivers.pop(codex_path, None)
        if driver is not None:
            driver.close()

    def _dispatch_queue_worker(self) -> None:
        try:
            self._start_available_queue_lanes()
        except Exception as exc:
            self._last_dispatch_error = str(exc)

    def _dispatch_proposals_worker(self) -> None:
        try:
            self._run_proposal_dispatch_loop()
        except Exception as exc:
            self._last_proposal_dispatch_error = str(exc)

    def _run_queue_once(self) -> None:
        if self.queue_path is None or self._scheduler_config is None:
            return
        task_store = TaskStore(self.queue_path)
        run_store = RunStore(self.runs_dir)
        with self._driver_context(self._scheduler_config.codex_binary_path) as driver:
            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=driver,
                config=self._scheduler_config,
                **self._scheduler_overrides(),
            )
            scheduler.run()

    def _start_available_queue_lanes(self) -> None:
        if self.queue_path is None or self._scheduler_config is None:
            return
        max_parallel = max(1, self._scheduler_config.max_parallel_workspaces)
        active_count = self._active_queue_lane_count()
        if active_count >= max_parallel:
            return
        with self._queue_lock:
            task_store = TaskStore(self.queue_path)
            run_store = RunStore(self.runs_dir)
            try:
                queue = task_store.load()
            except (OSError, ValueError):
                return
            reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
            candidates = self._queue_lane_candidates(queue, run_store=run_store)
            started = 0
            for lane_id, task in candidates:
                if active_count + started >= max_parallel:
                    break
                with self._queue_lane_threads_lock:
                    existing = self._queue_lane_threads.get(lane_id)
                    if existing is not None and existing.is_alive():
                        continue
                    self._queue_lane_threads.pop(lane_id, None)
                    thread = threading.Thread(
                        target=self._run_queue_lane_worker,
                        name=f"c-orch-queue-lane-{task.task_id}",
                        kwargs={"lane_id": lane_id, "task_id": task.task_id},
                        daemon=True,
                    )
                    self._queue_lane_threads[lane_id] = thread
                if task.status == TASK_PENDING:
                    task_store.update_task(queue, task.task_id, status=TASK_RUNNING, error=None, reason="worker")
                started += 1
                task_store.save(queue)
                thread.start()
            if not started:
                task_store.save(queue)

    def _queue_lane_candidates(self, queue: TaskQueue, *, run_store: RunStore) -> List[Tuple[str, TaskRecord]]:
        blocked_lanes: set[str] = set()
        candidates: List[Tuple[str, TaskRecord]] = []
        seen_lanes: set[str] = set()
        for task in queue.tasks:
            lane_id = self._task_workspace_id(task, run_store=run_store)
            if lane_id in seen_lanes:
                continue
            if task.status in {TASK_APPROVED, TASK_SKIPPED}:
                continue
            seen_lanes.add(lane_id)
            if lane_id in blocked_lanes:
                continue
            if task.status in {TASK_FAILED, TASK_BLOCKED}:
                blocked_lanes.add(lane_id)
                continue
            active = self._load_task_active_run(task, run_store=run_store)
            if active is not None:
                if active.status == RUN_PLAN_APPROVED or has_retryable_review_failure(active):
                    candidates.append((lane_id, task))
                continue
            if task.status in {TASK_PENDING, TASK_RUNNING}:
                candidates.append((lane_id, task))
        return candidates

    def _task_workspace_id(self, task: TaskRecord, *, run_store: RunStore) -> str:
        task_cwd = task.cwd
        if not task_cwd and task.active_run_id:
            try:
                task_cwd = run_store.load(task.active_run_id).cwd
            except OSError:
                task_cwd = None
        if not task_cwd and self._scheduler_config is not None:
            task_cwd = str(self._scheduler_config.cwd)
        if task_cwd:
            try:
                return str(canonical_git_root(task_cwd))
            except WorkspaceResolutionError:
                return str(Path(task_cwd).expanduser().resolve())
        return "__default__"

    def _load_task_active_run(self, task: TaskRecord, *, run_store: RunStore) -> Optional[RunManifest]:
        if not task.active_run_id:
            return None
        try:
            return run_store.load(task.active_run_id)
        except OSError:
            return None

    def _run_queue_lane_worker(self, *, lane_id: str, task_id: str) -> None:
        try:
            self._execute_queue_lane_task(task_id=task_id)
        except Exception as exc:
            self._last_dispatch_error = str(exc)
        finally:
            with self._queue_lane_threads_lock:
                self._queue_lane_threads.pop(lane_id, None)
            self.dispatch_queue_async()
            self.dispatch_proposals_async()

    @contextmanager
    def _fresh_driver_context(self, codex_path: str):
        if self._external_driver_factory is not None:
            with self._external_driver_factory(codex_path) as driver:
                yield driver
            return
        from .mcp_driver import McpCodexDriver

        with McpCodexDriver(codex_bin=codex_path) as driver:
            yield driver

    def _execute_queue_lane_task(self, *, task_id: str) -> None:
        if self.queue_path is None or self._scheduler_config is None:
            return
        task_store = TaskStore(self.queue_path)
        run_store = RunStore(self.runs_dir)
        manifest: Optional[RunManifest] = None
        action = "run"
        with self._queue_lock:
            queue = task_store.load()
            task = _find_task_record(queue, task_id)
            if task.active_run_id:
                manifest = run_store.load(task.active_run_id)
                if has_retryable_review_failure(manifest):
                    action = "retry_review"
                elif manifest.status == RUN_PLAN_APPROVED:
                    action = "run"
                else:
                    reconcile_task_with_active_run(
                        task,
                        active_run=manifest,
                        completed_at=run_store.now_iso(),
                    )
                    reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
                    task_store.save(queue)
                    return
                task_store.update_task(queue, task.task_id, status=TASK_RUNNING, error=None, reason="worker")
                task_store.save(queue)
            else:
                try:
                    task_cwd = self._scheduler_config.resolve_task_cwd(task.cwd)
                    worktrees_dir = self._scheduler_config.resolve_worktrees_dir(task_cwd)
                    manifest = run_store.create_run(
                        cwd=task_cwd,
                        user_task=task.prompt,
                        planner_model=self._scheduler_config.planner_model,
                        worker_model=self._scheduler_config.worker_model,
                        codex_binary_path=self._scheduler_config.codex_binary_path,
                        planner_reasoning_effort=self._scheduler_config.planner_reasoning_effort,
                        worker_reasoning_effort=self._scheduler_config.worker_reasoning_effort,
                        planner_service_tier=self._scheduler_config.planner_service_tier,
                        worker_service_tier=self._scheduler_config.worker_service_tier,
                    )
                    worker = manifest.workers[0]
                    worker.worktree_path = str(
                        self._worktree_factory(
                            repo_path=task_cwd,
                            worktrees_dir=worktrees_dir,
                            run_id=manifest.run_id,
                            worker_id=worker.id,
                        )
                        if self._worktree_factory is not None
                        else create_worker_worktree(
                            repo_path=task_cwd,
                            worktrees_dir=worktrees_dir,
                            run_id=manifest.run_id,
                            worker_id=worker.id,
                        )
                    )
                    run_store.save(manifest)
                    run_ids = list(task.run_ids)
                    run_ids.append(manifest.run_id)
                    task_store.update_task(
                        queue,
                        task.task_id,
                        cwd=str(task_cwd),
                        active_run_id=manifest.run_id,
                        run_ids=run_ids,
                        status=TASK_RUNNING,
                        error=None,
                        reason=None,
                    )
                    task_store.save(queue)
                except Exception as exc:
                    task_store.update_task(
                        queue,
                        task.task_id,
                        status=TASK_FAILED,
                        error=str(exc),
                        reason="run_prepare_failed",
                    )
                    reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
                    task_store.save(queue)
                    return
        if manifest is None:
            return
        try:
            with self._fresh_driver_context(self._scheduler_config.codex_binary_path) as driver:
                orchestrator = self._build_queue_orchestrator(run_store=run_store, driver=driver)
                if action == "retry_review":
                    manifest = orchestrator.retry_review(manifest)
                else:
                    manifest = orchestrator.run(manifest)
        except Exception as exc:
            with self._queue_lock:
                queue = task_store.load()
                task = _find_task_record(queue, task_id)
                task_store.update_task(
                    queue,
                    task.task_id,
                    status=TASK_FAILED,
                    error=str(exc),
                    reason="orchestrator_exception",
                )
                reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
                task_store.save(queue)
            return
        with self._queue_lock:
            queue = task_store.load()
            task = _find_task_record(queue, task_id)
            reconcile_task_with_active_run(
                task,
                active_run=manifest,
                completed_at=run_store.now_iso(),
            )
            reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
            task_store.save(queue)

    def _build_queue_orchestrator(self, *, run_store: RunStore, driver: CodexDriver) -> OrchestratorLike:
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory()
        from .orchestrator import OrchestratorConfig, RunOrchestrator

        if self._scheduler_config is None:
            raise RuntimeError("missing scheduler config")
        return RunOrchestrator(
            store=run_store,
            driver=driver,
            config=OrchestratorConfig(
                sandbox=self._scheduler_config.sandbox,
                approval_policy=self._scheduler_config.approval_policy,
                max_attempts=self._scheduler_config.max_attempts,
                require_plan_approval=True,
                approve_plan=True,
                controller_repo_path=str(self._scheduler_config.cwd),
            ),
        )

    def _run_proposal_dispatch_loop(self) -> None:
        if self.proposals_path is None or self._scheduler_config is None:
            return
        self._start_available_proposal_lanes()

    def _start_available_proposal_lanes(self) -> None:
        if self.proposals_path is None or self._scheduler_config is None:
            return
        max_parallel = max(1, self._scheduler_config.max_parallel_workspaces)
        active_count = self._active_proposal_lane_count()
        if active_count >= max_parallel:
            return
        self._promote_waiting_workspace_proposals(limit=max_parallel - active_count)
        active_count = self._active_proposal_lane_count()
        if active_count >= max_parallel:
            return
        claimed = self._claim_planning_proposals(limit=max_parallel - active_count)
        for lane_id, proposal_id, run_id in claimed:
            with self._proposal_lane_threads_lock:
                existing = self._proposal_lane_threads.get(lane_id)
                if existing is not None and existing.is_alive():
                    continue
                self._proposal_lane_threads.pop(lane_id, None)
                thread = threading.Thread(
                    target=self._run_proposal_lane_worker,
                    name=f"c-orch-proposal-lane-{proposal_id}",
                    kwargs={"lane_id": lane_id, "proposal_id": proposal_id, "run_id": run_id},
                    daemon=True,
                )
                self._proposal_lane_threads[lane_id] = thread
            thread.start()

    def _promote_waiting_workspace_proposals(self, *, limit: int) -> int:
        if self.proposals_path is None or self._scheduler_config is None:
            return 0
        promoted = 0
        with self._action_lock:
            proposal_store = ProposalStore(self.proposals_path)
            try:
                pool = proposal_store.load()
            except (OSError, ValueError):
                return 0
            run_store = RunStore(self.runs_dir)
            for proposal in pool.proposals:
                if promoted >= limit:
                    break
                if proposal.status != PROPOSAL_WAITING_WORKSPACE:
                    continue
                try:
                    proposal_cwd = _resolve_task_cwd(proposal.cwd, default_cwd=self._scheduler_config.cwd)
                    workspace_root = canonical_git_root(proposal_cwd)
                except (ValueError, WorkspaceResolutionError) as exc:
                    proposal_store.update_proposal(
                        pool,
                        proposal.proposal_id,
                        status=PROPOSAL_FAILED,
                        error=str(exc),
                        reason="workspace_resolution_failed",
                    )
                    promoted += 1
                    continue
                if _workspace_lane_is_locked(
                    workspace_root=workspace_root,
                    proposals=pool.proposals,
                    queue_path=self.queue_path,
                    run_store=run_store,
                    ignore_proposal_id=proposal.proposal_id,
                ):
                    continue
                try:
                    manifest = _create_preflight_run(
                        run_store=run_store,
                        config=self._scheduler_config,
                        user_task=proposal.prompt,
                        task_cwd=proposal_cwd,
                        worktree_factory=self._worktree_factory,
                    )
                except Exception as exc:
                    proposal_store.update_proposal(
                        pool,
                        proposal.proposal_id,
                        status=PROPOSAL_FAILED,
                        error=str(exc),
                        reason="proposal_preflight_failed",
                    )
                    promoted += 1
                    continue
                proposal_store.update_proposal(
                    pool,
                    proposal.proposal_id,
                    run_id=manifest.run_id,
                    status=PROPOSAL_PLANNING,
                    error=None,
                    reason="planner",
                )
                promoted += 1
            if promoted:
                proposal_store.save(pool)
        return promoted

    def _claim_planning_proposals(self, *, limit: int) -> List[Tuple[str, str, str]]:
        if self.proposals_path is None:
            return []
        active_lanes = set(self._active_proposal_lane_ids())
        with self._action_lock:
            proposal_store = ProposalStore(self.proposals_path)
            try:
                pool = proposal_store.load()
            except (OSError, ValueError):
                return []
            run_store = RunStore(self.runs_dir)
            claimed: List[Tuple[str, str, str]] = []
            seen_lanes: set[str] = set()
            for proposal in pool.proposals:
                if len(claimed) >= limit:
                    break
                if proposal.status != PROPOSAL_PLANNING:
                    continue
                if not proposal.run_id:
                    continue
                lane_id = self._proposal_workspace_id(proposal, run_store=run_store)
                if lane_id in active_lanes or lane_id in seen_lanes:
                    continue
                seen_lanes.add(lane_id)
                if _proposal_is_blocked_by_other_lane_item(
                    proposal=proposal,
                    proposals=pool.proposals,
                    queue_path=self.queue_path,
                    run_store=run_store,
                ):
                    continue
                claimed.append((lane_id, proposal.proposal_id, proposal.run_id))
            return claimed

    def _active_proposal_lane_ids(self) -> List[str]:
        with self._proposal_lane_threads_lock:
            self._proposal_lane_threads = {
                lane: thread for lane, thread in self._proposal_lane_threads.items() if thread.is_alive()
            }
            return list(self._proposal_lane_threads)

    def _proposal_workspace_id(self, proposal: ProposalRecord, *, run_store: RunStore) -> str:
        proposal_cwd = proposal.cwd
        if not proposal_cwd and proposal.run_id:
            try:
                proposal_cwd = run_store.load(proposal.run_id).cwd
            except OSError:
                proposal_cwd = None
        if not proposal_cwd and self._scheduler_config is not None:
            proposal_cwd = str(self._scheduler_config.cwd)
        if proposal_cwd:
            try:
                return str(canonical_git_root(proposal_cwd))
            except WorkspaceResolutionError:
                return str(Path(proposal_cwd).expanduser().resolve())
        return "__default__"

    def _run_proposal_lane_worker(self, *, lane_id: str, proposal_id: str, run_id: str) -> None:
        try:
            self._process_proposal_planning_job(proposal_id=proposal_id, run_id=run_id)
        except Exception as exc:
            self._last_proposal_dispatch_error = str(exc)
        finally:
            with self._proposal_lane_threads_lock:
                self._proposal_lane_threads.pop(lane_id, None)
            self.dispatch_proposals_async()
            self.dispatch_queue_async()

    def _process_proposal_planning_job(self, *, proposal_id: str, run_id: str) -> None:
        if self._scheduler_config is None:
            return
        run_store = RunStore(self.runs_dir)
        try:
            manifest = run_store.load(run_id)
        except OSError as exc:
            self._mark_proposal_planning_failed(
                proposal_id=proposal_id,
                run_id=run_id,
                error=str(exc),
                reason="proposal_run_not_found",
            )
            return
        if manifest.status not in {"NEW", "PLANNING"}:
            self._sync_proposal_from_manifest(proposal_id=proposal_id, manifest=manifest)
            return
        planner_error: Optional[Exception] = None
        try:
            with self._fresh_driver_context(self._scheduler_config.codex_binary_path) as driver:
                orchestrator = self._build_proposal_orchestrator(run_store=run_store, driver=driver)
                manifest = orchestrator.run(manifest)
        except Exception as exc:
            planner_error = exc
        if planner_error is not None:
            self._mark_run_failed_after_planner_exception(run_store=run_store, run_id=run_id, error=planner_error)
            self._mark_proposal_planning_failed(
                proposal_id=proposal_id,
                run_id=run_id,
                error=str(planner_error),
                reason="proposal_planning_failed",
            )
            return
        self._sync_proposal_from_manifest(proposal_id=proposal_id, manifest=manifest)

    def _build_proposal_orchestrator(self, *, run_store: RunStore, driver: CodexDriver) -> OrchestratorLike:
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory()
        from .orchestrator import OrchestratorConfig, RunOrchestrator

        return RunOrchestrator(
            store=run_store,
            driver=driver,
            config=OrchestratorConfig(
                sandbox=self._scheduler_config.sandbox,
                approval_policy=self._scheduler_config.approval_policy,
                max_attempts=self._scheduler_config.max_attempts,
                require_plan_approval=True,
                approve_plan=False,
                controller_repo_path=str(self._scheduler_config.cwd),
            ),
        )

    def _sync_proposal_from_manifest(self, *, proposal_id: str, manifest: RunManifest) -> None:
        if self.proposals_path is None:
            return
        with self._action_lock:
            proposal_store = ProposalStore(self.proposals_path)
            try:
                pool = proposal_store.load()
                proposal = proposal_store.find(pool, proposal_id)
            except (OSError, ValueError):
                return
            if proposal.status == PROPOSAL_FAILED:
                return
            status = _proposal_status_from_run(manifest)
            error = None
            reason = derive_run_waiting_for(manifest)
            if status == PROPOSAL_FAILED and not error:
                error = proposal.error or "Planner failed to produce a plan."
            proposal_store.update_proposal(
                pool,
                proposal_id,
                status=status,
                error=error,
                reason=reason,
            )
            proposal_store.save(pool)

    def _mark_proposal_planning_failed(
        self,
        *,
        proposal_id: str,
        run_id: str,
        error: str,
        reason: str,
    ) -> None:
        if self.proposals_path is None:
            return
        with self._action_lock:
            proposal_store = ProposalStore(self.proposals_path)
            try:
                pool = proposal_store.load()
                proposal_store.find(pool, proposal_id)
            except (OSError, ValueError):
                return
            proposal_store.update_proposal(
                pool,
                proposal_id,
                status=PROPOSAL_FAILED,
                error=error,
                reason=reason,
                run_id=run_id,
            )
            proposal_store.save(pool)

    def _mark_run_failed_after_planner_exception(
        self,
        *,
        run_store: RunStore,
        run_id: str,
        error: Exception,
    ) -> None:
        try:
            manifest = run_store.load(run_id)
        except OSError:
            return
        if manifest.status != "FAILED":
            record_run_status_transition(
                manifest,
                "FAILED",
                run_store.now_iso(),
                metadata={"reason": "proposal_planning_failed"},
            )
            manifest.planner.status = "FAILED"
            for worker in manifest.workers:
                if worker.status == "PENDING":
                    worker.status = "FAILED"
            run_store.save(manifest)
        run_store.append_event(
            run_id,
            "proposal_planning_failed",
            "Planner failed while generating proposal plan.",
            reason="proposal_planning_failed",
            error=str(error),
        )

    def _scheduler_overrides(self) -> Dict[str, Any]:
        overrides: Dict[str, Any] = {}
        if self._worktree_factory is not None:
            overrides["worktree_factory"] = self._worktree_factory
        if self._orchestrator_factory is not None:
            overrides["orchestrator_factory"] = self._orchestrator_factory
        return overrides

    def _attach_state(
        self,
        result: RunActionResponse,
        *,
        selected_run_id: Optional[str] = None,
    ) -> RunActionResponse:
        if result is None:
            return None
        status, payload = result
        if int(status) < 400:
            payload = dict(payload)
            selected = selected_run_id or _selected_run_from_payload(payload)
            state = self.build_state_payload(selected_run_id=selected)
            payload["state"] = state
            payload["state_version"] = state["version"]
        return status, payload


def run_action(
    runs_dir: Pathish,
    run_id: str,
    action: Any,
    feedback: Any = None,
    *,
    driver_factory: Optional[DriverFactory] = None,
) -> RunActionResponse:
    runs_path = Path(runs_dir).expanduser().resolve()
    if not _valid_run_id(run_id):
        return None
    store = RunStore(runs_path)
    try:
        manifest = store.load(run_id)
    except OSError:
        return None
    events = store.load_events(run_id)
    if action not in {"approve-plan", "revise-plan", "retry-review", "retry-verification"}:
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
    action_error = _validate_run_action(manifest, action, events=events)
    if action_error:
        return HTTPStatus.CONFLICT, {"error": action_error, "status": manifest.status}
    if action == "revise-plan":
        if not isinstance(feedback, str) or not feedback.strip():
            return HTTPStatus.BAD_REQUEST, {"error": "feedback is required for revise-plan"}

    from .orchestrator import OrchestratorConfig, RunOrchestrator

    try:
        if action == "retry-verification":
            orchestrator = RunOrchestrator(
                store=store,
                driver=_UnusedCodexDriver(),
                config=OrchestratorConfig(
                    require_plan_approval=True,
                ),
            )
            manifest = orchestrator.retry_verification(manifest)
        else:
            codex_path = (
                manifest.codex_binary_path
                or manifest.planner.codex_binary_path
            )
            if not codex_path:
                return HTTPStatus.BAD_REQUEST, {"error": "missing codex binary path"}
            factory = driver_factory or _default_driver_factory
            with factory(codex_path) as driver:
                orchestrator = RunOrchestrator(
                    store=store,
                    driver=driver,
                    config=OrchestratorConfig(
                        require_plan_approval=True,
                        approve_plan=action == "approve-plan",
                    ),
                )
                if action == "revise-plan":
                    manifest = orchestrator.revise_plan(manifest, feedback.strip())
                elif action == "retry-review":
                    manifest = orchestrator.retry_review(manifest)
                else:
                    manifest = orchestrator.run(manifest)
    except Exception as exc:
        store.append_event(
            run_id,
            "ui_action_failed",
            "Dashboard action failed",
            action=action,
            error=str(exc),
        )
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
    payload = dashboard_payloads.build_run_payload(runs_path, run_id)
    if payload is None:
        payload = {"ok": True}
    payload["transition"] = {
        "type": f"run_{action}_completed",
        "run_id": run_id,
        "selected_run_id": run_id,
    }
    return HTTPStatus.OK, payload


def task_action(
    queue_path: Pathish,
    task_id: str,
    action: Any,
    *,
    runs_dir: Optional[Pathish] = None,
) -> RunActionResponse:
    if action not in {"retry-task", "retry-verification", "mark-handled-skipped"}:
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
    queue_file = Path(queue_path).expanduser().resolve()
    store = TaskStore(queue_file)
    try:
        queue = store.load()
    except OSError:
        return None
    if runs_dir is not None:
        run_store = RunStore(Path(runs_dir).expanduser().resolve())
        reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
    if action == "retry-verification":
        if runs_dir is None:
            return HTTPStatus.BAD_REQUEST, {"error": "missing runs_dir"}
        try:
            task = _find_task_record(queue, task_id)
        except ValueError as exc:
            return HTTPStatus.CONFLICT, {"error": str(exc)}
        if task.status != TASK_FAILED:
            return HTTPStatus.CONFLICT, {"error": f"task is not failed: {task_id}"}
        if not task.active_run_id:
            return HTTPStatus.CONFLICT, {"error": f"task has no active run: {task_id}"}
        result = run_action(runs_dir, task.active_run_id, "retry-verification")
        if result is None:
            return None
        status, payload = result
        if int(status) >= 400:
            return status, payload
        run_store = RunStore(Path(runs_dir).expanduser().resolve())
        reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
        store.save(queue)
        queue_payload = dashboard_payloads.build_queue_payload(queue_file, runs_dir=runs_dir)
        queue_payload["transition"] = {
            "type": "task_verification_retried",
            "task_id": task_id,
            "run_id": task.active_run_id,
        }
        return HTTPStatus.OK, queue_payload
    if action == "mark-handled-skipped":
        try:
            mark_task_handled_skipped(
                queue,
                task_id=task_id,
                completed_at=run_store.now_iso() if runs_dir is not None else None,
            )
        except ValueError as exc:
            return HTTPStatus.CONFLICT, {"error": str(exc)}
        store.save(queue)
        payload = dashboard_payloads.build_queue_payload(queue_file, runs_dir=runs_dir)
        payload["transition"] = {
            "type": "task_marked_handled_skipped",
            "task_id": task_id,
        }
        return HTTPStatus.OK, payload
    try:
        mark_task_for_retry(queue, task_id=task_id)
    except ValueError as exc:
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    store.save(queue)
    payload = dashboard_payloads.build_queue_payload(queue_file, runs_dir=runs_dir)
    payload["transition"] = {
        "type": "task_requeued",
        "task_id": task_id,
    }
    return HTTPStatus.OK, payload


def queue_action(
    queue_path: Pathish,
    action: Any,
    *,
    runs_dir: Pathish,
    confirmed_by: str = "dashboard",
) -> RunActionResponse:
    if action != "confirm-runtime-restarted":
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
    queue_file = Path(queue_path).expanduser().resolve()
    queue_store = TaskStore(queue_file)
    try:
        queue = queue_store.load()
    except OSError:
        return None

    runs_path = Path(runs_dir).expanduser().resolve()
    run_store = RunStore(runs_path)
    seen_run_ids = set()
    for task in queue.tasks:
        run_id = task.active_run_id
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        try:
            manifest = run_store.load(run_id)
        except OSError:
            continue
        if manifest.status != "APPROVED" or not manifest.requires_restart:
            continue
        previous_restart_reason = manifest.restart_reason
        previous_restart_paths = list(manifest.restart_paths)
        manifest.requires_restart = False
        run_store.save(manifest)
        run_store.append_event(
            run_id,
            "runtime_restart_confirmed",
            "Runtime restart confirmed; restart gate cleared.",
            action="confirm-runtime-restarted",
            confirmed_by=confirmed_by,
            source=confirmed_by,
            previous_restart_reason=previous_restart_reason,
            previous_restart_paths=previous_restart_paths,
        )

    reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
    queue_store.save(queue)
    payload = dashboard_payloads.build_queue_payload(queue_file, runs_dir=runs_path)
    payload["transition"] = {
        "type": "runtime_restart_confirmed",
        "confirmed_by": confirmed_by,
    }
    return HTTPStatus.OK, payload


def create_proposal(
    proposals_path: Pathish,
    title: Any,
    prompt: Any,
    cwd: Any = None,
    *,
    runs_dir: Pathish,
    queue_path: Optional[Pathish] = None,
    config: SchedulerConfig,
    worktree_factory: Optional[Callable[..., Path]] = None,
) -> RunActionResponse:
    if not isinstance(title, str) or not title.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "title is required"}
    if not isinstance(prompt, str) or not prompt.strip():
        return HTTPStatus.BAD_REQUEST, {"error": "prompt is required"}
    try:
        proposal_cwd = _resolve_task_cwd(cwd, default_cwd=config.cwd)
        workspace_root = canonical_git_root(proposal_cwd)
    except (ValueError, WorkspaceResolutionError) as exc:
        return HTTPStatus.BAD_REQUEST, {"error": str(exc)}

    proposals_file = Path(proposals_path).expanduser().resolve()
    proposal_store = ProposalStore(proposals_file)
    pool = proposal_store.load_or_create()
    lane_busy = _workspace_lane_is_locked(
        workspace_root=workspace_root,
        proposals=pool.proposals,
        queue_path=queue_path,
        run_store=RunStore(Path(runs_dir).expanduser().resolve()),
    )
    proposal = proposal_store.add_proposal(
        pool,
        title=title.strip(),
        prompt=prompt.strip(),
        cwd=str(proposal_cwd),
    )
    if lane_busy:
        proposal_store.update_proposal(
            pool,
            proposal.proposal_id,
            status=PROPOSAL_WAITING_WORKSPACE,
            reason="workspace_lane",
        )
        proposal_store.save(pool)
        payload = dashboard_payloads.build_proposals_payload(proposals_file, runs_dir=runs_dir)
        payload["transition"] = {
            "type": "proposal_created_waiting_workspace",
            "proposal_id": proposal.proposal_id,
        }
        return HTTPStatus.OK, payload
    proposal_store.save(pool)

    run_store = RunStore(Path(runs_dir).expanduser().resolve())
    try:
        manifest = _create_preflight_run(
            run_store=run_store,
            config=config,
            user_task=proposal.prompt,
            task_cwd=proposal_cwd,
            worktree_factory=worktree_factory,
        )
        proposal_store.update_proposal(
            pool,
            proposal.proposal_id,
            run_id=manifest.run_id,
            status=PROPOSAL_PLANNING,
            error=None,
            reason="planner",
        )
        proposal_store.save(pool)
    except Exception as exc:
        proposal_store.update_proposal(
            pool,
            proposal.proposal_id,
            status=PROPOSAL_FAILED,
            error=str(exc),
            reason="proposal_preflight_failed",
        )
        proposal_store.save(pool)
        return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}

    payload = dashboard_payloads.build_proposals_payload(proposals_file, runs_dir=runs_dir)
    payload["transition"] = {
        "type": "proposal_created",
        "proposal_id": proposal.proposal_id,
        "run_id": manifest.run_id,
        "selected_run_id": manifest.run_id,
    }
    return HTTPStatus.OK, payload


def proposal_action(
    proposals_path: Pathish,
    proposal_id: str,
    action: Any,
    feedback: Any = None,
    *,
    runs_dir: Pathish,
    queue_path: Pathish,
    driver_factory: DriverFactory,
) -> RunActionResponse:
    if action not in {"approve-plan", "revise-plan"}:
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
    proposals_file = Path(proposals_path).expanduser().resolve()
    proposal_store = ProposalStore(proposals_file)
    try:
        pool = proposal_store.load()
        proposal = proposal_store.find(pool, proposal_id)
    except (OSError, ValueError) as exc:
        return HTTPStatus.NOT_FOUND, {"error": str(exc)}
    if not proposal.run_id:
        return HTTPStatus.CONFLICT, {"error": "proposal has no Planner run"}
    run_store = RunStore(Path(runs_dir).expanduser().resolve())
    try:
        manifest = run_store.load(proposal.run_id)
    except OSError:
        return HTTPStatus.NOT_FOUND, {"error": "run not found"}
    if manifest.status != RUN_PLAN_REVIEW_REQUIRED or manifest.plan is None:
        return HTTPStatus.CONFLICT, {
            "error": f"proposal requires PLAN_REVIEW_REQUIRED, current status is {manifest.status}",
            "status": manifest.status,
        }
    if action == "revise-plan":
        if not isinstance(feedback, str) or not feedback.strip():
            return HTTPStatus.BAD_REQUEST, {"error": "feedback is required for revise-plan"}
        codex_path = manifest.codex_binary_path or manifest.planner.codex_binary_path
        if not codex_path:
            return HTTPStatus.BAD_REQUEST, {"error": "missing codex binary path"}
        try:
            with driver_factory(codex_path) as driver:
                from .orchestrator import OrchestratorConfig, RunOrchestrator

                orchestrator = RunOrchestrator(
                    store=run_store,
                    driver=driver,
                    config=OrchestratorConfig(require_plan_approval=True),
                )
                manifest = orchestrator.revise_plan(manifest, feedback.strip())
        except Exception as exc:
            proposal_store.update_proposal(
                pool,
                proposal.proposal_id,
                status=PROPOSAL_FAILED,
                error=str(exc),
                reason="proposal_revision_failed",
            )
            proposal_store.save(pool)
            return HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
        proposal_store.update_proposal(
            pool,
            proposal.proposal_id,
            status=_proposal_status_from_run(manifest),
            error=None,
            reason=derive_run_waiting_for(manifest),
        )
        proposal_store.save(pool)
        payload = dashboard_payloads.build_proposals_payload(proposals_file, runs_dir=runs_dir)
        payload["transition"] = {
            "type": "proposal_plan_revised",
            "proposal_id": proposal.proposal_id,
            "run_id": manifest.run_id,
            "selected_run_id": manifest.run_id,
        }
        return HTTPStatus.OK, payload

    _approve_manifest_plan(run_store, manifest)
    task_id = _enqueue_approved_proposal(
        queue_path=queue_path,
        proposal=proposal,
        run_id=manifest.run_id,
        fallback_cwd=manifest.cwd,
    )
    proposal_store.remove_proposal(pool, proposal.proposal_id)
    proposal_store.save(pool)
    run_store.append_event(
        manifest.run_id,
        "proposal_plan_approved",
        "Human approved proposal plan; task queued for execution.",
        proposal_id=proposal.proposal_id,
        task_id=task_id,
        source="dashboard",
    )
    payload = dashboard_payloads.build_proposals_payload(proposals_file, runs_dir=runs_dir)
    payload["transition"] = {
        "type": "proposal_approved_and_queued",
        "proposal_id": proposal.proposal_id,
        "run_id": manifest.run_id,
        "task_id": task_id,
        "selected_run_id": manifest.run_id,
        "removed_from_pool": True,
        "queued": True,
    }
    return HTTPStatus.OK, payload


def prune_queued_proposals(proposals_path: Pathish) -> int:
    proposals_file = Path(proposals_path).expanduser().resolve()
    store = ProposalStore(proposals_file)
    try:
        pool = store.load()
    except OSError:
        return 0
    kept = [proposal for proposal in pool.proposals if proposal.status != PROPOSAL_QUEUED]
    removed = len(pool.proposals) - len(kept)
    if removed:
        pool.proposals = kept
        store.save(pool)
    return removed


_LOCKING_PROPOSAL_STATUSES = {
    PROPOSAL_PLANNING,
    PROPOSAL_PLAN_REVIEW_REQUIRED,
    PROPOSAL_PLAN_REVISING,
    PROPOSAL_FAILED,
}


def _workspace_lane_is_locked(
    *,
    workspace_root: Path,
    proposals: Sequence[ProposalRecord],
    queue_path: Optional[Pathish],
    run_store: RunStore,
    ignore_proposal_id: Optional[str] = None,
) -> bool:
    for proposal in proposals:
        if proposal.proposal_id == ignore_proposal_id:
            continue
        if proposal.status not in _LOCKING_PROPOSAL_STATUSES:
            continue
        proposal_cwd = proposal.cwd
        if not proposal_cwd and proposal.run_id:
            try:
                proposal_cwd = run_store.load(proposal.run_id).cwd
            except OSError:
                proposal_cwd = None
        if proposal_cwd and _same_workspace(proposal_cwd, workspace_root):
            return True
    if queue_path is None:
        return False
    try:
        queue = TaskStore(Path(queue_path).expanduser().resolve()).load()
    except (OSError, ValueError):
        return False
    for task in queue.tasks:
        if task.status in {TASK_APPROVED, TASK_SKIPPED}:
            continue
        task_cwd = task.cwd
        if not task_cwd and task.active_run_id:
            try:
                task_cwd = run_store.load(task.active_run_id).cwd
            except OSError:
                task_cwd = None
        if task_cwd and _same_workspace(task_cwd, workspace_root):
            return True
    return False


def _proposal_is_blocked_by_other_lane_item(
    *,
    proposal: ProposalRecord,
    proposals: Sequence[ProposalRecord],
    queue_path: Optional[Pathish],
    run_store: RunStore,
) -> bool:
    proposal_cwd = proposal.cwd
    if not proposal_cwd and proposal.run_id:
        try:
            proposal_cwd = run_store.load(proposal.run_id).cwd
        except OSError:
            proposal_cwd = None
    if not proposal_cwd:
        return False
    try:
        workspace_root = canonical_git_root(proposal_cwd)
    except WorkspaceResolutionError:
        return False
    return _workspace_lane_is_locked(
        workspace_root=workspace_root,
        proposals=proposals,
        queue_path=queue_path,
        run_store=run_store,
        ignore_proposal_id=proposal.proposal_id,
    )


def _same_workspace(path: Pathish, workspace_root: Path) -> bool:
    try:
        return canonical_git_root(path) == workspace_root
    except WorkspaceResolutionError:
        return False












def _create_preflight_run(
    *,
    run_store: RunStore,
    config: SchedulerConfig,
    user_task: str,
    task_cwd: Path,
    worktree_factory: Optional[Callable[..., Path]],
) -> RunManifest:
    worktrees_dir = config.resolve_worktrees_dir(task_cwd)
    manifest = run_store.create_run(
        cwd=task_cwd,
        user_task=user_task,
        planner_model=config.planner_model,
        worker_model=config.worker_model,
        codex_binary_path=config.codex_binary_path,
        planner_reasoning_effort=config.planner_reasoning_effort,
        worker_reasoning_effort=config.worker_reasoning_effort,
        planner_service_tier=config.planner_service_tier,
        worker_service_tier=config.worker_service_tier,
    )
    factory = worktree_factory or create_worker_worktree
    worker = manifest.workers[0]
    worker.worktree_path = str(
        factory(
            repo_path=task_cwd,
            worktrees_dir=worktrees_dir,
            run_id=manifest.run_id,
            worker_id=worker.id,
        )
    )
    run_store.save(manifest)
    return manifest


def _resolve_task_cwd(raw_cwd: Any, *, default_cwd: Path) -> Path:
    if raw_cwd is None:
        target = default_cwd
    elif not isinstance(raw_cwd, str):
        raise ValueError("cwd must be a string when provided")
    else:
        text = raw_cwd.strip()
        if not text:
            raise ValueError("cwd cannot be empty when provided")
        target = Path(text).expanduser()
        if not target.is_absolute():
            target = default_cwd / target
    resolved = target.expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"cwd does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"cwd must be a directory: {resolved}")
    return resolved


def _proposal_status_from_run(manifest: RunManifest) -> str:
    if manifest.status == RUN_PLAN_REVIEW_REQUIRED:
        return PROPOSAL_PLAN_REVIEW_REQUIRED
    if manifest.status == RUN_PLAN_APPROVED:
        return PROPOSAL_APPROVED
    if manifest.status == "PLAN_REVISING":
        return PROPOSAL_PLAN_REVISING
    if manifest.status == "FAILED":
        return PROPOSAL_FAILED
    return PROPOSAL_PLANNING


def _approve_manifest_plan(run_store: RunStore, manifest: RunManifest) -> None:
    if manifest.plan is None:
        raise ValueError("manifest has no Planner plan")
    manifest.plan.approval_status = "approved"
    manifest.plan.approved_at = run_store.now_iso()
    manifest.plan.approved_by = "human"
    record_run_status_transition(
        manifest,
        RUN_PLAN_APPROVED,
        run_store.now_iso(),
        metadata={"approved_by": "human"},
    )
    run_store.save(manifest)


def _enqueue_approved_proposal(
    *,
    queue_path: Pathish,
    proposal: ProposalRecord,
    run_id: str,
    fallback_cwd: Optional[str] = None,
) -> str:
    queue_file = Path(queue_path).expanduser().resolve()
    store = TaskStore(queue_file)
    try:
        queue = store.load()
    except OSError:
        queue = TaskQueue(queue_id="default")
    task_id = _unique_task_id(queue, proposal.proposal_id)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    queue.tasks.append(
        TaskRecord(
            task_id=task_id,
            title=proposal.title,
            prompt=proposal.prompt,
            cwd=proposal.cwd or fallback_cwd,
            status=TASK_PENDING,
            active_run_id=run_id,
            run_ids=[run_id],
            created_at=now,
            updated_at=now,
        )
    )
    store.save(queue)
    return task_id


def _unique_task_id(queue: TaskQueue, preferred: str) -> str:
    used = {task.task_id for task in queue.tasks}
    candidate = preferred
    suffix = 1
    while candidate in used:
        suffix += 1
        candidate = f"{preferred}-{suffix:02d}"
    return candidate


def _selected_run_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    transition = payload.get("transition")
    if isinstance(transition, dict):
        selected = transition.get("selected_run_id") or transition.get("run_id")
        if isinstance(selected, str) and selected:
            return selected
    run = payload.get("run")
    if isinstance(run, dict):
        run_id = run.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    state = payload.get("state")
    if isinstance(state, dict):
        selected_run = state.get("selected_run")
        if isinstance(selected_run, dict):
            run = selected_run.get("run")
            if isinstance(run, dict):
                run_id = run.get("run_id")
                if isinstance(run_id, str) and run_id:
                    return run_id
    return None






def _validate_run_action(
    manifest: Any,
    action: Any,
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Optional[str]:
    if action in {"approve-plan", "revise-plan"}:
        if manifest.status != RUN_PLAN_REVIEW_REQUIRED:
            return f"{action} requires PLAN_REVIEW_REQUIRED, current status is {manifest.status}"
        if manifest.plan is None:
            return f"{action} requires a saved Planner plan"
        if action == "approve-plan" and manifest.plan.approval_status == "approved":
            return "Planner plan is already approved"
    if action == "retry-review" and not has_retryable_review_failure(manifest):
        return f"retry-review requires saved failed review evidence, current status is {manifest.status}"
    if action == "retry-verification" and not has_retryable_verification_failure(manifest, events or []):
        return (
            "retry-verification requires an accepted Planner review with a failed "
            f"verification gate, current status is {manifest.status}"
        )
    return None








def reconcile_queue_file(*, queue_path: Pathish, runs_dir: Pathish) -> bool:
    queue_file = Path(queue_path).expanduser().resolve()
    runs_path = Path(runs_dir).expanduser().resolve()
    store = TaskStore(queue_file)
    queue = store.load()
    run_store = RunStore(runs_path)
    changed = reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
    if changed:
        store.save(queue)
    return changed




















def _find_task_record(queue: TaskQueue, task_id: str) -> TaskRecord:
    for task in queue.tasks:
        if task.task_id == task_id:
            return task
    raise ValueError(f"task not found: {task_id}")


















def _valid_run_id(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}










@contextmanager
def _default_driver_factory(codex_path: str):
    from .mcp_driver import McpCodexDriver

    with McpCodexDriver(codex_bin=codex_path) as driver:
        yield driver
