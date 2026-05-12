from __future__ import annotations

import unittest

from c_orch.cli import build_parser


class CliTests(unittest.TestCase):
    def test_run_command_uses_session_defaults(self) -> None:
        args = build_parser().parse_args(["run", "Task"])

        self.assertEqual(args.command, "run")
        self.assertEqual(args.planner_model, None)
        self.assertEqual(args.planner_reasoning_effort, "high")
        self.assertEqual(args.worker_model, "gpt-5.3-codex-spark")
        self.assertFalse(args.auto_approve_plan)

    def test_run_command_parses_reasoning_and_service_tier(self) -> None:
        args = build_parser().parse_args(
            [
                "run",
                "--planner-reasoning-effort",
                "high",
                "--worker-reasoning-effort",
                "medium",
                "--planner-service-tier",
                "fast",
                "--worker-service-tier",
                "flex",
                "Task",
            ]
        )
        self.assertEqual(args.command, "run")
        self.assertEqual(args.planner_reasoning_effort, "high")
        self.assertEqual(args.worker_reasoning_effort, "medium")
        self.assertEqual(args.planner_service_tier, "fast")
        self.assertEqual(args.worker_service_tier, "flex")

    def test_run_command_parses_auto_approve_plan(self) -> None:
        args = build_parser().parse_args(["run", "--auto-approve-plan", "Task"])

        self.assertEqual(args.command, "run")
        self.assertTrue(args.auto_approve_plan)

    def test_doctor_command_parses(self) -> None:
        args = build_parser().parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)

    def test_resume_command_parses(self) -> None:
        args = build_parser().parse_args(
            [
                "resume",
                "run-123",
                "--cwd",
                "repo",
                "--runs-dir",
                "runs",
                "--planner-reasoning-effort",
                "high",
                "--worker-service-tier",
                "fast",
            ]
        )
        self.assertEqual(args.command, "resume")
        self.assertEqual(args.run_id, "run-123")
        self.assertEqual(args.cwd, "repo")
        self.assertEqual(args.runs_dir, "runs")
        self.assertEqual(args.planner_reasoning_effort, "high")
        self.assertEqual(args.worker_service_tier, "fast")
        self.assertFalse(args.approve_plan)

    def test_resume_command_parses_plan_approval(self) -> None:
        args = build_parser().parse_args(["resume", "run-123", "--approve-plan"])

        self.assertEqual(args.command, "resume")
        self.assertTrue(args.approve_plan)

    def test_ui_command_parses(self) -> None:
        args = build_parser().parse_args(
            ["ui", "--cwd", "repo", "--runs-dir", "runs", "--host", "0.0.0.0", "--port", "7777"]
        )
        self.assertEqual(args.command, "ui")
        self.assertEqual(args.cwd, "repo")
        self.assertEqual(args.runs_dir, "runs")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 7777)


if __name__ == "__main__":
    unittest.main()
