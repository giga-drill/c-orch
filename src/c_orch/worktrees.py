from __future__ import annotations

import subprocess
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Union


Pathish = Union[str, Path]


@dataclass(frozen=True)
class DiffEvidence:
    summary_path: Path
    patch_path: Path
    summary: str
    patch: str
    changed_paths: List[str] = field(default_factory=list)

    @property
    def evidence_files(self) -> List[str]:
        return [str(self.summary_path), str(self.patch_path)]


@dataclass(frozen=True)
class ApplyReport:
    applied: bool
    summary: str
    output_path: Path

    @property
    def evidence_files(self) -> List[str]:
        return [str(self.output_path)]


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
        changed_paths = _git_z(["diff", "--name-only", "-z", "HEAD", "--"], cwd=worktree)
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
        changed_paths=changed_paths,
    )


def apply_diff_evidence_to_repo(
    diff: DiffEvidence,
    target_repo_path: Pathish,
    evidence_dir: Pathish,
) -> ApplyReport:
    target_repo = Path(target_repo_path).expanduser().resolve()
    output_dir = Path(evidence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "git-apply-output.txt"

    status_result = _git_process(["status", "--short"], cwd=target_repo)
    check_result = _git_process(
        ["apply", "--check", "--binary", "-"],
        cwd=target_repo,
        stdin=diff.patch,
    )
    patch_is_empty = not diff.patch.strip()
    empty_patch_noop = patch_is_empty and _is_empty_patch_failure(check_result)

    apply_result: Optional[subprocess.CompletedProcess[str]] = None
    applied = check_result.returncode == 0 or empty_patch_noop
    if check_result.returncode == 0 and not patch_is_empty:
        apply_result = _git_process(
            ["apply", "--binary", "-"],
            cwd=target_repo,
            stdin=diff.patch,
        )
        applied = apply_result.returncode == 0

    summary = "Patch applied successfully."
    if patch_is_empty:
        summary = "No-op: empty patch."
    if check_result.returncode != 0 and not empty_patch_noop:
        summary = "Failed: git apply --check rejected the patch."
    elif apply_result is not None and apply_result.returncode != 0:
        summary = "Failed: git apply could not apply the patch."

    output_path.write_text(
        _format_apply_output(
            target_repo=target_repo,
            diff=diff,
            status_result=status_result,
            check_result=check_result,
            apply_result=apply_result,
            summary=summary,
        ),
        encoding="utf-8",
    )
    return ApplyReport(
        applied=applied,
        summary=summary,
        output_path=output_path,
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


def _git_process(
    args: Sequence[str],
    cwd: Path,
    stdin: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _format_apply_output(
    *,
    target_repo: Path,
    diff: DiffEvidence,
    status_result: subprocess.CompletedProcess[str],
    check_result: subprocess.CompletedProcess[str],
    apply_result: Optional[subprocess.CompletedProcess[str]],
    summary: str,
) -> str:
    sections = OrderedDict()
    sections["Summary"] = summary
    sections["Target repo"] = str(target_repo)
    sections["Patch path"] = str(diff.patch_path)
    sections["Patch bytes"] = str(len(diff.patch.encode("utf-8")))
    sections["Git status --short before apply"] = status_result.stdout.rstrip() or "(clean)"

    lines = ["# Git apply output", ""]
    for key, value in sections.items():
        lines.append("{key}: {value}".format(key=key, value=value))
    lines.append("")
    lines.extend(
        _format_command_result(
            command=["apply", "--check", "--binary", "-"],
            cwd=target_repo,
            result=check_result,
        )
    )
    if apply_result is not None:
        lines.append("")
        lines.extend(
            _format_command_result(
                command=["apply", "--binary", "-"],
                cwd=target_repo,
                result=apply_result,
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_command_result(
    *,
    command: Sequence[str],
    cwd: Path,
    result: subprocess.CompletedProcess[str],
) -> List[str]:
    return [
        "[command]",
        "git -C {cwd} {args}".format(
            cwd=str(cwd),
            args=" ".join(command),
        ),
        "returncode: {code}".format(code=result.returncode),
        "stdout:",
        result.stdout.rstrip() or "(empty)",
        "stderr:",
        result.stderr.rstrip() or "(empty)",
    ]


def _is_empty_patch_failure(result: subprocess.CompletedProcess[str]) -> bool:
    combined = "\n".join(
        [result.stdout or "", result.stderr or ""]
    )
    return "No valid patches in input" in combined
