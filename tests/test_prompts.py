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
        self.assertIn("decomposition_suggestion", prompt)
        self.assertIn('"recommended": true', prompt)
        self.assertIn("Advisory decomposition suggestion requirements", prompt)
        self.assertIn("commit-sized subtasks", prompt)
        self.assertIn("Keep JSON keys", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("docs/mcp-orchestrator-design.md", prompt)
        self.assertIn("verification_commands` are hard gates", prompt)
        self.assertIn("any non-zero exit blocks apply/commit", prompt)
        self.assertIn("optional diagnostics", prompt)
        self.assertIn("COrch workflow control boundary", prompt)
        self.assertIn("c-orch owns queue/proposal/run state", prompt)
        self.assertIn("Do not create or switch worktrees", prompt)
        self.assertIn("Do not create or switch worktrees, commit", prompt)
        self.assertIn("dispatch", prompt)
        self.assertIn("Superpowers-inspired planning discipline", prompt)
        self.assertIn("exact files", prompt)
        self.assertIn("bite-sized, testable steps", prompt)
        self.assertIn("failing test", prompt)
        self.assertIn("expected outputs", prompt)
        self.assertIn("Self-review the plan", prompt)
        self.assertIn("Return exactly the requested JSON object", prompt)

    def test_planner_revision_prompt_requests_chinese_plan_content(self) -> None:
        prompt = planner_revision_prompt(human_feedback="请缩小范围")

        self.assertIn("Use Simplified Chinese", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("worker_prompt", prompt)
        self.assertIn("risk_notes", prompt)
        self.assertIn("decomposition_suggestion", prompt)
        self.assertIn('"recommended": true', prompt)
        self.assertIn("Advisory decomposition suggestion requirements", prompt)
        self.assertIn("If `recommended` is false, still provide a clear reason", prompt)
        self.assertIn("Keep JSON keys", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("verification_commands` are hard gates", prompt)
        self.assertIn("COrch workflow control boundary", prompt)
        self.assertIn("Superpowers-inspired planning discipline", prompt)
        self.assertIn("do not create or switch worktrees", prompt.lower())
        self.assertIn("Return exactly the requested JSON object", prompt)

    def test_worker_prompt_includes_project_context_docs(self) -> None:
        prompt = worker_prompt(
            planner_worker_prompt="Implement the focused change",
            acceptance_criteria=["Tests pass"],
        )

        self.assertIn("README.md", prompt)
        self.assertIn("docs/architecture-principles.md", prompt)
        self.assertIn("Implement the focused change", prompt)
        self.assertIn("COrch workflow control boundary", prompt)
        self.assertIn("Do not create or switch worktrees", prompt)
        self.assertIn("commit, merge", prompt)
        self.assertIn("ask the", prompt)
        self.assertIn("Superpowers-inspired execution discipline", prompt)
        self.assertIn("Critically review the Planner instructions", prompt)
        self.assertIn("report them in the `blockers` array", prompt)
        self.assertIn("Use TDD for behavior changes", prompt)
        self.assertIn("confirm it fails for the expected reason", prompt)
        self.assertIn("tests that exercise real behavior", prompt)
        self.assertIn('"status": "work_done"', prompt)

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
            self.assertIn("COrch workflow control boundary", value)
            self.assertIn("Do not create or switch worktrees", value)
            self.assertIn("Superpowers-inspired review discipline", value)
            self.assertIn("Review in two passes", value)
            self.assertIn("first spec compliance", value)
            self.assertIn("then code quality", value)
            self.assertIn("Critical, Important, or Minor", value)
            self.assertIn("Do not dispatch implementers", value)
            self.assertIn("c-orch owns those transitions", value)

    def test_superpowers_extraction_manifest_records_sources_and_sanitization(self) -> None:
        from pathlib import Path

        manifest = (
            Path(__file__).resolve().parents[1] / "docs" / "superpowers-prompt-extraction.md"
        ).read_text(encoding="utf-8")

        self.assertIn("f2cbfbefebbfef77321e4c9abc9e949826bea9d7", manifest)
        self.assertIn("skills/writing-plans/SKILL.md:L10-L12", manifest)
        self.assertIn("skills/writing-plans/SKILL.md:L106-L120", manifest)
        self.assertIn("skills/executing-plans/SKILL.md:L18-L30", manifest)
        self.assertIn("skills/test-driven-development/SKILL.md:L47-L68", manifest)
        self.assertIn("skills/requesting-code-review/SKILL.md:L24-L46", manifest)
        self.assertIn("skills/subagent-driven-development/SKILL.md:L8-L12", manifest)
        self.assertIn("Sanitized out", manifest)
        self.assertIn("c-orch owns commit", manifest)
        self.assertIn("c-orch owns worktree setup", manifest)
        self.assertIn("Reviewer must not dispatch subagents", manifest)


if __name__ == "__main__":
    unittest.main()
