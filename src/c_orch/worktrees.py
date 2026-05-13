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


@dataclass(frozen=True)
class GitCommitReport:
    status: str
    summary: str
    output_path: Path
    message_path: Path
    commit_hash: Optional[str] = None
    hash_path: Optional[Path] = None
    failure_reason: Optional[str] = None

    @property
    def committed(self) -> bool:
        return self.status == "committed"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def failed(self) -> bool:
        return self.status == "failed"

    @property
    def evidence_files(self) -> List[str]:
        files = [str(self.message_path), str(self.output_path)]
        if self.hash_path is not None:
            files.append(str(self.hash_path))
        return files


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


def collect_repo_changed_paths(repo_path: Pathish) -> List[str]:
    repo = Path(repo_path).expanduser().resolve()
    tracked = _git_z(["diff", "--name-only", "-z", "HEAD", "--"], cwd=repo)
    untracked = _git_z(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo)
    return sorted(_normalize_path_set([*tracked, *untracked]))


def collect_repo_staged_paths(repo_path: Pathish) -> List[str]:
    repo = Path(repo_path).expanduser().resolve()
    return sorted(_normalize_path_set(_git_z(["diff", "--cached", "--name-only", "-z", "--"], cwd=repo)))


def commit_applied_changes(
    *,
    target_repo_path: Pathish,
    evidence_dir: Pathish,
    run_id: str,
    worker_id: str,
    planner_review_decision: str,
    user_task: str,
    plan_summary: Optional[str],
    review_reason: Optional[str],
    changed_paths: Sequence[str],
    pre_apply_changed_paths: Sequence[str],
    pre_apply_staged_paths: Optional[Sequence[str]] = None,
    post_apply_changed_paths: Optional[Sequence[str]] = None,
) -> GitCommitReport:
    target_repo = Path(target_repo_path).expanduser().resolve()
    output_dir = Path(evidence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    message_path = output_dir / "git-commit-message.txt"
    output_path = output_dir / "git-commit-output.txt"
    hash_path = output_dir / "git-commit-hash.txt"

    commit_message = _build_commit_message(
        run_id=run_id,
        worker_id=worker_id,
        planner_review_decision=planner_review_decision,
        user_task=user_task,
        plan_summary=plan_summary,
        review_reason=review_reason,
    )
    message_path.write_text(commit_message, encoding="utf-8")

    planned_paths = _normalize_path_set(changed_paths)
    preexisting_paths = _normalize_path_set(pre_apply_changed_paths)
    preexisting_staged_paths = _normalize_path_set(
        pre_apply_staged_paths
        if pre_apply_staged_paths is not None
        else collect_repo_staged_paths(target_repo)
    )
    post_paths = _normalize_path_set(
        post_apply_changed_paths
        if post_apply_changed_paths is not None
        else collect_repo_changed_paths(target_repo)
    )

    if planner_review_decision != "accepted":
        summary = "Failed: commit is only allowed for accepted Planner review decisions."
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="invalid_review_decision",
        )

    if preexisting_staged_paths:
        summary = (
            "Failed: repository has preexisting staged changes before commit. "
            "Refusing to commit mixed staged state."
        )
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="preexisting_staged_changes",
        )

    overlapping_paths = sorted(planned_paths & preexisting_paths)
    if overlapping_paths:
        summary = (
            "Failed: preexisting repository changes overlap with applied patch paths. "
            "Refusing to commit mixed provenance."
        )
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
                overlapping_paths=overlapping_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="preexisting_overlap",
        )

    unexpected_paths = sorted(post_paths - preexisting_paths - planned_paths)
    if unexpected_paths:
        summary = (
            "Failed: detected changed paths that are neither preexisting nor part of the "
            "applied patch."
        )
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
                unexpected_paths=unexpected_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="unexpected_paths_after_apply",
        )

    stage_paths = sorted(path for path in planned_paths if path in post_paths)
    if not stage_paths:
        summary = "Skipped: apply succeeded but there are no commit-worthy changes in planned paths."
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="skipped",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
        )

    add_result = _git_process(["add", "--", *stage_paths], cwd=target_repo)
    if add_result.returncode != 0:
        summary = "Failed: git add returned a non-zero exit code."
        output_path.write_text(
            _format_git_commit_execution_output(
                target_repo=target_repo,
                summary=summary,
                add_result=add_result,
                commit_result=None,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="git_add_failed",
        )

    staged_paths = _git_z(["diff", "--cached", "--name-only", "-z", "--"], cwd=target_repo)
    staged_path_set = _normalize_path_set(staged_paths)
    unexpected_staged_paths = sorted(staged_path_set - planned_paths)
    if unexpected_staged_paths:
        summary = (
            "Failed: staged paths include files outside planned patch paths before commit."
        )
        output_path.write_text(
            _format_git_commit_precheck_output(
                target_repo=target_repo,
                summary=summary,
                planned_paths=planned_paths,
                preexisting_paths=preexisting_paths,
                preexisting_staged_paths=preexisting_staged_paths,
                post_paths=post_paths,
                post_add_staged_paths=staged_path_set,
                unexpected_staged_paths=unexpected_staged_paths,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="staged_paths_outside_planned",
        )

    if not staged_paths:
        summary = "Skipped: no staged changes after git add."
        output_path.write_text(
            _format_git_commit_execution_output(
                target_repo=target_repo,
                summary=summary,
                add_result=add_result,
                commit_result=None,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="skipped",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
        )

    commit_result = _git_process(["commit", "-F", str(message_path)], cwd=target_repo)
    if commit_result.returncode != 0:
        summary = "Failed: git commit returned a non-zero exit code."
        output_path.write_text(
            _format_git_commit_execution_output(
                target_repo=target_repo,
                summary=summary,
                add_result=add_result,
                commit_result=commit_result,
            ),
            encoding="utf-8",
        )
        return GitCommitReport(
            status="failed",
            summary=summary,
            output_path=output_path,
            message_path=message_path,
            failure_reason="git_commit_failed",
        )

    commit_hash = _git(["rev-parse", "HEAD"], cwd=target_repo).strip()
    hash_path.write_text(f"{commit_hash}\n", encoding="utf-8")
    summary = "Committed applied changes."
    output_path.write_text(
        _format_git_commit_execution_output(
            target_repo=target_repo,
            summary=summary,
            add_result=add_result,
            commit_result=commit_result,
        ),
        encoding="utf-8",
    )
    return GitCommitReport(
        status="committed",
        summary=summary,
        output_path=output_path,
        message_path=message_path,
        commit_hash=commit_hash,
        hash_path=hash_path,
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


def _normalize_path_set(paths: Sequence[str]) -> set[str]:
    return {
        _normalize_repo_path(path)
        for path in paths
        if _normalize_repo_path(path)
    }


def _normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip()


def _build_commit_message(
    *,
    run_id: str,
    worker_id: str,
    planner_review_decision: str,
    user_task: str,
    plan_summary: Optional[str],
    review_reason: Optional[str],
) -> str:
    headline_source = (plan_summary or _first_nonempty_line(user_task) or "Apply reviewed worker patch").strip()
    headline = _truncate_line(headline_source, 72)
    task_line = _truncate_line(_first_nonempty_line(user_task) or headline_source, 120)
    summary_line = _truncate_line((plan_summary or headline_source).strip(), 120)
    lines = [
        headline,
        "",
        f"Run-ID: {run_id}",
        f"Worker-ID: {worker_id}",
        f"Planner-Review: {planner_review_decision}",
        f"Task: {task_line}",
        f"Plan-Summary: {summary_line}",
    ]
    if review_reason:
        lines.append(f"Review-Reason: {_truncate_line(review_reason.strip(), 160)}")
    return "\n".join(lines).rstrip() + "\n"


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def _truncate_line(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 1)].rstrip() + "..."


def _format_git_commit_precheck_output(
    *,
    target_repo: Path,
    summary: str,
    planned_paths: set[str],
    preexisting_paths: set[str],
    preexisting_staged_paths: set[str],
    post_paths: set[str],
    overlapping_paths: Optional[List[str]] = None,
    unexpected_paths: Optional[List[str]] = None,
    post_add_staged_paths: Optional[set[str]] = None,
    unexpected_staged_paths: Optional[List[str]] = None,
) -> str:
    lines = [
        "# Git commit output",
        "",
        f"Summary: {summary}",
        f"Target repo: {target_repo}",
        "",
        "Planned paths:",
        *(sorted(planned_paths) or ["(none)"]),
        "",
        "Pre-apply changed paths:",
        *(sorted(preexisting_paths) or ["(none)"]),
        "",
        "Pre-apply staged paths:",
        *(sorted(preexisting_staged_paths) or ["(none)"]),
        "",
        "Post-apply changed paths:",
        *(sorted(post_paths) or ["(none)"]),
    ]
    if overlapping_paths is not None:
        lines.extend(["", "Overlapping paths:", *(overlapping_paths or ["(none)"])])
    if unexpected_paths is not None:
        lines.extend(["", "Unexpected paths:", *(unexpected_paths or ["(none)"])])
    if post_add_staged_paths is not None:
        lines.extend(["", "Staged paths before commit:", *(sorted(post_add_staged_paths) or ["(none)"])])
    if unexpected_staged_paths is not None:
        lines.extend(
            [
                "",
                "Unexpected staged paths:",
                *(unexpected_staged_paths or ["(none)"]),
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _format_git_commit_execution_output(
    *,
    target_repo: Path,
    summary: str,
    add_result: subprocess.CompletedProcess[str],
    commit_result: Optional[subprocess.CompletedProcess[str]],
) -> str:
    lines = [
        "# Git commit output",
        "",
        f"Summary: {summary}",
        f"Target repo: {target_repo}",
        "",
    ]
    lines.extend(
        _format_command_result(
            command=["add", "--"],
            cwd=target_repo,
            result=add_result,
        )
    )
    if commit_result is not None:
        lines.append("")
        lines.extend(
            _format_command_result(
                command=["commit", "-F", "<message-file>"],
                cwd=target_repo,
                result=commit_result,
            )
        )
    return "\n".join(lines).rstrip() + "\n"
