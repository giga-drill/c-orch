from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


Pathish = Union[str, Path]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _list_of_strings(value: Any) -> List[str]:
    if value is None:
        return []
    return [str(item) for item in value]


@dataclass
class PlannerRecord:
    model: str
    thread_id: Optional[str] = None
    codex_binary_path: Optional[str] = None
    status: str = "PENDING"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "model": self.model,
            "codex_binary_path": self.codex_binary_path,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlannerRecord":
        return cls(
            thread_id=data.get("thread_id"),
            model=str(data.get("model", "")),
            codex_binary_path=data.get("codex_binary_path"),
            status=str(data.get("status", "PENDING")),
        )


@dataclass
class WorkerRecord:
    id: str
    model: str
    thread_id: Optional[str] = None
    worktree_path: Optional[str] = None
    status: str = "PENDING"
    attempt: int = 1
    evidence_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "status": self.status,
            "attempt": self.attempt,
            "evidence_files": list(self.evidence_files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerRecord":
        return cls(
            id=str(data.get("id", "")),
            thread_id=data.get("thread_id"),
            model=str(data.get("model", "")),
            worktree_path=data.get("worktree_path"),
            status=str(data.get("status", "PENDING")),
            attempt=int(data.get("attempt", 1)),
            evidence_files=_list_of_strings(data.get("evidence_files")),
        )


@dataclass
class ReviewRecord:
    decision: Optional[str] = None
    reason: Optional[str] = None
    next_worker_prompt: Optional[str] = None
    evidence_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "next_worker_prompt": self.next_worker_prompt,
            "evidence_files": list(self.evidence_files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewRecord":
        return cls(
            decision=data.get("decision"),
            reason=data.get("reason"),
            next_worker_prompt=data.get("next_worker_prompt"),
            evidence_files=_list_of_strings(data.get("evidence_files")),
        )


@dataclass
class RunManifest:
    run_id: str
    cwd: str
    user_task: str
    status: str
    planner: PlannerRecord
    workers: List[WorkerRecord]
    acceptance_criteria: List[str]
    verification_commands: List[str]
    review: Optional[ReviewRecord]
    created_at: str
    updated_at: str
    codex_binary_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "cwd": self.cwd,
            "user_task": self.user_task,
            "status": self.status,
            "planner": self.planner.to_dict(),
            "workers": [worker.to_dict() for worker in self.workers],
            "acceptance_criteria": list(self.acceptance_criteria),
            "verification_commands": list(self.verification_commands),
            "review": self.review.to_dict() if self.review is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "codex_binary_path": self.codex_binary_path,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunManifest":
        review_data = data.get("review")
        return cls(
            run_id=str(data.get("run_id", "")),
            cwd=str(data.get("cwd", "")),
            user_task=str(data.get("user_task", "")),
            status=str(data.get("status", "PENDING")),
            planner=PlannerRecord.from_dict(data.get("planner") or {}),
            workers=[
                WorkerRecord.from_dict(worker)
                for worker in data.get("workers", [])
            ],
            acceptance_criteria=_list_of_strings(data.get("acceptance_criteria")),
            verification_commands=_list_of_strings(data.get("verification_commands")),
            review=ReviewRecord.from_dict(review_data) if review_data else None,
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            codex_binary_path=data.get("codex_binary_path"),
        )


class RunStore:
    def __init__(self, runs_dir: Pathish = "runs") -> None:
        self.runs_dir = Path(runs_dir)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def create_run(
        self,
        cwd: Pathish,
        user_task: str,
        planner_model: str,
        worker_model: str,
        codex_binary_path: Optional[Pathish] = None,
    ) -> RunManifest:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        run_id = self._new_run_id()
        now = _now_iso()
        codex_path = str(codex_binary_path) if codex_binary_path is not None else None
        manifest = RunManifest(
            run_id=run_id,
            cwd=str(Path(cwd).expanduser().resolve()),
            user_task=user_task,
            status="NEW",
            planner=PlannerRecord(
                model=planner_model,
                codex_binary_path=codex_path,
            ),
            workers=[
                WorkerRecord(
                    id="worker-1",
                    model=worker_model,
                )
            ],
            acceptance_criteria=[],
            verification_commands=[],
            review=None,
            created_at=now,
            updated_at=now,
            codex_binary_path=codex_path,
        )
        self.save(manifest, touch=False)
        return manifest

    def load(self, run_id: str) -> RunManifest:
        path = self.manifest_path(run_id)
        with path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        return RunManifest.from_dict(data)

    def save(self, manifest: RunManifest, touch: bool = True) -> Path:
        if touch:
            manifest.updated_at = _now_iso()
        if not manifest.created_at:
            manifest.created_at = manifest.updated_at
        path = self.manifest_path(manifest.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as file_obj:
            json.dump(
                manifest.to_dict(),
                file_obj,
                ensure_ascii=False,
                indent=2,
            )
            file_obj.write("\n")
        tmp_path.replace(path)
        return path

    def _new_run_id(self) -> str:
        base = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        candidate = base
        suffix = 1
        while self.run_dir(candidate).exists():
            suffix += 1
            candidate = f"{base}-{suffix:02d}"
        return candidate
