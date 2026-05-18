from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
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
SOURCE_RUNTIME_STARTUP = "runtime_startup"

DEFAULT_RETRY_BUDGET = 3
DEFAULT_RETRY_BACKOFF_SECONDS = (5, 30, 120)


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
    retry_attempt: Optional[int] = None
    retry_budget: Optional[int] = None
    next_retry_at: Optional[str] = None
    backoff_reason: Optional[str] = None
    budget_exhausted: Optional[bool] = None

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
            "retry_attempt": self.retry_attempt,
            "retry_budget": self.retry_budget,
            "next_retry_at": self.next_retry_at,
            "backoff_reason": self.backoff_reason,
            "budget_exhausted": self.budget_exhausted,
        }


@dataclass(frozen=True)
class RetryBackoffState:
    retry_attempt: int
    retry_budget: int
    next_retry_at: Optional[str]
    backoff_reason: str
    budget_exhausted: bool
    ready: bool


@dataclass(frozen=True)
class RetryBackoffDecision:
    retry_attempt: int
    retry_budget: int
    next_retry_at: Optional[str]
    backoff_reason: str
    budget_exhausted: bool
    ready: bool


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
    now: Optional[datetime] = None,
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
    retry_state = classify_retry_backoff_decision(
        manifest,
        now=now,
        retry_budget=DEFAULT_RETRY_BUDGET,
        backoff_schedule_seconds=DEFAULT_RETRY_BACKOFF_SECONDS,
    )
    if retry_state is None:
        return None
    automatic = source == SOURCE_QUEUE_SCHEDULER and retry_state.ready and not retry_state.budget_exhausted
    requires_human = source != SOURCE_QUEUE_SCHEDULER or retry_state.budget_exhausted or not retry_state.ready
    return _decision(
        phase=PHASE_PLANNER_REVIEW,
        category=category,
        reason=reason,
        recovery_action=RECOVERY_RETRY_REVIEW,
        retryable=True,
        automatic=automatic,
        requires_human=requires_human,
        source=source,
        attempt=attempt,
        error=error or None,
        retry_attempt=retry_state.retry_attempt,
        retry_budget=retry_state.retry_budget,
        next_retry_at=retry_state.next_retry_at,
        backoff_reason=retry_state.backoff_reason,
        budget_exhausted=retry_state.budget_exhausted,
    )


def classify_retry_backoff_decision(
    manifest: Any,
    *,
    now: Optional[datetime] = None,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
    backoff_schedule_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Optional[RetryBackoffDecision]:
    state = derive_retry_backoff_state(
        manifest,
        now=now,
        retry_budget=retry_budget,
        backoff_schedule_seconds=backoff_schedule_seconds,
    )
    if state is None:
        return None
    return RetryBackoffDecision(
        retry_attempt=state.retry_attempt,
        retry_budget=state.retry_budget,
        next_retry_at=state.next_retry_at,
        backoff_reason=state.backoff_reason,
        budget_exhausted=state.budget_exhausted,
        ready=state.ready,
    )


def derive_retry_backoff_state(
    manifest: Any,
    *,
    now: Optional[datetime] = None,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
    backoff_schedule_seconds: tuple[int, ...] = DEFAULT_RETRY_BACKOFF_SECONDS,
) -> Optional[RetryBackoffState]:
    if not _has_retryable_review_failure(manifest):
        return None
    attempts = list(getattr(manifest, "review_attempts", []) or [])
    last_attempt = attempts[-1] if attempts else None
    if last_attempt is None:
        return None
    persisted_retry_attempt = getattr(last_attempt, "retry_attempt", None)
    persisted_retry_budget = getattr(last_attempt, "retry_budget", None)
    persisted_next_retry_at = getattr(last_attempt, "next_retry_at", None)
    persisted_backoff_reason = getattr(last_attempt, "backoff_reason", None)
    persisted_budget_exhausted = getattr(last_attempt, "retry_budget_exhausted", None)
    has_persisted_retry_state = any(
        value is not None
        for value in (
            persisted_retry_attempt,
            persisted_retry_budget,
            persisted_next_retry_at,
            persisted_backoff_reason,
            persisted_budget_exhausted,
        )
    )
    normalized_budget = _normalize_retry_budget(getattr(last_attempt, "retry_budget", None), retry_budget)
    retry_attempt = _normalize_retry_attempt(getattr(last_attempt, "retry_attempt", None))
    if retry_attempt is None:
        retry_attempt = _retryable_review_failure_count(attempts)
    if retry_attempt < 1:
        retry_attempt = 1
    persisted_exhausted = bool(getattr(last_attempt, "retry_budget_exhausted", False))
    budget_exhausted = persisted_exhausted or retry_attempt >= normalized_budget
    next_retry_at = _string_or_none(getattr(last_attempt, "next_retry_at", None))
    backoff_reason = _string_or_none(getattr(last_attempt, "backoff_reason", None))
    if not has_persisted_retry_state:
        return RetryBackoffState(
            retry_attempt=retry_attempt,
            retry_budget=normalized_budget,
            next_retry_at=None,
            backoff_reason=backoff_reason or "legacy_retry_immediate",
            budget_exhausted=False,
            ready=True,
        )
    if not budget_exhausted and not next_retry_at:
        base = _parse_iso_datetime(_string_or_none(getattr(last_attempt, "completed_at", None)))
        delay_seconds = _backoff_delay_seconds(retry_attempt, backoff_schedule_seconds)
        if base is not None:
            next_retry_at = (base + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
        backoff_reason = backoff_reason or f"retry_backoff_{delay_seconds}s"
    if budget_exhausted:
        backoff_reason = backoff_reason or "retry_budget_exhausted"
        return RetryBackoffState(
            retry_attempt=retry_attempt,
            retry_budget=normalized_budget,
            next_retry_at=next_retry_at,
            backoff_reason=backoff_reason,
            budget_exhausted=True,
            ready=False,
        )
    current_time = now or datetime.now().astimezone()
    next_retry_dt = _parse_iso_datetime(next_retry_at)
    ready = next_retry_dt is None or current_time >= next_retry_dt
    return RetryBackoffState(
        retry_attempt=retry_attempt,
        retry_budget=normalized_budget,
        next_retry_at=next_retry_at,
        backoff_reason=backoff_reason or "retry_backoff_waiting",
        budget_exhausted=False,
        ready=ready,
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
    retry_attempt: Optional[int] = None,
    retry_budget: Optional[int] = None,
    next_retry_at: Optional[str] = None,
    backoff_reason: Optional[str] = None,
    budget_exhausted: Optional[bool] = None,
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
        retry_attempt=retry_attempt,
        retry_budget=retry_budget,
        next_retry_at=next_retry_at,
        backoff_reason=backoff_reason,
        budget_exhausted=budget_exhausted,
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


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def _normalize_retry_budget(value: Any, default_value: int) -> int:
    candidate = value
    if not isinstance(candidate, int):
        candidate = default_value
    if candidate < 1:
        return 1
    return candidate


def _normalize_retry_attempt(value: Any) -> Optional[int]:
    if not isinstance(value, int):
        return None
    if value < 1:
        return None
    return value


def _retryable_review_failure_count(attempts: list[Any]) -> int:
    total = 0
    for attempt in attempts:
        if getattr(attempt, "status", None) == REVIEW_ATTEMPT_FAILED_RETRYABLE:
            total += 1
    return total


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _backoff_delay_seconds(retry_attempt: int, schedule: tuple[int, ...]) -> int:
    if retry_attempt <= 0:
        return schedule[0] if schedule else 0
    if not schedule:
        return 0
    index = min(retry_attempt - 1, len(schedule) - 1)
    return schedule[index]
