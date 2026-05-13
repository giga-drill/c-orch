from __future__ import annotations

import unittest

from c_orch.prompts import (
    planner_initial_prompt,
    planner_review_fallback_prompt,
    planner_review_prompt,
    planner_revision_prompt,
    worker_prompt,
)


class PromptTests(unittest.TestCase):
    def test_planner_initial_prompt_requests_chinese_plan_content(self) -> None:
        prompt = planner_initial_prompt(
            user_task="Improve dashboard",
            cwd="/repo",
            worker_model="gpt-5.3-codex",
        )

        self.assertIn("Use Simplified Chinese", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("worker_prompt", prompt)
        self.assertIn("risk_notes", prompt)
        self.assertIn("Keep JSON keys", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("docs/mcp-orchestrator-design.md", prompt)

    def test_planner_revision_prompt_requests_chinese_plan_content(self) -> None:
        prompt = planner_revision_prompt(human_feedback="请缩小范围")

        self.assertIn("Use Simplified Chinese", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("worker_prompt", prompt)
        self.assertIn("risk_notes", prompt)
        self.assertIn("Keep JSON keys", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)

    def test_worker_prompt_includes_project_context_docs(self) -> None:
        prompt = worker_prompt(
            planner_worker_prompt="Implement the focused change",
            acceptance_criteria=["Tests pass"],
        )

        self.assertIn("README.md", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("Implement the focused change", prompt)

    def test_review_prompts_keep_two_outcome_contract_and_project_context(self) -> None:
        prompt = planner_review_prompt(
            original_plan_json={"summary": "plan"},
            worker_result_json={"summary": "done"},
            diff_summary="diff",
            diff_path="runs/r/evidence/git-diff.patch",
            test_summary="tests passed",
        )
        fallback = planner_review_fallback_prompt(
            user_task="Task",
            original_plan_json={"summary": "plan"},
            acceptance_criteria=["criterion"],
            worker_result_json={"summary": "done"},
            diff_summary="diff",
            diff_path="runs/r/evidence/git-diff.patch",
            test_summary="tests passed",
        )

        for value in (prompt, fallback):
            self.assertIn('"decision": "accepted|revision_requested"', value)
            self.assertIn("Infrastructure errors are handled by c-orch", value)
            self.assertIn("docs/architecture-principles.md", value)


if __name__ == "__main__":
    unittest.main()
