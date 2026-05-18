from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional


LEASE_STATUS_ACTIVE = "active"
LEASE_STATUS_COMPLETED = "completed"
LEASE_STATUS_FAILED = "failed"
LEASE_STATUS_EXPIRED = "expired"

_LEASE_STATUSES = {
    LEASE_STATUS_ACTIVE,
    LEASE_STATUS_COMPLETED,
    LEASE_STATUS_FAILED,
    LEASE_STATUS_EXPIRED,
}


def _now(now: Optional[datetime] = None) -> datetime:
    if now is not None:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _normalize_checkpoint(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in _LEASE_STATUSES else LEASE_STATUS_ACTIVE


@dataclass
class RunnerLeaseRecord:
    runner_id: str
    runtime_generation: str
    process_hint: Optional[str]
    pid: Optional[int]
    run_id: str
    task_id: Optional[str]
    proposal_id: Optional[str]
    phase: str
    started_at: str
    heartbeat_at: str
    lease_expires_at: str
    status: str
    checkpoint: Dict[str, Any]
    completed_at: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "runner_id": self.runner_id,
            "runtime_generation": self.runtime_generation,
            "process_hint": self.process_hint,
            "pid": self.pid,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "proposal_id": self.proposal_id,
            "phase": self.phase,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "lease_expires_at": self.lease_expires_at,
            "status": self.status,
            "checkpoint": dict(self.checkpoint),
            "completed_at": self.completed_at,
            "error": self.error,
        }
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunnerLeaseRecord":
        pid_raw = data.get("pid")
        pid = int(pid_raw) if isinstance(pid_raw, int) or (isinstance(pid_raw, str) and pid_raw.isdigit()) else None
        return cls(
            runner_id=str(data.get("runner_id", "")),
            runtime_generation=str(data.get("runtime_generation", "")),
            process_hint=data.get("process_hint"),
            pid=pid,
            run_id=str(data.get("run_id", "")),
            task_id=data.get("task_id"),
            proposal_id=data.get("proposal_id"),
            phase=str(data.get("phase", "")),
            started_at=str(data.get("started_at", "")),
            heartbeat_at=str(data.get("heartbeat_at", "")),
            lease_expires_at=str(data.get("lease_expires_at", "")),
            status=_normalize_status(data.get("status")),
            checkpoint=_normalize_checkpoint(data.get("checkpoint")),
            completed_at=data.get("completed_at"),
            error=data.get("error"),
        )


@dataclass
class RunnerLeaseReadResult:
    schema_version: int
    updated_at: Optional[str]
    leases: List[Dict[str, Any]]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": self.updated_at,
            "summary": dict(self.summary),
            "leases": [dict(item) for item in self.leases],
        }


class RunnerLeaseStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def create(
        self,
        *,
        runtime_generation: str,
        run_id: str,
        phase: str,
        task_id: Optional[str],
        proposal_id: Optional[str],
        process_hint: Optional[str],
        pid: Optional[int],
        checkpoint: Optional[Dict[str, Any]] = None,
        lease_ttl_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> RunnerLeaseRecord:
        moment = _now(now)
        lease = RunnerLeaseRecord(
            runner_id=self._new_runner_id(runtime_generation=runtime_generation, run_id=run_id, phase=phase),
            runtime_generation=str(runtime_generation),
            process_hint=process_hint,
            pid=pid,
            run_id=str(run_id),
            task_id=task_id,
            proposal_id=proposal_id,
            phase=str(phase),
            started_at=_iso(moment),
            heartbeat_at=_iso(moment),
            lease_expires_at=_iso(moment + timedelta(seconds=max(1, int(lease_ttl_seconds)))),
            status=LEASE_STATUS_ACTIVE,
            checkpoint=dict(checkpoint or {}),
        )
        payload = self._load_payload()
        leases = payload["leases"]
        leases = [entry for entry in leases if entry.get("runner_id") != lease.runner_id]
        leases.append(lease.to_dict())
        payload["leases"] = leases
        payload["updated_at"] = _iso(moment)
        self._write_payload(payload)
        return lease

    def heartbeat(
        self,
        runner_id: str,
        *,
        checkpoint: Optional[Dict[str, Any]] = None,
        lease_ttl_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> Optional[RunnerLeaseRecord]:
        return self.update(
            runner_id,
            checkpoint=checkpoint,
            lease_ttl_seconds=lease_ttl_seconds,
            now=now,
        )

    def update(
        self,
        runner_id: str,
        *,
        checkpoint: Optional[Dict[str, Any]] = None,
        lease_ttl_seconds: int = 300,
        now: Optional[datetime] = None,
    ) -> Optional[RunnerLeaseRecord]:
        moment = _now(now)
        payload = self._load_payload()
        leases = payload["leases"]
        updated: Optional[RunnerLeaseRecord] = None
        for index, raw in enumerate(leases):
            if str(raw.get("runner_id", "")) != runner_id:
                continue
            record = RunnerLeaseRecord.from_dict(raw)
            if checkpoint is not None:
                record.checkpoint = dict(checkpoint)
            record.heartbeat_at = _iso(moment)
            if record.status == LEASE_STATUS_ACTIVE:
                record.lease_expires_at = _iso(moment + timedelta(seconds=max(1, int(lease_ttl_seconds))))
            leases[index] = record.to_dict()
            updated = record
            break
        if updated is None:
            return None
        payload["updated_at"] = _iso(moment)
        self._write_payload(payload)
        return updated

    def complete(
        self,
        runner_id: str,
        *,
        checkpoint: Optional[Dict[str, Any]] = None,
        now: Optional[datetime] = None,
    ) -> Optional[RunnerLeaseRecord]:
        return self._set_terminal(
            runner_id,
            status=LEASE_STATUS_COMPLETED,
            checkpoint=checkpoint,
            error=None,
            now=now,
        )

    def fail(
        self,
        runner_id: str,
        *,
        checkpoint: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Optional[RunnerLeaseRecord]:
        return self._set_terminal(
            runner_id,
            status=LEASE_STATUS_FAILED,
            checkpoint=checkpoint,
            error=error,
            now=now,
        )

    def expire(self, *, now: Optional[datetime] = None) -> List[str]:
        moment = _now(now)
        payload = self._load_payload()
        leases = payload["leases"]
        expired_ids: List[str] = []
        changed = False
        for index, raw in enumerate(leases):
            record = RunnerLeaseRecord.from_dict(raw)
            if record.status != LEASE_STATUS_ACTIVE:
                continue
            lease_expires_at = _parse_iso(record.lease_expires_at)
            if lease_expires_at is None or lease_expires_at >= moment:
                continue
            record.status = LEASE_STATUS_EXPIRED
            record.completed_at = _iso(moment)
            record.heartbeat_at = _iso(moment)
            leases[index] = record.to_dict()
            expired_ids.append(record.runner_id)
            changed = True
        if changed:
            payload["updated_at"] = _iso(moment)
            self._write_payload(payload)
        return expired_ids

    def read(self, *, now: Optional[datetime] = None) -> RunnerLeaseReadResult:
        payload = self._load_payload()
        moment = _now(now)
        leases: List[Dict[str, Any]] = []
        summary = {
            "total": 0,
            "active": 0,
            "stale": 0,
            "completed": 0,
            "failed": 0,
            "expired": 0,
        }
        for raw in payload["leases"]:
            record = RunnerLeaseRecord.from_dict(raw)
            effective_status = record.status
            if record.status == LEASE_STATUS_ACTIVE:
                expires_at = _parse_iso(record.lease_expires_at)
                if expires_at is not None and expires_at < moment:
                    effective_status = "stale"
                    summary["stale"] += 1
                else:
                    summary["active"] += 1
            elif record.status in summary:
                summary[record.status] += 1
            else:
                summary["failed"] += 1
            lease = record.to_dict()
            lease["effective_status"] = effective_status
            leases.append(lease)

        leases.sort(
            key=lambda item: (
                str(item.get("heartbeat_at") or ""),
                str(item.get("runner_id") or ""),
            ),
            reverse=True,
        )
        summary["total"] = len(leases)
        summary["has_stale"] = summary["stale"] > 0
        summary["has_active"] = summary["active"] > 0
        return RunnerLeaseReadResult(
            schema_version=int(payload.get("schema_version", 1)),
            updated_at=payload.get("updated_at"),
            leases=leases,
            summary=summary,
        )

    def _set_terminal(
        self,
        runner_id: str,
        *,
        status: str,
        checkpoint: Optional[Dict[str, Any]],
        error: Optional[str],
        now: Optional[datetime],
    ) -> Optional[RunnerLeaseRecord]:
        moment = _now(now)
        payload = self._load_payload()
        leases = payload["leases"]
        updated: Optional[RunnerLeaseRecord] = None
        for index, raw in enumerate(leases):
            if str(raw.get("runner_id", "")) != runner_id:
                continue
            record = RunnerLeaseRecord.from_dict(raw)
            if checkpoint is not None:
                record.checkpoint = dict(checkpoint)
            record.status = status
            record.heartbeat_at = _iso(moment)
            record.completed_at = _iso(moment)
            if error:
                record.error = str(error)
            leases[index] = record.to_dict()
            updated = record
            break
        if updated is None:
            return None
        payload["updated_at"] = _iso(moment)
        self._write_payload(payload)
        return updated

    def _new_runner_id(self, *, runtime_generation: str, run_id: str, phase: str) -> str:
        suffix = uuid.uuid4().hex[:8]
        return f"{runtime_generation}:{run_id}:{phase}:{suffix}"

    def _load_payload(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "updated_at": None, "leases": []}
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError:
            return {"schema_version": 1, "updated_at": None, "leases": []}
        if not raw_text.strip():
            return {"schema_version": 1, "updated_at": None, "leases": []}
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError:
            return {"schema_version": 1, "updated_at": None, "leases": []}
        if not isinstance(payload, dict):
            return {"schema_version": 1, "updated_at": None, "leases": []}
        leases_raw = payload.get("leases")
        leases = [dict(item) for item in leases_raw if isinstance(item, dict)] if isinstance(leases_raw, list) else []
        schema_version = payload.get("schema_version")
        schema = int(schema_version) if isinstance(schema_version, int) else 1
        updated_at = payload.get("updated_at") if isinstance(payload.get("updated_at"), str) else None
        return {
            "schema_version": schema,
            "updated_at": updated_at,
            "leases": leases,
        }

    def _write_payload(self, payload: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
            file_obj.write("\n")
        tmp_path.replace(self.path)
