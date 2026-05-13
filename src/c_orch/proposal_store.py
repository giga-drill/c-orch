from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


Pathish = Union[str, Path]

PROPOSAL_PLANNING = "PLANNING"
PROPOSAL_PLAN_REVIEW_REQUIRED = "PLAN_REVIEW_REQUIRED"
PROPOSAL_PLAN_REVISING = "PLAN_REVISING"
PROPOSAL_APPROVED = "APPROVED"
PROPOSAL_QUEUED = "QUEUED"
PROPOSAL_FAILED = "FAILED"


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _as_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} cannot be empty")
    return text


@dataclass
class ProposalRecord:
    proposal_id: str
    title: str
    prompt: str
    status: str = PROPOSAL_PLANNING
    run_id: Optional[str] = None
    task_id: Optional[str] = None
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    error: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "title": self.title,
            "prompt": self.prompt,
            "status": self.status,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProposalRecord":
        now = _now_iso()
        return cls(
            proposal_id=str(data.get("proposal_id") or ""),
            title=str(data.get("title") or ""),
            prompt=str(data.get("prompt") or ""),
            status=str(data.get("status") or PROPOSAL_PLANNING),
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
            error=data.get("error"),
            reason=data.get("reason"),
        )


@dataclass
class ProposalPool:
    pool_id: str = "default"
    proposals: List[ProposalRecord] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "proposals": [proposal.to_dict() for proposal in self.proposals],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProposalPool":
        now = _now_iso()
        proposals_raw = data.get("proposals")
        proposals = [
            ProposalRecord.from_dict(item)
            for item in proposals_raw
            if isinstance(item, dict)
        ] if isinstance(proposals_raw, list) else []
        return cls(
            pool_id=str(data.get("pool_id") or "default"),
            proposals=proposals,
            created_at=str(data.get("created_at", now)),
            updated_at=str(data.get("updated_at", now)),
        )


class ProposalStore:
    def __init__(self, proposals_path: Pathish) -> None:
        self.proposals_path = Path(proposals_path)

    def create(self, *, pool_id: str = "default") -> ProposalPool:
        pool = ProposalPool(pool_id=pool_id)
        self.save(pool, touch=False)
        return pool

    def load(self) -> ProposalPool:
        with self.proposals_path.open("r", encoding="utf-8") as file_obj:
            data = json.load(file_obj)
        if not isinstance(data, dict):
            raise ValueError("proposals file must contain a JSON object")
        pool = ProposalPool.from_dict(data)
        self.validate(pool)
        return pool

    def load_or_create(self) -> ProposalPool:
        try:
            return self.load()
        except OSError:
            return self.create()

    def save(self, pool: ProposalPool, *, touch: bool = True) -> Path:
        if touch:
            now = _now_iso()
            pool.updated_at = now
            for proposal in pool.proposals:
                if not proposal.updated_at:
                    proposal.updated_at = now
                if not proposal.created_at:
                    proposal.created_at = now
        if not pool.created_at:
            pool.created_at = pool.updated_at or _now_iso()
        self.validate(pool)
        self.proposals_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.proposals_path.parent,
            prefix=f".{self.proposals_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file_obj:
            json.dump(pool.to_dict(), file_obj, ensure_ascii=False, indent=2)
            file_obj.write("\n")
            tmp_path = Path(file_obj.name)
        tmp_path.replace(self.proposals_path)
        return self.proposals_path

    def add_proposal(self, pool: ProposalPool, *, title: str, prompt: str) -> ProposalRecord:
        now = _now_iso()
        proposal = ProposalRecord(
            proposal_id=self._new_proposal_id(pool, title=title),
            title=title,
            prompt=prompt,
            created_at=now,
            updated_at=now,
        )
        pool.proposals.append(proposal)
        pool.updated_at = now
        return proposal

    def update_proposal(self, pool: ProposalPool, proposal_id: str, **fields: Any) -> ProposalRecord:
        proposal = self.find(pool, proposal_id)
        for key, value in fields.items():
            if not hasattr(proposal, key):
                raise ValueError(f"unknown proposal field: {key}")
            setattr(proposal, key, value)
        proposal.updated_at = _now_iso()
        pool.updated_at = proposal.updated_at
        return proposal

    def remove_proposal(self, pool: ProposalPool, proposal_id: str) -> ProposalRecord:
        for index, proposal in enumerate(pool.proposals):
            if proposal.proposal_id == proposal_id:
                removed = pool.proposals.pop(index)
                pool.updated_at = _now_iso()
                return removed
        raise ValueError(f"proposal not found: {proposal_id}")

    def find(self, pool: ProposalPool, proposal_id: str) -> ProposalRecord:
        for proposal in pool.proposals:
            if proposal.proposal_id == proposal_id:
                return proposal
        raise ValueError(f"proposal not found: {proposal_id}")

    def validate(self, pool: ProposalPool) -> None:
        if not pool.pool_id:
            raise ValueError("pool_id cannot be empty")
        seen = set()
        for index, proposal in enumerate(pool.proposals):
            proposal_id = _as_string(
                proposal.proposal_id,
                field_name=f"proposals[{index}].proposal_id",
            )
            if proposal_id in seen:
                raise ValueError(f"duplicate proposal_id: {proposal_id}")
            seen.add(proposal_id)
            proposal.proposal_id = proposal_id
            proposal.title = _as_string(proposal.title, field_name=f"proposals[{index}].title")
            proposal.prompt = _as_string(proposal.prompt, field_name=f"proposals[{index}].prompt")
            if not proposal.status:
                proposal.status = PROPOSAL_PLANNING

    def _new_proposal_id(self, pool: ProposalPool, *, title: str) -> str:
        base = _slug(title) or "proposal"
        used = {proposal.proposal_id for proposal in pool.proposals}
        candidate = base
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{base}-{suffix:02d}"
        return candidate


def _slug(value: str) -> str:
    chars: List[str] = []
    previous_dash = False
    for raw_char in value.lower().strip():
        if raw_char.isalnum():
            chars.append(raw_char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
        if len(chars) >= 48:
            break
    return "".join(chars).strip("-")
