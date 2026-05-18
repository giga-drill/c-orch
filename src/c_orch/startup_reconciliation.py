from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .failure_policy import (
    SOURCE_RUNTIME_STARTUP,
    classify_apply_failure,
    classify_git_commit_failure,
    classify_retry_backoff_decision,
    classify_retryable_review_failure,
    classify_verification_gate_failure,
)
from .phase_timing import record_run_status_transition
from .run_store import ReviewAttemptRecord, ReviewRecord, RunManifest, RunStore
from .runner_leases import LEASE_STATUS_ACTIVE, LEASE_STATUS_EXPIRED, LEASE_STATUS_FAILED
from .runner_subprocess import (
    RUNNER_PHASE_CODE_REVIEW,
    RUNNER_RESULT_COMPLETED,
    RUNNER_RESULT_FAILED,
)
from .states import (
    REVIEW_ATTEMPT_FAILED_RETRYABLE,
    RUN_FAILED,
    RUN_NEW,
    RUN_PLAN_APPROVED,
    RUN_PLAN_READY,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_PLAN_REVISING,
    RUN_PLANNING,
    RUN_REVIEWING,
    RUN_REVISION_REQUESTED,
    RUN_STATUS_ORDER,
    RUN_WORK_DONE,
    RUN_WORKING,
    TERMINAL_RUN_STATUSES,
    WORKER_DONE,
)


CATEGORY_RUNNER_ALIVE = "runner_alive"
CATEGORY_RUNNER_STALE = "runner_stale"
CATEGORY_RUNNER_COMPLETED = "runner_completed"
CATEGORY_RUNNER_FAILED = "runner_failed"

RECOVERY_NOOP_KEEP_RUNNING = "noop_keep_running"
RECOVERY_IMPORT_AND_RETRY_REVIEW = "import_and_retry_review"
RECOVERY_IMPORT_AND_FAIL_REVIEW = "import_and_fail_review"
RECOVERY_TERMINALIZE_FAILED = "terminalize_failed"
RECOVERY_SKIP_NOT_RELEVANT = "skip_not_relevant"


@dataclass(frozen=True)
class StartupReconciliationDecision:
    run_id: str
    runner_id: str
    phase: str
    category: str
    reason: str
    recovery_action: str
    automatic: bool
    requires_human: bool
    retryable: bool
    lease_status: str
    effective_status: str
    result_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "runner_id": self.runner_id,
            "phase": self.phase,
            "category": self.category,
            "reason": self.reason,
            "recovery_action": self.recovery_action,
            "automatic": self.automatic,
            "requires_human": self.requires_human,
            "retryable": self.retryable,
            "lease_status": self.lease_status,
            "effective_status": self.effective_status,
            "result_path": self.result_path,
        }


def reconcile_runtime_startup_leases(
    *,
    run_store: RunStore,
    runtime_generation: Optional[str] = None,
    process_hint: Optional[str] = None,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    decisions: List[Dict[str, Any]] = []
    for lease_path in sorted(run_store.runs_dir.glob("*/runner-leases.json")):
        run_id = lease_path.parent.name
        try:
            manifest = run_store.load(run_id)
        except OSError:
            continue
        events = run_store.load_events(run_id)
        lease_store = run_store.runner_lease_store(run_id)
        leases_payload = lease_store.read(now=now).to_dict()
        for lease in leases_payload.get("leases", []):
            if not isinstance(lease, dict):
                continue
            runner_id = str(lease.get("runner_id", "")).strip()
            phase = str(lease.get("phase", "")).strip()
            if not runner_id or not phase:
                continue
            status = str(lease.get("status", "")).strip().lower()
            effective_status = str(lease.get("effective_status", status)).strip().lower()
            if status == LEASE_STATUS_EXPIRED:
                continue
            decision = _reconcile_single_lease(
                run_store=run_store,
                lease_store=lease_store,
                manifest=manifest,
                events=events,
                lease=lease,
                status=status,
                effective_status=effective_status,
                runtime_generation=runtime_generation,
                process_hint=process_hint,
            )
            if decision is not None:
                decisions.append(decision.to_dict())
    return decisions


def _reconcile_single_lease(
    *,
    run_store: RunStore,
    lease_store,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    lease: Dict[str, Any],
    status: str,
    effective_status: str,
    runtime_generation: Optional[str],
    process_hint: Optional[str],
) -> Optional[StartupReconciliationDecision]:
    runner_id = str(lease.get("runner_id") or "")
    phase = str(lease.get("phase") or "")
    pid_int = _lease_liveness_pid(phase=phase, lease=lease)
    requires_external_liveness = _requires_external_subprocess_liveness(phase=phase, lease=lease)
    result_path = _string_or_none(lease.get("checkpoint", {}).get("result_path")) if isinstance(lease.get("checkpoint"), dict) else None

    if manifest.status in TERMINAL_RUN_STATUSES:
        terminal_decision = _handle_terminal_manifest_lease(
            run_store=run_store,
            lease_store=lease_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=phase,
            lease=lease,
            status=status,
            effective_status=effective_status,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            pid_int=pid_int,
            result_path=result_path,
        )
        return terminal_decision

    if not _is_lease_relevant_to_manifest(manifest=manifest, lease=lease, phase=phase):
        decision = StartupReconciliationDecision(
            run_id=manifest.run_id,
            runner_id=runner_id,
            phase=phase,
            category=CATEGORY_RUNNER_ALIVE,
            reason="lease_not_relevant_to_current_manifest",
            recovery_action=RECOVERY_SKIP_NOT_RELEVANT,
            automatic=True,
            requires_human=False,
            retryable=False,
            lease_status=status,
            effective_status=effective_status,
            result_path=result_path,
        )
        _record_startup_decision_event(
            run_store=run_store,
            events=events,
            decision=decision,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            lease=lease,
        )
        return decision

    if phase == RUNNER_PHASE_CODE_REVIEW:
        prefetched = _load_code_review_result_if_exists(
            run_store=run_store,
            run_id=manifest.run_id,
            runner_id=runner_id,
        )
        if prefetched is not None:
            return _recover_code_review_result(
                run_store=run_store,
                manifest=manifest,
                events=events,
                runner_id=runner_id,
                lease=lease,
                runtime_generation=runtime_generation,
                process_hint=process_hint,
                prefetched_result=prefetched,
            )

    if status == LEASE_STATUS_ACTIVE and effective_status == LEASE_STATUS_ACTIVE:
        if pid_int is not None and not _pid_is_alive(pid_int):
            effective_status = "stale"
        elif requires_external_liveness and pid_int is None:
            # External subprocess lease cannot be proven alive without subprocess metadata.
            effective_status = "stale"
        else:
            decision = StartupReconciliationDecision(
                run_id=manifest.run_id,
                runner_id=runner_id,
                phase=phase,
                category=CATEGORY_RUNNER_ALIVE,
                reason="lease_active_process_alive",
                recovery_action=RECOVERY_NOOP_KEEP_RUNNING,
                automatic=True,
                requires_human=False,
                retryable=False,
                lease_status=status,
                effective_status=effective_status,
                result_path=result_path,
            )
            _record_startup_decision_event(
                run_store=run_store,
                events=events,
                decision=decision,
                runtime_generation=runtime_generation,
                process_hint=process_hint,
                lease=lease,
            )
            return decision

    if status == LEASE_STATUS_ACTIVE and effective_status == "stale":
        if requires_external_liveness and pid_int is not None and _pid_is_alive(pid_int):
            decision = StartupReconciliationDecision(
                run_id=manifest.run_id,
                runner_id=runner_id,
                phase=phase,
                category=CATEGORY_RUNNER_ALIVE,
                reason="lease_stale_but_subprocess_alive",
                recovery_action=RECOVERY_NOOP_KEEP_RUNNING,
                automatic=True,
                requires_human=False,
                retryable=False,
                lease_status=status,
                effective_status=effective_status,
                result_path=result_path,
            )
            _record_startup_decision_event(
                run_store=run_store,
                events=events,
                decision=decision,
                runtime_generation=runtime_generation,
                process_hint=process_hint,
                lease=lease,
            )
            return decision
        if pid_int is not None and not _pid_is_alive(pid_int):
            lease_store.fail(runner_id, error="startup reconciliation: runner pid is not alive")
            status = LEASE_STATUS_FAILED
        elif requires_external_liveness:
            lease_store.fail(
                runner_id,
                error="startup reconciliation: external runner subprocess pid/process_hint missing",
            )
            status = LEASE_STATUS_FAILED
        else:
            lease_store.expire()
            status = LEASE_STATUS_EXPIRED
        stale_decision = _recover_stale_or_failed_runner(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=phase,
            lease=lease,
            result_path=result_path,
            reason="lease_stale_at_startup",
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            stale=True,
        )
        return stale_decision

    if status == "completed":
        return _reconcile_completed_runner(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=phase,
            lease=lease,
            result_path=result_path,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
        )

    if status == LEASE_STATUS_FAILED:
        return _recover_stale_or_failed_runner(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=phase,
            lease=lease,
            result_path=result_path,
            reason="lease_failed",
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            stale=False,
        )
    return None


def _handle_terminal_manifest_lease(
    *,
    run_store: RunStore,
    lease_store,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    runner_id: str,
    phase: str,
    lease: Dict[str, Any],
    status: str,
    effective_status: str,
    runtime_generation: Optional[str],
    process_hint: Optional[str],
    pid_int: Optional[int],
    result_path: Optional[str],
) -> StartupReconciliationDecision:
    cleanup = "none"
    stale_by_pid = status == LEASE_STATUS_ACTIVE and pid_int is not None and not _pid_is_alive(pid_int)
    if status == LEASE_STATUS_ACTIVE and effective_status == "stale":
        lease_store.expire()
        cleanup = "expired_stale_active"
        status = LEASE_STATUS_EXPIRED
        effective_status = LEASE_STATUS_EXPIRED
    elif stale_by_pid:
        cleanup = "pid_not_alive_observed"
        effective_status = "stale"
    decision = StartupReconciliationDecision(
        run_id=manifest.run_id,
        runner_id=runner_id,
        phase=phase,
        category=CATEGORY_RUNNER_COMPLETED,
        reason=f"terminal_run_lease_ignored:{manifest.status}:{cleanup}",
        recovery_action=RECOVERY_NOOP_KEEP_RUNNING,
        automatic=True,
        requires_human=False,
        retryable=False,
        lease_status=status,
        effective_status=effective_status,
        result_path=result_path,
    )
    _record_startup_decision_event(
        run_store=run_store,
        events=events,
        decision=decision,
        runtime_generation=runtime_generation,
        process_hint=process_hint,
        lease=lease,
    )
    return decision


def _is_lease_relevant_to_manifest(
    *,
    manifest: RunManifest,
    lease: Dict[str, Any],
    phase: str,
) -> bool:
    manifest_rank = RUN_STATUS_ORDER.get(manifest.status)
    checkpoint = lease.get("checkpoint")
    checkpoint_status = None
    review_attempt_id = None
    if isinstance(checkpoint, dict):
        checkpoint_status = _string_or_none(checkpoint.get("run_status"))
        review_attempt_id = _string_or_none(checkpoint.get("review_attempt_id"))
    if checkpoint_status and manifest_rank is not None:
        checkpoint_rank = RUN_STATUS_ORDER.get(checkpoint_status)
        if checkpoint_rank is not None and checkpoint_rank < manifest_rank:
            # Historical checkpoint from an earlier run phase should not overwrite current state.
            if phase != RUNNER_PHASE_CODE_REVIEW:
                return False
    relevant = _relevant_phases_for_status(manifest.status)
    if phase not in relevant:
        return False
    if phase == RUNNER_PHASE_CODE_REVIEW:
        latest_attempt_id = _latest_review_attempt_id(manifest)
        if latest_attempt_id and review_attempt_id and review_attempt_id != latest_attempt_id:
            return False
    return True


def _relevant_phases_for_status(status: str) -> set[str]:
    mapping = {
        RUN_NEW: {"planner_plan", "planner_revise"},
        RUN_PLANNING: {"planner_plan", "planner_revise"},
        RUN_PLAN_READY: {"planner_plan", "planner_revise"},
        RUN_PLAN_REVIEW_REQUIRED: {"planner_plan", "planner_revise"},
        RUN_PLAN_REVISING: {"planner_revise"},
        RUN_PLAN_APPROVED: {"worker_implement", "worker_rework"},
        RUN_WORKING: {"worker_implement", "worker_rework", "verification"},
        RUN_WORK_DONE: {"verification", "planner_review", RUNNER_PHASE_CODE_REVIEW, "apply_terminalization", "commit_terminalization"},
        RUN_REVIEWING: {"planner_review", RUNNER_PHASE_CODE_REVIEW, "apply_terminalization", "commit_terminalization"},
        RUN_REVISION_REQUESTED: {"worker_rework"},
    }
    return mapping.get(status, set())


def _latest_review_attempt_id(manifest: RunManifest) -> Optional[str]:
    if not manifest.review_attempts:
        return None
    return _string_or_none(manifest.review_attempts[-1].id)


def _load_code_review_result_if_exists(
    *,
    run_store: RunStore,
    run_id: str,
    runner_id: str,
):
    try:
        return run_store.load_runner_subprocess_result(
            run_id,
            phase=RUNNER_PHASE_CODE_REVIEW,
            runner_id=runner_id,
        )
    except Exception:
        return None


def _is_result_relevant_to_manifest(
    *,
    manifest: RunManifest,
    lease: Dict[str, Any],
    result_path: Optional[str],
) -> bool:
    checkpoint = lease.get("checkpoint")
    review_attempt_id = None
    if isinstance(checkpoint, dict):
        review_attempt_id = _string_or_none(checkpoint.get("review_attempt_id"))
    latest_attempt = _latest_review_attempt_id(manifest)
    if review_attempt_id and latest_attempt and review_attempt_id != latest_attempt:
        return False
    if manifest.status not in {RUN_REVIEWING, RUN_WORK_DONE}:
        return False
    if result_path:
        path_text = result_path
        if latest_attempt and f"/{latest_attempt}/" not in path_text and f"{latest_attempt}" not in path_text:
            # Path hint is only a weak signal; do not hard-reject when no clear mismatch.
            pass
    return True


def _reconcile_completed_runner(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    runner_id: str,
    phase: str,
    lease: Dict[str, Any],
    result_path: Optional[str],
    runtime_generation: Optional[str],
    process_hint: Optional[str],
) -> StartupReconciliationDecision:
    if phase != RUNNER_PHASE_CODE_REVIEW:
        decision = StartupReconciliationDecision(
            run_id=manifest.run_id,
            runner_id=runner_id,
            phase=phase,
            category=CATEGORY_RUNNER_COMPLETED,
            reason="completed_runner_already_terminal",
            recovery_action=RECOVERY_NOOP_KEEP_RUNNING,
            automatic=True,
            requires_human=False,
            retryable=False,
            lease_status="completed",
            effective_status="completed",
            result_path=result_path,
        )
        _record_startup_decision_event(
            run_store=run_store,
            events=events,
            decision=decision,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            lease=lease,
        )
        return decision

    return _recover_code_review_result(
        run_store=run_store,
        manifest=manifest,
        events=events,
        runner_id=runner_id,
        lease=lease,
        runtime_generation=runtime_generation,
        process_hint=process_hint,
    )


def _recover_stale_or_failed_runner(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    runner_id: str,
    phase: str,
    lease: Dict[str, Any],
    result_path: Optional[str],
    reason: str,
    runtime_generation: Optional[str],
    process_hint: Optional[str],
    stale: bool,
) -> StartupReconciliationDecision:
    if phase == RUNNER_PHASE_CODE_REVIEW:
        return _mark_retryable_review_failure(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=phase,
            reason="code_review_failed",
            error=f"startup reconciliation: {reason}",
            event_type="code_review_failed",
            event_message="Code review failed before Planner review",
            evidence_files=[],
            lease=lease,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            category=CATEGORY_RUNNER_STALE if stale else CATEGORY_RUNNER_FAILED,
            recovery_action=RECOVERY_IMPORT_AND_FAIL_REVIEW,
            result_path=result_path,
        )

    if phase == "verification":
        _ensure_verification_failed_state(run_store=run_store, manifest=manifest, events=events, reason=reason)
        policy = classify_verification_gate_failure(
            source=SOURCE_RUNTIME_STARTUP,
            attempt=_worker_attempt(manifest),
            error=f"startup reconciliation: {reason}",
        )
        decision = StartupReconciliationDecision(
            run_id=manifest.run_id,
            runner_id=runner_id,
            phase=phase,
            category=policy.category,
            reason=policy.reason,
            recovery_action=policy.recovery_action,
            automatic=policy.automatic,
            requires_human=policy.requires_human,
            retryable=policy.retryable,
            lease_status=str(lease.get("status") or ""),
            effective_status="stale" if stale else "failed",
            result_path=result_path,
        )
        _record_startup_decision_event(
            run_store=run_store,
            events=events,
            decision=decision,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            lease=lease,
            error=f"startup reconciliation: {reason}",
        )
        return decision

    _ensure_failed_manifest_state(
        run_store=run_store,
        manifest=manifest,
        events=events,
        reason=f"{phase}_{reason}",
    )
    if phase == "apply_terminalization":
        policy = classify_apply_failure(
            source=SOURCE_RUNTIME_STARTUP,
            attempt=_worker_attempt(manifest),
            error=f"startup reconciliation: {reason}",
        )
    elif phase == "commit_terminalization":
        policy = classify_git_commit_failure(
            failure_reason="generic_git_error",
            summary=f"startup reconciliation: {reason}",
            source=SOURCE_RUNTIME_STARTUP,
            attempt=_worker_attempt(manifest),
        )
    else:
        policy = classify_apply_failure(
            source=SOURCE_RUNTIME_STARTUP,
            attempt=_worker_attempt(manifest),
            error=f"startup reconciliation: {phase}:{reason}",
        )
    decision = StartupReconciliationDecision(
        run_id=manifest.run_id,
        runner_id=runner_id,
        phase=phase,
        category=policy.category,
        reason=policy.reason,
        recovery_action=RECOVERY_TERMINALIZE_FAILED,
        automatic=False,
        requires_human=True,
        retryable=False,
        lease_status=str(lease.get("status") or ""),
        effective_status="stale" if stale else "failed",
        result_path=result_path,
    )
    _record_startup_decision_event(
        run_store=run_store,
        events=events,
        decision=decision,
        runtime_generation=runtime_generation,
        process_hint=process_hint,
        lease=lease,
        error=f"startup reconciliation: {reason}",
    )
    return decision


def _recover_code_review_result(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    runner_id: str,
    lease: Dict[str, Any],
    runtime_generation: Optional[str],
    process_hint: Optional[str],
    prefetched_result=None,
) -> StartupReconciliationDecision:
    if prefetched_result is None:
        try:
            result = run_store.load_runner_subprocess_result(
                manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                runner_id=runner_id,
            )
        except Exception as exc:
            return _mark_retryable_review_failure(
                run_store=run_store,
                manifest=manifest,
                events=events,
                runner_id=runner_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                reason="code_review_failed",
                error=f"startup reconciliation: runner result unreadable: {exc}",
                event_type="code_review_failed",
                event_message="Code review failed before Planner review",
                evidence_files=[],
                lease=lease,
                runtime_generation=runtime_generation,
                process_hint=process_hint,
                category=CATEGORY_RUNNER_FAILED,
                recovery_action=RECOVERY_IMPORT_AND_FAIL_REVIEW,
                result_path=None,
            )
    else:
        result = prefetched_result
    try:
        result_path_hint = result.result_path or None
    except Exception:
        result_path_hint = None
    if not _is_result_relevant_to_manifest(manifest=manifest, lease=lease, result_path=result_path_hint):
        decision = StartupReconciliationDecision(
            run_id=manifest.run_id,
            runner_id=runner_id,
            phase=RUNNER_PHASE_CODE_REVIEW,
            category=CATEGORY_RUNNER_COMPLETED,
            reason="completed_result_not_relevant_to_current_attempt",
            recovery_action=RECOVERY_SKIP_NOT_RELEVANT,
            automatic=True,
            requires_human=False,
            retryable=False,
            lease_status=str(lease.get("status") or ""),
            effective_status=str(lease.get("effective_status") or lease.get("status") or ""),
            result_path=result_path_hint,
        )
        _record_startup_decision_event(
            run_store=run_store,
            events=events,
            decision=decision,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            lease=lease,
        )
        return decision
    if result.status != RUNNER_RESULT_COMPLETED:
        return _mark_retryable_review_failure(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=RUNNER_PHASE_CODE_REVIEW,
            reason="code_review_failed",
            error=(
                "startup reconciliation: runner subprocess failed: "
                f"{result.error or result.status or RUNNER_RESULT_FAILED}"
            ),
            event_type="code_review_failed",
            event_message="Code review failed before Planner review",
            evidence_files=list(result.evidence_files),
            lease=lease,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            category=CATEGORY_RUNNER_FAILED,
            recovery_action=RECOVERY_IMPORT_AND_FAIL_REVIEW,
            result_path=result.result_path or None,
        )
    try:
        report = result.to_codex_review_report()
    except Exception as exc:
        return _mark_retryable_review_failure(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=RUNNER_PHASE_CODE_REVIEW,
            reason="code_review_failed",
            error=f"startup reconciliation: runner report invalid: {exc}",
            event_type="code_review_failed",
            event_message="Code review failed before Planner review",
            evidence_files=list(result.evidence_files),
            lease=lease,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            category=CATEGORY_RUNNER_FAILED,
            recovery_action=RECOVERY_IMPORT_AND_FAIL_REVIEW,
            result_path=result.result_path or None,
        )
    imported = _import_code_review_evidence(
        run_store=run_store,
        manifest=manifest,
        runner_id=runner_id,
        report_evidence=report.evidence_files,
        result_evidence=list(result.evidence_files),
    )
    if imported:
        events.append(
            run_store.append_event(
                manifest.run_id,
                "runner_subprocess_imported",
                "Code review runner subprocess result imported during runtime startup reconciliation",
                source=SOURCE_RUNTIME_STARTUP,
                runner_id=runner_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                result_path=result.result_path or None,
                review_status=report.status,
                evidence_files=report.evidence_files,
            )
        )
    if report.status == "error":
        return _mark_retryable_review_failure(
            run_store=run_store,
            manifest=manifest,
            events=events,
            runner_id=runner_id,
            phase=RUNNER_PHASE_CODE_REVIEW,
            reason="code_review_error",
            error=(
                f"status={report.status} returncode={report.returncode} "
                f"summary={report.summary}"
            ),
            event_type="code_review_failed",
            event_message="Code review infra error blocked Planner review",
            evidence_files=report.evidence_files,
            lease=lease,
            runtime_generation=runtime_generation,
            process_hint=process_hint,
            category=CATEGORY_RUNNER_COMPLETED,
            recovery_action=RECOVERY_IMPORT_AND_FAIL_REVIEW,
            result_path=result.result_path or None,
        )
    return _mark_retryable_review_failure(
        run_store=run_store,
        manifest=manifest,
        events=events,
        runner_id=runner_id,
        phase=RUNNER_PHASE_CODE_REVIEW,
        reason="planner_review_failed",
        error=(
            "startup reconciliation: code review finished but planner review did not complete; "
            "retry review required"
        ),
        event_type="planner_review_failed",
        event_message="Planner review failed after Worker evidence was saved",
        evidence_files=report.evidence_files,
        lease=lease,
        runtime_generation=runtime_generation,
        process_hint=process_hint,
        category=CATEGORY_RUNNER_COMPLETED,
        recovery_action=RECOVERY_IMPORT_AND_RETRY_REVIEW,
        result_path=result.result_path or None,
    )


def _mark_retryable_review_failure(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    runner_id: str,
    phase: str,
    reason: str,
    error: str,
    event_type: str,
    event_message: str,
    evidence_files: Sequence[str],
    lease: Dict[str, Any],
    runtime_generation: Optional[str],
    process_hint: Optional[str],
    category: str,
    recovery_action: str,
    result_path: Optional[str],
) -> StartupReconciliationDecision:
    attempt = _resolve_review_attempt(manifest=manifest, lease=lease, run_store=run_store)
    merged_evidence = _append_unique(attempt.evidence_files, evidence_files)
    if manifest.review is None:
        manifest.review = ReviewRecord(evidence_files=list(merged_evidence))
    else:
        manifest.review.evidence_files = _append_unique(manifest.review.evidence_files, merged_evidence)
    attempt.evidence_files = list(merged_evidence)
    attempt.status = REVIEW_ATTEMPT_FAILED_RETRYABLE
    attempt.reason = reason
    attempt.error = error
    if not attempt.completed_at:
        attempt.completed_at = run_store.now_iso()
    record_run_status_transition(
        manifest,
        RUN_WORK_DONE,
        run_store.now_iso(),
        metadata={"reason": reason, "source": SOURCE_RUNTIME_STARTUP},
    )
    manifest.planner.status = (
        RUN_PLAN_APPROVED
        if manifest.plan is not None and manifest.plan.approval_status in {"approved", "not_required"}
        else RUN_PLAN_READY
    )
    for worker in manifest.workers:
        if worker.status == RUN_REVIEWING:
            worker.status = WORKER_DONE
        worker.evidence_files = _append_unique(worker.evidence_files, merged_evidence)
    retry_backoff = classify_retry_backoff_decision(manifest)
    if retry_backoff is not None:
        attempt.retry_attempt = retry_backoff.retry_attempt
        attempt.retry_budget = retry_backoff.retry_budget
        attempt.next_retry_at = retry_backoff.next_retry_at
        attempt.backoff_reason = retry_backoff.backoff_reason
        attempt.retry_budget_exhausted = retry_backoff.budget_exhausted
    run_store.save(manifest)
    if not _has_event_with_fields(
        events,
        event_type,
        {"source": SOURCE_RUNTIME_STARTUP, "review_attempt_id": attempt.id, "reason": reason},
    ):
        events.append(
            run_store.append_event(
                manifest.run_id,
                event_type,
                event_message,
                source=SOURCE_RUNTIME_STARTUP,
                review_attempt_id=attempt.id,
                worker_id=attempt.worker_id,
                error=error,
                reason=reason,
                runner_id=runner_id,
                phase=phase,
            )
        )
    policy = classify_retryable_review_failure(
        manifest,
        source=SOURCE_RUNTIME_STARTUP,
        attempt=_worker_attempt(manifest),
    )
    decision = StartupReconciliationDecision(
        run_id=manifest.run_id,
        runner_id=runner_id,
        phase=phase,
        category=category if policy is None else policy.category,
        reason=reason if policy is None else policy.reason,
        recovery_action=recovery_action if policy is None else policy.recovery_action,
        automatic=False if policy is None else policy.automatic,
        requires_human=True if policy is None else policy.requires_human,
        retryable=True if policy is None else policy.retryable,
        lease_status=str(lease.get("status") or ""),
        effective_status=str(lease.get("effective_status") or lease.get("status") or ""),
        result_path=result_path,
    )
    _record_startup_decision_event(
        run_store=run_store,
        events=events,
        decision=decision,
        runtime_generation=runtime_generation,
        process_hint=process_hint,
        lease=lease,
        error=error,
    )
    return decision


def _resolve_review_attempt(
    *,
    manifest: RunManifest,
    lease: Dict[str, Any],
    run_store: RunStore,
) -> ReviewAttemptRecord:
    checkpoint = lease.get("checkpoint")
    review_attempt_id = None
    if isinstance(checkpoint, dict):
        raw = checkpoint.get("review_attempt_id")
        review_attempt_id = str(raw).strip() if isinstance(raw, str) else None
    for item in manifest.review_attempts:
        if review_attempt_id and item.id == review_attempt_id:
            return item
    if manifest.review_attempts:
        return manifest.review_attempts[-1]
    worker_id = manifest.workers[0].id if manifest.workers else "worker-1"
    attempt = ReviewAttemptRecord(
        id=review_attempt_id or "review-1",
        worker_id=worker_id,
        status=REVIEW_ATTEMPT_FAILED_RETRYABLE,
        started_at=run_store.now_iso(),
        worker_attempt=_worker_attempt(manifest),
    )
    manifest.review_attempts.append(attempt)
    return attempt


def _import_code_review_evidence(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    runner_id: str,
    report_evidence: Sequence[str],
    result_evidence: Sequence[str],
) -> bool:
    merged = _append_unique(report_evidence, result_evidence)
    changed = False
    for worker in manifest.workers:
        next_files = _append_unique(worker.evidence_files, merged)
        if next_files != worker.evidence_files:
            worker.evidence_files = next_files
            changed = True
    if manifest.review is None:
        manifest.review = ReviewRecord(evidence_files=list(merged))
        changed = True
    else:
        next_review_files = _append_unique(manifest.review.evidence_files, merged)
        if next_review_files != manifest.review.evidence_files:
            manifest.review.evidence_files = next_review_files
            changed = True
    if changed:
        run_store.save(manifest)
    return changed


def _ensure_verification_failed_state(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    reason: str,
) -> None:
    if not _has_event_with_fields(events, "verification_gate_failed", {"source": SOURCE_RUNTIME_STARTUP}):
        events.append(
            run_store.append_event(
                manifest.run_id,
                "verification_gate_failed",
                "Verification gate failed; refusing apply and commit.",
                source=SOURCE_RUNTIME_STARTUP,
                summary=f"startup reconciliation: {reason}",
            )
        )
    _ensure_failed_manifest_state(run_store=run_store, manifest=manifest, events=events, reason="verification_failed")


def _ensure_failed_manifest_state(
    *,
    run_store: RunStore,
    manifest: RunManifest,
    events: List[Dict[str, Any]],
    reason: str,
) -> None:
    if manifest.status != RUN_FAILED:
        record_run_status_transition(
            manifest,
            RUN_FAILED,
            run_store.now_iso(),
            metadata={"reason": reason, "source": SOURCE_RUNTIME_STARTUP},
        )
    manifest.planner.status = RUN_FAILED
    for worker in manifest.workers:
        if worker.status not in TERMINAL_RUN_STATUSES and worker.status != WORKER_DONE:
            worker.status = RUN_FAILED
    run_store.save(manifest)
    if not _has_event_with_fields(events, "run_terminal_status", {"status": RUN_FAILED, "reason": reason}):
        events.append(
            run_store.append_event(
                manifest.run_id,
                "run_terminal_status",
                f"Run finished with {RUN_FAILED}",
                status=RUN_FAILED,
                reason=reason,
                source=SOURCE_RUNTIME_STARTUP,
            )
        )


def _record_startup_decision_event(
    *,
    run_store: RunStore,
    events: List[Dict[str, Any]],
    decision: StartupReconciliationDecision,
    runtime_generation: Optional[str],
    process_hint: Optional[str],
    lease: Dict[str, Any],
    error: Optional[str] = None,
) -> None:
    key = _decision_fingerprint(decision)
    if _has_event_with_fields(
        events,
        "recovery_decision_recorded",
        {"source": SOURCE_RUNTIME_STARTUP, "reconciliation_key": key},
    ):
        return
    payload = {
        "phase": decision.phase,
        "category": decision.category,
        "reason": decision.reason,
        "recovery_action": decision.recovery_action,
        "source": SOURCE_RUNTIME_STARTUP,
        "attempt": _worker_attempt_str(lease),
        "retryable": decision.retryable,
        "automatic": decision.automatic,
        "requires_human": decision.requires_human,
        "runner_id": decision.runner_id,
        "startup_run_id": decision.run_id,
        "lease_status": decision.lease_status,
        "effective_status": decision.effective_status,
        "runtime_generation": runtime_generation,
        "process_hint": process_hint or lease.get("process_hint"),
        "result_path": decision.result_path,
        "lease_expires_at": lease.get("lease_expires_at"),
        "heartbeat_at": lease.get("heartbeat_at"),
        "pid": lease.get("pid"),
        "reconciliation_key": key,
        "error": error,
    }
    events.append(
        run_store.append_event(
            decision.run_id,
            "recovery_decision_recorded",
            "Failure recovery decision recorded",
            **payload,
        )
    )


def _decision_fingerprint(decision: StartupReconciliationDecision) -> str:
    return "|".join(
        [
            decision.run_id,
            decision.runner_id,
            decision.phase,
            decision.category,
            decision.reason,
            decision.recovery_action,
            decision.lease_status,
            decision.effective_status,
            decision.result_path or "",
        ]
    )


def _has_event_with_fields(events: List[Dict[str, Any]], event_type: str, fields: Dict[str, Any]) -> bool:
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != event_type:
            continue
        if all(event.get(key) == value for key, value in fields.items()):
            return True
    return False


def _requires_external_subprocess_liveness(*, phase: str, lease: Dict[str, Any]) -> bool:
    if phase != RUNNER_PHASE_CODE_REVIEW:
        return False
    checkpoint = lease.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return False
    return _string_or_none(checkpoint.get("phase_boundary")) == "runner_subprocess_started"


def _lease_liveness_pid(*, phase: str, lease: Dict[str, Any]) -> Optional[int]:
    checkpoint = lease.get("checkpoint")
    if _requires_external_subprocess_liveness(phase=phase, lease=lease) and isinstance(checkpoint, dict):
        checkpoint_pid = _int_or_none(checkpoint.get("pid"))
        if checkpoint_pid is not None:
            return checkpoint_pid
        hint_pid = _pid_from_process_hint(checkpoint.get("process_hint"))
        if hint_pid is not None:
            return hint_pid
        return None
    return _int_or_none(lease.get("pid"))


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _pid_from_process_hint(value: Any) -> Optional[int]:
    hint = _string_or_none(value)
    if hint is None:
        return None
    if hint.startswith("pid:"):
        return _int_or_none(hint.split(":", 1)[1])
    return None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno in {errno.ESRCH}:
            return False
        if exc.errno in {errno.EPERM}:
            return True
        return False
    return True


def _worker_attempt(manifest: RunManifest) -> Optional[int]:
    if not manifest.workers:
        return None
    attempt = manifest.workers[0].attempt
    if isinstance(attempt, int) and attempt > 0:
        return attempt
    return None


def _worker_attempt_str(lease: Dict[str, Any]) -> str:
    checkpoint = lease.get("checkpoint")
    if isinstance(checkpoint, dict):
        value = checkpoint.get("worker_attempt")
        if isinstance(value, int) and value > 0:
            return str(value)
    return "unknown"


def _append_unique(current: Sequence[str], additions: Sequence[str]) -> List[str]:
    seen = {str(item) for item in current if isinstance(item, str)}
    merged = [str(item) for item in current if isinstance(item, str)]
    for raw in additions:
        if not isinstance(raw, str):
            continue
        item = raw.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None
