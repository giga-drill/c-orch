from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional


class ContractError(ValueError):
    """Raised when an agent response does not satisfy the expected contract."""


def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract the first JSON object from a raw model response."""
    decoder = json.JSONDecoder()
    stripped = _strip_fenced_json(text.strip())
    if stripped.startswith("{"):
        try:
            value, _ = decoder.raw_decode(stripped)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass

    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ContractError("response did not contain a JSON object")


def require_fields(payload: Mapping[str, Any], required: Iterable[str]) -> None:
    missing = [field for field in required if field not in payload]
    if missing:
        raise ContractError(f"missing required fields: {', '.join(missing)}")


def require_list(payload: Mapping[str, Any], field: str) -> List[Any]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise ContractError(f"{field} must be a list")
    return value


@dataclass(frozen=True)
class PlannerPlan:
    status: str
    summary: str
    acceptance_criteria: List[str]
    worker_prompt: str
    verification_commands: List[str]
    risk_notes: List[str]
    raw: Dict[str, Any]

    @classmethod
    def parse(cls, text: str) -> "PlannerPlan":
        payload = extract_json_object(text)
        require_fields(
            payload,
            ["status", "summary", "acceptance_criteria", "worker_prompt", "verification_commands"],
        )
        status = _expect_string(payload, "status")
        if status != "plan_ready":
            raise ContractError("planner status must be plan_ready")
        return cls(
            status=status,
            summary=_expect_string(payload, "summary"),
            acceptance_criteria=_string_list(payload, "acceptance_criteria"),
            worker_prompt=_expect_string(payload, "worker_prompt"),
            verification_commands=_string_list(payload, "verification_commands"),
            risk_notes=_string_list(payload, "risk_notes", required=False),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class WorkerResult:
    status: str
    summary: str
    changed_files: List[str]
    verification: List[Dict[str, Any]]
    blockers: List[str]
    raw: Dict[str, Any]

    @classmethod
    def parse(cls, text: str) -> "WorkerResult":
        payload = extract_json_object(text)
        require_fields(payload, ["status", "summary", "changed_files", "verification", "blockers"])
        status = _expect_string(payload, "status")
        if status not in {"work_done", "blocked", "failed"}:
            raise ContractError("worker status must be work_done, blocked, or failed")
        verification = require_list(payload, "verification")
        if not all(isinstance(item, dict) for item in verification):
            raise ContractError("verification entries must be objects")
        return cls(
            status=status,
            summary=_expect_string(payload, "summary"),
            changed_files=_string_list(payload, "changed_files"),
            verification=[dict(item) for item in verification],
            blockers=_string_list(payload, "blockers"),
            raw=dict(payload),
        )


@dataclass(frozen=True)
class ReviewDecision:
    decision: str
    reason: str
    next_worker_prompt: Optional[str]
    raw: Dict[str, Any]

    @classmethod
    def parse(cls, text: str) -> "ReviewDecision":
        payload = extract_json_object(text)
        require_fields(payload, ["decision", "reason", "next_worker_prompt"])
        decision = _expect_string(payload, "decision")
        if decision not in {"accepted", "revision_requested"}:
            raise ContractError("review decision must be accepted or revision_requested")
        next_prompt = payload.get("next_worker_prompt")
        if next_prompt is not None and not isinstance(next_prompt, str):
            raise ContractError("next_worker_prompt must be a string or null")
        if decision == "accepted" and next_prompt is not None:
            raise ContractError("accepted requires next_worker_prompt to be null")
        if decision == "revision_requested" and not next_prompt:
            raise ContractError("revision_requested requires next_worker_prompt")
        return cls(
            decision=decision,
            reason=_expect_string(payload, "reason"),
            next_worker_prompt=next_prompt,
            raw=dict(payload),
        )


def _strip_fenced_json(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _expect_string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field} must be a non-empty string")
    return value


def _string_list(payload: Mapping[str, Any], field: str, required: bool = True) -> List[str]:
    if not required and field not in payload:
        return []
    values = require_list(payload, field)
    if not all(isinstance(item, str) for item in values):
        raise ContractError(f"{field} must be a list of strings")
    return list(values)
