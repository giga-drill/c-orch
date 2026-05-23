from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from c_orch.cli import main
from c_orch.codex_discovery import CodexCandidateReport, CodexEnvironmentReport
from c_orch.experiments import (
    STRATEGY_CODEX_CORCH_WORKFLOW,
    STRATEGY_CODEX_SUPERPOWERS,
    ExperimentArmResult,
    ExperimentResult,
)


class CliExperimentTests(unittest.TestCase):
    def test_experiment_run_passes_strategy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.3-codex",),
                    usable=True,
                ),
            )
            fake_result = ExperimentResult(
                experiment_id="exp-test",
                task="Try variants",
                cwd=str(repo),
                base_ref="HEAD",
                base_commit="abc123",
                experiment_dir=str(root / "experiments" / "exp-test"),
                strategies=[
                    STRATEGY_CODEX_CORCH_WORKFLOW,
                    STRATEGY_CODEX_SUPERPOWERS,
                ],
                arms=[
                    ExperimentArmResult(
                        strategy=STRATEGY_CODEX_CORCH_WORKFLOW,
                        status="done",
                        worktree_path=str(root / "worktree-a"),
                        evidence_dir=str(root / "evidence-a"),
                        patch_path=str(root / "patch-a.diff"),
                    ),
                    ExperimentArmResult(
                        strategy=STRATEGY_CODEX_SUPERPOWERS,
                        status="verification_failed",
                        worktree_path=str(root / "worktree-b"),
                        evidence_dir=str(root / "evidence-b"),
                        patch_path=str(root / "patch-b.diff"),
                        verification_summary="1 of 1 verification command(s) failed.",
                    ),
                ],
                created_at="2026-05-20T00:00:00+00:00",
            )

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.experiments.run_experiment",
                return_value=fake_result,
            ) as run_experiment, redirect_stdout(StringIO()) as stdout:
                exit_code = main(
                    [
                        "experiment",
                        "run",
                        "--cwd",
                        str(repo),
                        "--experiments-dir",
                        str(root / "experiments"),
                        "--worktrees-dir",
                        str(root / "worktrees"),
                        "--strategy",
                        STRATEGY_CODEX_CORCH_WORKFLOW,
                        "--strategy",
                        STRATEGY_CODEX_SUPERPOWERS,
                        "--max-parallel",
                        "2",
                        "--verification-command",
                        "python -m unittest",
                        "Try variants",
                    ]
                )

            self.assertEqual(exit_code, 0)
            config = run_experiment.call_args.args[0]
            self.assertEqual(
                tuple(config.strategies),
                (STRATEGY_CODEX_CORCH_WORKFLOW, STRATEGY_CODEX_SUPERPOWERS),
            )
            self.assertEqual(config.max_parallel, 2)
            self.assertEqual(tuple(config.verification_commands), ("python -m unittest",))
            self.assertIn("experiment_id: exp-test", stdout.getvalue())
