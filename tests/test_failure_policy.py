from __future__ import annotations

import unittest
from types import SimpleNamespace

from c_orch.failure_policy import (
    RECOVERY_RETRY_LATER,
    RECOVERY_START_REPLACEMENT_AGENT,
    classify_operation_failure,
    has_retryable_review_failure,
    should_start_replacement_agent,
)


class FailurePolicyTests(unittest.TestCase):
    def test_missing_agent_context_starts_replacement_agent(self) -> None:
        error = RuntimeError("Session not found for thread_id: planner-thread")

        decision = classify_operation_failure(error)

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.recovery_action, RECOVERY_START_REPLACEMENT_AGENT)
        self.assertTrue(should_start_replacement_agent(error))

    def test_transient_infrastructure_error_is_retry_later(self) -> None:
        decision = classify_operation_failure(BrokenPipeError("Broken pipe"))

        self.assertTrue(decision.retryable)
        self.assertEqual(decision.recovery_action, RECOVERY_RETRY_LATER)
        self.assertFalse(should_start_replacement_agent(BrokenPipeError("Broken pipe")))

    def test_retryable_review_failure_is_not_a_run_status(self) -> None:
        manifest = SimpleNamespace(
            status="WORK_DONE",
            review=SimpleNamespace(evidence_files=["git-diff.patch"]),
            review_attempts=[SimpleNamespace(status="FAILED_RETRYABLE")],
        )

        self.assertTrue(has_retryable_review_failure(manifest))


if __name__ == "__main__":
    unittest.main()
