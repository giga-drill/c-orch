from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
import json
from pathlib import Path
import threading
from typing import Any, Callable, ContextManager, Dict, List, Optional, Sequence, Tuple, Union

from .codex_session_logs import CodexSessionLogStore
from .drivers import CodexDriver
from .failure_policy import has_retryable_review_failure, has_retryable_review_failure_dict
from .run_store import RunManifest, RunStore
from .scheduler import OrchestratorLike, SchedulerConfig, TaskScheduler
from .states import (
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_STATUS_ORDER,
    TERMINAL_RUN_STATUSES,
)
from .task_lifecycle import (
    derive_run_waiting_for,
    derive_task_progress,
    mark_task_for_retry,
    reconcile_queue,
)
from .task_store import TaskStore


Pathish = Union[str, Path]
RunActionResponse = Optional[Tuple[HTTPStatus, Dict[str, Any]]]
DriverFactory = Callable[[str], ContextManager[CodexDriver]]


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
        scheduler_config: Optional[SchedulerConfig] = None,
        driver_factory: Optional[DriverFactory] = None,
        worktree_factory: Optional[Callable[..., Path]] = None,
        orchestrator_factory: Optional[Callable[[], OrchestratorLike]] = None,
    ) -> None:
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.queue_path = Path(queue_path).expanduser().resolve() if queue_path is not None else None
        self._scheduler_config = scheduler_config
        self._external_driver_factory = driver_factory
        self._worktree_factory = worktree_factory
        self._orchestrator_factory = orchestrator_factory
        self._action_lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._dispatch_lock = threading.RLock()
        self._drivers: Dict[str, Any] = {}
        self._dispatch_thread: Optional[threading.Thread] = None
        self._last_dispatch_error: Optional[str] = None

    def build_runs_payload(self) -> Dict[str, Any]:
        return build_runs_payload(self.runs_dir)

    def build_queue_payload(self) -> Dict[str, Any]:
        return build_queue_payload(self.queue_path, runs_dir=self.runs_dir)

    def build_run_payload(self, run_id: str) -> Optional[Dict[str, Any]]:
        return build_run_payload(self.runs_dir, run_id)

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
            return result

    def task_action(self, task_id: str, action: Any) -> RunActionResponse:
        with self._action_lock:
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
            return result

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
        with self._dispatch_lock:
            thread = self._dispatch_thread
        if thread is None:
            return True
        thread.join(timeout=timeout)
        return not thread.is_alive()

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
            with self._action_lock:
                self._run_queue_once()
        except Exception as exc:
            self._last_dispatch_error = str(exc)

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

    def _scheduler_overrides(self) -> Dict[str, Any]:
        overrides: Dict[str, Any] = {}
        if self._worktree_factory is not None:
            overrides["worktree_factory"] = self._worktree_factory
        if self._orchestrator_factory is not None:
            overrides["orchestrator_factory"] = self._orchestrator_factory
        return overrides


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
    if action not in {"approve-plan", "revise-plan", "retry-review"}:
        return HTTPStatus.BAD_REQUEST, {"error": "unsupported action"}
    action_error = _validate_run_action(manifest, action)
    if action_error:
        return HTTPStatus.CONFLICT, {"error": action_error, "status": manifest.status}
    if action == "revise-plan":
        if not isinstance(feedback, str) or not feedback.strip():
            return HTTPStatus.BAD_REQUEST, {"error": "feedback is required for revise-plan"}

    from .orchestrator import OrchestratorConfig, RunOrchestrator

    codex_path = (
        manifest.codex_binary_path
        or manifest.planner.codex_binary_path
    )
    if not codex_path:
        return HTTPStatus.BAD_REQUEST, {"error": "missing codex binary path"}

    try:
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
    payload = build_run_payload(runs_path, run_id)
    return HTTPStatus.OK, payload or {"ok": True}


def task_action(
    queue_path: Pathish,
    task_id: str,
    action: Any,
    *,
    runs_dir: Optional[Pathish] = None,
) -> RunActionResponse:
    if action != "retry-task":
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
    try:
        mark_task_for_retry(queue, task_id=task_id)
    except ValueError as exc:
        return HTTPStatus.CONFLICT, {"error": str(exc)}
    store.save(queue)
    return HTTPStatus.OK, build_queue_payload(queue_file, runs_dir=runs_dir)


def build_runs_payload(runs_dir: Pathish) -> Dict[str, Any]:
    runs_path = Path(runs_dir).expanduser().resolve()
    store = RunStore(runs_path)
    runs = [
        _summarize_manifest(
            manifest,
            events=store.load_events(str(manifest.get("run_id", ""))),
        )
        for manifest in _load_manifests(runs_path)
    ]
    runs.sort(key=lambda item: item["sort_key"], reverse=True)
    for run in runs:
        run.pop("sort_key", None)
    return {
        "runs_dir": str(runs_path),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runs": runs,
    }


def build_queue_payload(
    queue_path: Optional[Pathish],
    *,
    runs_dir: Optional[Pathish] = None,
) -> Dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if queue_path is None:
        return {
            "queue_file": None,
            "generated_at": generated_at,
            "queue": None,
            "tasks": [],
        }
    queue_file = Path(queue_path).expanduser().resolve()
    store = TaskStore(queue_file)
    try:
        queue = store.load()
    except (OSError, ValueError):
        return {
            "queue_file": str(queue_file),
            "generated_at": generated_at,
            "queue": None,
            "tasks": [],
        }
    run_store = RunStore(Path(runs_dir).expanduser().resolve()) if runs_dir is not None else None
    if run_store is not None:
        if reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso):
            store.save(queue)
    tasks = [_summarize_task(task, run_store=run_store) for task in queue.tasks]
    return {
        "queue_file": str(queue_file),
        "generated_at": generated_at,
        "queue": {
            "queue_id": queue.queue_id,
            "status": queue.status,
            "created_at": queue.created_at,
            "updated_at": queue.updated_at,
        },
        "summary": _queue_summary(tasks),
        "tasks": tasks,
    }


def build_run_payload(runs_dir: Pathish, run_id: str) -> Optional[Dict[str, Any]]:
    runs_path = Path(runs_dir).expanduser().resolve()
    if not _valid_run_id(run_id):
        return None
    manifest_path = runs_path / run_id / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return None
    events = RunStore(runs_path).load_events(run_id)
    return {
        "run": _summarize_manifest(manifest, events=events),
        "manifest": manifest,
        "evidence_files": _evidence_details(manifest),
        "events": events,
        "worker_activity": _worker_activity(manifest),
    }


def _validate_run_action(manifest: Any, action: Any) -> Optional[str]:
    if action in {"approve-plan", "revise-plan"}:
        if manifest.status != RUN_PLAN_REVIEW_REQUIRED:
            return f"{action} requires PLAN_REVIEW_REQUIRED, current status is {manifest.status}"
        if manifest.plan is None:
            return f"{action} requires a saved Planner plan"
        if action == "approve-plan" and manifest.plan.approval_status == "approved":
            return "Planner plan is already approved"
    if action == "retry-review" and not has_retryable_review_failure(manifest):
        return f"retry-review requires saved failed review evidence, current status is {manifest.status}"
    return None


def _load_manifests(runs_dir: Path) -> List[Dict[str, Any]]:
    manifests: List[Dict[str, Any]] = []
    if not runs_dir.exists():
        return manifests
    for path in sorted(runs_dir.glob("*/manifest.json")):
        manifest = _load_manifest(path)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _load_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return RunManifest.from_dict(data).to_dict()


def _summarize_manifest(
    manifest: Dict[str, Any],
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    planner = _dict_value(manifest.get("planner"))
    workers = [_summarize_worker(worker) for worker in _list_value(manifest.get("workers"))]
    review = _dict_value(manifest.get("review"))
    review_attempts = [_dict_value(attempt) for attempt in _list_value(manifest.get("review_attempts"))]
    plan = _dict_value(manifest.get("plan"))
    plan_revisions = [_dict_value(revision) for revision in _list_value(manifest.get("plan_revisions"))]
    latest_plan_revision = plan_revisions[-1] if plan_revisions else {}
    status = str(manifest.get("status", "UNKNOWN"))
    evidence_files = _unique_strings(
        _flatten(
            [
                worker.get("evidence_files", [])
                for worker in _list_value(manifest.get("workers"))
                if isinstance(worker, dict)
            ]
        )
        + _list_value(review.get("evidence_files"))
    )
    updated_at = str(manifest.get("updated_at", ""))
    created_at = str(manifest.get("created_at", ""))
    waiting_for = _derive_manifest_waiting_for(manifest)
    return {
        "run_id": str(manifest.get("run_id", "")),
        "status": status,
        "status_index": RUN_STATUS_ORDER.get(status, 0),
        "terminal": status in TERMINAL_RUN_STATUSES,
        "waiting_for": waiting_for,
        "next_action": waiting_for,
        "requires_restart": bool(manifest.get("requires_restart", False)),
        "restart_reason": manifest.get("restart_reason"),
        "restart_paths": _list_value(manifest.get("restart_paths")),
        "user_task": str(manifest.get("user_task", "")),
        "cwd": str(manifest.get("cwd", "")),
        "created_at": created_at,
        "updated_at": updated_at,
        "sort_key": updated_at or created_at,
        "planner": {
            "status": str(planner.get("status", "PENDING")),
            "model": str(planner.get("model", "")),
            "thread_id": planner.get("thread_id"),
            "reasoning_effort": planner.get("reasoning_effort"),
            "service_tier": planner.get("service_tier"),
        },
        "workers": workers,
        "review": {
            "decision": review.get("decision"),
            "reason": review.get("reason"),
        } if review else None,
        "review_attempt_count": len(review_attempts),
        "last_review_attempt": _review_attempt_summary(review_attempts[-1]) if review_attempts else None,
        "can_retry_review": has_retryable_review_failure_dict(manifest),
        "last_event": _event_summary(events[-1]) if events else None,
        "last_error_event": _last_error_event(events or []),
        "plan": {
            "approval_status": plan.get("approval_status"),
            "summary": plan.get("summary"),
        } if plan else None,
        "plan_revision_count": len(plan_revisions),
        "latest_plan_revision_id": latest_plan_revision.get("id"),
        "latest_plan_revision_created_at": latest_plan_revision.get("created_at"),
        "latest_plan_revision_feedback": latest_plan_revision.get("human_feedback"),
        "acceptance_count": len(_list_value(manifest.get("acceptance_criteria"))),
        "verification_count": len(_list_value(manifest.get("verification_commands"))),
        "evidence_count": len(evidence_files),
    }


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


def _summarize_task(task: Any, *, run_store: Optional[RunStore]) -> Dict[str, Any]:
    active_run = None
    if run_store is not None and task.active_run_id:
        try:
            active_run = run_store.load(task.active_run_id)
        except OSError:
            active_run = None
    progress = derive_task_progress(task, active_run=active_run)
    return {
        "task_id": task.task_id,
        "title": task.title,
        "status": task.status,
        "active_run_id": task.active_run_id,
        "run_ids": list(task.run_ids),
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "reason": task.reason,
        "error": task.error,
        "waiting_for": progress.waiting_for,
        "next_action": progress.waiting_for,
    }


def _queue_summary(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(tasks)
    approved = sum(1 for task in tasks if task.get("status") == "APPROVED")
    pending = sum(1 for task in tasks if task.get("status") == "PENDING")
    failed = sum(1 for task in tasks if task.get("status") == "FAILED")
    running = sum(1 for task in tasks if task.get("status") in {"RUNNING", "WAITING"})
    current = next((task for task in tasks if task.get("status") != "APPROVED"), None)
    return {
        "total_tasks": total,
        "approved_tasks": approved,
        "completed_tasks": approved,
        "pending_tasks": pending,
        "failed_tasks": failed,
        "running_tasks": running,
        "current_waiting_point": current.get("waiting_for") if current else "done",
    }


def _derive_manifest_waiting_for(manifest: Dict[str, Any]) -> str:
    try:
        return derive_run_waiting_for(RunManifest.from_dict(manifest))
    except Exception:
        status = str(manifest.get("status", ""))
        if status == "APPROVED":
            return "restart" if manifest.get("requires_restart") else "done"
        if status == "FAILED":
            return "failed"
        return "planner"


def _review_attempt_summary(attempt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": attempt.get("id"),
        "status": attempt.get("status"),
        "worker_attempt": attempt.get("worker_attempt"),
        "workspace_path": attempt.get("workspace_path"),
        "evidence_count": len(_list_value(attempt.get("evidence_files"))),
        "decision": attempt.get("decision"),
        "reason": attempt.get("reason"),
        "error": attempt.get("error"),
        "completed_at": attempt.get("completed_at"),
    }


def _event_summary(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": event.get("timestamp"),
        "type": event.get("type"),
        "message": event.get("message"),
        "worker_id": event.get("worker_id"),
        "attempt": event.get("attempt"),
        "decision": event.get("decision"),
        "error": event.get("error"),
    }


def _last_error_event(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if "error" in event or str(event.get("type", "")).endswith("_failed"):
            return _event_summary(event)
    return None


def _summarize_worker(worker: Any) -> Dict[str, Any]:
    data = _dict_value(worker)
    evidence_files = _list_value(data.get("evidence_files"))
    return {
        "id": str(data.get("id", "")),
        "status": str(data.get("status", "PENDING")),
        "model": str(data.get("model", "")),
        "thread_id": data.get("thread_id"),
        "reasoning_effort": data.get("reasoning_effort"),
        "service_tier": data.get("service_tier"),
        "attempt": data.get("attempt", 1),
        "worktree_path": data.get("worktree_path"),
        "evidence_count": len(evidence_files),
    }


def _evidence_details(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths = []
    for worker in _list_value(manifest.get("workers")):
        if isinstance(worker, dict):
            paths.extend(_list_value(worker.get("evidence_files")))
    review = _dict_value(manifest.get("review"))
    paths.extend(_list_value(review.get("evidence_files")))
    details = []
    for value in _unique_strings(paths):
        path = Path(value).expanduser()
        details.append(
            {
                "path": value,
                "name": path.name,
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else None,
            }
        )
    return details


def _worker_activity(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = CodexSessionLogStore()
    activities: List[Dict[str, Any]] = []
    for worker in _list_value(manifest.get("workers")):
        data = _dict_value(worker)
        thread_id = data.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        for item in store.load_recent_activity(thread_id=thread_id, limit=60):
            value = item.to_dict()
            value["worker_id"] = str(data.get("id", "worker"))
            activities.append(value)
    return activities[-80:]


def _valid_run_id(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _flatten(values: Sequence[Any]) -> List[Any]:
    result: List[Any] = []
    for value in values:
        if isinstance(value, list):
            result.extend(value)
        else:
            result.append(value)
    return result


def _unique_strings(values: Sequence[Any]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


@contextmanager
def _default_driver_factory(codex_path: str):
    from .mcp_driver import McpCodexDriver

    with McpCodexDriver(codex_bin=codex_path) as driver:
        yield driver
