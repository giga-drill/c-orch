from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Union


Pathish = Union[str, Path]


@dataclass(frozen=True)
class DiffEvidence:
    summary_path: Path
    patch_path: Path
    summary: str
    patch: str

    @property
    def evidence_files(self) -> List[str]:
        return [str(self.summary_path), str(self.patch_path)]


def worker_worktree_path(worktrees_dir: Pathish, run_id: str, worker_id: str) -> Path:
    _validate_path_component(run_id, "run_id")
    _validate_path_component(worker_id, "worker_id")
    return Path(worktrees_dir) / run_id / worker_id


def create_worker_worktree(
    repo_path: Pathish,
    worktrees_dir: Pathish,
    run_id: str,
    worker_id: str,
    base_ref: str = "HEAD",
) -> Path:
    repo = Path(repo_path).expanduser().resolve()
    target = worker_worktree_path(worktrees_dir, run_id, worker_id).expanduser().resolve()

    if target.exists():
        _git(["rev-parse", "--is-inside-work-tree"], cwd=target)
        return target

    if not _git_ok(["rev-parse", "--verify", base_ref], cwd=repo):
        raise RuntimeError(
            "Worker worktree isolation requires a committed base ref. "
            f"Could not resolve {base_ref!r} in {repo}. "
            "Create an initial commit or pass a valid base ref before running c-orch."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "--detach", str(target), base_ref], cwd=repo)
    return target


def collect_diff_evidence(worktree_path: Pathish, evidence_dir: Pathish) -> DiffEvidence:
    worktree = Path(worktree_path).expanduser().resolve()
    output_dir = Path(evidence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    status = _git(["status", "--short"], cwd=worktree)
    untracked = _git_z(["ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree)
    added_intent = False
    try:
        if untracked:
            _git(["add", "--intent-to-add", "--", *untracked], cwd=worktree)
            added_intent = True
        diff_stat = _git(["diff", "--stat", "HEAD", "--"], cwd=worktree)
        patch = _git(["diff", "--binary", "HEAD", "--"], cwd=worktree)
    finally:
        if added_intent:
            _git(["reset", "--mixed", "--", *untracked], cwd=worktree)

    summary = _format_summary(status=status, diff_stat=diff_stat)
    summary_path = output_dir / "git-diff-summary.md"
    patch_path = output_dir / "git-diff.patch"
    summary_path.write_text(summary, encoding="utf-8")
    patch_path.write_text(patch, encoding="utf-8")
    return DiffEvidence(
        summary_path=summary_path,
        patch_path=patch_path,
        summary=summary,
        patch=patch,
    )


def _format_summary(status: str, diff_stat: str) -> str:
    status_text = status.rstrip() or "clean"
    stat_text = diff_stat.rstrip() or "No diff."
    return "\n".join(
        [
            "# Git diff summary",
            "",
            "## Status",
            "",
            "```",
            status_text,
            "```",
            "",
            "## Diff stat",
            "",
            "```",
            stat_text,
            "```",
            "",
        ]
    )


def _validate_path_component(value: str, name: str) -> None:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"{name} must be a single path component")


def _git(
    args: Sequence[str],
    cwd: Path,
    ok_returncodes: Iterable[int] = (0,),
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in set(ok_returncodes):
        command = " ".join(["git", "-C", str(cwd), *args])
        details = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"{command} failed with {completed.returncode}: {details}")
    return completed.stdout


def _git_ok(args: Sequence[str], cwd: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _git_z(args: Sequence[str], cwd: Path) -> List[str]:
    output = _git(args, cwd=cwd)
    return sorted(item for item in output.split("\0") if item)
