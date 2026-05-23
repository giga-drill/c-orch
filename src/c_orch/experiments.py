from __future__ import annotations

import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Union

from .drivers import CodexDriver
from .prompts import (
    CORCH_WORKFLOW_CONTROL_BOUNDARY,
    SUPERPOWERS_PLANNING_DISCIPLINE,
    SUPERPOWERS_REVIEW_DISCIPLINE,
    SUPERPOWERS_WORKER_DISCIPLINE,
)
from .verification import run_verification_commands
from .worktrees import collect_diff_evidence, create_worker_worktree


Pathish = Union[str, Path]
DriverFactory = Callable[[str], AbstractContextManager[CodexDriver]]
WorktreeFactory = Callable[..., Path]

STRATEGY_CODEX_DIRECT = "codex-direct"
STRATEGY_CODEX_CORCH_WORKFLOW = "codex-corch-workflow"
STRATEGY_CODEX_SUPERPOWERS = "codex-superpowers"
STRATEGY_CORCH_PLAIN = "corch-plain"
STRATEGY_CORCH_SUPERPOWERS = "corch-superpowers"
DEFAULT_EXPERIMENT_STRATEGIES = (
    STRATEGY_CODEX_DIRECT,
    STRATEGY_CODEX_CORCH_WORKFLOW,
    STRATEGY_CODEX_SUPERPOWERS,
    STRATEGY_CORCH_PLAIN,
    STRATEGY_CORCH_SUPERPOWERS,
)
EXPERIMENT_STRATEGIES = frozenset(DEFAULT_EXPERIMENT_STRATEGIES)


@dataclass(frozen=True)
class ExperimentConfig:
    cwd: Path
    task: str
    experiment_id: str
    experiments_dir: Path
    worktrees_dir: Path
    codex_path: str
    model: str
    sandbox: str
    approval_policy: str
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None
    strategies: Sequence[str] = DEFAULT_EXPERIMENT_STRATEGIES
    verification_commands: Sequence[str] = ()
    max_parallel: int = 3
    base_ref: str = "HEAD"


@dataclass(frozen=True)
class ExperimentArmResult:
    strategy: str
    status: str
    worktree_path: str
    evidence_dir: str
    summary_path: Optional[str] = None
    patch_path: Optional[str] = None
    verification_output_path: Optional[str] = None
    verification_summary: Optional[str] = None
    thread_id: Optional[str] = None
    elapsed_seconds: float = 0.0
    changed_paths: List[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "strategy": self.strategy,
            "status": self.status,
            "worktree_path": self.worktree_path,
            "evidence_dir": self.evidence_dir,
            "summary_path": self.summary_path,
            "patch_path": self.patch_path,
            "verification_output_path": self.verification_output_path,
            "verification_summary": self.verification_summary,
            "thread_id": self.thread_id,
            "elapsed_seconds": self.elapsed_seconds,
            "changed_paths": list(self.changed_paths),
            "error": self.error,
        }


@dataclass(frozen=True)
class ExperimentResult:
    experiment_id: str
    task: str
    cwd: str
    base_ref: str
    base_commit: str
    experiment_dir: str
    strategies: List[str]
    arms: List[ExperimentArmResult]
    created_at: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "task": self.task,
            "cwd": self.cwd,
            "base_ref": self.base_ref,
            "base_commit": self.base_commit,
            "experiment_dir": self.experiment_dir,
            "strategies": list(self.strategies),
            "arms": [arm.to_dict() for arm in self.arms],
            "created_at": self.created_at,
        }


def new_experiment_id(now: Optional[datetime] = None) -> str:
    value = now or datetime.now(timezone.utc)
    return "exp-" + value.strftime("%Y%m%d-%H%M%S")


def run_experiment(
    config: ExperimentConfig,
    *,
    driver_factory: DriverFactory,
    worktree_factory: WorktreeFactory = create_worker_worktree,
) -> ExperimentResult:
    _validate_strategies(config.strategies)
    base_commit = _git(["rev-parse", config.base_ref], cwd=config.cwd).strip()
    experiment_dir = config.experiments_dir / config.experiment_id
    arms_dir = experiment_dir / "arms"
    arms_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now(timezone.utc).isoformat()

    max_workers = max(1, min(config.max_parallel, len(config.strategies)))
    arm_results: List[ExperimentArmResult] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _run_experiment_arm,
                config,
                strategy,
                base_commit,
                arms_dir,
                driver_factory,
                worktree_factory,
            )
            for strategy in config.strategies
        ]
        for future in as_completed(futures):
            arm_results.append(future.result())
    arm_results.sort(key=lambda arm: list(config.strategies).index(arm.strategy))

    result = ExperimentResult(
        experiment_id=config.experiment_id,
        task=config.task,
        cwd=str(config.cwd),
        base_ref=config.base_ref,
        base_commit=base_commit,
        experiment_dir=str(experiment_dir),
        strategies=list(config.strategies),
        arms=arm_results,
        created_at=created_at,
    )
    _write_json(experiment_dir / "experiment.json", result.to_dict())
    _write_markdown_summary(experiment_dir / "summary.md", result)
    return result


def _run_experiment_arm(
    config: ExperimentConfig,
    strategy: str,
    base_commit: str,
    arms_dir: Path,
    driver_factory: DriverFactory,
    worktree_factory: WorktreeFactory,
) -> ExperimentArmResult:
    started = time.monotonic()
    arm_dir = arms_dir / strategy
    evidence_dir = arm_dir / "evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    worktree_path: Optional[Path] = None
    thread_id: Optional[str] = None
    error: Optional[str] = None
    status = "done"
    diff_summary_path: Optional[Path] = None
    diff_patch_path: Optional[Path] = None
    changed_paths: List[str] = []
    verification_output_path: Optional[str] = None
    verification_summary: Optional[str] = None
    try:
        worktree_path = worktree_factory(
            repo_path=config.cwd,
            worktrees_dir=config.worktrees_dir / config.experiment_id,
            run_id=strategy,
            worker_id="candidate",
            base_ref=base_commit,
        )
        prompt = build_strategy_prompt(strategy=strategy, task=config.task)
        prompt_path = arm_dir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        with driver_factory(config.codex_path) as driver:
            session = driver.start_session(
                role=f"experiment:{strategy}",
                model=config.model,
                cwd=str(worktree_path),
                prompt=prompt,
                sandbox=config.sandbox,
                approval_policy=config.approval_policy,
                reasoning_effort=config.reasoning_effort,
                service_tier=config.service_tier,
            )
            thread_id = session.thread_id
            (arm_dir / "codex-output.txt").write_text(session.content, encoding="utf-8")

        diff = collect_diff_evidence(worktree_path, evidence_dir)
        diff_summary_path = diff.summary_path
        diff_patch_path = diff.patch_path
        changed_paths = list(diff.changed_paths)

        if config.verification_commands:
            verification = run_verification_commands(
                config.verification_commands,
                cwd=worktree_path,
                evidence_dir=evidence_dir,
            )
            verification_output_path = str(verification.output_path)
            verification_summary = verification.summary
            if any(item.status != "passed" for item in verification.results):
                status = "verification_failed"
    except Exception as exc:
        status = "failed"
        error = str(exc)
        (arm_dir / "error.txt").write_text(error, encoding="utf-8")

    result = ExperimentArmResult(
        strategy=strategy,
        status=status,
        worktree_path=str(worktree_path) if worktree_path is not None else "",
        evidence_dir=str(evidence_dir),
        summary_path=str(diff_summary_path) if diff_summary_path is not None else None,
        patch_path=str(diff_patch_path) if diff_patch_path is not None else None,
        verification_output_path=verification_output_path,
        verification_summary=verification_summary,
        thread_id=thread_id,
        elapsed_seconds=round(time.monotonic() - started, 3),
        changed_paths=changed_paths,
        error=error,
    )
    _write_json(arm_dir / "result.json", result.to_dict())
    return result


def build_strategy_prompt(*, strategy: str, task: str) -> str:
    if strategy == STRATEGY_CODEX_DIRECT:
        body = """Implement the task directly in this isolated worktree.
Keep the change focused. Run relevant checks if you can."""
    elif strategy == STRATEGY_CODEX_CORCH_WORKFLOW:
        body = f"""Use a COrch-style single-session workflow: briefly plan,
implement, self-review against acceptance, and verify. {CORCH_WORKFLOW_CONTROL_BOUNDARY}"""
    elif strategy == STRATEGY_CODEX_SUPERPOWERS:
        body = f"""Use Superpowers-style work discipline only, without adopting
Superpowers workflow control.

{SUPERPOWERS_PLANNING_DISCIPLINE}

{SUPERPOWERS_WORKER_DISCIPLINE}

{SUPERPOWERS_REVIEW_DISCIPLINE}

{CORCH_WORKFLOW_CONTROL_BOUNDARY}"""
    elif strategy == STRATEGY_CORCH_PLAIN:
        body = f"""Use COrch's Planner/Worker/Reviewer mental phases inside this
single candidate run: make a concise plan, implement it, review your own diff
against the plan, and verify. {CORCH_WORKFLOW_CONTROL_BOUNDARY}"""
    elif strategy == STRATEGY_CORCH_SUPERPOWERS:
        body = f"""Use COrch's phase boundaries with Superpowers-inspired work
discipline inside each phase. Plan with exact files and tests, implement with
TDD where practical, then review spec compliance before code quality.

{SUPERPOWERS_PLANNING_DISCIPLINE}

{SUPERPOWERS_WORKER_DISCIPLINE}

{SUPERPOWERS_REVIEW_DISCIPLINE}

{CORCH_WORKFLOW_CONTROL_BOUNDARY}"""
    else:
        raise ValueError(f"unknown experiment strategy: {strategy}")
    return f"""You are running one candidate arm in a c-orch experiment arena.

This is not a production queue task. Produce a candidate implementation only.
Do not commit, merge, apply to another checkout, open a PR, or clean up the
worktree. c-orch will collect your diff and evidence for human comparison.

Task:
{task}

Strategy:
{strategy}

Instructions:
{body}
"""


def _validate_strategies(strategies: Iterable[str]) -> None:
    unknown = [strategy for strategy in strategies if strategy not in EXPERIMENT_STRATEGIES]
    if unknown:
        raise ValueError(f"unknown experiment strategy: {', '.join(unknown)}")


def _write_json(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _write_markdown_summary(path: Path, result: ExperimentResult) -> None:
    lines = [
        f"# Experiment {result.experiment_id}",
        "",
        f"- Task: {result.task}",
        f"- CWD: `{result.cwd}`",
        f"- Base: `{result.base_commit}`",
        "",
        "## Arms",
        "",
    ]
    for arm in result.arms:
        lines.extend(
            [
                f"### {arm.strategy}",
                "",
                f"- Status: {arm.status}",
                f"- Worktree: `{arm.worktree_path}`",
                f"- Patch: `{arm.patch_path}`",
                f"- Verification: {arm.verification_summary or '(not run)'}",
                f"- Changed paths: {', '.join(arm.changed_paths) if arm.changed_paths else '(none)'}",
                f"- Error: {arm.error or '(none)'}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _git(args: List[str], *, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout
