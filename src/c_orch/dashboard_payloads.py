from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence, Union

from .codex_session_logs import CodexSessionLogStore
from .failure_policy import (
    has_retryable_review_failure,
    has_retryable_review_failure_dict,
    has_retryable_verification_failure,
    has_retryable_verification_failure_dict,
)
from .phase_timing import build_timing_summary
from .proposal_store import (
    PROPOSAL_APPROVED,
    PROPOSAL_FAILED,
    PROPOSAL_PLAN_REVIEW_REQUIRED,
    PROPOSAL_PLAN_REVISING,
    PROPOSAL_PLANNING,
    PROPOSAL_QUEUED,
    PROPOSAL_WAITING_WORKSPACE,
    PROPOSAL_WAITING_WORKSPACE_CLEAN,
    ProposalRecord,
    ProposalStore,
)
from .run_store import RunManifest, RunStore
from .states import (
    REVIEW_ATTEMPT_FAILED_RETRYABLE,
    RUN_PLAN_APPROVED,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_STATUS_ORDER,
    TERMINAL_RUN_STATUSES,
)
from .task_lifecycle import derive_run_waiting_for, derive_task_progress, reconcile_queue
from .task_store import TASK_SKIPPED, TaskStore
from .workspace_lanes import WorkspaceResolutionError, canonical_git_root


Pathish = Union[str, Path]
_SENTENCE_BOUNDARY_PATTERN = re.compile(r"[。！？]|[.?!](?=\s|$)|(?:\r?\n)")


def _proposal_status_from_run(manifest: RunManifest) -> str:
    if manifest.status == RUN_PLAN_REVIEW_REQUIRED:
        return PROPOSAL_PLAN_REVIEW_REQUIRED
    if manifest.status == RUN_PLAN_APPROVED:
        return PROPOSAL_APPROVED
    if manifest.status == "PLAN_REVISING":
        return PROPOSAL_PLAN_REVISING
    if manifest.status == "FAILED":
        return PROPOSAL_FAILED
    return PROPOSAL_PLANNING


def _valid_run_id(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def build_proposals_payload(
    proposals_path: Optional[Pathish],
    *,
    runs_dir: Optional[Pathish] = None,
) -> Dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if proposals_path is None:
        return {
            "proposals_file": None,
            "generated_at": generated_at,
            "pool": None,
            "proposals": [],
        }
    proposals_file = Path(proposals_path).expanduser().resolve()
    store = ProposalStore(proposals_file)
    try:
        pool = store.load()
    except (OSError, ValueError):
        return {
            "proposals_file": str(proposals_file),
            "generated_at": generated_at,
            "pool": None,
            "proposals": [],
        }
    run_store = RunStore(Path(runs_dir).expanduser().resolve()) if runs_dir is not None else None
    proposals = [_summarize_proposal(proposal, run_store=run_store) for proposal in pool.proposals]
    return {
        "proposals_file": str(proposals_file),
        "generated_at": generated_at,
        "pool": {
            "pool_id": pool.pool_id,
            "created_at": pool.created_at,
            "updated_at": pool.updated_at,
        },
        "summary": _proposal_summary(proposals),
        "proposals": proposals,
    }


def build_runs_payload(runs_dir: Pathish) -> Dict[str, Any]:
    runs_path = Path(runs_dir).expanduser().resolve()
    store = RunStore(runs_path)
    runs = [
        _summarize_manifest(
            manifest,
            events=store.load_events(str(manifest.get("run_id", ""))),
        )
        for manifest in _load_manifests(runs_path)
    ]
    runs.sort(key=lambda item: item["sort_key"], reverse=True)
    for run in runs:
        run.pop("sort_key", None)
    return {
        "runs_dir": str(runs_path),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runs": runs,
    }


def build_state_payload(
    *,
    runs_dir: Pathish,
    queue_path: Optional[Pathish],
    proposals_path: Optional[Pathish],
    runtime_generation: str,
    cost_mode: Optional[Dict[str, Any]] = None,
    dispatch_running: bool = False,
    queue_dispatch_running: bool = False,
    proposal_dispatch_running: bool = False,
    last_dispatch_error: Optional[str] = None,
    last_proposal_dispatch_error: Optional[str] = None,
    selected_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    runs_payload = build_runs_payload(runs_dir)
    queue_payload = build_queue_payload(queue_path, runs_dir=runs_dir)
    proposals_payload = build_proposals_payload(proposals_path, runs_dir=runs_dir)
    focused_run_id = selected_run_id or _focused_run_id(
        proposals_payload=proposals_payload,
        queue_payload=queue_payload,
        runs_payload=runs_payload,
    )
    selected_run = build_run_payload(runs_dir, focused_run_id) if focused_run_id else None
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "version": _state_version(
            runs_dir=Path(runs_dir).expanduser().resolve(),
            queue_path=Path(queue_path).expanduser().resolve() if queue_path is not None else None,
            proposals_path=Path(proposals_path).expanduser().resolve() if proposals_path is not None else None,
        ),
        "runtime": {
            "generation": runtime_generation,
            "pid": os.getpid(),
            "dispatch_running": dispatch_running,
            "queue_dispatch_running": queue_dispatch_running,
            "proposal_dispatch_running": proposal_dispatch_running,
            "last_dispatch_error": last_dispatch_error,
            "last_proposal_dispatch_error": last_proposal_dispatch_error,
        },
        "cost_mode": _cost_mode_payload(cost_mode),
        "workspace_lanes": _workspace_lane_summary(
            proposals_payload=proposals_payload,
            queue_payload=queue_payload,
        ),
        "proposals": proposals_payload,
        "queue": queue_payload,
        "runs": runs_payload,
        "focused_run_id": focused_run_id,
        "selected_run": selected_run,
    }


def build_queue_payload(
    queue_path: Optional[Pathish],
    *,
    runs_dir: Optional[Pathish] = None,
) -> Dict[str, Any]:
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    if queue_path is None:
        return {
            "queue_file": None,
            "generated_at": generated_at,
            "queue": None,
            "tasks": [],
        }
    queue_file = Path(queue_path).expanduser().resolve()
    store = TaskStore(queue_file)
    try:
        queue = store.load()
    except (OSError, ValueError):
        return {
            "queue_file": str(queue_file),
            "generated_at": generated_at,
            "queue": None,
            "tasks": [],
        }
    run_store = RunStore(Path(runs_dir).expanduser().resolve()) if runs_dir is not None else None
    if run_store is not None:
        reconcile_queue(queue, run_loader=run_store.load, now_iso=run_store.now_iso)
    tasks = _annotate_task_lane_waits([_summarize_task(task, run_store=run_store) for task in queue.tasks])
    return {
        "queue_file": str(queue_file),
        "generated_at": generated_at,
        "queue": {
            "queue_id": queue.queue_id,
            "status": queue.status,
            "created_at": queue.created_at,
            "updated_at": queue.updated_at,
        },
        "summary": _queue_summary(tasks),
        "tasks": tasks,
    }


def build_run_payload(runs_dir: Pathish, run_id: str) -> Optional[Dict[str, Any]]:
    runs_path = Path(runs_dir).expanduser().resolve()
    if not _valid_run_id(run_id):
        return None
    manifest_path = runs_path / run_id / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return None
    events = RunStore(runs_path).load_events(run_id)
    return {
        "run": _summarize_manifest(manifest, events=events),
        "manifest": manifest,
        "evidence_files": _evidence_details(manifest),
        "events": events,
        "worker_activity": _worker_activity(manifest),
    }


def _focused_run_id(
    *,
    proposals_payload: Dict[str, Any],
    queue_payload: Dict[str, Any],
    runs_payload: Dict[str, Any],
) -> Optional[str]:
    for proposal in reversed(_list_value(proposals_payload.get("proposals"))):
        if not isinstance(proposal, dict):
            continue
        if proposal.get("waiting_for") == "human_plan_review" and proposal.get("run_id"):
            return str(proposal["run_id"])

    for task in reversed(_list_value(queue_payload.get("tasks"))):
        if not isinstance(task, dict):
            continue
        run_id = task.get("active_run_id")
        if run_id and task.get("waiting_for") != "done":
            return str(run_id)

    runs = _list_value(runs_payload.get("runs"))
    if runs and isinstance(runs[0], dict) and runs[0].get("run_id"):
        return str(runs[0]["run_id"])
    return None


def _state_version(
    *,
    runs_dir: Path,
    queue_path: Optional[Path],
    proposals_path: Optional[Path],
) -> int:
    mtimes: List[int] = []
    for path in (queue_path, proposals_path):
        if path is None:
            continue
        try:
            mtimes.append(path.stat().st_mtime_ns)
        except OSError:
            continue
    try:
        for path in runs_dir.glob("*/manifest.json"):
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
        for path in runs_dir.glob("*/events.jsonl"):
            try:
                mtimes.append(path.stat().st_mtime_ns)
            except OSError:
                continue
    except OSError:
        pass
    return max(mtimes, default=0)


def _workspace_lane_summary(
    *,
    proposals_payload: Dict[str, Any],
    queue_payload: Dict[str, Any],
) -> Dict[str, Any]:
    lanes: Dict[str, Dict[str, Any]] = {}
    for proposal in _list_value(proposals_payload.get("proposals")):
        if not isinstance(proposal, dict):
            continue
        workspace_id = proposal.get("workspace_id")
        if not workspace_id:
            continue
        lane = lanes.setdefault(str(workspace_id), _new_lane(str(workspace_id), proposal.get("cwd")))
        lane["proposals"] += 1
        status = str(proposal.get("status") or "")
        waiting_for = proposal.get("waiting_for")
        if status == PROPOSAL_FAILED:
            lane["failed"] += 1
            lane["status"] = "failed"
            lane["blocked_by"] = {"type": "proposal", "id": proposal.get("proposal_id")}
        elif waiting_for == "human_plan_review":
            lane["status"] = "waiting_review"
            lane["active_item"] = {"type": "proposal", "id": proposal.get("proposal_id"), "title": proposal.get("title")}
        elif status in {PROPOSAL_PLANNING, PROPOSAL_PLAN_REVISING}:
            lane["status"] = "active"
            lane["active_item"] = {"type": "proposal", "id": proposal.get("proposal_id"), "title": proposal.get("title")}
        elif status in {PROPOSAL_WAITING_WORKSPACE, PROPOSAL_WAITING_WORKSPACE_CLEAN}:
            lane["queued"] += 1
    for task in _list_value(queue_payload.get("tasks")):
        if not isinstance(task, dict):
            continue
        workspace_id = task.get("workspace_id")
        if not workspace_id:
            continue
        lane = lanes.setdefault(str(workspace_id), _new_lane(str(workspace_id), task.get("cwd")))
        status = str(task.get("status") or "")
        if status == "FAILED":
            lane["failed"] += 1
            lane["status"] = "failed"
            lane["blocked_by"] = {"type": "task", "id": task.get("task_id")}
        elif status in {"RUNNING", "WAITING"}:
            if lane["status"] not in {"failed"}:
                lane["status"] = "active"
                lane["active_item"] = {"type": "task", "id": task.get("task_id"), "title": task.get("title")}
        elif status == "PENDING":
            lane["queued"] += 1
    lane_list = sorted(lanes.values(), key=lambda item: str(item["workspace_id"]))
    return {
        "lanes": lane_list,
        "total_lanes": len(lane_list),
        "active_lanes": sum(1 for lane in lane_list if lane["status"] == "active"),
        "failed_lanes": sum(1 for lane in lane_list if lane["status"] == "failed"),
        "waiting_review_lanes": sum(1 for lane in lane_list if lane["status"] == "waiting_review"),
        "pending_lanes": sum(1 for lane in lane_list if lane["queued"] and lane["status"] == "idle"),
    }


def _new_lane(workspace_id: str, cwd: Any) -> Dict[str, Any]:
    root = str(cwd or workspace_id)
    return {
        "workspace_id": workspace_id,
        "workspace_root": root,
        "workspace_name": Path(root).name,
        "status": "idle",
        "active_item": None,
        "blocked_by": None,
        "queued": 0,
        "failed": 0,
        "proposals": 0,
    }


def _workspace_id(cwd: Any) -> Optional[str]:
    if not cwd:
        return None
    try:
        return str(canonical_git_root(str(cwd)))
    except (WorkspaceResolutionError, OSError):
        try:
            return str(Path(str(cwd)).expanduser().resolve())
        except OSError:
            return str(cwd)


def _load_manifests(runs_dir: Path) -> List[Dict[str, Any]]:
    manifests: List[Dict[str, Any]] = []
    if not runs_dir.exists():
        return manifests
    for path in sorted(runs_dir.glob("*/manifest.json")):
        manifest = _load_manifest(path)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _load_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return RunManifest.from_dict(data).to_dict()


def _summarize_manifest(
    manifest: Dict[str, Any],
    *,
    events: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    planner = _dict_value(manifest.get("planner"))
    workers = [_summarize_worker(worker) for worker in _list_value(manifest.get("workers"))]
    review = _dict_value(manifest.get("review"))
    review_attempts = [_dict_value(attempt) for attempt in _list_value(manifest.get("review_attempts"))]
    plan = _dict_value(manifest.get("plan"))
    plan_revisions = [_dict_value(revision) for revision in _list_value(manifest.get("plan_revisions"))]
    latest_plan_revision = plan_revisions[-1] if plan_revisions else {}
    status = str(manifest.get("status", "UNKNOWN"))
    event_list = events or []
    allowed_actions = _allowed_run_actions(manifest, event_list)
    last_error_event = _last_error_event(event_list)
    latest_revision_request = _latest_revision_request_summary(
        review=review,
        review_attempts=review_attempts,
    )
    latest_review_failure = _latest_review_failure_summary(
        review_attempts=review_attempts,
        last_error_event=last_error_event,
    )
    review_retry_count = sum(
        1
        for attempt in review_attempts
        if attempt.get("status") == REVIEW_ATTEMPT_FAILED_RETRYABLE
    )
    evidence_files = _unique_strings(
        _flatten(
            [
                worker.get("evidence_files", [])
                for worker in _list_value(manifest.get("workers"))
                if isinstance(worker, dict)
            ]
        )
        + _list_value(review.get("evidence_files"))
    )
    updated_at = str(manifest.get("updated_at", ""))
    created_at = str(manifest.get("created_at", ""))
    waiting_for = _derive_manifest_waiting_for(manifest)
    service_tiers = _service_tier_summary(manifest, workers=workers, review_attempts=review_attempts)
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    timing = build_timing_summary(manifest, events or [], now_iso=now_iso)
    return {
        "run_id": str(manifest.get("run_id", "")),
        "status": status,
        "status_index": RUN_STATUS_ORDER.get(status, 0),
        "terminal": status in TERMINAL_RUN_STATUSES,
        "waiting_for": waiting_for,
        "next_action": waiting_for,
        "allowed_actions": allowed_actions,
        "requires_restart": bool(manifest.get("requires_restart", False)),
        "restart_reason": manifest.get("restart_reason"),
        "restart_paths": _list_value(manifest.get("restart_paths")),
        "user_task": str(manifest.get("user_task", "")),
        "cwd": str(manifest.get("cwd", "")),
        "created_at": created_at,
        "updated_at": updated_at,
        "sort_key": updated_at or created_at,
        "planner": {
            "status": str(planner.get("status", "PENDING")),
            "model": str(planner.get("model", "")),
            "thread_id": planner.get("thread_id"),
            "reasoning_effort": planner.get("reasoning_effort"),
            "service_tier": planner.get("service_tier"),
        },
        "workers": workers,
        "service_tiers": service_tiers,
        "review": {
            "decision": review.get("decision"),
            "reason": review.get("reason"),
            "summary": review.get("summary"),
        } if review else None,
        "review_attempt_count": len(review_attempts),
        "review_retry_count": review_retry_count,
        "last_review_attempt": _review_attempt_summary(review_attempts[-1]) if review_attempts else None,
        "latest_revision_request": latest_revision_request,
        "latest_review_failure": latest_review_failure,
        "can_retry_review": has_retryable_review_failure_dict(manifest),
        "last_event": _event_summary(event_list[-1]) if event_list else None,
        "last_error_event": last_error_event,
        "timing": timing,
        "plan": {
            "approval_status": plan.get("approval_status"),
            "summary": plan.get("summary"),
        } if plan else None,
        "plan_revision_count": len(plan_revisions),
        "latest_plan_revision_id": latest_plan_revision.get("id"),
        "latest_plan_revision_created_at": latest_plan_revision.get("created_at"),
        "latest_plan_revision_feedback": latest_plan_revision.get("human_feedback"),
        "acceptance_count": len(_list_value(manifest.get("acceptance_criteria"))),
        "verification_count": len(_list_value(manifest.get("verification_commands"))),
        "evidence_count": len(evidence_files),
    }


def _cost_mode_payload(cost_mode: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = _dict_value(cost_mode)
    tiers = _dict_value(payload.get("effective_service_tiers"))
    return {
        "low_cost_mode": bool(payload.get("low_cost_mode", False)),
        "mode_label": payload.get("mode_label") or "unavailable",
        "effective_service_tiers": {
            "planner": tiers.get("planner"),
            "worker": tiers.get("worker"),
            "reviewer": tiers.get("reviewer"),
        },
        "toggle_available": bool(payload.get("toggle_available", False)),
    }


def _service_tier_summary(
    manifest: Dict[str, Any],
    *,
    workers: List[Dict[str, Any]],
    review_attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    planner = _dict_value(manifest.get("planner"))
    worker_tier = None
    for worker in reversed(workers):
        worker_tier = worker.get("service_tier")
        if worker_tier is not None:
            break
    reviewer_tier = manifest.get("reviewer_service_tier")
    reviewer_source = "manifest"
    if reviewer_tier is None:
        reviewer_source = "review_attempt"
        reviewer_tier = _latest_review_attempt_service_tier(review_attempts)
    if reviewer_tier is None:
        reviewer_source = "unset"
    return {
        "planner": planner.get("service_tier"),
        "worker": worker_tier,
        "reviewer": reviewer_tier,
        "reviewer_source": reviewer_source,
    }


def _latest_review_attempt_service_tier(review_attempts: List[Dict[str, Any]]) -> Optional[str]:
    for attempt in reversed(review_attempts):
        service_tier = attempt.get("service_tier")
        if isinstance(service_tier, str) and service_tier.strip():
            return service_tier.strip()
    return None


def _summarize_task(task: Any, *, run_store: Optional[RunStore]) -> Dict[str, Any]:
    active_run = None
    active_run_events: List[Dict[str, Any]] = []
    if run_store is not None and task.active_run_id:
        try:
            active_run = run_store.load(task.active_run_id)
            active_run_events = run_store.load_events(task.active_run_id)
        except OSError:
            active_run = None
    task_cwd = task.cwd or (active_run.cwd if active_run is not None else None)
    progress = derive_task_progress(task, active_run=active_run)
    last_error_event = _last_error_event(active_run_events)
    return {
        "task_id": task.task_id,
        "title": task.title,
        "cwd": task_cwd,
        "workspace_id": _workspace_id(task_cwd),
        "status": task.status,
        "active_run_id": task.active_run_id,
        "run_ids": list(task.run_ids),
        "updated_at": task.updated_at,
        "completed_at": task.completed_at,
        "reason": task.reason,
        "error": task.error,
        "failure_summary": _task_failure_summary(task, last_error_event),
        "last_error_event": last_error_event,
        "waiting_for": progress.waiting_for,
        "next_action": progress.waiting_for,
        "blocked_by": None,
        "allowed_actions": _allowed_task_actions(task, active_run=active_run, events=active_run_events),
    }


def _summarize_proposal(proposal: ProposalRecord, *, run_store: Optional[RunStore]) -> Dict[str, Any]:
    run_summary = None
    manifest = None
    plan_detail = None
    if run_store is not None and proposal.run_id:
        try:
            manifest = run_store.load(proposal.run_id)
        except OSError:
            manifest = None
    if manifest is not None:
        manifest_dict = manifest.to_dict()
        run_summary = _summarize_manifest(
            manifest_dict,
            events=run_store.load_events(manifest.run_id) if run_store is not None else [],
        )
        plan = _dict_value(manifest_dict.get("plan"))
        if plan:
            plan_detail = {
                "approval_status": plan.get("approval_status"),
                "summary": plan.get("summary"),
                "worker_prompt": plan.get("worker_prompt"),
                "risk_notes": _list_value(plan.get("risk_notes")),
                "acceptance_criteria": _list_value(manifest_dict.get("acceptance_criteria")),
                "verification_commands": _list_value(manifest_dict.get("verification_commands")),
            }
    proposal_cwd = proposal.cwd or (manifest.cwd if manifest is not None else None)
    status = proposal.status
    if manifest is not None and status not in {
        PROPOSAL_QUEUED,
        PROPOSAL_FAILED,
        PROPOSAL_WAITING_WORKSPACE,
        PROPOSAL_WAITING_WORKSPACE_CLEAN,
    }:
        status = _proposal_status_from_run(manifest)
    return {
        "proposal_id": proposal.proposal_id,
        "title": proposal.title,
        "prompt": proposal.prompt,
        "cwd": proposal_cwd,
        "workspace_id": _workspace_id(proposal_cwd),
        "status": status,
        "run_id": proposal.run_id,
        "task_id": proposal.task_id,
        "created_at": proposal.created_at,
        "updated_at": proposal.updated_at,
        "error": proposal.error,
        "reason": proposal.reason,
        "blocker": proposal.blocker,
        "waiting_for": _proposal_waiting_for(status),
        "allowed_actions": _allowed_proposal_actions(status, run_summary, reason=proposal.reason),
        "run": run_summary,
        "plan_detail": plan_detail,
    }


def _proposal_summary(proposals: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(proposals)
    review_required = sum(1 for proposal in proposals if proposal.get("status") == PROPOSAL_PLAN_REVIEW_REQUIRED)
    queued = sum(1 for proposal in proposals if proposal.get("status") == PROPOSAL_QUEUED)
    failed = sum(1 for proposal in proposals if proposal.get("status") == PROPOSAL_FAILED)
    active = sum(
        1
        for proposal in proposals
        if proposal.get("status") in {PROPOSAL_PLANNING, PROPOSAL_PLAN_REVISING}
    )
    waiting_workspace = sum(1 for proposal in proposals if proposal.get("status") == PROPOSAL_WAITING_WORKSPACE)
    waiting_workspace_clean = sum(
        1 for proposal in proposals if proposal.get("status") == PROPOSAL_WAITING_WORKSPACE_CLEAN
    )
    return {
        "total_proposals": total,
        "review_required": review_required,
        "queued": queued,
        "failed": failed,
        "active": active,
        "waiting_workspace": waiting_workspace,
        "waiting_workspace_clean": waiting_workspace_clean,
    }


def _proposal_waiting_for(status: str) -> str:
    if status == PROPOSAL_WAITING_WORKSPACE:
        return "workspace_lane"
    if status == PROPOSAL_WAITING_WORKSPACE_CLEAN:
        return "workspace_clean"
    if status == PROPOSAL_PLAN_REVIEW_REQUIRED:
        return "human_plan_review"
    if status == PROPOSAL_PLAN_REVISING:
        return "planner_revision"
    if status == PROPOSAL_QUEUED:
        return "execution_queue"
    if status == PROPOSAL_FAILED:
        return "failed"
    if status == PROPOSAL_APPROVED:
        return "queue_approval"
    return "planner"


def _allowed_proposal_actions(
    status: str,
    run_summary: Optional[Dict[str, Any]],
    *,
    reason: Optional[str],
) -> List[str]:
    if status == PROPOSAL_WAITING_WORKSPACE_CLEAN:
        return ["retry-plan"]
    if status == PROPOSAL_FAILED and reason in {
        "workspace_resolution_failed",
        "workspace_git_status_failed",
        "proposal_preflight_failed",
        "workspace_clean",
    }:
        return ["retry-plan"]
    if status != PROPOSAL_PLAN_REVIEW_REQUIRED:
        return []
    if not run_summary or not run_summary.get("plan"):
        return []
    return ["approve-plan", "revise-plan"]


def _queue_summary(tasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(tasks)
    approved = sum(1 for task in tasks if task.get("status") == "APPROVED")
    skipped = sum(1 for task in tasks if task.get("status") == TASK_SKIPPED)
    pending = sum(1 for task in tasks if task.get("status") == "PENDING")
    failed = sum(1 for task in tasks if task.get("status") == "FAILED")
    running = sum(1 for task in tasks if task.get("status") in {"RUNNING", "WAITING"})
    current = next((task for task in tasks if task.get("status") not in {"APPROVED", TASK_SKIPPED}), None)
    return {
        "total_tasks": total,
        "approved_tasks": approved,
        "completed_tasks": approved + skipped,
        "skipped_tasks": skipped,
        "pending_tasks": pending,
        "failed_tasks": failed,
        "running_tasks": running,
        "current_waiting_point": current.get("waiting_for") if current else "done",
    }


def _annotate_task_lane_waits(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lane_blockers: Dict[str, Dict[str, Any]] = {}
    seen_active: set[str] = set()
    for task in tasks:
        workspace_id = task.get("workspace_id")
        if not workspace_id:
            continue
        lane_key = str(workspace_id)
        if task.get("status") in {"APPROVED", TASK_SKIPPED}:
            continue
        blocker = lane_blockers.get(lane_key)
        if blocker is not None and task.get("status") == "PENDING":
            task["waiting_for"] = "failed_workspace" if blocker.get("status") == "FAILED" else "workspace_lane"
            task["next_action"] = task["waiting_for"]
            task["blocked_by"] = {
                "type": "task",
                "id": blocker.get("task_id"),
                "title": blocker.get("title"),
                "status": blocker.get("status"),
            }
            continue
        if lane_key in seen_active and task.get("status") == "PENDING":
            task["waiting_for"] = "workspace_lane"
            task["next_action"] = "workspace_lane"
            continue
        seen_active.add(lane_key)
        if task.get("status") in {"FAILED", "BLOCKED", "RUNNING", "WAITING"}:
            lane_blockers[lane_key] = task
    return tasks


def _allowed_run_actions(manifest: Dict[str, Any], events: List[Dict[str, Any]]) -> List[str]:
    actions: List[str] = []
    if has_retryable_review_failure_dict(manifest):
        actions.append("retry-review")
    if has_retryable_verification_failure_dict(manifest, events):
        actions.append("retry-verification")
    return actions


def _allowed_task_actions(
    task: Any,
    *,
    active_run: Optional[RunManifest] = None,
    events: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    if getattr(task, "status", None) == "FAILED":
        actions: List[str] = []
        if active_run is not None and has_retryable_verification_failure(active_run, events or []):
            actions.append("retry-verification")
        actions.append("retry-task")
        actions.append("mark-handled-skipped")
        return actions
    return []


def _task_failure_summary(task: Any, last_error_event: Optional[Dict[str, Any]]) -> Optional[str]:
    if last_error_event:
        for key in ("summary", "message", "reason", "error"):
            value = last_error_event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for value in (getattr(task, "error", None), getattr(task, "reason", None)):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _derive_manifest_waiting_for(manifest: Dict[str, Any]) -> str:
    try:
        return derive_run_waiting_for(RunManifest.from_dict(manifest))
    except Exception:
        status = str(manifest.get("status", ""))
        if status == "APPROVED":
            return "restart" if manifest.get("requires_restart") else "done"
        if status == "FAILED":
            return "failed"
        return "planner"


def _latest_revision_request_summary(
    *,
    review: Dict[str, Any],
    review_attempts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for attempt in reversed(review_attempts):
        if attempt.get("decision") != "revision_requested":
            continue
        summary = _review_summary_text(attempt)
        return {
            "review_attempt_id": attempt.get("id"),
            "worker_attempt": attempt.get("worker_attempt"),
            "decision": "revision_requested",
            "summary": summary,
            "reason": attempt.get("reason"),
            "completed_at": attempt.get("completed_at"),
            "source": "review_attempt",
        }
    if review.get("decision") != "revision_requested":
        return None
    return {
        "review_attempt_id": None,
        "worker_attempt": None,
        "decision": "revision_requested",
        "summary": _review_summary_text(review),
        "reason": review.get("reason"),
        "completed_at": None,
        "source": "review",
    }


def _latest_review_failure_summary(
    *,
    review_attempts: List[Dict[str, Any]],
    last_error_event: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for attempt in reversed(review_attempts):
        if attempt.get("status") != REVIEW_ATTEMPT_FAILED_RETRYABLE:
            continue
        return {
            "review_attempt_id": attempt.get("id"),
            "worker_attempt": attempt.get("worker_attempt"),
            "status": attempt.get("status"),
            "reason": attempt.get("reason"),
            "summary": _first_non_empty(
                _string_or_none(attempt.get("summary")),
                _first_sentence(_string_or_none(attempt.get("error"))),
                _first_sentence(_string_or_none(attempt.get("reason"))),
            ),
            "error": attempt.get("error"),
            "completed_at": attempt.get("completed_at"),
            "source": "review_attempt",
        }
    if not isinstance(last_error_event, dict):
        return None
    event_type = str(last_error_event.get("type") or "")
    if event_type not in {"code_review_failed", "planner_review_failed"}:
        return None
    summary = _first_non_empty(
        _string_or_none(last_error_event.get("summary")),
        _first_sentence(_string_or_none(last_error_event.get("message"))),
        _first_sentence(_string_or_none(last_error_event.get("error"))),
        _first_sentence(_string_or_none(last_error_event.get("reason"))),
    )
    return {
        "review_attempt_id": None,
        "worker_attempt": last_error_event.get("attempt"),
        "status": REVIEW_ATTEMPT_FAILED_RETRYABLE,
        "reason": last_error_event.get("reason"),
        "summary": summary,
        "error": last_error_event.get("error"),
        "completed_at": last_error_event.get("timestamp"),
        "source": "event",
    }


def _review_summary_text(payload: Dict[str, Any]) -> Optional[str]:
    summary = _string_or_none(payload.get("summary"))
    if summary:
        return summary
    return _first_sentence(_string_or_none(payload.get("reason")))


def _first_sentence(value: Optional[str]) -> Optional[str]:
    text = _string_or_none(value)
    if not text:
        return None
    match = _SENTENCE_BOUNDARY_PATTERN.search(text)
    if not match:
        return text
    sentence = text[: match.end()].strip()
    return sentence or text


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        text = _string_or_none(value)
        if text:
            return text
    return None


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _review_attempt_summary(attempt: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": attempt.get("id"),
        "status": attempt.get("status"),
        "worker_attempt": attempt.get("worker_attempt"),
        "workspace_path": attempt.get("workspace_path"),
        "service_tier": attempt.get("service_tier"),
        "evidence_count": len(_list_value(attempt.get("evidence_files"))),
        "decision": attempt.get("decision"),
        "reason": attempt.get("reason"),
        "summary": attempt.get("summary"),
        "error": attempt.get("error"),
        "completed_at": attempt.get("completed_at"),
    }


def _event_summary(event: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "timestamp": event.get("timestamp"),
        "type": event.get("type"),
        "message": event.get("message"),
        "summary": event.get("summary"),
        "reason": event.get("reason"),
        "worker_id": event.get("worker_id"),
        "attempt": event.get("attempt"),
        "decision": event.get("decision"),
        "error": event.get("error"),
        "applied": event.get("applied"),
    }


def _last_error_event(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        summary = event.get("summary")
        if (
            "error" in event
            or str(event.get("type", "")).endswith("_failed")
            or event.get("applied") is False
            or (isinstance(summary, str) and summary.startswith("Failed:"))
        ):
            return _event_summary(event)
    return None


def _summarize_worker(worker: Any) -> Dict[str, Any]:
    data = _dict_value(worker)
    evidence_files = _list_value(data.get("evidence_files"))
    return {
        "id": str(data.get("id", "")),
        "status": str(data.get("status", "PENDING")),
        "model": str(data.get("model", "")),
        "thread_id": data.get("thread_id"),
        "reasoning_effort": data.get("reasoning_effort"),
        "service_tier": data.get("service_tier"),
        "attempt": data.get("attempt", 1),
        "worktree_path": data.get("worktree_path"),
        "evidence_count": len(evidence_files),
    }


def _evidence_details(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths = []
    for worker in _list_value(manifest.get("workers")):
        if isinstance(worker, dict):
            paths.extend(_list_value(worker.get("evidence_files")))
    review = _dict_value(manifest.get("review"))
    paths.extend(_list_value(review.get("evidence_files")))
    details = []
    for value in _unique_strings(paths):
        path = Path(value).expanduser()
        exists = path.exists()
        size = path.stat().st_size if exists else None
        details.append(
            {
                "path": value,
                "name": path.name,
                "exists": exists,
                "size": size,
                "preview": _text_preview(path, size=size) if exists else None,
            }
        )
    return details


def _text_preview(path: Path, *, size: Optional[int]) -> Optional[str]:
    if size is None or size > 20_000:
        return None
    if path.suffix.lower() not in {".txt", ".md", ".json", ".jsonl", ".log"}:
        return None
    try:
        return path.read_text(encoding="utf-8")[:8_000]
    except UnicodeDecodeError:
        return None
    except OSError:
        return None


def _worker_activity(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    store = CodexSessionLogStore()
    activities: List[Dict[str, Any]] = []
    for worker in _list_value(manifest.get("workers")):
        data = _dict_value(worker)
        thread_id = data.get("thread_id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        for item in store.load_recent_activity(thread_id=thread_id, limit=60):
            value = item.to_dict()
            value["worker_id"] = str(data.get("id", "worker"))
            activities.append(value)
    return activities[-80:]


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _flatten(values: Sequence[Any]) -> List[Any]:
    result: List[Any] = []
    for value in values:
        if isinstance(value, list):
            result.extend(value)
        else:
            result.append(value)
    return result


def _unique_strings(values: Sequence[Any]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result
