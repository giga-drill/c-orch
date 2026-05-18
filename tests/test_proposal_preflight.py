from __future__ import annotations

import unittest

from c_orch.proposal_preflight import evaluate_proposal_preflight


class ProposalPreflightTests(unittest.TestCase):
    def test_passes_when_prompt_has_scope_and_verification_hint(self) -> None:
        result = evaluate_proposal_preflight(
            "Update src/c_orch/runtime.py to persist proposal blocker reason and add unittest coverage for retry-plan."
        )
        self.assertTrue(result.passed)
        self.assertIsNone(result.reason)

    def test_blocks_obvious_too_vague_prompt(self) -> None:
        result = evaluate_proposal_preflight("fix it")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "proposal_too_vague")
        self.assertIn("prompt_too_vague", result.issues)

    def test_blocks_prompt_missing_outcome_or_acceptance(self) -> None:
        result = evaluate_proposal_preflight("Improve dashboard")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "proposal_missing_outcome")
        self.assertIn("missing_outcome_or_acceptance", result.issues)

    def test_blocks_scope_too_large_prompt(self) -> None:
        result = evaluate_proposal_preflight("重写整个系统并重新设计架构")
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "proposal_scope_too_large")
        self.assertIn("scope_too_large", result.issues)


if __name__ == "__main__":
    unittest.main()
