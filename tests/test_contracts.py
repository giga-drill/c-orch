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
        self.assertIsNone(plan.decomposition_suggestion)

    def test_parse_planner_plan_with_decomposition_suggestion(self) -> None:
        plan = PlannerPlan.parse(
            """
            {
              "status":"plan_ready",
              "summary":"s",
              "acceptance_criteria":["a"],
              "worker_prompt":"do it",
              "verification_commands":["pytest"],
              "risk_notes":[],
              "decomposition_suggestion":{
                "recommended":true,
                "reason":"scope is broad",
                "subtasks":[
                  {
                    "id":"subtask-1",
                    "title":"Slice 1",
                    "goal":"Deliver API",
                    "acceptance_criteria":["API tests pass"]
                  }
                ]
              }
            }
            """
        )
        self.assertIsNotNone(plan.decomposition_suggestion)
        suggestion = plan.decomposition_suggestion or {}
        self.assertTrue(suggestion.get("recommended"))
        self.assertEqual(suggestion.get("reason"), "scope is broad")

    def test_parse_planner_plan_rejects_invalid_decomposition_shape(self) -> None:
        with self.assertRaises(ContractError):
            PlannerPlan.parse(
                """
                {
                  "status":"plan_ready",
                  "summary":"s",
                  "acceptance_criteria":["a"],
                  "worker_prompt":"do it",
                  "verification_commands":["pytest"],
                  "risk_notes":[],
                  "decomposition_suggestion":{
                    "recommended":"yes",
                    "reason":"bad",
                    "subtasks":[]
                  }
                }
                """
            )

    def test_parse_planner_plan_rejects_recommended_decomposition_with_empty_subtasks(self) -> None:
        with self.assertRaises(ContractError):
            PlannerPlan.parse(
                """
                {
                  "status":"plan_ready",
                  "summary":"s",
                  "acceptance_criteria":["a"],
                  "worker_prompt":"do it",
                  "verification_commands":["pytest"],
                  "risk_notes":[],
                  "decomposition_suggestion":{
                    "recommended":true,
                    "reason":"should split",
                    "subtasks":[]
                  }
                }
                """
            )

    def test_parse_planner_plan_rejects_subtask_with_empty_acceptance_criteria(self) -> None:
        with self.assertRaises(ContractError):
            PlannerPlan.parse(
                """
                {
                  "status":"plan_ready",
                  "summary":"s",
                  "acceptance_criteria":["a"],
                  "worker_prompt":"do it",
                  "verification_commands":["pytest"],
                  "risk_notes":[],
                  "decomposition_suggestion":{
                    "recommended":true,
                    "reason":"should split",
                    "subtasks":[
                      {
                        "id":"subtask-1",
                        "title":"Slice 1",
                        "goal":"Deliver API",
                        "acceptance_criteria":[]
                      }
                    ]
                  }
                }
                """
            )

    def test_parse_planner_plan_allows_non_recommended_decomposition_with_empty_subtasks(self) -> None:
        plan = PlannerPlan.parse(
            """
            {
              "status":"plan_ready",
              "summary":"s",
              "acceptance_criteria":["a"],
              "worker_prompt":"do it",
              "verification_commands":["pytest"],
              "risk_notes":[],
              "decomposition_suggestion":{
                "recommended":false,
                "reason":"single coherent change",
                "subtasks":[]
              }
            }
            """
        )
        self.assertEqual(
            plan.decomposition_suggestion,
            {
                "recommended": False,
                "reason": "single coherent change",
                "subtasks": [],
            },
        )

    def test_worker_status_validation(self) -> None:
        with self.assertRaises(ContractError):
            WorkerResult.parse(
                '{"status":"unknown","summary":"s","changed_files":[],"verification":[],"blockers":[]}'
            )

    def test_revision_requested_requires_prompt(self) -> None:
        with self.assertRaises(ContractError):
            ReviewDecision.parse(
                '{"decision":"revision_requested","reason":"r","next_worker_prompt":null}'
            )

    def test_accepted_requires_no_worker_prompt(self) -> None:
        with self.assertRaises(ContractError):
            ReviewDecision.parse(
                '{"decision":"accepted","reason":"r","next_worker_prompt":"keep going"}'
            )


if __name__ == "__main__":
    unittest.main()
