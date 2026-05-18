from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Union

from .states import REVIEW_ATTEMPT_FAILED_RETRYABLE, RUN_FAILED, RUN_WORK_DONE


PHASE_AGENT_REPLY = "agent_reply"
PHASE_WORKER_REPLY = "worker_reply"
PHASE_PLANNER_REVIEW = "planner_review"
PHASE_VERIFICATION = "verification"
PHASE_APPLY = "apply"
PHASE_COMMIT = "commit"
PHASE_TASK_RETRY = "task_retry"

CATEGORY_TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT = "mcp_session_lost_or_timeout"
CATEGORY_VERIFICATION_FAILURE = "verification_failure"
CATEGORY_CODE_REVIEW_FINDINGS = "code_review_findings"
CATEGORY_APPLY_CONFLICT = "apply_conflict"
CATEGORY_GIT_COMMIT_FAILURE = "git_commit_failure"
CATEGORY_MAX_ATTEMPTS_EXCEEDED = "max_attempts_exceeded"

RECOVERY_RETRY_LATER = "retry_later"
RECOVERY_START_REPLACEMENT_AGENT = "start_replacement_agent"
RECOVERY_RESUME_AGENT_SESSION = "resume_agent_session"
RECOVERY_RETRY_REVIEW = "retry_review"
RECOVERY_RETRY_VERIFICATION = "retry_verification"
RECOVERY_WORKER_REWORK = "worker_rework"
RECOVERY_MANUAL_OVERRIDE = "manual_override"
RECOVERY_FAIL_WITH_EVIDENCE = "fail_with_evidence"

SOURCE_ORCHESTRATOR = "orchestrator"
SOURCE_QUEUE_SCHEDULER = "queue_scheduler"
SOURCE_DASHBOARD_ACTION = "dashboard_action"


@dataclass(frozen=True)
class FailurePolicyDecision:
    phase: str
    category: str
    reason: str
    recovery_action: str
    retryable: bool
    automatic: bool
    requires_human: bool
    source: str
    attempt: Union[int, str] = "unknown"
    error: Optional[str] = None

    def to_event_fields(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "category": self.category,
            "reason": self.reason,
            "recovery_action": self.recovery_action,
            "source": self.source,
            "attempt": self.attempt,
            "retryable": self.retryable,
            "automatic": self.automatic,
            "requires_human": self.requires_human,
            "error": self.error,
        }


def classify_operation_failure(
    error: BaseException,
    *,
    phase: str = PHASE_AGENT_REPLY,
    source: str = SOURCE_ORCHESTRATOR,
    attempt: Optional[int] = None,
) -> FailurePolicyDecision:
    text = str(error)
    if _contains_any(
        text,
        (
            "Session not found for thread_id",
            "Session not found",
            "session missing",
            "timed out while waiting for session",
        ),
    ):
        return _decision(
            phase=phase,
            category=CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT,
            reason="session_lost_or_timeout",
            recovery_action=RECOVERY_START_REPLACEMENT_AGENT,
            retryable=True,
            automatic=True,
            requires_human=False,
            source=source,
            attempt=attempt,
            error=text,
        )
    if _contains_any(
        text,
        (
            "Broken pipe",
            "Timed out waiting for MCP server output",
            "timeout waiting for response",
            "transport error",
            "connection reset by peer",
            "ECONNRESET",
        ),
    ):
        return _decision(
            phase=phase,
            category=CATEGORY_TRANSIENT_INFRASTRUCTURE,
            reason="transient_infrastructure_error",
            recovery_action=RECOVERY_RETRY_LATER,
            retryable=True,
            automatic=True,
            requires_human=False,
            source=source,
            attempt=attempt,
            error=text,
        )
    return _decision(
        phase=phase,
        category=CATEGORY_TRANSIENT_INFRASTRUCTURE,
        reason="unclassified_operation_error",
        recovery_action=RECOVERY_RETRY_LATER,
        retryable=True,
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
        error=text,
    )


def should_start_replacement_agent(error: BaseException) -> bool:
    decision = classify_operation_failure(error)
    return decision.recovery_action == RECOVERY_START_REPLACEMENT_AGENT


def classify_session_recovery_result(
    result: Any,
    *,
    phase: str = PHASE_PLANNER_REVIEW,
    source: str = SOURCE_ORCHESTRATOR,
    attempt: Optional[int] = None,
) -> Optional[FailurePolicyDecision]:
    raw = getattr(result, "raw", None)
    if not isinstance(raw, dict):
        return None
    reason: Optional[str] = None
    if raw.get("resumedAfterMcpTimeout") is True:
        reason = "resume_after_mcp_timeout"
    elif raw.get("resumedWithCodexExec") is True:
        reason = "resume_with_codex_exec"
    elif raw.get("recoveredFromSessionLog"):
        reason = "resume_from_session_log"
    if reason is None:
        return None
    return _decision(
        phase=phase,
        category=CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT,
        reason=reason,
        recovery_action=RECOVERY_RESUME_AGENT_SESSION,
        retryable=True,
        automatic=True,
        requires_human=False,
        source=source,
        attempt=attempt,
    )


def classify_retryable_review_failure(
    manifest: Any,
    *,
    source: str,
    attempt: Optional[int] = None,
) -> Optional[FailurePolicyDecision]:
    if not _has_retryable_review_failure(manifest):
        return None
    attempts = getattr(manifest, "review_attempts", [])
    last_attempt = attempts[-1] if attempts else None
    error = str(getattr(last_attempt, "error", "") or "")
    reason = str(getattr(last_attempt, "reason", "") or "retryable_review_failure")
    category = CATEGORY_TRANSIENT_INFRASTRUCTURE
    if error:
        category = classify_operation_failure(
            RuntimeError(error),
            phase=PHASE_PLANNER_REVIEW,
            source=source,
            attempt=attempt,
        ).category
    automatic = source == SOURCE_QUEUE_SCHEDULER
    return _decision(
        phase=PHASE_PLANNER_REVIEW,
        category=category,
        reason=reason,
        recovery_action=RECOVERY_RETRY_REVIEW,
        retryable=True,
        automatic=automatic,
        requires_human=not automatic,
        source=source,
        attempt=attempt,
        error=error or None,
    )


def classify_verification_gate_failure(
    *,
    source: str,
    attempt: Optional[int] = None,
    error: Optional[str] = None,
) -> FailurePolicyDecision:
    return _decision(
        phase=PHASE_VERIFICATION,
        category=CATEGORY_VERIFICATION_FAILURE,
        reason="verification_gate_failed",
        recovery_action=RECOVERY_MANUAL_OVERRIDE,
        retryable=True,
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
        error=error,
    )


def classify_retry_verification_action(
    manifest: Any,
    events: list[Any],
    *,
    source: str,
    attempt: Optional[int] = None,
) -> Optional[FailurePolicyDecision]:
    if not has_retryable_verification_failure(manifest, events):
        return None
    automatic = source == SOURCE_QUEUE_SCHEDULER
    return _decision(
        phase=PHASE_VERIFICATION,
        category=CATEGORY_VERIFICATION_FAILURE,
        reason="verification_retry_requested",
        recovery_action=RECOVERY_RETRY_VERIFICATION,
        retryable=True,
        automatic=automatic,
        requires_human=not automatic,
        source=source,
        attempt=attempt,
    )


def classify_code_review_findings(
    *,
    reason: Optional[str],
    source: str,
    attempt: Optional[int] = None,
) -> FailurePolicyDecision:
    return _decision(
        phase=PHASE_PLANNER_REVIEW,
        category=CATEGORY_CODE_REVIEW_FINDINGS,
        reason=(reason or "revision_requested"),
        recovery_action=RECOVERY_WORKER_REWORK,
        retryable=True,
        automatic=True,
        requires_human=False,
        source=source,
        attempt=attempt,
    )


def classify_apply_failure(
    *,
    source: str,
    attempt: Optional[int] = None,
    error: Optional[str] = None,
) -> FailurePolicyDecision:
    return _decision(
        phase=PHASE_APPLY,
        category=CATEGORY_APPLY_CONFLICT,
        reason="apply_failed",
        recovery_action=RECOVERY_FAIL_WITH_EVIDENCE,
        retryable=False,
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
        error=error,
    )


def classify_git_commit_failure(
    *,
    failure_reason: Optional[str],
    summary: Optional[str],
    source: str,
    attempt: Optional[int] = None,
) -> FailurePolicyDecision:
    normalized_reason = _classify_git_commit_reason(failure_reason, summary)
    recovery_action = RECOVERY_MANUAL_OVERRIDE
    if normalized_reason == "generic_git_error":
        recovery_action = RECOVERY_FAIL_WITH_EVIDENCE
    return _decision(
        phase=PHASE_COMMIT,
        category=CATEGORY_GIT_COMMIT_FAILURE,
        reason=normalized_reason,
        recovery_action=recovery_action,
        retryable=normalized_reason == "no_changes",
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
        error=summary,
    )


def classify_max_attempts_exceeded(
    *,
    source: str,
    attempt: Optional[int] = None,
) -> FailurePolicyDecision:
    return _decision(
        phase=PHASE_PLANNER_REVIEW,
        category=CATEGORY_MAX_ATTEMPTS_EXCEEDED,
        reason="max_attempts_exceeded",
        recovery_action=RECOVERY_FAIL_WITH_EVIDENCE,
        retryable=False,
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
    )


def classify_retry_task_decision(
    manifest: Any,
    events: list[Any],
    *,
    source: str,
    attempt: Optional[int] = None,
) -> Optional[FailurePolicyDecision]:
    if getattr(manifest, "status", None) != RUN_FAILED:
        return None
    terminal_reason = _latest_terminal_reason(events)
    if terminal_reason == "max_attempts_reached":
        category = CATEGORY_MAX_ATTEMPTS_EXCEEDED
        reason = "max_attempts_exceeded"
    elif terminal_reason == "verification_failed":
        category = CATEGORY_VERIFICATION_FAILURE
        reason = "verification_failed"
    elif terminal_reason == "apply_failed":
        category = CATEGORY_APPLY_CONFLICT
        reason = "apply_failed"
    elif terminal_reason == "git_commit_failed":
        git_reason = _latest_git_commit_reason(events)
        category = CATEGORY_GIT_COMMIT_FAILURE
        reason = _classify_git_commit_reason(git_reason, None)
    else:
        category = CATEGORY_TRANSIENT_INFRASTRUCTURE
        reason = terminal_reason or "retry_task_requested"
    return _decision(
        phase=PHASE_TASK_RETRY,
        category=category,
        reason=reason,
        recovery_action=RECOVERY_MANUAL_OVERRIDE,
        retryable=True,
        automatic=False,
        requires_human=True,
        source=source,
        attempt=attempt,
    )


def has_retryable_review_failure(manifest: Any) -> bool:
    return _has_retryable_review_failure(manifest)


def has_retryable_review_failure_dict(run: dict[str, Any]) -> bool:
    attempts = run.get("review_attempts")
    review = run.get("review")
    if not isinstance(attempts, list) or not attempts:
        return False
    last_attempt = attempts[-1]
    return (
        run.get("status") == RUN_WORK_DONE
        and isinstance(review, dict)
        and isinstance(last_attempt, dict)
        and last_attempt.get("status") == REVIEW_ATTEMPT_FAILED_RETRYABLE
    )


def has_retryable_verification_failure(manifest: Any, events: list[Any]) -> bool:
    review = getattr(manifest, "review", None)
    return (
        getattr(manifest, "status", None) == RUN_FAILED
        and getattr(review, "decision", None) == "accepted"
        and _has_verification_gate_failed_event(events)
    )


def has_retryable_verification_failure_dict(run: dict[str, Any], events: list[Any]) -> bool:
    review = run.get("review")
    return (
        run.get("status") == RUN_FAILED
        and isinstance(review, dict)
        and review.get("decision") == "accepted"
        and _has_verification_gate_failed_event(events)
    )


def _has_verification_gate_failed_event(events: list[Any]) -> bool:
    return any(
        isinstance(event, dict) and event.get("type") == "verification_gate_failed"
        for event in events
    )


def _decision(
    *,
    phase: str,
    category: str,
    reason: str,
    recovery_action: str,
    retryable: bool,
    automatic: bool,
    requires_human: bool,
    source: str,
    attempt: Optional[int] = None,
    error: Optional[str] = None,
) -> FailurePolicyDecision:
    normalized_attempt: Union[int, str] = "unknown"
    if isinstance(attempt, int) and attempt > 0:
        normalized_attempt = attempt
    return FailurePolicyDecision(
        phase=phase,
        category=category,
        reason=reason,
        recovery_action=recovery_action,
        retryable=retryable,
        automatic=automatic,
        requires_human=requires_human,
        source=source,
        attempt=normalized_attempt,
        error=error,
    )


def _has_retryable_review_failure(manifest: Any) -> bool:
    attempts = getattr(manifest, "review_attempts", [])
    return (
        getattr(manifest, "status", None) == RUN_WORK_DONE
        and bool(getattr(manifest, "review", None))
        and bool(attempts)
        and getattr(attempts[-1], "status", None) == REVIEW_ATTEMPT_FAILED_RETRYABLE
    )


def _classify_git_commit_reason(failure_reason: Optional[str], summary: Optional[str]) -> str:
    reason = str(failure_reason or "").strip()
    text = str(summary or "").lower()
    dirty_reasons = {
        "preexisting_staged_changes",
        "preexisting_overlap",
        "unexpected_paths_after_apply",
        "staged_paths_outside_planned",
        "invalid_review_decision",
    }
    if reason in dirty_reasons:
        return "dirty_repo"
    if "hook" in text or reason == "hook_failed":
        return "hook_failure"
    if reason in {"no_changes", "no_changes_to_commit"} or "nothing to commit" in text:
        return "no_changes"
    return "generic_git_error"


def _latest_terminal_reason(events: list[Any]) -> Optional[str]:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "run_terminal_status":
            value = event.get("reason")
            if isinstance(value, str) and value:
                return value
            return None
    return None


def _latest_git_commit_reason(events: list[Any]) -> Optional[str]:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "git_commit_failed":
            value = event.get("reason")
            if isinstance(value, str) and value:
                return value
            return None
    return None


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
