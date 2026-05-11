from __future__ import annotations

import unittest

from c_orch.contracts import ContractError, PlannerPlan, ReviewDecision, WorkerResult, extract_json_object


class ContractTests(unittest.TestCase):
    def test_extract_json_from_fence(self) -> None:
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_parse_planner_plan(self) -> None:
        plan = PlannerPlan.parse(
            """
            {"status":"plan_ready","summary":"s","acceptance_criteria":["a"],
             "worker_prompt":"do it","verification_commands":["pytest"],"risk_notes":[]}
            """
        )
        self.assertEqual(plan.worker_prompt, "do it")

    def test_worker_status_validation(self) -> None:
        with self.assertRaises(ContractError):
            WorkerResult.parse(
                '{"status":"unknown","summary":"s","changed_files":[],"verification":[],"blockers":[]}'
            )

    def test_needs_changes_requires_prompt(self) -> None:
        with self.assertRaises(ContractError):
            ReviewDecision.parse(
                '{"decision":"needs_changes","reason":"r","next_worker_prompt":null}'
            )


if __name__ == "__main__":
    unittest.main()

