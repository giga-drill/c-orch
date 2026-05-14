from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Union


Pathish = Union[str, Path]


@dataclass(frozen=True)
class CommandVerification:
    command: str
    status: str
    returncode: Optional[int]
    summary: str

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "status": self.status,
            "returncode": self.returncode,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class VerificationReport:
    summary: str
    output_path: Path
    results: List[CommandVerification]

    @property
    def evidence_files(self) -> List[str]:
        return [str(self.output_path)]


def run_verification_commands(
    commands: Iterable[str],
    *,
    cwd: Pathish,
    evidence_dir: Pathish,
    timeout_seconds: float = 300.0,
) -> VerificationReport:
    worktree = Path(cwd).expanduser().resolve()
    output_dir = Path(evidence_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "verification-output.txt"

    command_list = [command.strip() for command in commands if command.strip()]
    results: List[CommandVerification] = []
    log_sections: List[str] = []

    setup = _run_frontend_dependency_setup(
        worktree=worktree,
        commands=command_list,
        timeout_seconds=timeout_seconds,
    )
    if setup is not None:
        setup_result, setup_log = setup
        log_sections.append(setup_log)
        if setup_result.status != "passed":
            output_path.write_text("\n\n".join(log_sections) + "\n", encoding="utf-8")
            return VerificationReport(
                summary="Frontend dependency setup failed.",
                output_path=output_path,
                results=[setup_result],
            )

    for command in command_list:
        result, log = _run_one_command(
            command,
            cwd=worktree,
            timeout_seconds=timeout_seconds,
        )
        results.append(result)
        log_sections.append(log)

    if not results:
        summary = "No verification commands configured."
        output_path.write_text(summary + "\n", encoding="utf-8")
        return VerificationReport(
            summary=summary,
            output_path=output_path,
            results=[],
        )

    failed = [item for item in results if item.status != "passed"]
    if failed:
        summary = f"{len(failed)} of {len(results)} verification command(s) failed."
    else:
        summary = f"All {len(results)} verification command(s) passed."

    output_path.write_text("\n\n".join(log_sections) + "\n", encoding="utf-8")
    return VerificationReport(
        summary=summary,
        output_path=output_path,
        results=results,
    )


def _run_frontend_dependency_setup(
    *,
    worktree: Path,
    commands: List[str],
    timeout_seconds: float,
) -> Optional[tuple[CommandVerification, str]]:
    if not _needs_frontend_dependency_setup(worktree=worktree, commands=commands):
        return None
    return _run_one_command(
        "pnpm --dir web install --frozen-lockfile",
        cwd=worktree,
        timeout_seconds=timeout_seconds,
    )


def _needs_frontend_dependency_setup(*, worktree: Path, commands: List[str]) -> bool:
    if not commands:
        return False
    if not (worktree / "web" / "pnpm-lock.yaml").is_file():
        return False
    return any(_looks_like_web_package_command(command) for command in commands)


def _looks_like_web_package_command(command: str) -> bool:
    normalized = " ".join(command.split())
    markers = (
        "cd web && npm run ",
        "cd web && pnpm run ",
        "npm --prefix web run ",
        "npm --prefix=web run ",
        "pnpm --dir web run ",
        "pnpm --dir=web run ",
        "pnpm -C web run ",
    )
    return any(marker in normalized for marker in markers)


def _run_one_command(
    command: str,
    *,
    cwd: Path,
    timeout_seconds: float,
) -> tuple[CommandVerification, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _coerce_process_text(exc.stdout)
        stderr = _coerce_process_text(exc.stderr)
        result = CommandVerification(
            command=command,
            status="failed",
            returncode=None,
            summary=f"timed out after {timeout_seconds:g}s",
        )
        return result, _format_log(
            command=command,
            returncode=None,
            stdout=stdout,
            stderr=stderr,
            extra=result.summary,
        )

    status = "passed" if completed.returncode == 0 else "failed"
    result = CommandVerification(
        command=command,
        status=status,
        returncode=completed.returncode,
        summary=f"exit {completed.returncode}",
    )
    return result, _format_log(
        command=command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _format_log(
    *,
    command: str,
    returncode: Optional[int],
    stdout: str,
    stderr: str,
    extra: Optional[str] = None,
) -> str:
    returncode_text = "timeout" if returncode is None else str(returncode)
    lines = [
        f"$ {command}",
        f"returncode: {returncode_text}",
    ]
    if extra:
        lines.append(extra)
    lines.extend(
        [
            "",
            "stdout:",
            stdout.rstrip() or "<empty>",
            "",
            "stderr:",
            stderr.rstrip() or "<empty>",
        ]
    )
    return "\n".join(lines)


def _coerce_process_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
