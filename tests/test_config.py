from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch.config import load_project_config


class ProjectConfigTests(unittest.TestCase):
    def test_loads_project_toml_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".c-orch.toml").write_text(
                """
[codex]
bin = "/bin/codex"

[planner]
preferred_models = ["gpt-5.5", "gpt-5.4"]
reasoning_effort = "high"
service_tier = "fast"

[worker]
model = "gpt-5.3-codex-spark"
reasoning_effort = "medium"

[run]
runs_dir = "custom-runs"
worktrees_dir = ".custom/worktrees"
max_attempts = 2
max_parallel_workspaces = 3
require_proposal_plan_review = true
low_cost_mode = true
sandbox = "read-only"
approval_policy = "on-request"

[ui]
host = "0.0.0.0"
port = 9876
dev_host = "localhost"
dev_port = 6173
""".strip(),
                encoding="utf-8",
            )

            config = load_project_config(cwd=cwd)

            self.assertEqual(config.codex_bin, "/bin/codex")
            self.assertEqual(config.planner_preferred_models, ["gpt-5.5", "gpt-5.4"])
            self.assertEqual(config.planner.reasoning_effort, "high")
            self.assertEqual(config.planner.service_tier, "fast")
            self.assertEqual(config.worker.model, "gpt-5.3-codex-spark")
            self.assertEqual(config.worker.reasoning_effort, "medium")
            self.assertEqual(config.run.runs_dir, "custom-runs")
            self.assertEqual(config.run.worktrees_dir, ".custom/worktrees")
            self.assertEqual(config.run.max_attempts, 2)
            self.assertEqual(config.run.max_parallel_workspaces, 3)
            self.assertTrue(config.run.require_proposal_plan_review)
            self.assertTrue(config.run.low_cost_mode)
            self.assertEqual(config.run.sandbox, "read-only")
            self.assertEqual(config.run.approval_policy, "on-request")
            self.assertEqual(config.ui.host, "0.0.0.0")
            self.assertEqual(config.ui.port, 9876)
            self.assertEqual(config.ui.dev_host, "localhost")
            self.assertEqual(config.ui.dev_port, 6173)

    def test_rejects_invalid_plan_review_flag_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".c-orch.toml").write_text(
                """
[run]
require_proposal_plan_review = "yes"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run.require_proposal_plan_review"):
                load_project_config(cwd=cwd)

    def test_run_low_cost_mode_defaults_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)

            config = load_project_config(cwd=cwd)

            self.assertFalse(config.run.low_cost_mode)

    def test_rejects_invalid_low_cost_mode_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".c-orch.toml").write_text(
                """
[run]
low_cost_mode = "true"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run.low_cost_mode"):
                load_project_config(cwd=cwd)

    def test_rejects_invalid_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            (cwd / ".c-orch.toml").write_text(
                """
[planner]
reasoning_effort = "giant"
""".strip(),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "planner.reasoning_effort"):
                load_project_config(cwd=cwd)


if __name__ == "__main__":
    unittest.main()
