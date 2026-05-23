from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Optional

from c_orch.drivers import SessionResult
from c_orch.experiments import (
    DEFAULT_EXPERIMENT_STRATEGIES,
    STRATEGY_CODEX_CORCH_WORKFLOW,
    STRATEGY_CODEX_SUPERPOWERS,
    ExperimentConfig,
    build_strategy_prompt,
    run_experiment,
)


class _FakeDriver:
    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        output = Path(cwd) / "candidate.txt"
        output.write_text(f"{role}\n{model}\n{prompt}\n", encoding="utf-8")
        return SessionResult(thread_id=f"thr-{role}", content="done", raw={})


class _FakeDriverContext(AbstractContextManager[_FakeDriver]):
    def __enter__(self) -> _FakeDriver:
        return _FakeDriver()

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None


class ExperimentTests(unittest.TestCase):
    def test_default_strategies_include_codex_skill_variants(self) -> None:
        self.assertIn(STRATEGY_CODEX_CORCH_WORKFLOW, DEFAULT_EXPERIMENT_STRATEGIES)
        self.assertIn(STRATEGY_CODEX_SUPERPOWERS, DEFAULT_EXPERIMENT_STRATEGIES)

    def test_strategy_prompts_keep_experiment_boundary(self) -> None:
        for strategy in DEFAULT_EXPERIMENT_STRATEGIES:
            with self.subTest(strategy=strategy):
                prompt = build_strategy_prompt(strategy=strategy, task="Add feature")
                self.assertIn(strategy, prompt)
                self.assertIn("Do not commit, merge, apply", prompt)
                self.assertIn("Task:\nAdd feature", prompt)

    def test_run_experiment_writes_arm_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _git(["init"], cwd=repo)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")
            _git(["add", "README.md"], cwd=repo)
            _git(
                [
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-m",
                    "init",
                ],
                cwd=repo,
            )

            config = ExperimentConfig(
                cwd=repo,
                task="Compare implementations",
                experiment_id="exp-test",
                experiments_dir=root / "experiments",
                worktrees_dir=root / "worktrees",
                codex_path="/usr/bin/codex",
                model="gpt-test",
                sandbox="workspace-write",
                approval_policy="never",
                strategies=(STRATEGY_CODEX_CORCH_WORKFLOW,),
                verification_commands=("test -f candidate.txt",),
                max_parallel=1,
            )

            result = run_experiment(
                config,
                driver_factory=lambda codex_path: _FakeDriverContext(),
            )

            self.assertEqual(result.experiment_id, "exp-test")
            self.assertEqual(len(result.arms), 1)
            arm = result.arms[0]
            self.assertEqual(arm.status, "done")
            self.assertIn("candidate.txt", arm.changed_paths)
            self.assertTrue(Path(arm.patch_path or "").is_file())
            self.assertTrue(Path(arm.verification_output_path or "").is_file())
            self.assertTrue((root / "experiments" / "exp-test" / "experiment.json").is_file())
            self.assertTrue((root / "experiments" / "exp-test" / "summary.md").is_file())


def _git(args: list[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout

