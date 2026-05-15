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
    DEFAULT_WORKER_REASONING_EFFORT,
    DEFAULT_WORKER_SERVICE_TIER,
    REASONING_EFFORT_CHOICES,
    SANDBOX_CHOICES,
    SERVICE_TIER_CHOICES,
)
from .failure_policy import has_retryable_review_failure
from .phase_timing import record_run_status_transition
from .states import RUN_FAILED, RUN_PLAN_REVIEW_REQUIRED, TERMINAL_RUN_STATUSES
from .workspace_lanes import WorkspaceResolutionError, canonical_git_root


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
        "--revise-plan",
        default=None,
        help="Human feedback for revising a PLAN_REVIEW_REQUIRED plan in the same Planner thread.",
    )
    resume.add_argument(
        "--revise-plan-file",
        default=None,
        help="Path to a UTF-8 text file containing plan revision feedback.",
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
    ui.add_argument(
        "--queue-file",
        default=None,
        help="Optional task queue file for dashboard queue view.",
    )
    ui.add_argument(
        "--api-only",
        action="store_true",
        help="Serve only local /api/* endpoints; use the dev UI entry for the browser UI.",
    )

    dev_ui = subparsers.add_parser(
        "dev-ui",
        help="Run the API runtime plus Vite dev server for local dashboard development.",
    )
    dev_ui.add_argument("--cwd", default=".", help="Target repository path.")
    dev_ui.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    dev_ui.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    dev_ui.add_argument("--host", default=None, help="API host interface to bind.")
    dev_ui.add_argument("--port", type=int, default=None, help="API port.")
    dev_ui.add_argument("--queue-file", default=None, help="Optional task queue file for dashboard queue view.")
    dev_ui.add_argument("--vite-host", default=None, help="Vite dev server host. Defaults to ui.dev_host.")
    dev_ui.add_argument("--vite-port", type=int, default=None, help="Vite dev server port. Defaults to ui.dev_port.")
    dev_ui.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between backend source-change checks.",
    )

    supervise_ui = subparsers.add_parser(
        "supervise-ui",
        help="Run the local dashboard under a supervisor that auto-restarts restart gates.",
    )
    supervise_ui.add_argument("--cwd", default=".", help="Target repository path.")
    supervise_ui.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    supervise_ui.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    supervise_ui.add_argument("--host", default=None, help="Host interface to bind.")
    supervise_ui.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for the local dashboard.",
    )
    supervise_ui.add_argument(
        "--queue-file",
        default=None,
        help="Optional task queue file for dashboard queue view.",
    )
    supervise_ui.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds between supervisor queue checks.",
    )
    supervise_ui.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for the dashboard child process to become ready.",
    )
    supervise_ui.add_argument(
        "--stop-timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for graceful dashboard shutdown before killing it.",
    )

    queue = subparsers.add_parser(
        "queue",
        help="Manage and execute a serial task queue.",
    )
    queue_subparsers = queue.add_subparsers(dest="queue_command", required=True)

    queue_import = queue_subparsers.add_parser(
        "import",
        help="Import tasks JSON into queue storage.",
    )
    queue_import.add_argument("tasks_json", help="Path to tasks JSON file.")
    queue_import.add_argument("--cwd", default=".", help="Target repository path.")
    queue_import.add_argument("--queue-file", default=None, help="Queue JSON path.")

    queue_status = queue_subparsers.add_parser(
        "status",
        help="Show queue and task status.",
    )
    queue_status.add_argument("--cwd", default=".", help="Target repository path.")
    queue_status.add_argument("--queue-file", default=None, help="Queue JSON path.")
    queue_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    queue_retry = queue_subparsers.add_parser(
        "retry",
        help="Requeue a failed task so the next queue run creates a new run attempt.",
    )
    queue_retry.add_argument("task_id", help="Failed task ID to requeue.")
    queue_retry.add_argument("--cwd", default=".", help="Target repository path.")
    queue_retry.add_argument("--queue-file", default=None, help="Queue JSON path.")

    queue_run = queue_subparsers.add_parser(
        "run",
        help="Run queue tasks serially from the first PENDING task.",
    )
    _add_queue_run_arguments(queue_run)

    queue_resume = queue_subparsers.add_parser(
        "resume",
        help="Alias of queue run; queue run is already idempotent.",
    )
    _add_queue_run_arguments(queue_resume)

    return parser


def _add_queue_run_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd", default=".", help="Target repository path.")
    parser.add_argument("--config", default=None, help="Project config file. Defaults to .c-orch.toml.")
    parser.add_argument("--queue-file", default=None, help="Queue JSON path.")
    parser.add_argument("--codex-bin", default=None, help="Explicit Codex binary path.")
    parser.add_argument("--runs-dir", default=None, help="Run manifest directory.")
    parser.add_argument(
        "--worktrees-dir",
        default=None,
        help="Worker worktree root. Relative paths resolve under --cwd.",
    )
    parser.add_argument(
        "--planner-model",
        default=None,
        help=(
            "Planner model override "
            f"(default: first available of {', '.join(DEFAULT_PLANNER_MODELS)})."
        ),
    )
    parser.add_argument(
        "--worker-model",
        default=None,
        help=f"Worker model (default: {DEFAULT_WORKER_MODEL}).",
    )
    parser.add_argument(
        "--planner-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help=(
            "Planner reasoning effort passed as Codex model_reasoning_effort "
            f"(default: {DEFAULT_PLANNER_REASONING_EFFORT})."
        ),
    )
    parser.add_argument(
        "--worker-reasoning-effort",
        default=None,
        choices=REASONING_EFFORT_CHOICES,
        help="Worker reasoning effort passed as Codex model_reasoning_effort.",
    )
    parser.add_argument(
        "--planner-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Planner service tier.",
    )
    parser.add_argument(
        "--worker-service-tier",
        default=None,
        choices=SERVICE_TIER_CHOICES,
        help="Worker service tier.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=None,
        help="Maximum Worker attempts including the initial attempt.",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=None,
        help="Maximum tasks to execute in this invocation.",
    )
    parser.add_argument(
        "--sandbox",
        default=None,
        choices=SANDBOX_CHOICES,
        help="Sandbox mode passed to Codex MCP sessions.",
    )
    parser.add_argument(
        "--approval-policy",
        default=None,
        choices=APPROVAL_POLICY_CHOICES,
        help="Approval policy passed to Codex MCP sessions.",
    )


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


def _resolve_execution_config(
    args: argparse.Namespace,
    *,
    cwd: Path,
) -> dict[str, Any]:
    from .codex_discovery import choose_first_available_model, inspect_codex_environment
    from .config import load_project_config

    project_config = load_project_config(cwd=cwd, config_path=getattr(args, "config", None))
    codex_bin = getattr(args, "codex_bin", None) or project_config.codex_bin
    report = inspect_codex_environment(explicit_codex_bin=codex_bin)
    if not report.selected:
        raise ValueError("No usable Codex binary found. Run `c-orch doctor` for details.")

    planner_models = (
        [args.planner_model]
        if getattr(args, "planner_model", None)
        else project_config.planner_preferred_models
    )
    planner_model = choose_first_available_model(report.selected, planner_models)
    if not planner_model:
        raise ValueError(f"No planner model found in selected Codex binary: {report.selected.path}")

    worker_model = getattr(args, "worker_model", None) or project_config.worker.model or DEFAULT_WORKER_MODEL
    if not report.selected.has_model(worker_model):
        raise ValueError(
            f"Worker model {worker_model!r} was not found in selected Codex binary: {report.selected.path}"
        )

    return {
        "project_config": project_config,
        "codex_path": report.selected.path,
        "planner_model": planner_model,
        "worker_model": worker_model,
        "runs_dir": _resolve_under_cwd(
            cwd,
            getattr(args, "runs_dir", None) or project_config.run.runs_dir,
        ),
        "worktrees_dir": _resolve_under_cwd(
            cwd,
            getattr(args, "worktrees_dir", None) or project_config.run.worktrees_dir,
        ),
        "planner_reasoning_effort": _first_value(
            getattr(args, "planner_reasoning_effort", None),
            project_config.planner.reasoning_effort,
            DEFAULT_PLANNER_REASONING_EFFORT,
        ),
        "worker_reasoning_effort": _first_value(
            getattr(args, "worker_reasoning_effort", None),
            project_config.worker.reasoning_effort,
            DEFAULT_WORKER_REASONING_EFFORT,
        ),
        "planner_service_tier": _first_value(
            getattr(args, "planner_service_tier", None),
            project_config.planner.service_tier,
        ),
        "worker_service_tier": _first_value(
            getattr(args, "worker_service_tier", None),
            project_config.worker.service_tier,
            DEFAULT_WORKER_SERVICE_TIER,
        ),
        "max_attempts": getattr(args, "max_attempts", None) or project_config.run.max_attempts,
        "max_parallel_workspaces": project_config.run.max_parallel_workspaces,
        "sandbox": getattr(args, "sandbox", None) or project_config.run.sandbox,
        "approval_policy": getattr(args, "approval_policy", None) or project_config.run.approval_policy,
    }


def _default_queue_file(cwd: Path) -> Path:
    return cwd / ".c-orch" / "tasks" / "queue.json"


def _resolve_queue_path(cwd: Path, queue_file: Optional[str]) -> Path:
    if not queue_file:
        return _default_queue_file(cwd)
    return _resolve_under_cwd(cwd, queue_file)


def run_prepare(args: argparse.Namespace) -> int:
    from .mcp_driver import McpCodexDriver
    from .orchestrator import run_single_worker
    from .run_store import RunStore
    from .worktrees import create_worker_worktree

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        config = _resolve_execution_config(args, cwd=cwd)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    store = RunStore(config["runs_dir"])
    manifest = store.create_run(
        cwd=cwd,
        user_task=args.task,
        planner_model=config["planner_model"],
        worker_model=config["worker_model"],
        codex_binary_path=config["codex_path"],
        planner_reasoning_effort=config["planner_reasoning_effort"],
        worker_reasoning_effort=config["worker_reasoning_effort"],
        planner_service_tier=config["planner_service_tier"],
        worker_service_tier=config["worker_service_tier"],
    )
    worker = manifest.workers[0]
    worker.worktree_path = str(
        create_worker_worktree(
            repo_path=cwd,
            worktrees_dir=config["worktrees_dir"],
            run_id=manifest.run_id,
            worker_id=worker.id,
        )
    )
    store.save(manifest)

    if not args.prepare_only:
        try:
            with McpCodexDriver(codex_bin=config["codex_path"]) as driver:
                manifest = run_single_worker(
                    manifest=manifest,
                    store=store,
                    driver=driver,
                    max_attempts=config["max_attempts"],
                    sandbox=config["sandbox"],
                    approval_policy=config["approval_policy"],
                    require_plan_approval=not args.auto_approve_plan,
                )
        except Exception as exc:
            if not _is_terminal_manifest_status(manifest.status):
                record_run_status_transition(
                    manifest,
                    RUN_FAILED,
                    store.now_iso(),
                    metadata={"reason": "cli_run_exception"},
                )
            store.save(manifest)
            _print_run_summary(
                manifest=manifest,
                manifest_path=store.manifest_path(manifest.run_id),
                codex_path=config["codex_path"],
                planner_model=config["planner_model"],
                worker_model=config["worker_model"],
            )
            print(f"error: {exc}", file=sys.stderr)
            return 1

    _print_run_summary(
        manifest=manifest,
        manifest_path=store.manifest_path(manifest.run_id),
        codex_path=config["codex_path"],
        planner_model=config["planner_model"],
        worker_model=config["worker_model"],
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
    revise_feedback: Optional[str] = None
    try:
        revise_feedback = _resolve_plan_revision_feedback(args, cwd=cwd)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if revise_feedback and args.retry_review:
        print("--revise-plan/--revise-plan-file cannot be combined with --retry-review", file=sys.stderr)
        return 1
    if revise_feedback and args.approve_plan:
        print("--revise-plan/--revise-plan-file cannot be combined with --approve-plan", file=sys.stderr)
        return 1

    if _is_terminal_manifest_status(manifest.status):
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=summary_codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        return 0

    if manifest.status == RUN_PLAN_REVIEW_REQUIRED and not args.approve_plan and not revise_feedback:
        _print_run_summary(
            manifest=manifest,
            manifest_path=store.manifest_path(manifest.run_id),
            codex_path=summary_codex_path,
            planner_model=planner_model,
            worker_model=worker_model,
        )
        print("next: rerun resume with --approve-plan or --revise-plan after human review")
        return 0

    if has_retryable_review_failure(manifest) and not args.retry_review:
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
            if revise_feedback:
                manifest = orchestrator.revise_plan(manifest, revise_feedback)
            elif args.retry_review:
                manifest = orchestrator.retry_review(manifest)
            else:
                manifest = orchestrator.run(manifest)
    except Exception as exc:
        if not revise_feedback and not _is_terminal_manifest_status(manifest.status):
            record_run_status_transition(
                manifest,
                RUN_FAILED,
                store.now_iso(),
                metadata={"reason": "cli_resume_exception"},
            )
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


def run_queue_import(args: argparse.Namespace) -> int:
    from .task_store import TaskStore

    cwd = Path(args.cwd).expanduser().resolve()
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    tasks_path = _resolve_under_cwd(cwd, args.tasks_json)
    try:
        payload = json.loads(tasks_path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"failed to read tasks file: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"invalid tasks json: {exc}", file=sys.stderr)
        return 1
    if not isinstance(payload, list):
        print("tasks json must be an array", file=sys.stderr)
        return 1

    store = TaskStore(queue_path=queue_path)
    try:
        queue = store.import_tasks(
            payload,
            cwd_resolver=lambda raw_cwd, index: _resolve_import_task_cwd(
                raw_cwd,
                index=index,
                base_cwd=cwd,
            ),
        )
    except ValueError as exc:
        print(f"queue import error: {exc}", file=sys.stderr)
        return 1

    print(f"queue_file: {queue_path}")
    print(f"queue_id: {queue.queue_id}")
    print(f"tasks: {len(queue.tasks)}")
    return 0


def run_queue_status(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .dashboard_payloads import build_queue_payload
    from .task_store import TaskStore

    cwd = Path(args.cwd).expanduser().resolve()
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    store = TaskStore(queue_path=queue_path)
    try:
        queue = store.load()
    except OSError:
        print(f"queue file not found: {queue_path}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"queue load error: {exc}", file=sys.stderr)
        return 1
    try:
        project_config = load_project_config(cwd=cwd, config_path=None)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, project_config.run.runs_dir)
    dashboard_payload = build_queue_payload(queue_path, runs_dir=runs_dir)
    queue = store.load()

    payload = {
        "queue_file": str(queue_path),
        "queue": queue.to_dict(),
        "summary": dashboard_payload.get("summary"),
        "tasks": dashboard_payload.get("tasks", []),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"queue_file: {queue_path}")
    print(f"queue_id: {queue.queue_id}")
    print(f"status: {queue.status}")
    for task in queue.tasks:
        print(
            f"- {task.task_id} [{task.status}] run={task.active_run_id or '-'} "
            f"cwd={task.cwd or '-'} reason={task.reason or '-'} title={task.title}"
        )
    return 0


def run_queue_retry(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .runtime import task_action

    cwd = Path(args.cwd).expanduser().resolve()
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    try:
        project_config = load_project_config(cwd=cwd, config_path=None)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, project_config.run.runs_dir)
    result = task_action(queue_path, args.task_id, "retry-task", runs_dir=runs_dir)
    if result is None:
        print(f"queue file not found: {queue_path}", file=sys.stderr)
        return 1
    status, payload = result
    if int(status) >= 400:
        print(str(payload.get("error", "retry failed")), file=sys.stderr)
        return 1
    print(f"queue_file: {queue_path}")
    print(f"task: {args.task_id}")
    print("status: PENDING")
    print("next: run `c-orch queue run` to create a new run attempt")
    return 0


def run_queue_run(args: argparse.Namespace) -> int:
    from .mcp_driver import McpCodexDriver
    from .run_store import RunStore
    from .scheduler import SchedulerConfig, TaskScheduler
    from .task_store import TaskStore

    cwd = Path(args.cwd).expanduser().resolve()
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    try:
        config = _resolve_execution_config(args, cwd=cwd)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    task_store = TaskStore(queue_path=queue_path)
    try:
        task_store.load()
    except OSError:
        print(f"queue file not found: {queue_path}. run `c-orch queue import ...` first.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"queue load error: {exc}", file=sys.stderr)
        return 1

    run_store = RunStore(config["runs_dir"])
    scheduler_config = SchedulerConfig(
        cwd=cwd,
        runs_dir=config["runs_dir"],
        worktrees_dir=config["worktrees_dir"],
        planner_model=config["planner_model"],
        worker_model=config["worker_model"],
        codex_binary_path=config["codex_path"],
        planner_reasoning_effort=config["planner_reasoning_effort"],
        worker_reasoning_effort=config["worker_reasoning_effort"],
        planner_service_tier=config["planner_service_tier"],
        worker_service_tier=config["worker_service_tier"],
        max_attempts=config["max_attempts"],
        max_parallel_workspaces=config["max_parallel_workspaces"],
        sandbox=config["sandbox"],
        approval_policy=config["approval_policy"],
        max_tasks=args.max_tasks,
    )
    try:
        with McpCodexDriver(codex_bin=config["codex_path"]) as driver:
            scheduler = TaskScheduler(
                task_store=task_store,
                run_store=run_store,
                driver=driver,
                config=scheduler_config,
            )
            queue = scheduler.run()
    except Exception as exc:
        print(f"queue run error: {exc}", file=sys.stderr)
        return 1

    print(f"queue_file: {queue_path}")
    print(f"status: {queue.status}")
    for task in queue.tasks:
        print(f"- {task.task_id} [{task.status}] run={task.active_run_id or '-'}")
    return 0


def run_ui(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .scheduler import SchedulerConfig
    from .ui import serve_dashboard

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.ui.runs_dir)
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    host = args.host or project_config.ui.host
    port = args.port or project_config.ui.port
    scheduler_config = None
    if queue_path.exists():
        try:
            execution_config = _resolve_execution_config(args, cwd=cwd)
        except ValueError as exc:
            print(f"queue auto-dispatch disabled: {exc}", file=sys.stderr, flush=True)
        else:
            scheduler_config = SchedulerConfig(
                cwd=cwd,
                runs_dir=runs_dir,
                worktrees_dir=execution_config["worktrees_dir"],
                planner_model=execution_config["planner_model"],
                worker_model=execution_config["worker_model"],
                codex_binary_path=execution_config["codex_path"],
                planner_reasoning_effort=execution_config["planner_reasoning_effort"],
                worker_reasoning_effort=execution_config["worker_reasoning_effort"],
                planner_service_tier=execution_config["planner_service_tier"],
                worker_service_tier=execution_config["worker_service_tier"],
                max_attempts=execution_config["max_attempts"],
                max_parallel_workspaces=execution_config["max_parallel_workspaces"],
                sandbox=execution_config["sandbox"],
                approval_policy=execution_config["approval_policy"],
            )
    label = "c-orch API" if args.api_only else "c-orch UI"
    print(f"{label}: http://{host}:{port}", flush=True)
    print(f"runs_dir: {runs_dir}", flush=True)
    if args.queue_file is not None:
        print(f"queue_file: {queue_path}", flush=True)
    if scheduler_config is not None:
        print("queue_auto_dispatch: enabled", flush=True)
    serve_dashboard(
        runs_dir=runs_dir,
        queue_path=queue_path,
        scheduler_config=scheduler_config,
        host=host,
        port=port,
        api_only=args.api_only,
    )
    return 0


def run_dev_ui(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .dev_ui import DevUiConfig, DevUiRunner, build_api_command, build_vite_command

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.ui.runs_dir)
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    api_host = args.host or project_config.ui.host
    api_port = args.port or project_config.ui.port
    api_client_host = "127.0.0.1" if api_host in {"0.0.0.0", "::"} else api_host
    api_url = f"http://{api_client_host}:{api_port}"
    vite_host = args.vite_host or project_config.ui.dev_host
    vite_port = args.vite_port or project_config.ui.dev_port
    vite_url = f"http://{vite_host}:{vite_port}"
    api_command = build_api_command(
        cwd=cwd,
        config_path=args.config,
        runs_dir=runs_dir,
        queue_path=queue_path,
        host=api_host,
        port=api_port,
        python_executable=sys.executable,
    )
    vite_command = build_vite_command(
        cwd=cwd,
        host=vite_host,
        port=vite_port,
    )
    print(f"c-orch API: {api_url}", flush=True)
    print(f"c-orch dev UI: {vite_url}", flush=True)
    print("dev mode: use Vite for frontend HMR; API restarts when src/c_orch/*.py changes.", flush=True)
    return DevUiRunner(
        DevUiConfig(
            cwd=cwd,
            api_command=api_command,
            vite_command=vite_command,
            api_base_url=api_url,
            api_target=api_url,
            poll_interval_seconds=args.poll_interval,
            watch_roots=(cwd / "src" / "c_orch",),
        )
    ).run_forever()


def run_supervise_ui(args: argparse.Namespace) -> int:
    from .config import load_project_config
    from .supervisor import DashboardSupervisor, SupervisorConfig, build_ui_command

    cwd = Path(args.cwd).expanduser().resolve()
    try:
        project_config = load_project_config(cwd=cwd, config_path=args.config)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir or project_config.ui.runs_dir)
    queue_path = _resolve_queue_path(cwd, args.queue_file)
    host = args.host or project_config.ui.host
    port = args.port or project_config.ui.port
    command = build_ui_command(
        cwd=cwd,
        runs_dir=runs_dir,
        queue_path=queue_path,
        host=host,
        port=port,
        config_path=args.config,
    )
    print(f"c-orch supervisor: http://{host}:{port}", flush=True)
    print(f"runs_dir: {runs_dir}", flush=True)
    print(f"queue_file: {queue_path}", flush=True)
    supervisor = DashboardSupervisor(
        SupervisorConfig(
            command=command,
            cwd=cwd,
            base_url=f"http://{host}:{port}",
            poll_interval_seconds=args.poll_interval,
            startup_timeout_seconds=args.startup_timeout,
            stop_timeout_seconds=args.stop_timeout,
        )
    )
    return supervisor.run_forever()


def _resolve_under_cwd(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return cwd / path


def _resolve_import_task_cwd(raw_cwd: Any, *, index: int, base_cwd: Path) -> Optional[str]:
    if raw_cwd is None:
        return None
    if not isinstance(raw_cwd, str):
        raise ValueError(f"tasks[{index}].cwd must be a string")
    text = raw_cwd.strip()
    if not text:
        raise ValueError(f"tasks[{index}].cwd cannot be empty")
    resolved = _resolve_under_cwd(base_cwd, text).resolve()
    try:
        canonical_git_root(resolved)
    except WorkspaceResolutionError as exc:
        raise ValueError(f"tasks[{index}].cwd {exc}") from exc
    return str(resolved)


def _is_terminal_manifest_status(status: str) -> bool:
    return status in TERMINAL_RUN_STATUSES


def _first_value(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value is not None:
            return value
    return None


def _resolve_plan_revision_feedback(args: argparse.Namespace, *, cwd: Path) -> Optional[str]:
    values = []
    if args.revise_plan is not None:
        values.append(args.revise_plan)
    if args.revise_plan_file is not None:
        path = _resolve_under_cwd(cwd, args.revise_plan_file)
        try:
            values.append(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"failed to read revise plan file: {exc}") from exc
    if not values:
        return None
    feedback = "\n\n".join(part.strip() for part in values if part.strip()).strip()
    if not feedback:
        raise ValueError("plan revision feedback cannot be empty")
    return feedback


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
    if args.command == "dev-ui":
        return run_dev_ui(args)
    if args.command == "supervise-ui":
        return run_supervise_ui(args)
    if args.command == "queue":
        if args.queue_command == "import":
            return run_queue_import(args)
        if args.queue_command == "status":
            return run_queue_status(args)
        if args.queue_command == "retry":
            return run_queue_retry(args)
        if args.queue_command in {"run", "resume"}:
            return run_queue_run(args)
        parser.error(f"unknown queue command: {args.queue_command}")
        return 2
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
