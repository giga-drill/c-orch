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
from .states import (
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_STATUS_ORDER,
    TERMINAL_RUN_STATUSES,
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
        driver_factory: Optional[DriverFactory] = None,
    ) -> None:
        self.runs_dir = Path(runs_dir).expanduser().resolve()
        self.queue_path = Path(queue_path).expanduser().resolve() if queue_path is not None else None
        self._external_driver_factory = driver_factory
        self._action_lock = threading.RLock()
        self._driver_lock = threading.RLock()
        self._drivers: Dict[str, Any] = {}

    def build_runs_payload(self) -> Dict[str, Any]:
        return build_runs_payload(self.runs_dir)

    def build_queue_payload(self) -> Dict[str, Any]:
        return build_queue_payload(self.queue_path)

    def build_run_payload(self, run_id: str) -> Optional[Dict[str, Any]]:
        return build_run_payload(self.runs_dir, run_id)

    def run_action(
        self,
        run_id: str,
        action: Any,
        feedback: Any = None,
    ) -> RunActionResponse:
        with self._action_lock:
            return run_action(
                self.runs_dir,
                run_id,
                action,
                feedback,
                driver_factory=self._driver_context,
            )

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


def build_queue_payload(queue_path: Optional[Pathish]) -> Dict[str, Any]:
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
    return {
        "queue_file": str(queue_file),
        "generated_at": generated_at,
        "queue": {
            "queue_id": queue.queue_id,
            "status": queue.status,
            "created_at": queue.created_at,
            "updated_at": queue.updated_at,
        },
        "tasks": [
            {
                "task_id": task.task_id,
                "title": task.title,
                "status": task.status,
                "active_run_id": task.active_run_id,
                "run_ids": list(task.run_ids),
                "updated_at": task.updated_at,
                "completed_at": task.completed_at,
            }
            for task in queue.tasks
        ],
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
    return {
        "run_id": str(manifest.get("run_id", "")),
        "status": status,
        "status_index": RUN_STATUS_ORDER.get(status, 0),
        "terminal": status in TERMINAL_RUN_STATUSES,
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
        "acceptance_count": len(_list_value(manifest.get("acceptance_criteria"))),
        "verification_count": len(_list_value(manifest.get("verification_commands"))),
        "evidence_count": len(evidence_files),
    }


def _review_attempt_summary(attempt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": attempt.get("id"),
        "status": attempt.get("status"),
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
