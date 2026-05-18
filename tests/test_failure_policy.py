from __future__ import annotations

import unittest
from types import SimpleNamespace

from c_orch.failure_policy import (
    CATEGORY_APPLY_CONFLICT,
    CATEGORY_CODE_REVIEW_FINDINGS,
    CATEGORY_GIT_COMMIT_FAILURE,
    CATEGORY_MAX_ATTEMPTS_EXCEEDED,
    CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT,
    CATEGORY_TRANSIENT_INFRASTRUCTURE,
    CATEGORY_VERIFICATION_FAILURE,
    RECOVERY_FAIL_WITH_EVIDENCE,
    RECOVERY_MANUAL_OVERRIDE,
    RECOVERY_RETRY_LATER,
    RECOVERY_RETRY_REVIEW,
    RECOVERY_RETRY_VERIFICATION,
    RECOVERY_RESUME_AGENT_SESSION,
    RECOVERY_START_REPLACEMENT_AGENT,
    RECOVERY_WORKER_REWORK,
    SOURCE_DASHBOARD_ACTION,
    SOURCE_ORCHESTRATOR,
    SOURCE_QUEUE_SCHEDULER,
    classify_apply_failure,
    classify_code_review_findings,
    classify_git_commit_failure,
    classify_max_attempts_exceeded,
    classify_operation_failure,
    classify_retry_task_decision,
    classify_retry_verification_action,
    classify_retryable_review_failure,
    classify_session_recovery_result,
    classify_verification_gate_failure,
    has_retryable_review_failure,
    should_start_replacement_agent,
)


class FailurePolicyTests(unittest.TestCase):
    def test_missing_agent_context_starts_replacement_agent(self) -> None:
        error = RuntimeError("Session not found for thread_id: planner-thread")
        decision = classify_operation_failure(error)
        self.assertEqual(decision.category, CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT)
        self.assertEqual(decision.recovery_action, RECOVERY_START_REPLACEMENT_AGENT)
        self.assertTrue(decision.retryable)
        self.assertTrue(should_start_replacement_agent(error))

    def test_transient_infrastructure_error_is_retry_later(self) -> None:
        decision = classify_operation_failure(BrokenPipeError("Broken pipe"))
        self.assertEqual(decision.category, CATEGORY_TRANSIENT_INFRASTRUCTURE)
        self.assertEqual(decision.recovery_action, RECOVERY_RETRY_LATER)
        self.assertTrue(decision.retryable)
        self.assertFalse(decision.requires_human)

    def test_retryable_review_failure_decision_is_scheduler_automatic(self) -> None:
        manifest = SimpleNamespace(
            status="WORK_DONE",
            review=SimpleNamespace(evidence_files=["git-diff.patch"]),
            review_attempts=[
                SimpleNamespace(
                    status="FAILED_RETRYABLE",
                    error="Timed out waiting for MCP server output",
                    reason="planner_review_failed",
                )
            ],
        )
        self.assertTrue(has_retryable_review_failure(manifest))
        decision = classify_retryable_review_failure(
            manifest,
            source=SOURCE_QUEUE_SCHEDULER,
            attempt=2,
        )
        assert decision is not None
        self.assertEqual(decision.recovery_action, RECOVERY_RETRY_REVIEW)
        self.assertTrue(decision.automatic)
        self.assertEqual(decision.category, CATEGORY_TRANSIENT_INFRASTRUCTURE)
        self.assertEqual(decision.attempt, 2)

    def test_session_resume_result_is_classified(self) -> None:
        result = SimpleNamespace(raw={"resumedWithCodexExec": True})
        decision = classify_session_recovery_result(result)
        assert decision is not None
        self.assertEqual(decision.category, CATEGORY_MCP_SESSION_LOST_OR_TIMEOUT)
        self.assertEqual(decision.recovery_action, RECOVERY_RESUME_AGENT_SESSION)
        self.assertTrue(decision.automatic)

    def test_verification_failure_and_retry_verification_actions(self) -> None:
        manifest = SimpleNamespace(
            status="FAILED",
            review=SimpleNamespace(decision="accepted"),
        )
        events = [{"type": "verification_gate_failed"}]
        retry_decision = classify_retry_verification_action(
            manifest,
            events,
            source=SOURCE_DASHBOARD_ACTION,
            attempt=1,
        )
        assert retry_decision is not None
        self.assertEqual(retry_decision.category, CATEGORY_VERIFICATION_FAILURE)
        self.assertEqual(retry_decision.recovery_action, RECOVERY_RETRY_VERIFICATION)
        self.assertFalse(retry_decision.automatic)
        gate_decision = classify_verification_gate_failure(
            source=SOURCE_ORCHESTRATOR,
            error="1 of 1 verification command(s) failed.",
        )
        self.assertEqual(gate_decision.category, CATEGORY_VERIFICATION_FAILURE)
        self.assertEqual(gate_decision.recovery_action, RECOVERY_MANUAL_OVERRIDE)

    def test_code_review_findings_use_worker_rework(self) -> None:
        decision = classify_code_review_findings(
            reason="revision_requested",
            source=SOURCE_ORCHESTRATOR,
            attempt=1,
        )
        self.assertEqual(decision.category, CATEGORY_CODE_REVIEW_FINDINGS)
        self.assertEqual(decision.recovery_action, RECOVERY_WORKER_REWORK)
        self.assertTrue(decision.automatic)

    def test_apply_conflict_category(self) -> None:
        decision = classify_apply_failure(
            source=SOURCE_ORCHESTRATOR,
            attempt=1,
            error="git apply --check rejected the patch",
        )
        self.assertEqual(decision.category, CATEGORY_APPLY_CONFLICT)
        self.assertEqual(decision.recovery_action, RECOVERY_FAIL_WITH_EVIDENCE)
        self.assertTrue(decision.requires_human)

    def test_git_commit_failure_category_and_subreasons(self) -> None:
        dirty = classify_git_commit_failure(
            failure_reason="preexisting_staged_changes",
            summary="staged changes present",
            source=SOURCE_ORCHESTRATOR,
        )
        hook = classify_git_commit_failure(
            failure_reason="git_commit_failed",
            summary="pre-commit hook failed",
            source=SOURCE_ORCHESTRATOR,
        )
        generic = classify_git_commit_failure(
            failure_reason="git_commit_failed",
            summary="fatal: unable to write new index file",
            source=SOURCE_ORCHESTRATOR,
        )
        self.assertEqual(dirty.category, CATEGORY_GIT_COMMIT_FAILURE)
        self.assertEqual(dirty.reason, "dirty_repo")
        self.assertEqual(dirty.recovery_action, RECOVERY_MANUAL_OVERRIDE)
        self.assertEqual(hook.reason, "hook_failure")
        self.assertEqual(generic.reason, "generic_git_error")
        self.assertEqual(generic.recovery_action, RECOVERY_FAIL_WITH_EVIDENCE)

    def test_max_attempts_exceeded_category(self) -> None:
        decision = classify_max_attempts_exceeded(source=SOURCE_ORCHESTRATOR, attempt=3)
        self.assertEqual(decision.category, CATEGORY_MAX_ATTEMPTS_EXCEEDED)
        self.assertEqual(decision.recovery_action, RECOVERY_FAIL_WITH_EVIDENCE)
        self.assertTrue(decision.requires_human)

    def test_retry_task_policy_for_failed_run(self) -> None:
        manifest = SimpleNamespace(status="FAILED")
        events = [
            {"type": "git_commit_failed", "reason": "preexisting_staged_changes"},
            {"type": "run_terminal_status", "reason": "git_commit_failed"},
        ]
        decision = classify_retry_task_decision(
            manifest,
            events,
            source=SOURCE_DASHBOARD_ACTION,
        )
        assert decision is not None
        self.assertEqual(decision.phase, "task_retry")
        self.assertEqual(decision.category, CATEGORY_GIT_COMMIT_FAILURE)
        self.assertEqual(decision.recovery_action, RECOVERY_MANUAL_OVERRIDE)


if __name__ == "__main__":
    unittest.main()
