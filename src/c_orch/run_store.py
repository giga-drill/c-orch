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


def _normalized_status(value: Any) -> str:
    status = str(value)
    return {
        "REVIEW_RETRYABLE": "WORK_DONE",
        "NEEDS_CHANGES": "REVISION_REQUESTED",
        "BLOCKED": "FAILED",
    }.get(status, status)


@dataclass
class PlannerRecord:
    model: str
    thread_id: Optional[str] = None
    codex_binary_path: Optional[str] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    status: str = "PENDING"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "model": self.model,
            "codex_binary_path": self.codex_binary_path,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlannerRecord":
        return cls(
            thread_id=data.get("thread_id"),
            model=str(data.get("model", "")),
            codex_binary_path=data.get("codex_binary_path"),
            reasoning_effort=data.get("reasoning_effort"),
            service_tier=data.get("service_tier"),
            status=_normalized_status(data.get("status", "PENDING")),
        )


@dataclass
class WorkerRecord:
    id: str
    model: str
    thread_id: Optional[str] = None
    worktree_path: Optional[str] = None
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    status: str = "PENDING"
    attempt: int = 1
    evidence_files: List[str] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "model": self.model,
            "worktree_path": self.worktree_path,
            "reasoning_effort": self.reasoning_effort,
            "service_tier": self.service_tier,
            "status": self.status,
            "attempt": self.attempt,
            "evidence_files": list(self.evidence_files),
            "result": dict(self.result) if isinstance(self.result, dict) else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkerRecord":
        return cls(
            id=str(data.get("id", "")),
            thread_id=data.get("thread_id"),
            model=str(data.get("model", "")),
            worktree_path=data.get("worktree_path"),
            reasoning_effort=data.get("reasoning_effort"),
            service_tier=data.get("service_tier"),
            status=_normalized_status(data.get("status", "PENDING")),
            attempt=int(data.get("attempt", 1)),
            evidence_files=_list_of_strings(data.get("evidence_files")),
            result=dict(data["result"]) if isinstance(data.get("result"), dict) else None,
        )


@dataclass
class ReviewRecord:
    decision: Optional[str] = None
    reason: Optional[str] = None
    summary: Optional[str] = None
    next_worker_prompt: Optional[str] = None
    evidence_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "summary": self.summary,
            "next_worker_prompt": self.next_worker_prompt,
            "evidence_files": list(self.evidence_files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewRecord":
        return cls(
            decision=data.get("decision"),
            reason=data.get("reason"),
            summary=data.get("summary"),
            next_worker_prompt=data.get("next_worker_prompt"),
            evidence_files=_list_of_strings(data.get("evidence_files")),
        )


@dataclass
class ReviewAttemptRecord:
    id: str
    worker_id: str
    status: str
    started_at: str
    worker_attempt: Optional[int] = None
    workspace_path: Optional[str] = None
    completed_at: Optional[str] = None
    decision: Optional[str] = None
    reason: Optional[str] = None
    summary: Optional[str] = None
    next_worker_prompt: Optional[str] = None
    error: Optional[str] = None
    evidence_files: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "worker_id": self.worker_id,
            "status": self.status,
            "started_at": self.started_at,
            "worker_attempt": self.worker_attempt,
            "workspace_path": self.workspace_path,
            "completed_at": self.completed_at,
            "decision": self.decision,
            "reason": self.reason,
            "summary": self.summary,
            "next_worker_prompt": self.next_worker_prompt,
            "error": self.error,
            "evidence_files": list(self.evidence_files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ReviewAttemptRecord":
        return cls(
            id=str(data.get("id", "")),
            worker_id=str(data.get("worker_id", "")),
            status=str(data.get("status", "")),
            started_at=str(data.get("started_at", "")),
            worker_attempt=int(data["worker_attempt"])
            if data.get("worker_attempt") is not None
            else None,
            workspace_path=data.get("workspace_path"),
            completed_at=data.get("completed_at"),
            decision=data.get("decision"),
            reason=data.get("reason"),
            summary=data.get("summary"),
            next_worker_prompt=data.get("next_worker_prompt"),
            error=data.get("error"),
            evidence_files=_list_of_strings(data.get("evidence_files")),
        )


@dataclass
class PlanRecord:
    summary: str
    worker_prompt: str
    risk_notes: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    approval_status: str = "pending"
    approved_at: Optional[str] = None
    approved_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "worker_prompt": self.worker_prompt,
            "risk_notes": list(self.risk_notes),
            "raw": dict(self.raw),
            "approval_status": self.approval_status,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanRecord":
        raw = data.get("raw")
        return cls(
            summary=str(data.get("summary", "")),
            worker_prompt=str(data.get("worker_prompt", "")),
            risk_notes=_list_of_strings(data.get("risk_notes")),
            raw=dict(raw) if isinstance(raw, dict) else {},
            approval_status=str(data.get("approval_status", "pending")),
            approved_at=data.get("approved_at"),
            approved_by=data.get("approved_by"),
        )


@dataclass
class PlanRevisionRecord:
    id: str
    created_at: str
    human_feedback: str
    previous_plan: Dict[str, Any]
    new_plan: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "human_feedback": self.human_feedback,
            "previous_plan": dict(self.previous_plan),
            "new_plan": dict(self.new_plan),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PlanRevisionRecord":
        return cls(
            id=str(data.get("id", "")),
            created_at=str(data.get("created_at", "")),
            human_feedback=str(data.get("human_feedback", "")),
            previous_plan=dict(data.get("previous_plan", {}))
            if isinstance(data.get("previous_plan"), dict)
            else {},
            new_plan=dict(data.get("new_plan", {}))
            if isinstance(data.get("new_plan"), dict)
            else {},
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
    plan: Optional[PlanRecord]
    plan_revisions: List[PlanRevisionRecord]
    review: Optional[ReviewRecord]
    review_attempts: List[ReviewAttemptRecord]
    created_at: str
    updated_at: str
    timing: Optional[Dict[str, Any]] = None
    codex_binary_path: Optional[str] = None
    requires_restart: bool = False
    restart_reason: Optional[str] = None
    restart_paths: List[str] = field(default_factory=list)

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
            "plan": self.plan.to_dict() if self.plan is not None else None,
            "plan_revisions": [revision.to_dict() for revision in self.plan_revisions],
            "review": self.review.to_dict() if self.review is not None else None,
            "review_attempts": [attempt.to_dict() for attempt in self.review_attempts],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timing": dict(self.timing) if isinstance(self.timing, dict) else None,
            "codex_binary_path": self.codex_binary_path,
            "requires_restart": self.requires_restart,
            "restart_reason": self.restart_reason,
            "restart_paths": list(self.restart_paths),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunManifest":
        review_data = data.get("review")
        plan_revisions = data.get("plan_revisions")
        review_attempts = data.get("review_attempts")
        return cls(
            run_id=str(data.get("run_id", "")),
            cwd=str(data.get("cwd", "")),
            user_task=str(data.get("user_task", "")),
            status=_normalized_status(data.get("status", "PENDING")),
            planner=PlannerRecord.from_dict(data.get("planner") or {}),
            workers=[
                WorkerRecord.from_dict(worker)
                for worker in data.get("workers", [])
            ],
            acceptance_criteria=_list_of_strings(data.get("acceptance_criteria")),
            verification_commands=_list_of_strings(data.get("verification_commands")),
            plan=PlanRecord.from_dict(data["plan"]) if isinstance(data.get("plan"), dict) else None,
            plan_revisions=[
                PlanRevisionRecord.from_dict(revision)
                for revision in plan_revisions
                if isinstance(revision, dict)
            ] if isinstance(plan_revisions, list) else [],
            review=ReviewRecord.from_dict(review_data) if review_data else None,
            review_attempts=[
                ReviewAttemptRecord.from_dict(attempt)
                for attempt in review_attempts
                if isinstance(attempt, dict)
            ] if isinstance(review_attempts, list) else [],
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            timing=dict(data.get("timing")) if isinstance(data.get("timing"), dict) else None,
            codex_binary_path=data.get("codex_binary_path"),
            requires_restart=bool(data.get("requires_restart", False)),
            restart_reason=data.get("restart_reason"),
            restart_paths=_list_of_strings(data.get("restart_paths")),
        )


class RunStore:
    def __init__(self, runs_dir: Pathish = "runs") -> None:
        self.runs_dir = Path(runs_dir)

    def now_iso(self) -> str:
        return _now_iso()

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    def manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def event_log_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "events.jsonl"

    def create_run(
        self,
        cwd: Pathish,
        user_task: str,
        planner_model: str,
        worker_model: str,
        codex_binary_path: Optional[Pathish] = None,
        planner_reasoning_effort: Optional[str] = None,
        worker_reasoning_effort: Optional[str] = None,
        planner_service_tier: Optional[str] = None,
        worker_service_tier: Optional[str] = None,
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
                reasoning_effort=planner_reasoning_effort,
                service_tier=planner_service_tier,
            ),
            workers=[
                WorkerRecord(
                    id="worker-1",
                    model=worker_model,
                    reasoning_effort=worker_reasoning_effort,
                    service_tier=worker_service_tier,
                )
            ],
            acceptance_criteria=[],
            verification_commands=[],
            plan=None,
            plan_revisions=[],
            review=None,
            review_attempts=[],
            created_at=now,
            updated_at=now,
            timing={"version": 1, "segments": []},
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

    def append_event(
        self,
        run_id: str,
        event_type: str,
        message: str,
        **fields: Any,
    ) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "timestamp": _now_iso(),
            "type": str(event_type),
            "message": str(message),
        }
        for key, value in fields.items():
            if value is None:
                continue
            event[str(key)] = value
        line = json.dumps(
            event,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        path = self.event_log_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as file_obj:
            file_obj.write(line)
            file_obj.write("\n")
        return json.loads(line)

    def load_events(self, run_id: str) -> List[Dict[str, Any]]:
        path = self.event_log_path(run_id)
        if not path.exists():
            return []
        events: List[Dict[str, Any]] = []
        try:
            with path.open("r", encoding="utf-8") as file_obj:
                for raw_line in file_obj:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(data, dict):
                        events.append(data)
        except OSError:
            return []
        return events

    def _new_run_id(self) -> str:
        base = datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
        candidate = base
        suffix = 1
        while self.run_dir(candidate).exists():
            suffix += 1
            candidate = f"{base}-{suffix:02d}"
        return candidate
