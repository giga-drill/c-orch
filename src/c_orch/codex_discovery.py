from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .settings import INTERESTING_MODEL_SLUGS

C_ORCH_CODEX_BIN_ENV = "C_ORCH_CODEX_BIN"
MACOS_CODEX_PATH = "/Applications/Codex.app/Contents/Resources/codex"
SUBPROCESS_TIMEOUT_SECONDS = 10.0

_MODEL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
_MODEL_FIELD_NAMES = frozenset({"slug", "id", "model"})


@dataclass(frozen=True)
class CodexCandidateReport:
    path: str
    source: str
    version: Optional[str] = None
    interesting_models: Tuple[str, ...] = field(default_factory=tuple)
    usable: bool = False
    error: Optional[str] = None

    def has_model(self, slug: str) -> bool:
        return slug in self.interesting_models

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "source": self.source,
            "version": self.version,
            "interesting_models": list(self.interesting_models),
            "usable": self.usable,
            "error": self.error,
        }


@dataclass(frozen=True)
class CodexEnvironmentReport:
    candidates: Tuple[CodexCandidateReport, ...] = field(default_factory=tuple)
    selected: Optional[CodexCandidateReport] = None

    def has_model(self, slug: str) -> bool:
        return bool(self.selected and self.selected.has_model(slug))

    def to_dict(self) -> dict:
        return {
            "selected": self.selected.to_dict() if self.selected else None,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


def choose_first_available_model(
    candidate: CodexCandidateReport,
    preferred_models: Sequence[str],
) -> Optional[str]:
    for model in preferred_models:
        if candidate.has_model(model):
            return model
    return None


@dataclass(frozen=True)
class _CandidateSpec:
    path: str
    source: str


@dataclass(frozen=True)
class _CommandResult:
    ok: bool
    stdout: str = ""
    error: Optional[str] = None


def inspect_codex_environment(
    explicit_codex_bin: str | None = None,
) -> CodexEnvironmentReport:
    candidates = tuple(
        _inspect_candidate(spec, timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS)
        for spec in _candidate_specs(explicit_codex_bin)
    )
    selected = _select_candidate(candidates)
    return CodexEnvironmentReport(candidates=candidates, selected=selected)


def _candidate_specs(explicit_codex_bin: str | None) -> List[_CandidateSpec]:
    specs: List[_CandidateSpec] = []

    if explicit_codex_bin is not None:
        specs.append(_CandidateSpec(_normalize_path(explicit_codex_bin), "explicit"))

    env_codex_bin = os.environ.get(C_ORCH_CODEX_BIN_ENV)
    if env_codex_bin:
        specs.append(_CandidateSpec(_normalize_path(env_codex_bin), "env"))

    specs.append(_CandidateSpec(_normalize_path(MACOS_CODEX_PATH), "macos_app"))

    for path_candidate in _path_codex_candidates(os.environ.get("PATH", "")):
        specs.append(_CandidateSpec(_normalize_path(path_candidate), "path"))

    return _dedupe_specs(specs)


def _normalize_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _dedupe_specs(specs: Sequence[_CandidateSpec]) -> List[_CandidateSpec]:
    deduped: List[_CandidateSpec] = []
    seen = set()
    for spec in specs:
        key = os.path.realpath(spec.path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(spec)
    return deduped


def _path_codex_candidates(path_value: str) -> Iterable[str]:
    for directory in path_value.split(os.pathsep):
        search_dir = directory or os.curdir
        candidate = os.path.join(search_dir, "codex")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            yield candidate


def _inspect_candidate(
    spec: _CandidateSpec,
    timeout_seconds: float,
) -> CodexCandidateReport:
    errors: List[str] = []
    version = None
    interesting_models: Tuple[str, ...] = ()

    version_result = _run_codex_command(spec.path, ["--version"], timeout_seconds)
    if version_result.ok:
        version = _first_stdout_line(version_result.stdout)
    elif version_result.error:
        errors.append(f"--version: {version_result.error}")

    models_result = _run_codex_command(spec.path, ["debug", "models"], timeout_seconds)
    if models_result.ok:
        try:
            parsed_models = json.loads(models_result.stdout)
        except json.JSONDecodeError as exc:
            errors.append(f"debug models: invalid JSON: {exc.msg}")
        else:
            interesting_models = tuple(_collect_interesting_models(parsed_models))
    elif models_result.error:
        errors.append(f"debug models: {models_result.error}")

    return CodexCandidateReport(
        path=spec.path,
        source=spec.source,
        version=version,
        interesting_models=interesting_models,
        usable=not errors,
        error="; ".join(errors) if errors else None,
    )


def _run_codex_command(
    codex_bin: str,
    args: Sequence[str],
    timeout_seconds: float,
) -> _CommandResult:
    command = [codex_bin] + list(args)
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError:
        return _CommandResult(ok=False, error="not found")
    except PermissionError as exc:
        return _CommandResult(ok=False, error=f"permission denied: {exc}")
    except subprocess.TimeoutExpired:
        return _CommandResult(ok=False, error=f"timed out after {timeout_seconds:g}s")
    except OSError as exc:
        return _CommandResult(ok=False, error=str(exc))

    if completed.returncode != 0:
        details = _command_failure_details(completed)
        return _CommandResult(
            ok=False,
            stdout=completed.stdout,
            error=f"exit {completed.returncode}{details}",
        )

    return _CommandResult(ok=True, stdout=completed.stdout)


def _command_failure_details(completed: subprocess.CompletedProcess) -> str:
    message = completed.stderr.strip() or completed.stdout.strip()
    if not message:
        return ""
    first_line = message.splitlines()[0]
    return f": {first_line}"


def _first_stdout_line(stdout: str) -> Optional[str]:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _collect_interesting_models(value: Any) -> List[str]:
    models: List[str] = []
    seen = set()
    for slug in _iter_model_slug_strings(value):
        normalized = slug.lower()
        if not _is_interesting_model_slug(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        models.append(normalized)
    return models


def _iter_model_slug_strings(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _MODEL_FIELD_NAMES and isinstance(item, str):
                yield item
            elif isinstance(item, (dict, list)):
                yield from _iter_model_slug_strings(item)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_model_slug_strings(item)


def _is_interesting_model_slug(slug: str) -> bool:
    if slug in INTERESTING_MODEL_SLUGS:
        return True
    if "codex" not in slug or not _MODEL_SLUG_RE.match(slug):
        return False
    return "-" in slug or slug.startswith("gpt-")


def _select_candidate(
    candidates: Sequence[CodexCandidateReport],
) -> Optional[CodexCandidateReport]:
    for candidate in candidates:
        if candidate.usable:
            return candidate
    return None
