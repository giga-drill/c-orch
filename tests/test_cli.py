from __future__ import annotations

import unittest

from c_orch.cli import build_parser


class CliTests(unittest.TestCase):
    def test_doctor_command_parses(self) -> None:
        args = build_parser().parse_args(["doctor", "--json"])
        self.assertEqual(args.command, "doctor")
        self.assertTrue(args.json)


if __name__ == "__main__":
    unittest.main()

