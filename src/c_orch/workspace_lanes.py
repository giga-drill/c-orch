from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


Pathish = Union[str, Path]


class WorkspaceResolutionError(ValueError):
    pass


class WorkspaceStatusCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkspaceLane:
    workspace_id: str
    root: Path
    slug: str


@dataclass(frozen=True)
class WorkspaceCleanStatus:
    root: Path
    clean: bool
    status_output: str
    command: str


def resolve_workspace_lane(path: Pathish) -> WorkspaceLane:
    root = canonical_git_root(path)
    return WorkspaceLane(
        workspace_id=str(root),
        root=root,
        slug=workspace_slug(root),
    )


def canonical_git_root(path: Pathish) -> Path:
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise WorkspaceResolutionError(f"cwd does not exist: {target}")
    if not target.is_dir():
        raise WorkspaceResolutionError(f"cwd must be a directory: {target}")
    result = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise WorkspaceResolutionError(f"cwd must be inside a Git repository: {target}")
    root = result.stdout.strip()
    if not root:
        raise WorkspaceResolutionError(f"Git repository root was empty for cwd: {target}")
    return Path(root).expanduser().resolve()


def workspace_slug(root: Pathish) -> str:
    resolved = Path(root).expanduser().resolve()
    name = resolved.name or "workspace"
    safe_name = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in name)
    safe_name = safe_name.strip("-_") or "workspace"
    digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"{safe_name}-{digest}"


def workspace_worktrees_dir(
    *,
    controller_cwd: Pathish,
    configured_worktrees_dir: Pathish,
    workspace_root: Pathish,
) -> Path:
    configured = Path(configured_worktrees_dir).expanduser()
    if configured.is_absolute():
        base = configured.resolve()
    else:
        base = (Path(controller_cwd).expanduser().resolve() / configured).resolve()
    return base / workspace_slug(workspace_root)


def optional_workspace_lane(path: Optional[Pathish]) -> Optional[WorkspaceLane]:
    if path is None:
        return None
    return resolve_workspace_lane(path)


def workspace_clean_status(path: Pathish) -> WorkspaceCleanStatus:
    root = canonical_git_root(path)
    command_parts = ["git", "-C", str(root), "status", "--short"]
    result = subprocess.run(
        command_parts,
        check=False,
        text=True,
        capture_output=True,
    )
    command = " ".join(command_parts)
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit code {result.returncode}"
        raise WorkspaceStatusCommandError(
            f"failed to run `{command}`: {detail}"
        )
    status_output = (result.stdout or "").rstrip()
    return WorkspaceCleanStatus(
        root=root,
        clean=not bool(status_output.strip()),
        status_output=status_output,
        command=command,
    )
