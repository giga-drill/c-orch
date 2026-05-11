from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional, Sequence


DEFAULT_PLANNER_MODELS = ("gpt-5.5", "gpt-5.4")
DEFAULT_WORKER_MODEL = "gpt-5.3-codex"
TERMINAL_MANIFEST_STATUSES = {"APPROVED", "BLOCKED", "FAILED"}


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
    run.add_argument("--codex-bin", default=None, help="Explicit Codex binary path.")
    run.add_argument("--runs-dir", default="runs", help="Run manifest directory.")
    run.add_argument(
        "--worktrees-dir",
        default=".c-orch/worktrees",
        help="Worker worktree root. Relative paths resolve under --cwd.",
    )
    run.add_argument("--planner-model", default=None, help="Planner model override.")
    run.add_argument("--worker-model", default=DEFAULT_WORKER_MODEL, help="Worker model.")
    run.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum Worker attempts including the initial attempt.",
    )
    run.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Sandbox mode passed to Codex MCP sessions.",
    )
    run.add_argument(
        "--approval-policy",
        default="never",
        choices=("untrusted", "on-failure", "on-request", "never"),
        help="Approval policy passed to Codex MCP sessions.",
    )
    run.add_argument(
        "--prepare-only",
        action="store_true",
        help="Create manifest and worker worktree without calling Codex MCP.",
    )

    resume = subparsers.add_parser(
        "resume",
        help="Resume an existing single-worker c-orch run.",
    )
    resume.add_argument("run_id", help="Existing run ID to resume.")
    resume.add_argument("--cwd", default=".", help="Target repository path.")
    resume.add_argument("--runs-dir", default="runs", help="Run manifest directory.")
    resume.add_argument("--codex-bin", default=None, help="Explicit Codex binary path.")
    resume.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum Worker attempts including the initial attempt.",
    )
    resume.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=("read-only", "workspace-write", "danger-full-access"),
        help="Sandbox mode passed to Codex MCP sessions.",
    )
    resume.add_argument(
        "--approval-policy",
        default="never",
        choices=("untrusted", "on-failure", "on-request", "never"),
        help="Approval policy passed to Codex MCP sessions.",
    )

    ui = subparsers.add_parser(
        "ui",
        help="Serve a local web dashboard for c-orch runs.",
    )
    ui.add_argument("--cwd", default=".", help="Target repository path.")
    ui.add_argument("--runs-dir", default="runs", help="Run manifest directory.")
    ui.add_argument("--host", default="127.0.0.1", help="Host interface to bind.")
    ui.add_argument(
        "--port",
        type=int,
        default=8765,
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
    from .codex_discovery import choose_first_available_model, inspect_codex_environment
    from .mcp_driver import McpCodexDriver
    from .orchestrator import run_single_worker
    from .run_store import RunStore
    from .worktrees import create_worker_worktree

    report = inspect_codex_environment(explicit_codex_bin=args.codex_bin)
    if not report.selected:
        print("No usable Codex binary found. Run `c-orch doctor` for details.", file=sys.stderr)
        return 1

    planner_model = args.planner_model or choose_first_available_model(
        report.selected,
        DEFAULT_PLANNER_MODELS,
    )
    if not planner_model:
        print(
            f"No planner model found in selected Codex binary: {report.selected.path}",
            file=sys.stderr,
        )
        return 1
    worker_model = args.worker_model
    if not report.selected.has_model(worker_model):
        print(
            f"Worker model {worker_model!r} was not found in selected Codex binary: {report.selected.path}",
            file=sys.stderr,
        )
        return 1

    cwd = Path(args.cwd).expanduser().resolve()
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir)
    worktrees_dir = _resolve_under_cwd(cwd, args.worktrees_dir)

    store = RunStore(runs_dir)
    manifest = store.create_run(
        cwd=cwd,
        user_task=args.task,
        planner_model=planner_model,
        worker_model=worker_model,
        codex_binary_path=report.selected.path,
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
                    max_attempts=args.max_attempts,
                    sandbox=args.sandbox,
                    approval_policy=args.approval_policy,
                )
        except Exception as exc:
            if not _is_terminal_manifest_status(manifest.status):
                manifest.status = "FAILED"
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
    from .mcp_driver import McpCodexDriver
    from .orchestrator import OrchestratorConfig, RunOrchestrator
    from .run_store import RunStore

    cwd = Path(args.cwd).expanduser().resolve()
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir)
    store = RunStore(runs_dir)
    manifest = store.load(args.run_id)
    worker = manifest.workers[0]
    planner_model = manifest.planner.model
    worker_model = worker.model
    summary_codex_path = (
        args.codex_bin
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

    codex_path = (
        args.codex_bin
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
                    max_attempts=args.max_attempts,
                    sandbox=args.sandbox,
                    approval_policy=args.approval_policy,
                ),
            )
            manifest = orchestrator.run(manifest)
    except Exception as exc:
        if not _is_terminal_manifest_status(manifest.status):
            manifest.status = "FAILED"
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
    from .ui import serve_dashboard

    cwd = Path(args.cwd).expanduser().resolve()
    runs_dir = _resolve_under_cwd(cwd, args.runs_dir)
    print(f"c-orch UI: http://{args.host}:{args.port}", flush=True)
    print(f"runs_dir: {runs_dir}", flush=True)
    serve_dashboard(runs_dir=runs_dir, host=args.host, port=args.port)
    return 0


def _resolve_under_cwd(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return cwd / path


def _is_terminal_manifest_status(status: str) -> bool:
    return status in TERMINAL_MANIFEST_STATUSES


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
