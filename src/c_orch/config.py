from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

from .settings import (
    APPROVAL_POLICY_CHOICES,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_PLANNER_MODELS,
    DEFAULT_RUNS_DIR,
    DEFAULT_SANDBOX,
    DEFAULT_UI_HOST,
    DEFAULT_UI_PORT,
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKTREES_DIR,
    REASONING_EFFORT_CHOICES,
    SANDBOX_CHOICES,
    SERVICE_TIER_CHOICES,
)


Pathish = Union[str, Path]
DEFAULT_CONFIG_FILE = ".c-orch.toml"


@dataclass(frozen=True)
class RoleConfig:
    model: Optional[str] = None
    preferred_models: List[str] = field(default_factory=list)
    reasoning_effort: Optional[str] = None
    service_tier: Optional[str] = None


@dataclass(frozen=True)
class RunConfig:
    runs_dir: str = DEFAULT_RUNS_DIR
    worktrees_dir: str = DEFAULT_WORKTREES_DIR
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL_POLICY


@dataclass(frozen=True)
class UiConfig:
    runs_dir: str = DEFAULT_RUNS_DIR
    host: str = DEFAULT_UI_HOST
    port: int = DEFAULT_UI_PORT


@dataclass(frozen=True)
class ProjectConfig:
    path: Optional[Path] = None
    codex_bin: Optional[str] = None
    planner: RoleConfig = field(default_factory=RoleConfig)
    worker: RoleConfig = field(default_factory=RoleConfig)
    run: RunConfig = field(default_factory=RunConfig)
    ui: UiConfig = field(default_factory=UiConfig)

    @property
    def planner_preferred_models(self) -> List[str]:
        if self.planner.preferred_models:
            return list(self.planner.preferred_models)
        if self.planner.model:
            return [self.planner.model]
        return list(DEFAULT_PLANNER_MODELS)


def load_project_config(
    *,
    cwd: Pathish,
    config_path: Optional[Pathish] = None,
) -> ProjectConfig:
    path = _resolve_config_path(Path(cwd), config_path)
    if path is None or not path.exists():
        return ProjectConfig(path=path)
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"config file must contain a TOML table: {path}")
    return _parse_project_config(payload, path=path)


def _resolve_config_path(cwd: Path, config_path: Optional[Pathish]) -> Optional[Path]:
    if config_path:
        path = Path(config_path).expanduser()
        return path if path.is_absolute() else cwd / path
    return cwd / DEFAULT_CONFIG_FILE


def _parse_project_config(payload: Mapping[str, Any], *, path: Path) -> ProjectConfig:
    planner = _role_config(_table(payload, "planner"), role="planner")
    worker = _role_config(
        _table(payload, "worker"),
        role="worker",
        default_model=DEFAULT_WORKER_MODEL,
    )
    return ProjectConfig(
        path=path,
        codex_bin=_optional_string(_table(payload, "codex").get("bin"), "codex.bin"),
        planner=planner,
        worker=worker,
        run=_run_config(_table(payload, "run")),
        ui=_ui_config(_table(payload, "ui")),
    )


def _role_config(
    data: Mapping[str, Any],
    *,
    role: str,
    default_model: Optional[str] = None,
) -> RoleConfig:
    model = _optional_string(data.get("model"), f"{role}.model") or default_model
    preferred_models = _string_list(data.get("preferred_models"), f"{role}.preferred_models")
    reasoning_effort = _choice(
        data.get("reasoning_effort"),
        f"{role}.reasoning_effort",
        REASONING_EFFORT_CHOICES,
    )
    service_tier = _choice(data.get("service_tier"), f"{role}.service_tier", SERVICE_TIER_CHOICES)
    return RoleConfig(
        model=model,
        preferred_models=preferred_models,
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
    )


def _run_config(data: Mapping[str, Any]) -> RunConfig:
    return RunConfig(
        runs_dir=_string(data.get("runs_dir"), "run.runs_dir", DEFAULT_RUNS_DIR),
        worktrees_dir=_string(data.get("worktrees_dir"), "run.worktrees_dir", DEFAULT_WORKTREES_DIR),
        max_attempts=_positive_int(data.get("max_attempts"), "run.max_attempts", DEFAULT_MAX_ATTEMPTS),
        sandbox=_choice(data.get("sandbox"), "run.sandbox", SANDBOX_CHOICES, DEFAULT_SANDBOX),
        approval_policy=_choice(
            data.get("approval_policy"),
            "run.approval_policy",
            APPROVAL_POLICY_CHOICES,
            DEFAULT_APPROVAL_POLICY,
        ),
    )


def _ui_config(data: Mapping[str, Any]) -> UiConfig:
    return UiConfig(
        runs_dir=_string(data.get("runs_dir"), "ui.runs_dir", DEFAULT_RUNS_DIR),
        host=_string(data.get("host"), "ui.host", DEFAULT_UI_HOST),
        port=_positive_int(data.get("port"), "ui.port", DEFAULT_UI_PORT),
    )


def _table(payload: Mapping[str, Any], key: str) -> Dict[str, Any]:
    value = payload.get(key, {})
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be a TOML table")
    return value


def _string(value: Any, name: str, default: str) -> str:
    if value is None:
        return default
    parsed = _optional_string(value, name)
    if parsed is None:
        return default
    return parsed


def _optional_string(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} cannot be empty")
    return value


def _string_list(value: Any, name: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return list(value)


def _choice(
    value: Any,
    name: str,
    choices: tuple[str, ...],
    default: Optional[str] = None,
) -> Optional[str]:
    if value is None:
        return default
    parsed = _optional_string(value, name)
    if parsed not in choices:
        allowed = ", ".join(choices)
        raise ValueError(f"{name} must be one of: {allowed}")
    return parsed


def _positive_int(value: Any, name: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value
