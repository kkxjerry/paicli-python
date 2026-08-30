from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr

from paicli.__main__ import build_parser


class AgentCliBudgetTest(unittest.TestCase):
    def test_defaults_match_java_parity_baseline(self) -> None:
        args = build_parser().parse_args([])

        self.assertEqual(50, args.max_steps)
        self.assertEqual(3, args.stagnation_window)
        self.assertIsNone(args.token_budget)

    def test_explicit_budget_controls_are_parsed(self) -> None:
        args = build_parser().parse_args(
            [
                "--max-steps",
                "12",
                "--stagnation-window",
                "4",
                "--token-budget",
                "9000",
            ]
        )

        self.assertEqual(12, args.max_steps)
        self.assertEqual(4, args.stagnation_window)
        self.assertEqual(9000, args.token_budget)

    def test_invalid_limits_are_rejected_by_cli(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(["--max-steps", "0"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--stagnation-window", "1"])
            with self.assertRaises(SystemExit):
                parser.parse_args(["--token-budget", "-1"])


if __name__ == "__main__":
    unittest.main()
