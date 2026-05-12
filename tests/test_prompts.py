from __future__ import annotations

import unittest

from c_orch.prompts import planner_initial_prompt, planner_revision_prompt


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

    def test_planner_revision_prompt_requests_chinese_plan_content(self) -> None:
        prompt = planner_revision_prompt(human_feedback="请缩小范围")

        self.assertIn("Use Simplified Chinese", prompt)
        self.assertIn("summary", prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("worker_prompt", prompt)
        self.assertIn("risk_notes", prompt)
        self.assertIn("Keep JSON keys", prompt)


if __name__ == "__main__":
    unittest.main()
