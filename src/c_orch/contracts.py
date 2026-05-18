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
    decomposition_suggestion: Optional[Dict[str, Any]]
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
            decomposition_suggestion=_decomposition_suggestion(
                payload,
                "decomposition_suggestion",
                required=False,
            ),
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


def _decomposition_suggestion(
    payload: Mapping[str, Any],
    field: str,
    *,
    required: bool,
) -> Optional[Dict[str, Any]]:
    if field not in payload:
        if required:
            raise ContractError(f"missing required field: {field}")
        return None
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ContractError(f"{field} must be an object")
    recommended = value.get("recommended")
    if not isinstance(recommended, bool):
        raise ContractError(f"{field}.recommended must be a boolean")
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ContractError(f"{field}.reason must be a non-empty string")
    subtasks_value = value.get("subtasks")
    if not isinstance(subtasks_value, list):
        raise ContractError(f"{field}.subtasks must be a list")
    if recommended and not subtasks_value:
        raise ContractError(f"{field}.subtasks must be non-empty when recommended is true")
    subtasks: List[Dict[str, Any]] = []
    for index, subtask_value in enumerate(subtasks_value):
        if not isinstance(subtask_value, Mapping):
            raise ContractError(f"{field}.subtasks[{index}] must be an object")
        subtask_id = subtask_value.get("id")
        title = subtask_value.get("title")
        goal = subtask_value.get("goal")
        if not isinstance(subtask_id, str) or not subtask_id.strip():
            raise ContractError(f"{field}.subtasks[{index}].id must be a non-empty string")
        if not isinstance(title, str) or not title.strip():
            raise ContractError(f"{field}.subtasks[{index}].title must be a non-empty string")
        if not isinstance(goal, str) or not goal.strip():
            raise ContractError(f"{field}.subtasks[{index}].goal must be a non-empty string")
        criteria = subtask_value.get("acceptance_criteria")
        if not isinstance(criteria, list) or not all(isinstance(item, str) for item in criteria):
            raise ContractError(
                f"{field}.subtasks[{index}].acceptance_criteria must be a list of strings"
            )
        if not criteria or not all(item.strip() for item in criteria):
            raise ContractError(
                f"{field}.subtasks[{index}].acceptance_criteria must be a non-empty list of non-empty strings"
            )
        subtasks.append(
            {
                "id": subtask_id,
                "title": title,
                "goal": goal,
                "acceptance_criteria": list(criteria),
            }
        )
    return {
        "recommended": recommended,
        "reason": reason,
        "subtasks": subtasks,
    }
