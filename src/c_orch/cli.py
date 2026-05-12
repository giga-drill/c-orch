from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .settings import (
    APPROVAL_POLICY_CHOICES,
    DEFAULT_PLANNER_MODELS,
    DEFAULT_PLANNER_REASONING_EFFORT,
    DEFAULT_WORKER_MODEL,
    REASONING_EFFORT_CHOICES,
    SANDBOX_CHOICES,
    SERVICE_TIER_CHOICES,
)
from .states import (
    RUN_FAILED,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_REVIEW_RETRYABLE,
    TERMINAL_RUN_STATUSES,
)


def _json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="c-orch",
        description="Codex-native Planner/Worker workflow orchestrator.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser(
        "doctor",
        help="Inspect Codex binaries, versions, and model availability.",
    )
    doctor.add_argument(
        "--codex-bin",
        default=None,
        help="Explicit Codex binary path. Overrides C_ORCH_CODEX_BIN and auto-detection.",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )

    run = subparsers.add_parser(
        "run",
        help="Create and optionally execute a single-worker c-orch run.",
    )
    run.add_argument("task", help="User task to orchestrate.")
    run.add_argument("--cwd", default=".", help="Target repository path.")
    run.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    run.add_argument("--codex-bin", default=None, help="Explicit Codex binary path.")
    run.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    run.add_argument(
        "--worktrees-dir",
        default=None,
        help="Worker worktree root. Relative paths resolve under --cwd.",
    )
    run.add_argument(
        "--planner-model",
        default=None,
        help=(
            "Planner model override "
            f"(default: first available of {', '.join(DEFAULT_PLANNER_MODELS)})."
        ),
    )
    run.add_argument(
        "--worker-model",
        default=None,
        help=f"Worker model (default: {DEFAULT_WORKER_MODEL}).",
    )
    run.add_argument(
        "--planner-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help=(
            "Planner reasoning effort passed as Codex model_reasoning_effort "
            f"(default: {DEFAULT_PLANNER_REASONING_EFFORT})."
        ),
    )
    run.add_argument(
        "--worker-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help="Worker reasoning effort passed as Codex model_reasoning_effort.",
    )
    run.add_argument(
        "--planner-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Planner service tier. Use fast for quicker responses that may consume more usage.",
    )
    run.add_argument(
        "--worker-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Worker service tier. Use fast for quicker responses that may consume more usage.",
    )
    run.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum Worker attempts including the initial attempt.",
    )
    run.add_argument(
        "--sandbox",
        default=None,
        choices=SANDBOX_CHOICES,
        help="Sandbox mode passed to Codex MCP sessions.",
    )
    run.add_argument(
        "--approval-policy",
        default=None,
        choices=APPROVAL_POLICY_CHOICES,
        help="Approval policy passed to Codex MCP sessions.",
    )
    run.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create manifest and worker worktree without calling Codex MCP.",
    )
    run.add_argument(
        "--auto-approve-plan",
        action="store_true",
        help="Skip the human Planner plan review gate and start the Worker immediately.",
    )

    resume = subparsers.add_parser(
        "resume",
        help="Resume an existing single-worker c-orch run.",
    )
    resume.add_argument("run_id", help="Existing run ID to resume.")
    resume.add_argument("--cwd", default=".", help="Target repository path.")
    resume.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    resume.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    resume.add_argument("--codex-bin", default=None, help="Explicit Codex binary path.")
    resume.add_argument(
        "--planner-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help="Override the saved Planner reasoning effort for this resume.",
    )
    resume.add_argument(
        "--worker-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help="Override the saved Worker reasoning effort for this resume.",
    )
    resume.add_argument(
        "--planner-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Override the saved Planner service tier for this resume.",
    )
    resume.add_argument(
        "--worker-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Override the saved Worker service tier for this resume.",
    )
    resume.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum Worker attempts including the initial attempt.",
    )
    resume.add_argument(
        "--sandbox",
        default=None,
        choices=SANDBOX_CHOICES,
        help="Sandbox mode passed to Codex MCP sessions.",
    )
    resume.add_argument(
        "--approval-policy",
        default=None,
        choices=APPROVAL_POLICY_CHOICES,
        help="Approval policy passed to Codex MCP sessions.",
    )
    resume.add_argument(
        "--approve-plan",
        action="store_true",
        help="Approve a PLAN_REVIEW_REQUIRED run and continue to the Worker.",
    )
    resume.add_argument(
        "--retry-review",
        action="store_true",
        help="Retry a saved Planner review after Worker evidence has been preserved.",
    )

    ui = subparsers.add_parser(
        "ui",
        help="Serve a local web dashboard for c-orch runs.",
    )
    ui.add_argument("--cwd", default=".", help="Target repository path.")
    ui.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    ui.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    ui.add_argument("--host", default=None, help="Host interface to bind.")
    ui.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the local dashboard.",
    )

    return parser


def run_doctor(args: argparse.Namespace) -> int:
    from .codex_discovery import inspect_codex_environment

    report = inspect_codex_environment(explicit_codex_bin=args.codex_bin)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False, default=_json_default))
        return 0 if report.selected and report.selected.usable else 1

    print(f"selected: {report.selected.path if report.selected else 'none'}")
    if report.selected:
        print(f"version: {report.selected.version or 'unknown'}")
        print(f"usable: {str(report.selected.usable).lower()}")
        print(f"gpt-5.5 available: {str(report.selected.has_model('gpt-5.5')).lower()}")
        if report.selected.error:
            print(f"error: {report.selected.error}")
    print()
    print("candidates:")
    for candidate in report.candidates:
        marker = "*" if report.selected and candidate.path == report.selected.path else "-"
        models = ", ".join(candidate.interesting_models) if candidate.interesting_models else "none"
        print(f"{marker} {candidate.path}")
        print(f"  version: {candidate.version or 'unknown'}")
        print(f"  usable: {str(candidate.usable).lower()}")
        print(f"  models: {models}")
        if candidate.error:
            print(f"  error: {candidate.error}")
    return 0 if report.selected and report.selected.usable else 1


def run_prepare(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .codex_discovery import choose_first_available_model, inspect_codex_environment
    from .mcp_driver import McpCodexDriver
    from .orchestrator import run_single_worker
    from .run_store import RunStore
    from .worktrees import create_worker_worktree

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    codex_bin = args.codex_bin or project_config.codex_bin
    report = inspect_codex_environment(explicit_codex_bin=codex_bin)
    if not report.selected:
        print("No usable Codex binary found. Run `c-orch doctor` for details.", file=sys.stderr)
        return 1

    planner_models = [args.planner_model] if args.planner_model else project_config.planner_preferred_models
    planner_model = choose_first_available_model(
        report.selected,
        planner_models,
    )
    if not planner_model:
        print(
            f"No planner model found in selected Codex binary: {report.selected.path}",
            file=sys.stderr,
        )
        return 1
    worker_model = args.worker_model or project_config.worker.model or DEFAULT_WORKER_MODEL
    if not report.selected.has_model(worker_model):
        print(
            f"Worker model {worker_model!r} was not found in selected Codex binary: {report.selected.path}",
            file=sys.stderr,
        )
        return 1

    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.run.runs_dir)
    worktrees_dir = _resolve_under_cwd(cwd, args.worktrees_dir or project_config.run.worktrees_dir)
    planner_reasoning_effort = _first_value(
        args.planner_reasoning_effort,
        project_config.planner.reasoning_effort,
        DEFAULT_PLANNER_REASONING_EFFORT,
    )
    worker_reasoning_effort = _first_value(
        args.worker_reasoning_effort,
        project_config.worker.reasoning_effort,
    )
    planner_service_tier = _first_value(
        args.planner_service_tier,
        project_config.planner.service_tier,
    )
    worker_service_tier = _first_value(
        args.worker_service_tier,
        project_config.worker.service_tier,
    )
    max_attempts = args.max_attempts or project_config.run.max_attempts
    sandbox = args.sandbox or project_config.run.sandbox
    approval_policy = args.approval_policy or project_config.run.approval_policy

    store = RunStore(runs_dir)
    manifest = store.create_run(
        cwd=cwd,
        user_task=args.task,
        planner_model=planner_model,
        worker_model=worker_model,
        codex_binary_path=report.selected.path,
        planner_reasoning_effort=planner_reasoning_effort,
        worker_reasoning_effort=worker_reasoning_effort,
        planner_service_tier=planner_service_tier,
        worker_service_tier=worker_service_tier,
    )
    worker = manifest.workers[0]
    worker.worktree_path = str(
        create_worker_worktree(
            repo_path=cwd,
            worktrees_dir=worktrees_dir,
            run_id=manifest.run_id,
            worker_id=worker.id,
        )
    )
    store.save(manifest)

    if not args.prepare_only:
        try:
            with McpCodexDriver(codex_bin=report.selected.path) as driver:
                manifest = run_single_worker(
                    manifest=manifest,
                    store=store,
                    driver=driver,
                    max_attempts=max_attempts,
                    sandbox=sandbox,
                    approval_policy=approval_policy,
                    require_plan_approval=not args.auto_approve_plan,
                )
        except Exception as exc:
            if not _is_terminal_manifest_status(manifest.status):
                manifest.status = RUN_FAILED
            store.save(manifest)
            _print_run_summary(
                manifest=manifest,
                manifest_path=store.manifest_path(manifest.run_id),
                codex_path=report.selected.path,
                planner_model=planner_model,
                worker_model=worker_model,
            )
            print(f"error: {exc}", file=sys.stderr)
            return 1

    _print_run_summary(
        manifest=manifest,
        manifest_path=store.manifest_path(manifest.run_id),
        codex_path=report.selected.path,
        planner_model=planner_model,
        worker_model=worker_model,
    )
    return 0


def run_resume(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .mcp_driver import McpCodexDriver
    from .orchestrator import OrchestratorConfig, RunOrchestrator
    from .run_store import RunStore

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.run.runs_dir)
    store = RunStore(runs_dir)
    manifest = store.load(args.run_id)
    worker = manifest.workers[0]
    planner_model = manifest.planner.model
    worker_model = worker.model
    summary_codex_path = (
        args.codex_bin
        or project_config.codex_bin
        or manifest.codex_binary_path
        or manifest.planner.codex_binary_path
        or "unknown"
    )

    if _is_terminal_manifest_status(manifest.status):
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=summary_codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        return 0

    if manifest.status == RUN_PLAN_REVIEW_REQUIRED and not args.approve_plan:
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=summary_codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        print("next: rerun resume with --approve-plan after human review")
        return 0

    if manifest.status == RUN_REVIEW_RETRYABLE and not args.retry_review:
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=summary_codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        print("next: rerun resume with --retry-review to reuse saved Worker evidence")
        return 0

    if args.approve_plan and manifest.plan is None:
        print("Cannot approve plan: this run does not have a saved Planner plan yet.", file=sys.stderr)
        return 1

    _apply_resume_session_overrides(manifest, args)
    store.save(manifest)

    codex_path = (
        args.codex_bin
        or project_config.codex_bin
        or manifest.codex_binary_path
        or manifest.planner.codex_binary_path
    )
    if not codex_path:
        from .codex_discovery import inspect_codex_environment

        report = inspect_codex_environment(explicit_codex_bin=args.codex_bin)
        if not report.selected:
            print("No usable Codex binary found. Run `c-orch doctor` for details.", file=sys.stderr)
            return 1
        codex_path = report.selected.path

    try:
        with McpCodexDriver(codex_bin=codex_path) as driver:
            orchestrator = RunOrchestrator(
                store=store,
                driver=driver,
                config=OrchestratorConfig(
                    max_attempts=args.max_attempts or project_config.run.max_attempts,
                    sandbox=args.sandbox or project_config.run.sandbox,
                    approval_policy=args.approval_policy or project_config.run.approval_policy,
                    require_plan_approval=True,
                    approve_plan=args.approve_plan,
                ),
            )
            if args.retry_review:
                manifest = orchestrator.retry_review(manifest)
            else:
                manifest = orchestrator.run(manifest)
    except Exception as exc:
        if not _is_terminal_manifest_status(manifest.status):
            manifest.status = RUN_FAILED
        store.save(manifest)
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_run_summary(
        manifest=manifest,
        manifest_path=store.manifest_path(manifest.run_id),
        codex_path=codex_path,
        planner_model=planner_model,
        worker_model=worker_model,
    )
    return 0


def run_ui(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .ui import serve_dashboard

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.ui.runs_dir)
    host = args.host or project_config.ui.host
    port = args.port or project_config.ui.port
    print(f"c-orch UI: http://{host}:{port}", flush=True)
    print(f"runs_dir: {runs_dir}", flush=True)
    serve_dashboard(runs_dir=runs_dir, host=host, port=port)
    return 0


def _resolve_under_cwd(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return cwd / path


def _is_terminal_manifest_status(status: str) -> bool:
    return status in TERMINAL_RUN_STATUSES


def _first_value(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value is not None:
            return value
    return None


def _apply_resume_session_overrides(manifest: Any, args: argparse.Namespace) -> None:
    worker = manifest.workers[0]
    if args.planner_reasoning_effort is not None:
        manifest.planner.reasoning_effort = args.planner_reasoning_effort
    if args.worker_reasoning_effort is not None:
        worker.reasoning_effort = args.worker_reasoning_effort
    if args.planner_service_tier is not None:
        manifest.planner.service_tier = args.planner_service_tier
    if args.worker_service_tier is not None:
        worker.service_tier = args.worker_service_tier


def _print_run_summary(
    *,
    manifest: Any,
    manifest_path: Path,
    codex_path: str,
    planner_model: str,
    worker_model: str,
) -> None:
    worker = manifest.workers[0]
    print(f"run_id: {manifest.run_id}")
    print(f"manifest: {manifest_path}")
    print(f"codex: {codex_path}")
    print(f"planner_model: {planner_model}")
    print(f"worker_model: {worker_model}")
    if manifest.planner.reasoning_effort:
        print(f"planner_reasoning_effort: {manifest.planner.reasoning_effort}")
    if worker.reasoning_effort:
        print(f"worker_reasoning_effort: {worker.reasoning_effort}")
    if manifest.planner.service_tier:
        print(f"planner_service_tier: {manifest.planner.service_tier}")
    if worker.service_tier:
        print(f"worker_service_tier: {worker.service_tier}")
    if manifest.plan is not None:
        print(f"plan_approval: {manifest.plan.approval_status}")
        if manifest.plan.summary:
            print(f"plan_summary: {manifest.plan.summary}")
    print(f"worker_worktree: {worker.worktree_path}")
    print(f"status: {manifest.status}")
    if manifest.planner.thread_id:
        print(f"planner_thread: {manifest.planner.thread_id}")
    if worker.thread_id:
        print(f"worker_thread: {worker.thread_id}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "doctor":
        return run_doctor(args)
    if args.command == "run":
        return run_prepare(args)
    if args.command == "resume":
        return run_resume(args)
    if args.command == "ui":
        return run_ui(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
