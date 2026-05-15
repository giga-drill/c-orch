from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union


Pathish = Union[str, Path]

QUEUE_PENDING = "PENDING"
QUEUE_RUNNING = "RUNNING"
QUEUE_APPROVED = "APPROVED"
QUEUE_FAILED = "FAILED"
QUEUE_BLOCKED = "BLOCKED"
QUEUE_RESTART_REQUIRED = "RESTART_REQUIRED"

TASK_PENDING = "PENDING"
TASK_RUNNING = "RUNNING"
TASK_WAITING = "WAITING"
TASK_APPROVED = "APPROVED"
TASK_FAILED = "FAILED"
TASK_BLOCKED = "BLOCKED"
TASK_SKIPPED = "SKIPPED"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


@dataclass
class TaskRecord:
    task_id: str
    title: str
    prompt: str
    cwd: Optional[str] = None
    status: str = TASK_PENDING
    active_run_id: Optional[str] = None
    run_ids: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    completed_at: Optional[str] = None
    error: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "prompt": self.prompt,
            "cwd": self.cwd,
            "status": self.status,
            "active_run_id": self.active_run_id,
            "run_ids": list(self.run_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRecord":
        task_id = data.get("task_id")
        if task_id is None:
            task_id = data.get("id")
        title = data.get("title", "")
        prompt = data.get("prompt", "")
        now = _now_iso()
        return cls(
            task_id=str(task_id or ""),
            title=str(title or ""),
            prompt=str(prompt or ""),
            cwd=_optional_text(data.get("cwd")),
            status=str(data.get("status", TASK_PENDING)),
            active_run_id=data.get("active_run_id"),
            run_ids=_string_list(data.get("run_ids")),
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
            reason=data.get("reason"),
        )


@dataclass
class TaskQueue:
    queue_id: str
    status: str = QUEUE_PENDING
    tasks: List[TaskRecord] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "status": self.status,
            "tasks": [task.to_dict() for task in self.tasks],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskQueue":
        now = _now_iso()
        tasks_raw = data.get("tasks")
        tasks = [
            TaskRecord.from_dict(item)
            for item in tasks_raw
            if isinstance(item, dict)
        ] if isinstance(tasks_raw, list) else []
        return cls(
            queue_id=str(data.get("queue_id") or "default"),
            status=str(data.get("status") or QUEUE_PENDING),
            tasks=tasks,
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
        )


class TaskStore:
    def __init__(self, queue_path: Pathish) -> None:
        self.queue_path = Path(queue_path)

    def create(self, *, queue_id: str = "default") -> TaskQueue:
        queue = TaskQueue(queue_id=queue_id)
        self.save(queue, touch=False)
        return queue

    def load(self) -> TaskQueue:
        with self.queue_path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        if not isinstance(data, dict):
            raise ValueError("queue file must contain a JSON object")
        queue = TaskQueue.from_dict(data)
        self.validate(queue)
        return queue

    def save(self, queue: TaskQueue, *, touch: bool = True) -> Path:
        if touch:
            now = _now_iso()
            queue.updated_at = now
            for task in queue.tasks:
                if not task.updated_at:
                    task.updated_at = now
                if not task.created_at:
                    task.created_at = now
        if not queue.created_at:
            queue.created_at = queue.updated_at or _now_iso()
        self.validate(queue)
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.queue_path.parent,
            prefix=f".{self.queue_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_obj:
            json.dump(queue.to_dict(), file_obj, ensure_ascii=False, indent=2)
            file_obj.write("\n")
            tmp_path = Path(file_obj.name)
        tmp_path.replace(self.queue_path)
        return self.queue_path

    def import_tasks(
        self,
        tasks: Iterable[Dict[str, Any]],
        *,
        queue_id: str = "default",
        cwd_resolver: Optional[Callable[[Any, int], Optional[str]]] = None,
    ) -> TaskQueue:
        now = _now_iso()
        parsed: List[TaskRecord] = []
        seen_ids = set()
        for index, raw_task in enumerate(tasks):
            if not isinstance(raw_task, dict):
                raise ValueError(f"task at index {index} must be an object")
            task_id = raw_task.get("task_id")
            if task_id is None:
                task_id = raw_task.get("id")
            task_id_text = _as_string(task_id, field_name=f"tasks[{index}].task_id")
            if task_id_text in seen_ids:
                raise ValueError(f"duplicate task_id: {task_id_text}")
            seen_ids.add(task_id_text)
            title = _as_string(raw_task.get("title"), field_name=f"tasks[{index}].title")
            prompt = _as_string(raw_task.get("prompt"), field_name=f"tasks[{index}].prompt")
            raw_cwd = raw_task.get("cwd")
            task_cwd = cwd_resolver(raw_cwd, index) if cwd_resolver is not None else _optional_text(raw_cwd)
            parsed.append(
                TaskRecord(
                    task_id=task_id_text,
                    title=title,
                    prompt=prompt,
                    cwd=task_cwd,
                    status=TASK_PENDING,
                    active_run_id=None,
                    run_ids=[],
                    created_at=now,
                    updated_at=now,
                )
            )
        queue = TaskQueue(
            queue_id=queue_id,
            status=QUEUE_PENDING,
            tasks=parsed,
            created_at=now,
            updated_at=now,
        )
        self.save(queue, touch=False)
        return queue

    def list_tasks(self, queue: TaskQueue) -> List[TaskRecord]:
        return list(queue.tasks)

    def next_pending(self, queue: TaskQueue) -> Optional[TaskRecord]:
        for task in queue.tasks:
            if task.status == TASK_PENDING:
                return task
        return None

    def update_task(self, queue: TaskQueue, task_id: str, **fields: Any) -> TaskRecord:
        task = self._find_task(queue, task_id)
        for key, value in fields.items():
            if not hasattr(task, key):
                raise ValueError(f"unknown task field: {key}")
            setattr(task, key, value)
        task.updated_at = _now_iso()
        queue.updated_at = task.updated_at
        return task

    def validate(self, queue: TaskQueue) -> None:
        if not queue.queue_id:
            raise ValueError("queue_id cannot be empty")
        seen = set()
        for index, task in enumerate(queue.tasks):
            task_id = _as_string(task.task_id, field_name=f"tasks[{index}].task_id")
            if task_id in seen:
                raise ValueError(f"duplicate task_id: {task_id}")
            seen.add(task_id)
            task.title = _as_string(task.title, field_name=f"tasks[{index}].title")
            task.prompt = _as_string(task.prompt, field_name=f"tasks[{index}].prompt")
            task.task_id = task_id
            if task.cwd is not None:
                task.cwd = _as_string(task.cwd, field_name=f"tasks[{index}].cwd")
            if not task.status:
                task.status = TASK_PENDING
            if task.run_ids is None:
                task.run_ids = []

    def _find_task(self, queue: TaskQueue, task_id: str) -> TaskRecord:
        for task in queue.tasks:
            if task.task_id == task_id:
                return task
        raise ValueError(f"task not found: {task_id}")


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    text = str(value).strip()
    return text or None
