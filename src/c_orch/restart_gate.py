from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .run_store import RunManifest
from .task_store import TaskQueue


@dataclass(frozen=True)
class RestartGateItem:
    task_id: str
    run_id: str
    run_status: str
    restart_reason: Optional[str]
    restart_paths: List[str]
    cwd: Optional[str]
    workspace_id: Optional[str]


@dataclass(frozen=True)
class RestartGateState:
    active: bool
    waiting_for: str
    run_ids: List[str]
    task_ids: List[str]
    items: List[RestartGateItem]
    message: Optional[str]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "active": self.active,
            "waiting_for": self.waiting_for,
            "run_ids": list(self.run_ids),
            "task_ids": list(self.task_ids),
            "message": self.message,
            "items": [
                {
                    "task_id": item.task_id,
                    "run_id": item.run_id,
                    "run_status": item.run_status,
                    "restart_reason": item.restart_reason,
                    "restart_paths": list(item.restart_paths),
                    "cwd": item.cwd,
                    "workspace_id": item.workspace_id,
                }
                for item in self.items
            ],
        }


def detect_restart_gate(
    queue: TaskQueue,
    *,
    run_loader: Callable[[str], RunManifest],
) -> RestartGateState:
    items: List[RestartGateItem] = []
    seen_run_ids: set[str] = set()
    for task in queue.tasks:
        run_id = task.active_run_id
        if not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        try:
            manifest = run_loader(run_id)
        except OSError:
            continue
        if manifest.status != "APPROVED" or not manifest.requires_restart:
            continue
        items.append(
            RestartGateItem(
                task_id=task.task_id,
                run_id=manifest.run_id,
                run_status=manifest.status,
                restart_reason=manifest.restart_reason,
                restart_paths=list(manifest.restart_paths),
                cwd=task.cwd or manifest.cwd,
                workspace_id=manifest.workspace_id,
            )
        )

    if not items:
        return RestartGateState(
            active=False,
            waiting_for="done",
            run_ids=[],
            task_ids=[],
            items=[],
            message=None,
        )

    return RestartGateState(
        active=True,
        waiting_for="restart",
        run_ids=[item.run_id for item in items],
        task_ids=[item.task_id for item in items],
        items=items,
        message="restart gate 暂停新调度，等待 runtime 重启确认。",
    )
