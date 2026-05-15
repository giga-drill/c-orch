from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .states import REVIEW_ATTEMPT_FAILED_RETRYABLE, RUN_FAILED, RUN_WORK_DONE


RECOVERY_RETRY_LATER = "retry_later"
RECOVERY_START_REPLACEMENT_AGENT = "start_replacement_agent"


@dataclass(frozen=True)
class FailurePolicyDecision:
    error: str
    retryable: bool
    recovery_action: str
    reason: str


def classify_operation_failure(error: BaseException) -> FailurePolicyDecision:
    text = str(error)
    if _contains_any(text, ("Session not found for thread_id",)):
        return FailurePolicyDecision(
            error=text,
            retryable=True,
            recovery_action=RECOVERY_START_REPLACEMENT_AGENT,
            reason="agent_context_lost",
        )
    if _contains_any(text, ("Broken pipe", "Timed out waiting for MCP server output")):
        return FailurePolicyDecision(
            error=text,
            retryable=True,
            recovery_action=RECOVERY_RETRY_LATER,
            reason="transient_infrastructure_error",
        )
    return FailurePolicyDecision(
        error=text,
        retryable=True,
        recovery_action=RECOVERY_RETRY_LATER,
        reason="unclassified_operation_error",
    )


def should_start_replacement_agent(error: BaseException) -> bool:
    decision = classify_operation_failure(error)
    return decision.recovery_action == RECOVERY_START_REPLACEMENT_AGENT


def has_retryable_review_failure(manifest: Any) -> bool:
    attempts = getattr(manifest, "review_attempts", [])
    return (
        getattr(manifest, "status", None) == RUN_WORK_DONE
        and bool(getattr(manifest, "review", None))
        and bool(attempts)
        and getattr(attempts[-1], "status", None) == REVIEW_ATTEMPT_FAILED_RETRYABLE
    )


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


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)
