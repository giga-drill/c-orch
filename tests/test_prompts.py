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
        self.assertIn("verification_commands` are hard gates", prompt)
        self.assertIn("any non-zero exit blocks apply/commit", prompt)
        self.assertIn("optional diagnostics", prompt)

    def test_planner_revision_prompt_requests_chinese_plan_content(self) -> None:
        prompt = planner_revision_prompt(human_feedback="请缩小范围")

        self.assertIn("Use Simplified Chinese", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("worker_prompt", prompt)
        self.assertIn("risk_notes", prompt)
        self.assertIn("Keep JSON keys", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("verification_commands` are hard gates", prompt)

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
            review_workspace="/tmp/task-workspace",
            diff_summary="diff",
            diff_path="runs/r/evidence/git-diff.patch",
            test_summary="tests passed",
            code_review_summary="codex review found 1 issue",
            code_review_output_path="runs/r/evidence/codex-review-output.txt",
        )
        fallback = planner_review_fallback_prompt(
            user_task="Task",
            original_plan_json={"summary": "plan"},
            acceptance_criteria=["criterion"],
            worker_result_json={"summary": "done"},
            review_workspace="/tmp/task-workspace",
            diff_summary="diff",
            diff_path="runs/r/evidence/git-diff.patch",
            test_summary="tests passed",
            code_review_summary="codex review found 1 issue",
            code_review_output_path="runs/r/evidence/codex-review-output.txt",
        )

        for value in (prompt, fallback):
            self.assertIn('"decision": "accepted|revision_requested"', value)
            self.assertIn("Infrastructure errors", value)
            self.assertIn("handled by c-orch", value)
            self.assertIn("docs/architecture-principles.md", value)
            self.assertIn("Review target workspace:", value)
            self.assertIn("Review depth requirements:", value)
            self.assertIn("decide from the diff alone", value)
            self.assertIn("Trace affected call sites", value)
            self.assertIn("original business expectations", value)
            self.assertIn("every acceptance criterion", value)
            self.assertIn("regressions outside the edited lines", value)
            self.assertIn("reason` field must briefly state what you inspected", value)
            self.assertIn("first sentence of `reason` must be", value)
            self.assertIn("frontend-ready core summary", value)
            self.assertIn("If `decision` is `accepted`, keep `reason` short", value)
            self.assertIn("Code review summary:", value)
            self.assertIn("codex review found 1 issue", value)
            self.assertIn("codex-review-output.txt", value)
            self.assertIn('If decision is "revision_requested", the first sentence of reason', value)
            self.assertIn('If decision is "accepted", keep reason concise', value)


if __name__ == "__main__":
    unittest.main()
