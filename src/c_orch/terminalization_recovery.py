from __future__ import annotations

from typing import Any, Dict, List, Optional

from .states import RUN_APPROVED, RUN_REVIEWING, RUN_WORK_DONE


WAITING_ACCEPTED_TERMINALIZATION_RECOVERY = "accepted_terminalization_recovery"
WAITING_MANUAL_TERMINALIZATION_RECOVERY = "manual_terminalization_recovery"
EVENT_TERMINALIZATION_RECOVERY_REQUIRED = "accepted_terminalization_recovery_required"

_COMMIT_SUCCESS_BOUNDARY_EVENTS = frozenset({"git_commit_completed", "git_commit_skipped"})
_TERMINALIZATION_PROGRESS_EVENTS = frozenset(
    {
        "apply_completed",
        "git_commit_completed",
        "git_commit_skipped",
        "git_commit_failed",
        "run_terminal_status",
    }
)


def accepted_review_needs_terminalization(manifest: Any, events: List[Dict[str, Any]]) -> bool:
    if _review_decision(manifest) != "accepted":
        return False
    if _status(manifest) not in {RUN_REVIEWING, RUN_WORK_DONE, RUN_APPROVED}:
        return False
    return not (
        _has_successful_apply_completed_event(events)
        and _has_commit_success_boundary_event(events)
        and _has_event(events, "run_terminal_status")
    )


def accepted_review_terminalization_requires_manual_recovery(
    manifest: Any,
    events: List[Dict[str, Any]],
) -> bool:
    if not accepted_review_needs_terminalization(manifest, events):
        return False
    index = _latest_event_index(events, EVENT_TERMINALIZATION_RECOVERY_REQUIRED)
    if index < 0:
        return False
    for event in events[index + 1 :]:
        if not isinstance(event, dict):
            continue
        if str(event.get("type") or "") in _TERMINALIZATION_PROGRESS_EVENTS:
            return False
    return True


def accepted_review_terminalization_waiting_for(
    manifest: Any,
    events: List[Dict[str, Any]],
) -> Optional[str]:
    if not accepted_review_needs_terminalization(manifest, events):
        return None
    if accepted_review_terminalization_requires_manual_recovery(manifest, events):
        return WAITING_MANUAL_TERMINALIZATION_RECOVERY
    return WAITING_ACCEPTED_TERMINALIZATION_RECOVERY


def latest_terminalization_recovery_required_event(
    events: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == EVENT_TERMINALIZATION_RECOVERY_REQUIRED:
            return event
    return None


def _has_event(events: List[Dict[str, Any]], event_type: str) -> bool:
    return any(isinstance(event, dict) and event.get("type") == event_type for event in events)


def _has_successful_apply_completed_event(events: List[Dict[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "apply_completed":
            continue
        if event.get("applied") is True:
            return True
    return False


def _has_commit_success_boundary_event(events: List[Dict[str, Any]]) -> bool:
    return any(
        isinstance(event, dict) and str(event.get("type") or "") in _COMMIT_SUCCESS_BOUNDARY_EVENTS
        for event in events
    )


def _latest_event_index(events: List[Dict[str, Any]], event_type: str) -> int:
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        if isinstance(event, dict) and event.get("type") == event_type:
            return index
    return -1


def _status(manifest: Any) -> str:
    return str(getattr(manifest, "status", ""))


def _review_decision(manifest: Any) -> Optional[str]:
    review = getattr(manifest, "review", None)
    if review is None:
        return None
    return getattr(review, "decision", None)
