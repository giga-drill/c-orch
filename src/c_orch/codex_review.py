from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence, Union


Pathish = Union[str, Path]


@dataclass(frozen=True)
class CodexReviewReport:
    summary: str
    output_path: Path
    result_path: Path
    status: str
    returncode: Optional[int]
    command: str

    @property
    def evidence_files(self) -> list[str]:
        return [str(self.output_path), str(self.result_path)]


def run_codex_uncommitted_review(
    *,
    cwd: Pathish,
    evidence_dir: Pathish,
    codex_binary_path: Optional[str] = None,
    patch_path: Optional[Pathish] = None,
    timeout_seconds: float = 300.0,
) -> CodexReviewReport:
    worktree = Path(cwd).expanduser().resolve()
    output_dir = Path(evidence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "codex-review-output.txt"
    result_path = output_dir / "codex-review-result.json"

    codex_bin = codex_binary_path or "codex"
    command_parts = [codex_bin, "review", "--uncommitted"]
    command_text = " ".join(command_parts)

    status = "failed"
    returncode: Optional[int] = None
    stdout = ""
    stderr = ""
    error: Optional[str] = None
    summary = "Codex review finished with findings or failures."
    review_workspace = worktree
    review_workspace_mode = "live_worktree"
    prep_summary = "Used live worktree for review."
    cleanup: Optional[Callable[[], None]] = None

    if patch_path is not None:
        prep = _prepare_review_workspace(
            worktree=worktree,
            patch_path=Path(patch_path).expanduser().resolve(),
            output_dir=output_dir,
            timeout_seconds=timeout_seconds,
        )
        if prep.error:
            status = "error"
            summary = "Codex review could not prepare isolated workspace from captured patch."
            error = prep.error
        else:
            review_workspace = prep.workspace
            review_workspace_mode = "isolated_patch_worktree"
            prep_summary = prep.summary
            cleanup = prep.cleanup

    try:
        if status != "error":
            completed = subprocess.run(
                command_parts,
                cwd=review_workspace,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
            if completed.returncode == 0:
                status = "passed"
                summary = "Codex review completed successfully."
            else:
                status = "error"
                error = f"codex review exited with code {completed.returncode}"
                summary = f"Codex review exited with code {completed.returncode}."
    except FileNotFoundError:
        status = "error"
        error = f"binary not found: {codex_bin}"
        summary = "Codex review could not start because the Codex binary was not found."
    except PermissionError as exc:
        status = "error"
        error = f"permission denied: {exc}"
        summary = "Codex review could not start due to a permission error."
    except subprocess.TimeoutExpired as exc:
        status = "error"
        returncode = None
        stdout = _coerce_text(exc.stdout)
        stderr = _coerce_text(exc.stderr)
        error = f"timed out after {timeout_seconds:g}s"
        summary = f"Codex review timed out after {timeout_seconds:g}s."
    except OSError as exc:
        status = "error"
        error = str(exc)
        summary = "Codex review failed to launch."
    finally:
        if cleanup is not None:
            cleanup()

    output_path.write_text(
        _format_output(
            command_text=command_text,
            cwd=worktree,
            status=status,
            returncode=returncode,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            error=error,
            review_workspace=review_workspace,
            review_workspace_mode=review_workspace_mode,
            prep_summary=prep_summary,
        ),
        encoding="utf-8",
    )

    result_payload = {
        "command": command_parts,
        "cwd": str(worktree),
        "status": status,
        "returncode": returncode,
        "summary": summary,
        "output_path": str(output_path),
        "error": error,
        "review_workspace_mode": review_workspace_mode,
        "review_workspace": str(review_workspace),
        "prep_summary": prep_summary,
    }
    result_path.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return CodexReviewReport(
        summary=summary,
        output_path=output_path,
        result_path=result_path,
        status=status,
        returncode=returncode,
        command=command_text,
    )


def _format_output(
    *,
    command_text: str,
    cwd: Path,
    status: str,
    returncode: Optional[int],
    summary: str,
    stdout: str,
    stderr: str,
    error: Optional[str],
    review_workspace: Path,
    review_workspace_mode: str,
    prep_summary: str,
) -> str:
    returncode_text = "n/a" if returncode is None else str(returncode)
    lines = [
        f"$ {command_text}",
        f"cwd: {cwd}",
        f"review_workspace_mode: {review_workspace_mode}",
        f"review_workspace: {review_workspace}",
        f"review_workspace_prep: {prep_summary}",
        f"status: {status}",
        f"returncode: {returncode_text}",
        f"summary: {summary}",
    ]
    if error:
        lines.append(f"error: {error}")
    lines.extend(
        [
            "",
            "stdout:",
            stdout.rstrip() or "<empty>",
            "",
            "stderr:",
            stderr.rstrip() or "<empty>",
            "",
        ]
    )
    return "\n".join(lines)


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass(frozen=True)
class _WorkspacePrepResult:
    workspace: Path
    summary: str
    cleanup: Optional[Callable[[], None]] = None
    error: Optional[str] = None


def _prepare_review_workspace(
    *,
    worktree: Path,
    patch_path: Path,
    output_dir: Path,
    timeout_seconds: float,
) -> _WorkspacePrepResult:
    if not patch_path.exists():
        return _WorkspacePrepResult(
            workspace=worktree,
            summary="Captured patch file was missing; used live worktree.",
            cleanup=None,
            error=f"missing patch file: {patch_path}",
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="codex-review-", dir=output_dir.parent))
    add_result = _run_command(
        ["git", "worktree", "add", "--detach", str(temp_dir), "HEAD"],
        cwd=worktree,
        timeout_seconds=timeout_seconds,
    )
    if add_result.returncode != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return _WorkspacePrepResult(
            workspace=worktree,
            summary="Failed to create isolated review workspace.",
            error=f"git worktree add failed (exit {add_result.returncode}): {_first_line(add_result.stderr)}",
        )

    patch = patch_path.read_text(encoding="utf-8")
    apply_error = _apply_patch_in_workspace(
        workspace=temp_dir,
        patch=patch,
        timeout_seconds=timeout_seconds,
    )
    if apply_error is not None:
        _cleanup_review_workspace(worktree=worktree, temp_dir=temp_dir)
        return _WorkspacePrepResult(
            workspace=worktree,
            summary="Failed to apply captured patch in isolated workspace.",
            error=apply_error,
        )

    return _WorkspacePrepResult(
        workspace=temp_dir,
        summary=f"Prepared isolated review workspace from {patch_path.name}.",
        cleanup=lambda: _cleanup_review_workspace(worktree=worktree, temp_dir=temp_dir),
    )


def _apply_patch_in_workspace(
    *,
    workspace: Path,
    patch: str,
    timeout_seconds: float,
) -> Optional[str]:
    if not patch.strip():
        return None

    check = _run_command(
        ["git", "apply", "--check", "--binary", "-"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=patch,
    )
    if check.returncode == 0:
        apply_result = _run_command(
            ["git", "apply", "--binary", "-"],
            cwd=workspace,
            timeout_seconds=timeout_seconds,
            stdin=patch,
        )
        if apply_result.returncode == 0:
            return None
        return f"git apply failed (exit {apply_result.returncode}): {_first_line(apply_result.stderr)}"

    check_3way = _run_command(
        ["git", "apply", "--3way", "--check", "--binary", "-"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stdin=patch,
    )
    if check_3way.returncode == 0:
        apply_3way = _run_command(
            ["git", "apply", "--3way", "--binary", "-"],
            cwd=workspace,
            timeout_seconds=timeout_seconds,
            stdin=patch,
        )
        if apply_3way.returncode == 0:
            return None
        return f"git apply --3way failed (exit {apply_3way.returncode}): {_first_line(apply_3way.stderr)}"

    return f"git apply --check failed (exit {check.returncode}): {_first_line(check.stderr)}"


def _cleanup_review_workspace(*, worktree: Path, temp_dir: Path) -> None:
    _run_command(
        ["git", "worktree", "remove", "--force", str(temp_dir)],
        cwd=worktree,
        timeout_seconds=30.0,
    )
    shutil.rmtree(temp_dir, ignore_errors=True)


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    stdin: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _first_line(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "<empty>"
    return stripped.splitlines()[0]
