from __future__ import annotations

import unittest

from c_orch.cli import build_parser


class CliTests(unittest.TestCase):
    def test_doctor_command_parses(self) -> None:
        args = build_parser().parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)

    def test_resume_command_parses(self) -> None:
        args = build_parser().parse_args(["resume", "run-123", "--cwd", "repo", "--runs-dir", "runs"])
        self.assertEqual(args.command, "resume")
        self.assertEqual(args.run_id, "run-123")
        self.assertEqual(args.cwd, "repo")
        self.assertEqual(args.runs_dir, "runs")


if __name__ == "__main__":
    unittest.main()
