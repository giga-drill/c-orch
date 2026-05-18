from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, List, Optional, Protocol, Union

from .codex_review import CodexReviewReport, run_codex_uncommitted_review
from .contracts import PlannerPlan, ReviewDecision, WorkerResult
from .drivers import CodexDriver, SessionResult
from .failure_policy import (
    DEFAULT_RETRY_BUDGET,
    FailurePolicyDecision,
    SOURCE_ORCHESTRATOR,
    classify_apply_failure,
    classify_code_review_findings,
    classify_git_commit_failure,
    classify_max_attempts_exceeded,
    has_retryable_review_failure,
    has_retryable_verification_failure,
    classify_operation_failure,
    classify_retry_backoff_decision,
    classify_retryable_review_failure,
    classify_session_recovery_result,
    classify_verification_gate_failure,
)
from .phase_timing import record_run_status_transition
from .prompts import (
    planner_initial_prompt,
    planner_review_fallback_prompt,
    planner_revision_prompt,
    planner_review_prompt,
    worker_prompt,
)
from .runner_leases import RunnerLeaseStore
from .run_store import (
    PlanRecord,
    PlanRevisionRecord,
    ReviewAttemptRecord,
    ReviewRecord,
    RunManifest,
    RunStore,
    WorkerRecord,
)
from .settings import DEFAULT_APPROVAL_POLICY, DEFAULT_MAX_ATTEMPTS, DEFAULT_SANDBOX
from .states import (
    REVIEW_ATTEMPT_ACTIVE,
    REVIEW_ATTEMPT_FAILED_RETRYABLE,
    RUN_APPROVED,
    RUN_FAILED,
    RUN_PLAN_APPROVED,
    RUN_PLAN_READY,
    RUN_PLAN_REVISING,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_PLANNING,
    RUN_REVIEWING,
    RUN_REVISION_REQUESTED,
    RUN_WORK_DONE,
    RUN_WORKING,
    TERMINAL_RUN_STATUSES,
    WORKER_ACTIVE,
    WORKER_DONE,
)
from .terminalization_recovery import (
    EVENT_TERMINALIZATION_RECOVERY_REQUIRED,
    accepted_review_needs_terminalization,
    accepted_review_terminalization_requires_manual_recovery,
)
from .verification import VerificationReport, run_verification_commands
from .worktrees import (
    ApplyReport,
    DiffEvidence,
    GitCommitReport,
    apply_diff_evidence_to_repo,
    collect_repo_changed_paths,
    collect_repo_staged_paths,
    commit_applied_changes,
    collect_diff_evidence,
)
from .workspace_lanes import WorkspaceResolutionError, canonical_git_root


Pathish = Union[str, Path]
RESTART_REQUIRED_PREFIXES = ("src/c_orch/",)
RESTART_REQUIRED_EXACT_PATHS = {
    "src/c_orch",
    "pyproject.toml",
    ".c-orch.toml",
}


class OrchestratorError(RuntimeError):
    """Raised when a run cannot advance through the MVP state machine."""


class EvidenceCollector(Protocol):
    def __call__(self, worktree_path: Pathish, evidence_dir: Pathish) -> DiffEvidence:
        ...


class VerificationRunner(Protocol):
    def __call__(
        self,
        commands: Iterable[str],
        *,
        cwd: Pathish,
        evidence_dir: Pathish,
    ) -> VerificationReport:
        ...


class CodeReviewRunner(Protocol):
    def __call__(
        self,
        *,
        cwd: Pathish,
        evidence_dir: Pathish,
        codex_binary_path: Optional[str] = None,
        patch_path: Optional[Pathish] = None,
        service_tier: Optional[str] = None,
    ) -> CodexReviewReport:
        ...


class DiffApplier(Protocol):
    def __call__(
        self,
        diff: DiffEvidence,
        target_repo_path: Pathish,
        evidence_dir: Pathish,
    ) -> ApplyReport:
        ...


class RepoChangedPathsCollector(Protocol):
    def __call__(self, repo_path: Pathish) -> List[str]:
        ...


class GitCommitter(Protocol):
    def __call__(
        self,
        *,
        target_repo_path: Pathish,
        evidence_dir: Pathish,
        run_id: str,
        worker_id: str,
        planner_review_decision: str,
        user_task: str,
        plan_summary: Optional[str],
        review_reason: Optional[str],
        changed_paths: List[str],
        pre_apply_changed_paths: List[str],
        pre_apply_staged_paths: Optional[List[str]] = None,
        post_apply_changed_paths: Optional[List[str]] = None,
    ) -> GitCommitReport:
        ...


class RepoStagedPathsCollector(Protocol):
    def __call__(self, repo_path: Pathish) -> List[str]:
        ...


@dataclass(frozen=True)
class OrchestratorConfig:
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL_POLICY
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    require_plan_approval: bool = False
    approve_plan: bool = False
    reviewer_service_tier: Optional[str] = None
    controller_repo_path: Optional[str] = None
    runtime_generation: Optional[str] = None
    process_hint: Optional[str] = None
    runner_lease_ttl_seconds: int = 300


class RunOrchestrator:
    """Single-worker Planner/Worker state machine."""

    def __init__(
        self,
        *,
        store: RunStore,
        driver: CodexDriver,
        evidence_collector: EvidenceCollector = collect_diff_evidence,
        diff_applier: DiffApplier = apply_diff_evidence_to_repo,
        repo_changed_paths_collector: RepoChangedPathsCollector = collect_repo_changed_paths,
        repo_staged_paths_collector: RepoStagedPathsCollector = collect_repo_staged_paths,
        git_committer: GitCommitter = commit_applied_changes,
        verification_runner: VerificationRunner = run_verification_commands,
        code_review_runner: Optional[CodeReviewRunner] = None,
        config: Optional[OrchestratorConfig] = None,
    ) -> None:
        self.store = store
        self.driver = driver
        self.evidence_collector = evidence_collector
        self.diff_applier = diff_applier
        self.repo_changed_paths_collector = repo_changed_paths_collector
        self.repo_staged_paths_collector = repo_staged_paths_collector
        self.git_committer = git_committer
        self.verification_runner = verification_runner
        self.code_review_runner = code_review_runner or run_codex_uncommitted_review
        self.config = config or OrchestratorConfig()
        if self.config.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._runtime_generation = self.config.runtime_generation or _default_runtime_generation()
        self._process_hint = self.config.process_hint or f"pid:{os.getpid()}"
        self._runner_lease_ttl_seconds = max(30, int(self.config.runner_lease_ttl_seconds or 300))

    def run(self, manifest: RunManifest) -> RunManifest:
        try:
            return self._run(manifest)
        except Exception:
            if manifest.status not in TERMINAL_RUN_STATUSES:
                self._transition_status(manifest, RUN_FAILED, reason="exception")
            try:
                self._record_terminal_status(manifest, reason="exception")
            except Exception:
                pass
            self._save(manifest)
            raise

    def _run(self, manifest: RunManifest) -> RunManifest:
        worker = _single_worker(manifest)
        worktree_path = _required_worktree_path(worker)

        plan = self._ensure_plan(manifest, worker)
        self._save(manifest)

        if self.config.approve_plan:
            self._approve_plan(manifest, approved_by="human")

        if self.config.require_plan_approval and not _plan_is_approved(manifest):
            self._require_plan_review(manifest)
            return manifest

        next_worker_prompt = _initial_worker_prompt(plan)
        worker_result: Optional[WorkerResult] = None

        for attempt in range(1, self.config.max_attempts + 1):
            worker.attempt = attempt
            worker_result = self._run_worker_attempt(
                manifest=manifest,
                worker=worker,
                prompt=next_worker_prompt,
                worktree_path=worktree_path,
                is_initial_attempt=attempt == 1,
            )
            evidence = self._collect_evidence(
                manifest=manifest,
                worker=worker,
                worktree_path=worktree_path,
            )
            verification = self._run_verification(
                manifest=manifest,
                worker=worker,
                worktree_path=worktree_path,
            )
            decision = self._review_attempt(
                manifest=manifest,
                worker=worker,
                plan=plan,
                worker_result=worker_result,
                evidence=evidence,
                verification=verification,
            )
            if decision is None:
                return manifest

            if decision.decision == "accepted":
                return self._complete_after_accepted_review(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    verification=verification,
                )

            if decision.decision == "revision_requested":
                self._record_recovery_decision(
                    manifest,
                    classify_code_review_findings(
                        reason=decision.reason,
                        source=SOURCE_ORCHESTRATOR,
                        attempt=worker.attempt,
                    ),
                )
                if attempt >= self.config.max_attempts:
                    self._record_recovery_decision(
                        manifest,
                        classify_max_attempts_exceeded(
                            source=SOURCE_ORCHESTRATOR,
                            attempt=worker.attempt,
                        ),
                    )
                    self._transition_status(manifest, RUN_FAILED, reason="max_attempts_reached")
                    manifest.planner.status = RUN_FAILED
                    worker.status = RUN_FAILED
                    self._record_terminal_status(manifest, reason="max_attempts_reached")
                    self._save(manifest)
                    return manifest
                self._enter_revision_requested(manifest, worker)
                self._save(manifest)
                next_worker_prompt = _rework_worker_prompt(
                    decision.next_worker_prompt or "",
                    plan.acceptance_criteria,
                    plan.verification_commands,
                )
                continue

            self._transition_status(manifest, RUN_FAILED, reason="unexpected_review_decision")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="unexpected_review_decision")
            self._save(manifest)
            return manifest

        if worker_result is None:
            raise OrchestratorError("worker did not run")
        self._transition_status(manifest, RUN_FAILED, reason="fallthrough")
        self._record_terminal_status(manifest, reason="fallthrough")
        self._save(manifest)
        return manifest

    def _ensure_plan(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        if manifest.plan is not None:
            return _planner_plan_from_record(manifest.plan, manifest)
        return self._start_planner(manifest, worker)

    def retry_review(self, manifest: RunManifest) -> RunManifest:
        worker = _single_worker(manifest)
        if not has_retryable_review_failure(manifest):
            raise OrchestratorError("run is not waiting for a saved Planner review retry")
        if manifest.plan is None:
            raise OrchestratorError("cannot retry review without a saved plan")
        if not worker.result:
            raise OrchestratorError("cannot retry review without saved Worker result")
        if not manifest.review:
            raise OrchestratorError("cannot retry review without saved evidence")
        plan = _planner_plan_from_record(manifest.plan, manifest)
        worker_result = WorkerResult.parse(json_dumps(worker.result))
        evidence = _evidence_from_review_record(manifest.review)
        verification = _verification_from_review_record(manifest.review)
        self._record_event(
            manifest,
            "planner_review_retry_started",
            "Planner review retry started",
            worker_id=worker.id,
        )
        decision = self._review_attempt(
            manifest=manifest,
            worker=worker,
            plan=plan,
            worker_result=worker_result,
            evidence=evidence,
            verification=verification,
        )
        if decision is None:
            return manifest
        if decision.decision == "accepted":
            return self._complete_after_accepted_review(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
                verification=verification,
            )
        if decision.decision == "revision_requested":
            return self._continue_after_revision_requested(manifest, worker, plan, decision)
        self._transition_status(manifest, RUN_FAILED, reason="unexpected_review_decision")
        manifest.planner.status = RUN_FAILED
        worker.status = RUN_FAILED
        self._record_terminal_status(manifest, reason="unexpected_review_decision")
        self._save(manifest)
        return manifest

    def retry_verification(self, manifest: RunManifest) -> RunManifest:
        worker = _single_worker(manifest)
        if not has_retryable_verification_failure(manifest, self.store.load_events(manifest.run_id)):
            raise OrchestratorError("run is not waiting for a retryable verification failure")
        if not manifest.review or manifest.review.decision != "accepted":
            raise OrchestratorError("cannot retry verification without an accepted Planner review")
        worktree_path = _required_worktree_path(worker)
        self._record_event(
            manifest,
            "verification_retry_started",
            "Verification retry started",
            worker_id=worker.id,
            attempt=worker.attempt,
        )
        evidence = _evidence_from_review_record(manifest.review)
        worker.evidence_files = _append_unique(worker.evidence_files, evidence.evidence_files)
        self._save(manifest)
        verification = self._run_verification(
            manifest=manifest,
            worker=worker,
            worktree_path=worktree_path,
        )
        return self._complete_after_accepted_review(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
            verification=verification,
        )

    def reconcile_accepted_review_terminalization(self, manifest: RunManifest) -> RunManifest:
        events = self.store.load_events(manifest.run_id)
        if not accepted_review_needs_terminalization(manifest, events):
            return manifest
        if accepted_review_terminalization_requires_manual_recovery(manifest, events):
            return manifest
        worker = _single_worker(manifest)
        if manifest.review is None or manifest.review.decision != "accepted":
            raise OrchestratorError("accepted review terminalization requires accepted review evidence")
        evidence = _evidence_from_review_record(manifest.review)
        verification = _verification_from_review_record(manifest.review)
        self._record_event(
            manifest,
            "accepted_terminalization_reconcile_started",
            "Accepted-review terminalization reconcile started",
            worker_id=worker.id,
            attempt=worker.attempt,
        )

        events = self.store.load_events(manifest.run_id)
        latest_apply_completed = self._latest_event_by_type(events=events, event_type="apply_completed")
        if isinstance(latest_apply_completed, dict) and latest_apply_completed.get("applied") is not True:
            self._transition_status(
                manifest,
                RUN_FAILED,
                reason="apply_failed",
                recovered_from="accepted_terminalization_reconcile",
            )
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="apply_failed")
            self._save(manifest)
            return manifest
        has_apply_started = any(event.get("type") == "apply_started" for event in events if isinstance(event, dict))
        has_apply_completed = isinstance(latest_apply_completed, dict)
        if not has_apply_completed:
            if not has_apply_started:
                return self._apply_then_continue_terminalization(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    verification=verification,
                    replay=False,
                )
            apply_state = self._determine_partial_apply_state(manifest=manifest, evidence=evidence)
            if apply_state == "already_applied":
                self._record_event(
                    manifest,
                    "apply_completed",
                    "Apply completed",
                    applied=True,
                    summary="Recovered: patch already applied in target repository.",
                    recovered=True,
                    source="accepted_terminalization_reconcile",
                )
            elif apply_state == "not_applied":
                return self._apply_then_continue_terminalization(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    verification=verification,
                    replay=True,
                )
            else:
                self._record_terminalization_manual_recovery_required(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    waiting_for="apply",
                    reason="cannot_prove_apply_boundary",
                )
                self._save(manifest)
                return manifest

        events = self.store.load_events(manifest.run_id)
        latest_commit_failed = self._latest_event_by_type(events=events, event_type="git_commit_failed")
        if isinstance(latest_commit_failed, dict):
            self._transition_status(
                manifest,
                RUN_FAILED,
                reason="git_commit_failed",
                recovered_from="accepted_terminalization_reconcile",
            )
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="git_commit_failed")
            self._save(manifest)
            return manifest
        has_commit_success_boundary = any(
            isinstance(event, dict)
            and str(event.get("type") or "") in {"git_commit_completed", "git_commit_skipped"}
            for event in events
        )
        if not has_commit_success_boundary:
            commit_marker = self._head_commit_terminalization_marker(
                manifest=manifest,
                worker=worker,
            )
            if commit_marker["matched"]:
                self._record_event(
                    manifest,
                    "git_commit_completed",
                    "Git commit completed",
                    commit_hash=commit_marker["head"],
                    summary="Recovered: HEAD commit already has this run's accepted-review marker.",
                    recovered=True,
                    source="accepted_terminalization_reconcile",
                )
            else:
                commit_state = self._determine_commit_replay_state(manifest=manifest, evidence=evidence)
                if commit_state != "safe_to_commit":
                    self._record_terminalization_manual_recovery_required(
                        manifest=manifest,
                        worker=worker,
                        evidence=evidence,
                        waiting_for="commit",
                        reason=commit_state,
                    )
                    self._save(manifest)
                    return manifest
                pre_apply_changed_paths = self._latest_apply_started_list_field(
                    events=events,
                    key="pre_apply_changed_paths",
                ) or self.repo_changed_paths_collector(manifest.cwd)
                pre_apply_staged_paths = self._latest_apply_started_list_field(
                    events=events,
                    key="pre_apply_staged_paths",
                ) or self.repo_staged_paths_collector(manifest.cwd)
                post_apply_changed_paths = self.repo_changed_paths_collector(manifest.cwd)
                self._record_git_commit_started(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    pre_apply_changed_paths=pre_apply_changed_paths,
                    pre_apply_staged_paths=pre_apply_staged_paths,
                    post_apply_changed_paths=post_apply_changed_paths,
                    replay=True,
                )
                commit_report = self._commit_applied_diff(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                    pre_apply_changed_paths=pre_apply_changed_paths,
                    pre_apply_staged_paths=pre_apply_staged_paths,
                    post_apply_changed_paths=post_apply_changed_paths,
                )
                if commit_report.failed:
                    self._record_recovery_decision(
                        manifest,
                        classify_git_commit_failure(
                            failure_reason=commit_report.failure_reason,
                            summary=commit_report.summary,
                            source=SOURCE_ORCHESTRATOR,
                            attempt=worker.attempt,
                        ),
                    )
                    self._transition_status(manifest, RUN_FAILED, reason="git_commit_failed")
                    manifest.planner.status = RUN_FAILED
                    worker.status = RUN_FAILED
                    self._record_terminal_status(manifest, reason="git_commit_failed")
                    self._save(manifest)
                    return manifest

        events = self.store.load_events(manifest.run_id)
        has_terminal = any(event.get("type") == "run_terminal_status" for event in events if isinstance(event, dict))
        if not has_terminal or manifest.status != RUN_APPROVED:
            self._transition_status(
                manifest,
                RUN_APPROVED,
                recovered_from="accepted_terminalization_reconcile",
            )
            manifest.planner.status = RUN_APPROVED
            worker.status = RUN_APPROVED
            self._record_terminal_status(manifest, reason="accepted_terminalization_recovered")
            self._save(manifest)
        return manifest

    def revise_plan(self, manifest: RunManifest, feedback: str) -> RunManifest:
        normalized_feedback = feedback.strip()
        if not normalized_feedback:
            raise OrchestratorError("plan revision feedback cannot be empty")
        if manifest.status != RUN_PLAN_REVIEW_REQUIRED:
            raise OrchestratorError("run is not waiting for plan revision")
        if manifest.plan is None:
            raise OrchestratorError("cannot revise plan before planner has produced one")
        if not manifest.planner.thread_id:
            raise OrchestratorError("cannot revise plan without planner thread_id")

        previous_plan = manifest.plan.to_dict()
        self._transition_status(manifest, RUN_PLAN_REVISING, feedback=normalized_feedback)
        manifest.planner.status = RUN_PLAN_REVISING
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_plan_revision_started",
            "Planner plan revision started",
        )
        lease_phase = "planner_revise"
        revise_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_planner_revision_reply",
                worker=_single_worker(manifest),
            ),
        )

        try:
            started_at = self.store.now_iso()
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=revise_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_revision_reply_inflight",
                    worker=_single_worker(manifest),
                ),
            )
            result = self.driver.reply(
                thread_id=manifest.planner.thread_id,
                prompt=planner_revision_prompt(human_feedback=normalized_feedback),
                model=manifest.planner.model,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
            )
            revised = PlannerPlan.parse(result.content)
            self._record_usage_attribution(
                manifest=manifest,
                role="planner",
                phase="plan",
                thread_id=result.thread_id,
                session_id=_session_id_from_result(result),
                model=manifest.planner.model,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
                started_at=started_at,
                updated_at=self.store.now_iso(),
                worktree_path=_single_worker(manifest).worktree_path,
            )
        except Exception as exc:
            self._transition_status(
                manifest,
                RUN_PLAN_REVIEW_REQUIRED,
                reason="planner_plan_revision_failed",
            )
            self._lease_fail(
                manifest=manifest,
                runner_id=revise_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_revision_failed",
                    worker=_single_worker(manifest),
                ),
                error=str(exc),
            )
            manifest.planner.status = RUN_PLAN_REVIEW_REQUIRED
            self._save(manifest)
            self._record_event(
                manifest,
                "planner_plan_revision_failed",
                "Planner plan revision failed",
                error=str(exc),
            )
            raise
        manifest.acceptance_criteria = list(revised.acceptance_criteria)
        manifest.verification_commands = list(revised.verification_commands)
        manifest.plan = PlanRecord(
            summary=revised.summary,
            worker_prompt=revised.worker_prompt,
            risk_notes=list(revised.risk_notes),
            raw=dict(revised.raw),
            decomposition_suggestion=dict(revised.decomposition_suggestion)
            if isinstance(revised.decomposition_suggestion, dict)
            else None,
            approval_status="pending",
            approved_at=None,
            approved_by=None,
        )
        revision = PlanRevisionRecord(
            id=f"plan-revision-{len(manifest.plan_revisions) + 1}",
            created_at=self.store.now_iso(),
            human_feedback=normalized_feedback,
            previous_plan=previous_plan,
            new_plan=manifest.plan.to_dict(),
        )
        manifest.plan_revisions.append(revision)
        self._transition_status(manifest, RUN_PLAN_READY)
        manifest.planner.status = RUN_PLAN_READY
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_plan_revised",
            "Planner plan revised",
            revision_id=revision.id,
            acceptance_count=len(manifest.acceptance_criteria),
            verification_count=len(manifest.verification_commands),
        )
        self._lease_complete(
            manifest=manifest,
            runner_id=revise_lease_id,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="planner_revision_completed",
                worker=_single_worker(manifest),
                extra={"revision_id": revision.id},
            ),
        )
        self._require_plan_review(manifest)
        return manifest

    def _start_planner(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        worktree_path = _required_worktree_path(worker)
        self._transition_status(manifest, RUN_PLANNING)
        manifest.planner.status = WORKER_ACTIVE
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_start",
            "Planner started",
        )
        lease_phase = "planner_plan"
        planner_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_planner_start_session",
                worker=worker,
            ),
        )

        started_at = self.store.now_iso()
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=planner_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_start_session_inflight",
                    worker=worker,
                ),
            )
            result = self.driver.start_session(
                role="planner",
                model=manifest.planner.model,
                cwd=worktree_path,
                prompt=planner_initial_prompt(
                    user_task=manifest.user_task,
                    cwd=worktree_path,
                    worker_model=worker.model,
                ),
                sandbox=self.config.sandbox,
                approval_policy=self.config.approval_policy,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
            )
            manifest.planner.thread_id = result.thread_id
            plan = PlannerPlan.parse(result.content)
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=planner_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_plan_failed",
                    worker=worker,
                ),
                error=str(exc),
            )
            raise
        self._record_usage_attribution(
            manifest=manifest,
            role="planner",
            phase="plan",
            thread_id=result.thread_id,
            session_id=_session_id_from_result(result),
            model=manifest.planner.model,
            reasoning_effort=manifest.planner.reasoning_effort,
            service_tier=manifest.planner.service_tier,
            started_at=started_at,
            updated_at=self.store.now_iso(),
            worktree_path=worktree_path,
        )
        manifest.acceptance_criteria = list(plan.acceptance_criteria)
        manifest.verification_commands = list(plan.verification_commands)
        manifest.plan = PlanRecord(
            summary=plan.summary,
            worker_prompt=plan.worker_prompt,
            risk_notes=list(plan.risk_notes),
            raw=dict(plan.raw),
            decomposition_suggestion=dict(plan.decomposition_suggestion)
            if isinstance(plan.decomposition_suggestion, dict)
            else None,
            approval_status="pending" if self.config.require_plan_approval else "not_required",
        )
        self._transition_status(manifest, RUN_PLAN_READY)
        manifest.planner.status = RUN_PLAN_READY
        self._record_event(
            manifest,
            "planner_plan_ready",
            "Planner plan ready",
            acceptance_count=len(manifest.acceptance_criteria),
            verification_count=len(manifest.verification_commands),
        )
        self._lease_complete(
            manifest=manifest,
            runner_id=planner_lease_id,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="planner_plan_completed",
                worker=worker,
            ),
        )
        return plan

    def _require_plan_review(self, manifest: RunManifest) -> None:
        self._transition_status(manifest, RUN_PLAN_REVIEW_REQUIRED)
        manifest.planner.status = RUN_PLAN_REVIEW_REQUIRED
        self._save(manifest)
        if not self._has_event(manifest, "plan_review_required"):
            self._record_event(
                manifest,
                "plan_review_required",
                "Human plan review required",
            )

    def _approve_plan(self, manifest: RunManifest, *, approved_by: str) -> None:
        if manifest.plan is None:
            raise OrchestratorError("cannot approve plan before planner has produced one")
        if manifest.plan.approval_status == "approved":
            return
        manifest.plan.approval_status = "approved"
        manifest.plan.approved_at = self.store.now_iso()
        manifest.plan.approved_by = approved_by
        self._transition_status(manifest, RUN_PLAN_APPROVED, approved_by=approved_by)
        manifest.planner.status = RUN_PLAN_APPROVED
        self._save(manifest)
        self._record_event(
            manifest,
            "plan_approved",
            "Human approved planner plan",
            approved_by=approved_by,
        )

    def _run_worker_attempt(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        prompt: str,
        worktree_path: str,
        is_initial_attempt: bool,
    ) -> WorkerResult:
        self._transition_status(
            manifest,
            RUN_WORKING,
            worker_id=worker.id,
            worker_attempt=worker.attempt,
        )
        worker.status = WORKER_ACTIVE
        self._save(manifest)
        self._record_event(
            manifest,
            "worker_start",
            "Worker attempt started",
            worker_id=worker.id,
            attempt=worker.attempt,
        )

        usage_phase = "implement" if is_initial_attempt else "rework"
        lease_phase = "worker_implement" if is_initial_attempt else "worker_rework"
        worker_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_worker_call",
                worker=worker,
                extra={"usage_phase": usage_phase},
            ),
        )
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=worker_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="worker_call_inflight",
                    worker=worker,
                    extra={"usage_phase": usage_phase},
                ),
            )
            if is_initial_attempt:
                started_at = self.store.now_iso()
                result = self.driver.start_session(
                    role="worker",
                    model=worker.model,
                    cwd=worktree_path,
                    prompt=prompt,
                    sandbox=self.config.sandbox,
                    approval_policy=self.config.approval_policy,
                    reasoning_effort=worker.reasoning_effort,
                    service_tier=worker.service_tier,
                )
                worker.thread_id = result.thread_id
            else:
                if not worker.thread_id:
                    raise OrchestratorError("cannot continue worker without thread_id")
                previous_thread_id = worker.thread_id
                try:
                    started_at = self.store.now_iso()
                    result = self.driver.reply(
                        thread_id=previous_thread_id,
                        prompt=prompt,
                        model=worker.model,
                        reasoning_effort=worker.reasoning_effort,
                        service_tier=worker.service_tier,
                    )
                except Exception as exc:
                    decision = classify_operation_failure(
                        exc,
                        phase="worker_reply",
                        source=SOURCE_ORCHESTRATOR,
                        attempt=worker.attempt,
                    )
                    self._record_recovery_decision(manifest, decision)
                    if decision.recovery_action != "start_replacement_agent":
                        raise
                    self._record_event(
                        manifest,
                        "worker_rework_fallback_started",
                        "Worker rework fallback started after unrecoverable thread error",
                        worker_id=worker.id,
                        old_thread_id=previous_thread_id,
                        error=str(exc),
                    )
                    self._lease_heartbeat(
                        manifest=manifest,
                        runner_id=worker_lease_id,
                        phase=lease_phase,
                        checkpoint=self._lease_checkpoint(
                            manifest=manifest,
                            phase_boundary="worker_rework_fallback_start_session_inflight",
                            worker=worker,
                            extra={"old_thread_id": previous_thread_id},
                        ),
                    )
                    started_at = self.store.now_iso()
                    result = self.driver.start_session(
                        role="worker",
                        model=worker.model,
                        cwd=worktree_path,
                        prompt=prompt,
                        sandbox=self.config.sandbox,
                        approval_policy=self.config.approval_policy,
                        reasoning_effort=worker.reasoning_effort,
                        service_tier=worker.service_tier,
                    )
                    worker.thread_id = result.thread_id
                    self._save(manifest)
                    self._record_event(
                        manifest,
                        "worker_rework_fallback_thread_started",
                        "Worker rework fallback thread started",
                        worker_id=worker.id,
                        old_thread_id=previous_thread_id,
                        new_thread_id=result.thread_id,
                    )

            worker_result = WorkerResult.parse(result.content)
            self._record_usage_attribution(
                manifest=manifest,
                role="worker",
                phase=usage_phase,
                thread_id=result.thread_id,
                session_id=_session_id_from_result(result),
                model=worker.model,
                reasoning_effort=worker.reasoning_effort,
                service_tier=worker.service_tier,
                started_at=started_at,
                updated_at=self.store.now_iso(),
                worktree_path=worktree_path,
                worker_id=worker.id,
                attempt=worker.attempt,
            )
            worker.result = dict(worker_result.raw)
            self._transition_status(
                manifest,
                RUN_WORK_DONE,
                worker_id=worker.id,
                worker_attempt=worker.attempt,
            )
            worker.status = _worker_status_from_result(worker_result)
            self._save(manifest)
            self._record_event(
                manifest,
                "worker_done",
                "Worker attempt completed",
                worker_id=worker.id,
                attempt=worker.attempt,
                status=worker.status,
                thread_id=worker.thread_id,
            )
            self._lease_complete(
                manifest=manifest,
                runner_id=worker_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="worker_attempt_completed",
                    worker=worker,
                    extra={"usage_phase": usage_phase},
                ),
            )
            return worker_result
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=worker_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="worker_attempt_failed",
                    worker=worker,
                    extra={"usage_phase": usage_phase},
                ),
                error=str(exc),
            )
            raise

    def _collect_evidence(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        worktree_path: str,
    ) -> DiffEvidence:
        evidence = self.evidence_collector(worktree_path, self._attempt_evidence_dir(manifest, worker))
        worker.evidence_files = _append_unique(worker.evidence_files, evidence.evidence_files)
        self._save(manifest)
        self._record_event(
            manifest,
            "evidence_collected",
            "Evidence collected",
            evidence_count=len(evidence.evidence_files),
        )
        return evidence

    def _run_verification(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        worktree_path: str,
    ) -> VerificationReport:
        lease_phase = "verification"
        verification_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_verification",
                worker=worker,
            ),
        )
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=verification_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="verification_inflight",
                    worker=worker,
                ),
            )
            report = self.verification_runner(
                manifest.verification_commands,
                cwd=worktree_path,
                evidence_dir=self._attempt_evidence_dir(manifest, worker),
            )
            worker.evidence_files = _append_unique(worker.evidence_files, report.evidence_files)
            self._save(manifest)
            self._record_event(
                manifest,
                "verification_finished",
                "Verification finished",
                result_count=len(report.results),
                summary=report.summary,
            )
            self._lease_complete(
                manifest=manifest,
                runner_id=verification_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="verification_completed",
                    worker=worker,
                    extra={"result_count": len(report.results)},
                ),
            )
            return report
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=verification_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="verification_failed",
                    worker=worker,
                ),
                error=str(exc),
            )
            raise

    def _review_attempt(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        plan: PlannerPlan,
        worker_result: WorkerResult,
        evidence: DiffEvidence,
        verification: VerificationReport,
    ) -> Optional[ReviewDecision]:
        if not manifest.planner.thread_id:
            raise OrchestratorError("cannot review without planner thread_id")

        self._transition_status(
            manifest,
            RUN_REVIEWING,
            worker_id=worker.id,
            worker_attempt=worker.attempt,
        )
        manifest.planner.status = RUN_REVIEWING
        workspace_path = _required_worktree_path(worker)
        evidence_files = _append_unique(evidence.evidence_files, verification.evidence_files)
        reviewer_service_tier = self._effective_reviewer_service_tier(manifest)
        attempt = ReviewAttemptRecord(
            id=f"review-{len(manifest.review_attempts) + 1}",
            worker_id=worker.id,
            status=REVIEW_ATTEMPT_ACTIVE,
            started_at=self.store.now_iso(),
            worker_attempt=worker.attempt,
            workspace_path=workspace_path,
            service_tier=reviewer_service_tier,
            evidence_files=evidence_files,
        )
        manifest.review_attempts.append(attempt)
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_review_start",
            "Planner review started",
            review_attempt_id=attempt.id,
            worker_id=worker.id,
        )

        try:
            code_review = self._run_code_review(
                manifest=manifest,
                worker=worker,
                attempt=attempt,
                worktree_path=workspace_path,
                evidence=evidence,
            )
        except Exception as exc:
            self._mark_retryable_review_failure(
                manifest=manifest,
                worker=worker,
                attempt=attempt,
                evidence_files=evidence_files,
                error=str(exc),
                reason="code_review_failed",
                event_type="code_review_failed",
                event_message="Code review failed before Planner review",
            )
            return None

        evidence_files = _append_unique(evidence_files, code_review.evidence_files)
        attempt.evidence_files = list(evidence_files)
        self._save(manifest)
        if code_review.status == "error":
            detail = (
                f"status={code_review.status} returncode={code_review.returncode} "
                f"summary={code_review.summary}"
            )
            self._mark_retryable_review_failure(
                manifest=manifest,
                worker=worker,
                attempt=attempt,
                evidence_files=evidence_files,
                error=detail,
                reason="code_review_error",
                event_type="code_review_failed",
                event_message="Code review infra error blocked Planner review",
            )
            return None

        try:
            decision = self._request_review_decision(
                manifest=manifest,
                plan=plan,
                worker_result=worker_result,
                evidence=evidence,
                verification=verification,
                code_review=code_review,
                review_workspace=workspace_path,
                worker_attempt=worker.attempt,
                review_attempt_id=attempt.id,
            )
        except Exception as exc:
            self._mark_retryable_review_failure(
                manifest=manifest,
                worker=worker,
                attempt=attempt,
                evidence_files=evidence_files,
                error=str(exc),
                reason="planner_review_failed",
                event_type="planner_review_failed",
                event_message="Planner review failed after Worker evidence was saved",
            )
            return None

        attempt.status = decision.decision.upper()
        attempt.completed_at = self.store.now_iso()
        attempt.decision = decision.decision
        attempt.reason = decision.reason
        attempt.summary = _string_or_none(decision.raw.get("summary"))
        attempt.next_worker_prompt = decision.next_worker_prompt
        manifest.review = ReviewRecord(
            decision=decision.decision,
            reason=decision.reason,
            summary=_string_or_none(decision.raw.get("summary")),
            next_worker_prompt=decision.next_worker_prompt,
            evidence_files=evidence_files,
        )
        if decision.decision == "revision_requested":
            worker.status = RUN_REVISION_REQUESTED
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_review_completed",
            "Planner review completed",
            review_attempt_id=attempt.id,
            decision=decision.decision,
        )
        return decision

    def _mark_retryable_review_failure(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        attempt: ReviewAttemptRecord,
        evidence_files: List[str],
        error: str,
        reason: str,
        event_type: str,
        event_message: str,
    ) -> None:
        attempt.status = REVIEW_ATTEMPT_FAILED_RETRYABLE
        attempt.completed_at = self.store.now_iso()
        attempt.reason = reason
        attempt.error = error
        attempt.evidence_files = list(evidence_files)
        self._transition_status(
            manifest,
            RUN_WORK_DONE,
            worker_id=worker.id,
            worker_attempt=worker.attempt,
            reason=reason,
        )
        manifest.planner.status = (
            RUN_PLAN_APPROVED
            if manifest.plan and manifest.plan.approval_status in {"approved", "not_required"}
            else RUN_PLAN_READY
        )
        worker.status = WORKER_DONE if worker.status == WORKER_DONE else worker.status
        manifest.review = ReviewRecord(evidence_files=evidence_files)
        attempt.retry_attempt = sum(
            1 for item in manifest.review_attempts if item.status == REVIEW_ATTEMPT_FAILED_RETRYABLE
        )
        attempt.retry_budget = DEFAULT_RETRY_BUDGET
        retry_backoff = classify_retry_backoff_decision(manifest)
        if retry_backoff is not None:
            attempt.retry_attempt = retry_backoff.retry_attempt
            attempt.retry_budget = retry_backoff.retry_budget
            attempt.next_retry_at = retry_backoff.next_retry_at
            attempt.backoff_reason = retry_backoff.backoff_reason
            attempt.retry_budget_exhausted = retry_backoff.budget_exhausted
        self._save(manifest)
        self._record_event(
            manifest,
            event_type,
            event_message,
            review_attempt_id=attempt.id,
            worker_id=worker.id,
            error=error,
        )
        decision = classify_retryable_review_failure(
            manifest,
            source=SOURCE_ORCHESTRATOR,
            attempt=worker.attempt,
        )
        if decision is not None:
            self._record_recovery_decision(manifest, decision)

    def _request_review_decision(
        self,
        *,
        manifest: RunManifest,
        plan: PlannerPlan,
        worker_result: WorkerResult,
        evidence: DiffEvidence,
        verification: VerificationReport,
        code_review: CodexReviewReport,
        review_workspace: str,
        worker_attempt: Optional[int] = None,
        review_attempt_id: Optional[str] = None,
    ) -> ReviewDecision:
        if not manifest.planner.thread_id:
            raise OrchestratorError("cannot review without planner thread_id")
        lease_phase = "planner_review"
        planner_review_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_planner_review_reply",
                review_attempt_id=review_attempt_id,
                extra={"worker_attempt": worker_attempt},
            ),
        )
        primary_prompt = planner_review_prompt(
            original_plan_json=plan.raw,
            worker_result_json=worker_result.raw,
            review_workspace=review_workspace,
            diff_summary=evidence.summary,
            diff_path=str(evidence.patch_path),
            test_summary=verification.summary,
            test_output_path=str(verification.output_path),
            code_review_summary=code_review.summary,
            code_review_output_path=str(code_review.output_path),
        )
        previous_thread_id = manifest.planner.thread_id
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=planner_review_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_review_reply_inflight",
                    review_attempt_id=review_attempt_id,
                    extra={"worker_attempt": worker_attempt},
                ),
            )
            started_at = self.store.now_iso()
            result = self.driver.reply(
                thread_id=previous_thread_id,
                prompt=primary_prompt,
                model=manifest.planner.model,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
            )
            self._record_usage_attribution(
                manifest=manifest,
                role="planner",
                phase="review",
                thread_id=result.thread_id,
                session_id=_session_id_from_result(result),
                model=manifest.planner.model,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
                started_at=started_at,
                updated_at=self.store.now_iso(),
                worktree_path=review_workspace,
            )
            self._record_review_recovery_event(
                manifest,
                result,
                old_thread_id=previous_thread_id,
                attempt=worker_attempt,
            )
            decision = ReviewDecision.parse(result.content)
            self._lease_complete(
                manifest=manifest,
                runner_id=planner_review_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="planner_review_completed",
                    review_attempt_id=review_attempt_id,
                    extra={"worker_attempt": worker_attempt, "decision": decision.decision},
                ),
            )
            return decision
        except Exception as exc:
            decision = classify_operation_failure(
                exc,
                phase="planner_review_reply",
                source=SOURCE_ORCHESTRATOR,
                attempt=worker_attempt,
            )
            self._record_recovery_decision(manifest, decision)
            if decision.recovery_action != "start_replacement_agent":
                self._lease_fail(
                    manifest=manifest,
                    runner_id=planner_review_lease_id,
                    phase=lease_phase,
                    checkpoint=self._lease_checkpoint(
                        manifest=manifest,
                        phase_boundary="planner_review_failed",
                        review_attempt_id=review_attempt_id,
                        extra={"worker_attempt": worker_attempt},
                    ),
                    error=str(exc),
                )
                raise
            self._record_event(
                manifest,
                "planner_review_fallback_started",
                "Planner review fallback started after unrecoverable thread error",
                old_thread_id=previous_thread_id,
                error=str(exc),
            )
            try:
                self._lease_heartbeat(
                    manifest=manifest,
                    runner_id=planner_review_lease_id,
                    phase=lease_phase,
                    checkpoint=self._lease_checkpoint(
                        manifest=manifest,
                        phase_boundary="planner_review_fallback_start_session_inflight",
                        review_attempt_id=review_attempt_id,
                        extra={"worker_attempt": worker_attempt, "old_thread_id": previous_thread_id},
                    ),
                )
                started_at = self.store.now_iso()
                fallback_result = self.driver.start_session(
                    role="planner",
                    model=manifest.planner.model,
                    cwd=review_workspace,
                    prompt=planner_review_fallback_prompt(
                        user_task=manifest.user_task,
                        original_plan_json=plan.raw,
                        acceptance_criteria=plan.acceptance_criteria,
                        worker_result_json=worker_result.raw,
                        review_workspace=review_workspace,
                        diff_summary=evidence.summary,
                        diff_path=str(evidence.patch_path),
                        test_summary=verification.summary,
                        test_output_path=str(verification.output_path),
                        code_review_summary=code_review.summary,
                        code_review_output_path=str(code_review.output_path),
                    ),
                    sandbox=self.config.sandbox,
                    approval_policy=self.config.approval_policy,
                    reasoning_effort=manifest.planner.reasoning_effort,
                    service_tier=manifest.planner.service_tier,
                )
                self._record_usage_attribution(
                    manifest=manifest,
                    role="planner",
                    phase="review",
                    thread_id=fallback_result.thread_id,
                    session_id=_session_id_from_result(fallback_result),
                    model=manifest.planner.model,
                    reasoning_effort=manifest.planner.reasoning_effort,
                    service_tier=manifest.planner.service_tier,
                    started_at=started_at,
                    updated_at=self.store.now_iso(),
                    worktree_path=review_workspace,
                )
                manifest.planner.thread_id = fallback_result.thread_id
                self._save(manifest)
                self._record_event(
                    manifest,
                    "planner_review_fallback_thread_started",
                    "Planner review fallback thread started",
                    old_thread_id=previous_thread_id,
                    new_thread_id=fallback_result.thread_id,
                )
                fallback_decision = ReviewDecision.parse(fallback_result.content)
                self._lease_complete(
                    manifest=manifest,
                    runner_id=planner_review_lease_id,
                    phase=lease_phase,
                    checkpoint=self._lease_checkpoint(
                        manifest=manifest,
                        phase_boundary="planner_review_completed_via_fallback",
                        review_attempt_id=review_attempt_id,
                        extra={"worker_attempt": worker_attempt, "decision": fallback_decision.decision},
                    ),
                )
                return fallback_decision
            except Exception as fallback_exc:
                self._lease_fail(
                    manifest=manifest,
                    runner_id=planner_review_lease_id,
                    phase=lease_phase,
                    checkpoint=self._lease_checkpoint(
                        manifest=manifest,
                        phase_boundary="planner_review_failed",
                        review_attempt_id=review_attempt_id,
                        extra={"worker_attempt": worker_attempt},
                    ),
                    error=str(fallback_exc),
                )
                raise

    def _run_code_review(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        attempt: ReviewAttemptRecord,
        worktree_path: str,
        evidence: DiffEvidence,
    ) -> CodexReviewReport:
        lease_phase = "code_review"
        code_review_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="before_code_review",
                worker=worker,
                review_attempt_id=attempt.id,
            ),
        )
        started_at = self.store.now_iso()
        reviewer_service_tier = self._effective_reviewer_service_tier(manifest)
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=code_review_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="code_review_inflight",
                    worker=worker,
                    review_attempt_id=attempt.id,
                ),
            )
            report = self.code_review_runner(
                cwd=worktree_path,
                evidence_dir=self._attempt_evidence_dir(manifest, worker) / attempt.id,
                codex_binary_path=_effective_codex_binary_path(manifest, self.driver),
                patch_path=evidence.patch_path,
                service_tier=reviewer_service_tier,
            )
            # review CLI currently does not return Codex thread/session ids.
            self._record_usage_attribution(
                manifest=manifest,
                role="reviewer",
                phase="review",
                thread_id=None,
                session_id=None,
                model=None,
                reasoning_effort=None,
                service_tier=reviewer_service_tier,
                started_at=started_at,
                updated_at=self.store.now_iso(),
                worktree_path=worktree_path,
                worker_id=worker.id,
                attempt=worker.attempt,
            )
            worker.evidence_files = _append_unique(worker.evidence_files, report.evidence_files)
            self._save(manifest)
            self._lease_complete(
                manifest=manifest,
                runner_id=code_review_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="code_review_completed",
                    worker=worker,
                    review_attempt_id=attempt.id,
                    extra={"code_review_status": report.status},
                ),
            )
            return report
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=code_review_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="code_review_failed",
                    worker=worker,
                    review_attempt_id=attempt.id,
                ),
                error=str(exc),
            )
            raise

    def _effective_reviewer_service_tier(self, manifest: RunManifest) -> Optional[str]:
        if self.config.reviewer_service_tier is not None:
            return self.config.reviewer_service_tier
        return manifest.reviewer_service_tier

    def _record_review_recovery_event(
        self,
        manifest: RunManifest,
        result: SessionResult,
        *,
        old_thread_id: str,
        attempt: Optional[int] = None,
    ) -> None:
        raw = result.raw
        if raw.get("resumedAfterMcpTimeout") is True:
            method = "codex_exec_resume_after_mcp_timeout"
        elif raw.get("resumedWithCodexExec") is True:
            method = "codex_exec_resume"
        elif raw.get("recoveredFromSessionLog"):
            method = "session_log"
        else:
            return
        self._record_event(
            manifest,
            "planner_review_recovered",
            "Planner review recovered from transport uncertainty",
            method=method,
            old_thread_id=old_thread_id,
            thread_id=result.thread_id,
            recovered_from_session_log=raw.get("recoveredFromSessionLog"),
        )
        decision = classify_session_recovery_result(
            result,
            source=SOURCE_ORCHESTRATOR,
            attempt=attempt,
        )
        if decision is not None:
            self._record_recovery_decision(manifest, decision)

    def _continue_after_revision_requested(
        self,
        manifest: RunManifest,
        worker: WorkerRecord,
        plan: PlannerPlan,
        decision: ReviewDecision,
    ) -> RunManifest:
        self._record_recovery_decision(
            manifest,
            classify_code_review_findings(
                reason=decision.reason,
                source=SOURCE_ORCHESTRATOR,
                attempt=worker.attempt,
            ),
        )
        self._enter_revision_requested(manifest, worker)
        self._save(manifest)
        if worker.attempt >= self.config.max_attempts:
            self._record_recovery_decision(
                manifest,
                classify_max_attempts_exceeded(
                    source=SOURCE_ORCHESTRATOR,
                    attempt=worker.attempt,
                ),
            )
            self._transition_status(manifest, RUN_FAILED, reason="max_attempts_reached")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="max_attempts_reached")
            self._save(manifest)
            return manifest
        next_worker_prompt = _rework_worker_prompt(
            decision.next_worker_prompt or "",
            plan.acceptance_criteria,
            plan.verification_commands,
        )
        worker.attempt += 1
        worker_result = self._run_worker_attempt(
            manifest=manifest,
            worker=worker,
            prompt=next_worker_prompt,
            worktree_path=_required_worktree_path(worker),
            is_initial_attempt=False,
        )
        evidence = self._collect_evidence(
            manifest=manifest,
            worker=worker,
            worktree_path=_required_worktree_path(worker),
        )
        verification = self._run_verification(
            manifest=manifest,
            worker=worker,
            worktree_path=_required_worktree_path(worker),
        )
        next_decision = self._review_attempt(
            manifest=manifest,
            worker=worker,
            plan=plan,
            worker_result=worker_result,
            evidence=evidence,
            verification=verification,
        )
        if next_decision is None:
            return manifest
        if next_decision.decision == "revision_requested":
            return self._continue_after_revision_requested(manifest, worker, plan, next_decision)
        if next_decision.decision == "accepted":
            return self._complete_after_accepted_review(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
                verification=verification,
            )
        self._transition_status(manifest, RUN_FAILED, reason="unexpected_review_decision")
        manifest.planner.status = RUN_FAILED
        worker.status = RUN_FAILED
        self._record_terminal_status(manifest, reason="unexpected_review_decision")
        self._save(manifest)
        return manifest

    def _save(self, manifest: RunManifest) -> None:
        self.store.save(manifest)

    def _transition_status(self, manifest: RunManifest, new_status: str, **metadata: Any) -> None:
        record_run_status_transition(
            manifest,
            new_status,
            self.store.now_iso(),
            metadata=metadata or None,
        )

    def _enter_revision_requested(self, manifest: RunManifest, worker: WorkerRecord) -> None:
        self._transition_status(
            manifest,
            RUN_REVISION_REQUESTED,
            worker_id=worker.id,
            worker_attempt=worker.attempt,
        )
        worker.status = RUN_REVISION_REQUESTED

    def _record_event(
        self,
        manifest: RunManifest,
        event_type: str,
        message: str,
        **fields: Any,
    ) -> None:
        self.store.append_event(manifest.run_id, event_type, message, **fields)

    def _record_usage_attribution(
        self,
        *,
        manifest: RunManifest,
        role: str,
        phase: str,
        thread_id: Optional[str],
        session_id: Optional[str],
        model: Optional[str],
        reasoning_effort: Optional[str],
        service_tier: Optional[str],
        started_at: Optional[str],
        updated_at: Optional[str],
        worktree_path: Optional[str],
        worker_id: Optional[str] = None,
        attempt: Optional[int] = None,
        note: Optional[str] = None,
    ) -> None:
        try:
            self.store.append_usage_attribution(
                manifest.run_id,
                source="c-orch",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                workspace_id=manifest.workspace_id,
                cwd=manifest.cwd,
                role=role,
                phase=phase,
                thread_id=thread_id,
                session_id=session_id,
                model=model,
                reasoning_effort=reasoning_effort,
                service_tier=service_tier,
                started_at=started_at,
                updated_at=updated_at,
                worktree_path=worktree_path,
                worker_id=worker_id,
                attempt=attempt,
                note=note,
            )
        except Exception as exc:
            try:
                self._record_event(
                    manifest,
                    "usage_attribution_failed",
                    "Failed to write usage attribution sidecar.",
                    role=role,
                    phase=phase,
                    error=str(exc),
                )
            except Exception:
                pass

    def _runner_lease_store(self, run_id: str) -> RunnerLeaseStore:
        return self.store.runner_lease_store(run_id)

    def _lease_checkpoint(
        self,
        *,
        manifest: RunManifest,
        phase_boundary: str,
        worker: Optional[WorkerRecord] = None,
        review_attempt_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        checkpoint: Dict[str, Any] = {
            "run_status": manifest.status,
            "phase_boundary": phase_boundary,
            "planner_thread_id": manifest.planner.thread_id,
        }
        if worker is not None:
            checkpoint["worker_id"] = worker.id
            checkpoint["worker_attempt"] = worker.attempt
            checkpoint["worker_thread_id"] = worker.thread_id
            checkpoint["worktree_path"] = worker.worktree_path
        if review_attempt_id:
            checkpoint["review_attempt_id"] = review_attempt_id
        if extra:
            checkpoint.update(extra)
        return checkpoint

    def _lease_create(
        self,
        *,
        manifest: RunManifest,
        phase: str,
        checkpoint: Dict[str, Any],
    ) -> Optional[str]:
        try:
            lease = self._runner_lease_store(manifest.run_id).create(
                runtime_generation=self._runtime_generation,
                run_id=manifest.run_id,
                phase=phase,
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint=self._process_hint,
                pid=os.getpid(),
                checkpoint=checkpoint,
                lease_ttl_seconds=self._runner_lease_ttl_seconds,
            )
            return lease.runner_id
        except Exception as exc:
            self._record_event(
                manifest,
                "runner_lease_write_failed",
                "Runner lease create failed",
                phase=phase,
                operation="create",
                error=str(exc),
            )
            return None

    def _lease_heartbeat(
        self,
        *,
        manifest: RunManifest,
        runner_id: Optional[str],
        phase: str,
        checkpoint: Dict[str, Any],
    ) -> None:
        if not runner_id:
            return
        try:
            self._runner_lease_store(manifest.run_id).heartbeat(
                runner_id,
                checkpoint=checkpoint,
                lease_ttl_seconds=self._runner_lease_ttl_seconds,
            )
        except Exception as exc:
            self._record_event(
                manifest,
                "runner_lease_write_failed",
                "Runner lease heartbeat failed",
                phase=phase,
                operation="heartbeat",
                runner_id=runner_id,
                error=str(exc),
            )

    def _lease_complete(
        self,
        *,
        manifest: RunManifest,
        runner_id: Optional[str],
        phase: str,
        checkpoint: Dict[str, Any],
    ) -> None:
        if not runner_id:
            return
        try:
            self._runner_lease_store(manifest.run_id).complete(
                runner_id,
                checkpoint=checkpoint,
            )
        except Exception as exc:
            self._record_event(
                manifest,
                "runner_lease_write_failed",
                "Runner lease complete failed",
                phase=phase,
                operation="complete",
                runner_id=runner_id,
                error=str(exc),
            )

    def _lease_fail(
        self,
        *,
        manifest: RunManifest,
        runner_id: Optional[str],
        phase: str,
        checkpoint: Dict[str, Any],
        error: str,
    ) -> None:
        if not runner_id:
            return
        try:
            self._runner_lease_store(manifest.run_id).fail(
                runner_id,
                checkpoint=checkpoint,
                error=error,
            )
        except Exception as exc:
            self._record_event(
                manifest,
                "runner_lease_write_failed",
                "Runner lease fail failed",
                phase=phase,
                operation="fail",
                runner_id=runner_id,
                error=str(exc),
            )

    def _record_recovery_decision(
        self,
        manifest: RunManifest,
        decision: FailurePolicyDecision,
    ) -> None:
        self._record_event(
            manifest,
            "recovery_decision_recorded",
            "Failure recovery decision recorded",
            **decision.to_event_fields(),
        )

    def _record_terminal_status(
        self,
        manifest: RunManifest,
        *,
        reason: Optional[str] = None,
    ) -> None:
        if manifest.status not in TERMINAL_RUN_STATUSES:
            return
        events = self.store.load_events(manifest.run_id)
        if events:
            last = events[-1]
            if (
                isinstance(last, dict)
                and last.get("type") == "run_terminal_status"
                and str(last.get("status", "")) == manifest.status
            ):
                return
        self._record_event(
            manifest,
            "run_terminal_status",
            f"Run finished with {manifest.status}",
            status=manifest.status,
            reason=reason,
        )

    def _record_apply_started(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        pre_apply_changed_paths: List[str],
        pre_apply_staged_paths: List[str],
        replay: bool = False,
    ) -> None:
        self._record_event(
            manifest,
            "apply_started",
            "Apply started",
            worker_id=worker.id,
            attempt=worker.attempt,
            patch_path=str(evidence.patch_path),
            patch_hash=_sha256_text(evidence.patch),
            changed_paths=list(evidence.changed_paths),
            pre_apply_changed_paths=list(pre_apply_changed_paths),
            pre_apply_staged_paths=list(pre_apply_staged_paths),
            replay=replay,
        )

    def _record_git_commit_started(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        pre_apply_changed_paths: List[str],
        pre_apply_staged_paths: List[str],
        post_apply_changed_paths: List[str],
        replay: bool = False,
    ) -> None:
        self._record_event(
            manifest,
            "git_commit_started",
            "Git commit started",
            worker_id=worker.id,
            attempt=worker.attempt,
            patch_path=str(evidence.patch_path),
            patch_hash=_sha256_text(evidence.patch),
            changed_paths=list(evidence.changed_paths),
            pre_apply_changed_paths=list(pre_apply_changed_paths),
            pre_apply_staged_paths=list(pre_apply_staged_paths),
            post_apply_changed_paths=list(post_apply_changed_paths),
            replay=replay,
        )

    def _apply_then_continue_terminalization(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        verification: VerificationReport,
        replay: bool,
    ) -> RunManifest:
        pre_apply_changed_paths = self.repo_changed_paths_collector(manifest.cwd)
        pre_apply_staged_paths = self.repo_staged_paths_collector(manifest.cwd)
        self._record_apply_started(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
            pre_apply_changed_paths=pre_apply_changed_paths,
            pre_apply_staged_paths=pre_apply_staged_paths,
            replay=replay,
        )
        apply_report = self._apply_reviewed_diff(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
        )
        if not apply_report.applied:
            self._record_recovery_decision(
                manifest,
                classify_apply_failure(
                    source=SOURCE_ORCHESTRATOR,
                    attempt=worker.attempt,
                    error=apply_report.summary,
                ),
            )
            self._transition_status(manifest, RUN_FAILED, reason="apply_failed")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="apply_failed")
            self._save(manifest)
            return manifest
        return self.reconcile_accepted_review_terminalization(manifest)

    def _determine_partial_apply_state(self, *, manifest: RunManifest, evidence: DiffEvidence) -> str:
        if not evidence.patch.strip():
            return "already_applied"
        reverse_ok = self._git_apply_check(manifest.cwd, evidence.patch, reverse=True)
        if reverse_ok:
            return "already_applied"
        forward_ok = self._git_apply_check(manifest.cwd, evidence.patch, reverse=False)
        if forward_ok:
            return "not_applied"
        return "unknown"

    def _determine_commit_replay_state(self, *, manifest: RunManifest, evidence: DiffEvidence) -> str:
        changed_paths = set(self.repo_changed_paths_collector(manifest.cwd))
        if not changed_paths:
            return "cannot_prove_commit_boundary"
        planned_paths = {_normalize_repo_path(path) for path in evidence.changed_paths if path}
        if not planned_paths:
            return "cannot_prove_commit_boundary"
        if changed_paths.issubset(planned_paths):
            return "safe_to_commit"
        return "cannot_prove_commit_boundary"

    def _head_commit_terminalization_marker(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
    ) -> dict[str, Optional[str] | bool]:
        result = {"matched": False, "head": None, "message": None}
        try:
            completed = _git_process(["log", "-1", "--pretty=%B"], cwd=Path(manifest.cwd).expanduser().resolve())
            if completed.returncode != 0:
                return result
            message = completed.stdout or ""
            head = _git_process(["rev-parse", "HEAD"], cwd=Path(manifest.cwd).expanduser().resolve())
            head_hash = head.stdout.strip() if head.returncode == 0 else None
            markers = (
                f"Run-ID: {manifest.run_id}",
                f"Worker-ID: {worker.id}",
                "Planner-Review: accepted",
            )
            matched = all(marker in message for marker in markers)
            result["matched"] = matched
            result["head"] = head_hash
            result["message"] = message
            return result
        except Exception:
            return result

    def _latest_apply_started_list_field(self, *, events: List[dict[str, Any]], key: str) -> List[str]:
        for event in reversed(events):
            if not isinstance(event, dict) or event.get("type") != "apply_started":
                continue
            raw = event.get(key)
            if not isinstance(raw, list):
                continue
            return [str(value) for value in raw]
        return []

    def _latest_event_by_type(
        self,
        *,
        events: List[dict[str, Any]],
        event_type: str,
    ) -> Optional[dict[str, Any]]:
        for event in reversed(events):
            if isinstance(event, dict) and event.get("type") == event_type:
                return event
        return None

    def _record_terminalization_manual_recovery_required(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        waiting_for: str,
        reason: str,
    ) -> None:
        events = self.store.load_events(manifest.run_id)
        changed_paths = self.repo_changed_paths_collector(manifest.cwd)
        staged_paths = self.repo_staged_paths_collector(manifest.cwd)
        marker = self._head_commit_terminalization_marker(manifest=manifest, worker=worker)
        latest_manual = None
        for event in reversed(events):
            if isinstance(event, dict) and event.get("type") == EVENT_TERMINALIZATION_RECOVERY_REQUIRED:
                latest_manual = event
                break
        fingerprint = {
            "waiting_for": waiting_for,
            "reason": reason,
            "changed_paths": changed_paths,
            "staged_paths": staged_paths,
            "head": marker.get("head"),
            "patch_hash": _sha256_text(evidence.patch),
        }
        if latest_manual is not None and all(latest_manual.get(key) == value for key, value in fingerprint.items()):
            return
        self._record_event(
            manifest,
            EVENT_TERMINALIZATION_RECOVERY_REQUIRED,
            "Accepted-review terminalization requires manual recovery.",
            worker_id=worker.id,
            attempt=worker.attempt,
            waiting_for=waiting_for,
            reason=reason,
            action="manual_terminalization_recovery",
            changed_paths=changed_paths,
            staged_paths=staged_paths,
            patch_path=str(evidence.patch_path),
            patch_hash=_sha256_text(evidence.patch),
            head=marker.get("head"),
            head_message=marker.get("message"),
            marker_matched=bool(marker.get("matched")),
            review_evidence_files=list(manifest.review.evidence_files) if manifest.review is not None else [],
        )

    def _git_apply_check(self, repo_path: str, patch: str, *, reverse: bool) -> bool:
        command = ["apply", "--check", "--binary", "-"]
        if reverse:
            command.insert(1, "--reverse")
        completed = _git_process(command, cwd=Path(repo_path).expanduser().resolve(), stdin=patch)
        return completed.returncode == 0

    def _has_event(self, manifest: RunManifest, event_type: str) -> bool:
        return any(
            isinstance(event, dict) and event.get("type") == event_type
            for event in self.store.load_events(manifest.run_id)
        )

    def _evidence_dir(self, manifest: RunManifest) -> Path:
        return self.store.run_dir(manifest.run_id) / "evidence"

    def _attempt_evidence_dir(self, manifest: RunManifest, worker: WorkerRecord) -> Path:
        return self._evidence_dir(manifest) / f"attempt-{worker.attempt}"

    def _apply_reviewed_diff(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
    ) -> ApplyReport:
        lease_phase = "apply_terminalization"
        apply_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="apply_boundary_before_diff_applier",
                worker=worker,
            ),
        )
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=apply_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="apply_boundary_inflight",
                    worker=worker,
                ),
            )
            apply_report = self.diff_applier(
                evidence,
                manifest.cwd,
                self._attempt_evidence_dir(manifest, worker),
            )
            worker.evidence_files = _append_unique(worker.evidence_files, apply_report.evidence_files)
            if manifest.review is not None:
                manifest.review.evidence_files = _append_unique(
                    manifest.review.evidence_files,
                    apply_report.evidence_files,
                )
            restart_paths: List[str] = []
            if apply_report.applied:
                restart_paths = self._restart_trigger_paths(manifest=manifest, changed_paths=evidence.changed_paths)
                if restart_paths:
                    manifest.requires_restart = True
                    manifest.restart_reason = (
                        "Applied diff touched c-orch runtime code or critical config."
                    )
                    manifest.restart_paths = _append_unique(manifest.restart_paths, restart_paths)
            self._save(manifest)
            self._record_event(
                manifest,
                "apply_completed",
                "Apply completed",
                applied=apply_report.applied,
                summary=apply_report.summary,
            )
            if apply_report.applied and restart_paths:
                self._record_event(
                    manifest,
                    "restart_required",
                    "Run applied self-modifying changes; restart before next queued work.",
                    restart_reason=manifest.restart_reason,
                    restart_paths=manifest.restart_paths,
                )
            self._lease_complete(
                manifest=manifest,
                runner_id=apply_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="apply_boundary_completed",
                    worker=worker,
                    extra={"applied": apply_report.applied},
                ),
            )
            return apply_report
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=apply_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="apply_boundary_failed",
                    worker=worker,
                ),
                error=str(exc),
            )
            raise

    def _complete_after_accepted_review(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        verification: VerificationReport,
    ) -> RunManifest:
        if _verification_has_failures(verification):
            self._record_recovery_decision(
                manifest,
                classify_verification_gate_failure(
                    source=SOURCE_ORCHESTRATOR,
                    attempt=worker.attempt,
                    error=verification.summary,
                ),
            )
            self._transition_status(manifest, RUN_FAILED, reason="verification_failed")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_event(
                manifest,
                "verification_gate_failed",
                "Verification gate failed; refusing apply and commit.",
                summary=verification.summary,
            )
            self._record_terminal_status(manifest, reason="verification_failed")
            self._save(manifest)
            return manifest

        pre_apply_changed_paths = self.repo_changed_paths_collector(manifest.cwd)
        pre_apply_staged_paths = self.repo_staged_paths_collector(manifest.cwd)
        self._record_apply_started(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
            pre_apply_changed_paths=pre_apply_changed_paths,
            pre_apply_staged_paths=pre_apply_staged_paths,
        )
        apply_report = self._apply_reviewed_diff(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
        )
        if not apply_report.applied:
            self._record_recovery_decision(
                manifest,
                classify_apply_failure(
                    source=SOURCE_ORCHESTRATOR,
                    attempt=worker.attempt,
                    error=apply_report.summary,
                ),
            )
            self._transition_status(manifest, RUN_FAILED, reason="apply_failed")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="apply_failed")
            self._save(manifest)
            return manifest

        post_apply_changed_paths = self.repo_changed_paths_collector(manifest.cwd)
        self._record_git_commit_started(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
            pre_apply_changed_paths=pre_apply_changed_paths,
            pre_apply_staged_paths=pre_apply_staged_paths,
            post_apply_changed_paths=post_apply_changed_paths,
        )
        commit_report = self._commit_applied_diff(
            manifest=manifest,
            worker=worker,
            evidence=evidence,
            pre_apply_changed_paths=pre_apply_changed_paths,
            pre_apply_staged_paths=pre_apply_staged_paths,
            post_apply_changed_paths=post_apply_changed_paths,
        )
        if commit_report.failed:
            self._record_recovery_decision(
                manifest,
                classify_git_commit_failure(
                    failure_reason=commit_report.failure_reason,
                    summary=commit_report.summary,
                    source=SOURCE_ORCHESTRATOR,
                    attempt=worker.attempt,
                ),
            )
            self._transition_status(manifest, RUN_FAILED, reason="git_commit_failed")
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="git_commit_failed")
            self._save(manifest)
            return manifest

        self._transition_status(manifest, RUN_APPROVED)
        manifest.planner.status = RUN_APPROVED
        worker.status = RUN_APPROVED
        self._record_terminal_status(manifest)
        self._save(manifest)
        return manifest

    def _commit_applied_diff(
        self,
        *,
        manifest: RunManifest,
        worker: WorkerRecord,
        evidence: DiffEvidence,
        pre_apply_changed_paths: List[str],
        pre_apply_staged_paths: List[str],
        post_apply_changed_paths: Optional[List[str]] = None,
    ) -> GitCommitReport:
        review_decision = "accepted"
        review_reason: Optional[str] = None
        if manifest.review is not None:
            review_decision = manifest.review.decision or "accepted"
            review_reason = manifest.review.reason
        lease_phase = "commit_terminalization"
        commit_lease_id = self._lease_create(
            manifest=manifest,
            phase=lease_phase,
            checkpoint=self._lease_checkpoint(
                manifest=manifest,
                phase_boundary="commit_boundary_before_git_commit",
                worker=worker,
            ),
        )
        try:
            self._lease_heartbeat(
                manifest=manifest,
                runner_id=commit_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="commit_boundary_inflight",
                    worker=worker,
                ),
            )
            commit_report = self.git_committer(
                target_repo_path=manifest.cwd,
                evidence_dir=self._attempt_evidence_dir(manifest, worker),
                run_id=manifest.run_id,
                worker_id=worker.id,
                planner_review_decision=review_decision,
                user_task=manifest.user_task,
                plan_summary=manifest.plan.summary if manifest.plan is not None else None,
                review_reason=review_reason,
                changed_paths=list(evidence.changed_paths),
                pre_apply_changed_paths=list(pre_apply_changed_paths),
                pre_apply_staged_paths=list(pre_apply_staged_paths),
                post_apply_changed_paths=list(post_apply_changed_paths)
                if post_apply_changed_paths is not None
                else self.repo_changed_paths_collector(manifest.cwd),
            )
            worker.evidence_files = _append_unique(worker.evidence_files, commit_report.evidence_files)
            if manifest.review is not None:
                manifest.review.evidence_files = _append_unique(
                    manifest.review.evidence_files,
                    commit_report.evidence_files,
                )
            self._save(manifest)
            if commit_report.committed:
                self._record_event(
                    manifest,
                    "git_commit_completed",
                    "Git commit completed",
                    commit_hash=commit_report.commit_hash,
                    summary=commit_report.summary,
                    message_path=str(commit_report.message_path),
                    output_path=str(commit_report.output_path),
                )
            elif commit_report.skipped:
                self._record_event(
                    manifest,
                    "git_commit_skipped",
                    "Git commit skipped",
                    summary=commit_report.summary,
                    message_path=str(commit_report.message_path),
                    output_path=str(commit_report.output_path),
                )
            else:
                self._record_event(
                    manifest,
                    "git_commit_failed",
                    "Git commit failed",
                    summary=commit_report.summary,
                    reason=commit_report.failure_reason,
                    message_path=str(commit_report.message_path),
                    output_path=str(commit_report.output_path),
                )
            self._lease_complete(
                manifest=manifest,
                runner_id=commit_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="commit_boundary_completed",
                    worker=worker,
                    extra={"commit_status": commit_report.status},
                ),
            )
            return commit_report
        except Exception as exc:
            self._lease_fail(
                manifest=manifest,
                runner_id=commit_lease_id,
                phase=lease_phase,
                checkpoint=self._lease_checkpoint(
                    manifest=manifest,
                    phase_boundary="commit_boundary_failed",
                    worker=worker,
                ),
                error=str(exc),
            )
            raise

    def _restart_trigger_paths(self, *, manifest: RunManifest, changed_paths: List[str]) -> List[str]:
        if not self._is_controller_repo(manifest.cwd):
            return []
        matched: List[str] = []
        seen = set()
        for raw_path in changed_paths:
            normalized = _normalize_repo_path(raw_path)
            if (
                normalized in RESTART_REQUIRED_EXACT_PATHS
                or any(normalized.startswith(prefix) for prefix in RESTART_REQUIRED_PREFIXES)
            ):
                if raw_path not in seen:
                    matched.append(raw_path)
                    seen.add(raw_path)
        return matched

    def _is_controller_repo(self, repo_path: str) -> bool:
        if not self.config.controller_repo_path:
            return False
        try:
            return canonical_git_root(repo_path) == canonical_git_root(self.config.controller_repo_path)
        except WorkspaceResolutionError:
            return Path(repo_path).expanduser().resolve() == Path(self.config.controller_repo_path).expanduser().resolve()


def run_single_worker(
    *,
    manifest: RunManifest,
    store: RunStore,
    driver: CodexDriver,
    evidence_collector: EvidenceCollector = collect_diff_evidence,
    verification_runner: VerificationRunner = run_verification_commands,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sandbox: str = DEFAULT_SANDBOX,
    approval_policy: str = DEFAULT_APPROVAL_POLICY,
    reviewer_service_tier: Optional[str] = None,
    require_plan_approval: bool = False,
    approve_plan: bool = False,
) -> RunManifest:
    """Run the MVP Planner/Worker loop for one manifest."""
    orchestrator = RunOrchestrator(
        store=store,
        driver=driver,
        evidence_collector=evidence_collector,
        verification_runner=verification_runner,
        config=OrchestratorConfig(
            sandbox=sandbox,
            approval_policy=approval_policy,
            max_attempts=max_attempts,
            reviewer_service_tier=reviewer_service_tier,
            require_plan_approval=require_plan_approval,
            approve_plan=approve_plan,
        ),
    )
    return orchestrator.run(manifest)


def _single_worker(manifest: RunManifest) -> WorkerRecord:
    if len(manifest.workers) != 1:
        raise OrchestratorError("MVP orchestrator requires exactly one worker")
    return manifest.workers[0]


def _required_worktree_path(worker: WorkerRecord) -> str:
    if not worker.worktree_path:
        raise OrchestratorError("worker worktree_path must be set before orchestration")
    return worker.worktree_path


def _initial_worker_prompt(plan: PlannerPlan) -> str:
    return _with_verification_commands(
        worker_prompt(
            planner_worker_prompt=plan.worker_prompt,
            acceptance_criteria=plan.acceptance_criteria,
        ),
        plan.verification_commands,
    )


def _rework_worker_prompt(
    next_worker_prompt: str,
    acceptance_criteria: List[str],
    verification_commands: List[str],
) -> str:
    return _with_verification_commands(
        worker_prompt(
            planner_worker_prompt=next_worker_prompt,
            acceptance_criteria=acceptance_criteria,
        ),
        verification_commands,
    )


def _with_verification_commands(prompt: str, commands: List[str]) -> str:
    if not commands:
        return prompt
    lines = "\n".join(f"- {command}" for command in commands)
    return f"{prompt}\nPlanner verification commands:\n{lines}\n"


def _planner_plan_from_record(record: PlanRecord, manifest: RunManifest) -> PlannerPlan:
    raw = dict(record.raw)
    if not raw:
        raw = {
            "status": "plan_ready",
            "summary": record.summary,
            "acceptance_criteria": list(manifest.acceptance_criteria),
            "worker_prompt": record.worker_prompt,
            "verification_commands": list(manifest.verification_commands),
            "risk_notes": list(record.risk_notes),
            "decomposition_suggestion": dict(record.decomposition_suggestion)
            if isinstance(record.decomposition_suggestion, dict)
            else None,
        }
    return PlannerPlan(
        status="plan_ready",
        summary=record.summary,
        acceptance_criteria=list(manifest.acceptance_criteria),
        worker_prompt=record.worker_prompt,
        verification_commands=list(manifest.verification_commands),
        risk_notes=list(record.risk_notes),
        decomposition_suggestion=dict(record.decomposition_suggestion)
        if isinstance(record.decomposition_suggestion, dict)
        else None,
        raw=raw,
    )


def _plan_is_approved(manifest: RunManifest) -> bool:
    return manifest.plan is not None and manifest.plan.approval_status == "approved"


def _worker_status_from_result(result: WorkerResult) -> str:
    if result.status == "work_done":
        return WORKER_DONE
    return result.status.upper()


def _evidence_from_review_record(review: ReviewRecord) -> DiffEvidence:
    summary_path = _required_evidence_path(review.evidence_files, "git-diff-summary.md")
    patch_path = _required_evidence_path(review.evidence_files, "git-diff.patch")
    summary = _read_text(summary_path)
    patch = _read_text(patch_path)
    return DiffEvidence(
        summary_path=summary_path,
        patch_path=patch_path,
        summary=summary,
        patch=patch,
        changed_paths=_changed_paths_from_patch(patch),
    )


def _verification_from_review_record(review: ReviewRecord) -> VerificationReport:
    output_path = _first_existing_path(review.evidence_files, "verification-output.txt")
    return VerificationReport(
        summary=_read_text(output_path) or "Saved verification output is available.",
        output_path=output_path,
        results=[],
    )


def _required_evidence_path(paths: List[str], name: str) -> Path:
    path = _first_existing_path(paths, name)
    if path.name != name or not path.exists():
        raise OrchestratorError(f"accepted Planner review is missing required evidence file: {name}")
    return path


def _first_existing_path(paths: List[str], name: str) -> Path:
    for value in paths:
        path = Path(value)
        if path.name == name:
            return path
    return Path(paths[0]) if paths else Path(name)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _append_unique(existing: List[str], new_values: List[str]) -> List[str]:
    values = list(existing)
    seen = set(values)
    for value in new_values:
        if value not in seen:
            values.append(value)
            seen.add(value)
    return values


def _normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _changed_paths_from_patch(patch: str) -> List[str]:
    paths: List[str] = []
    seen = set()
    for line in patch.splitlines():
        if not line.startswith("diff --git "):
            continue
        parts = line.split(" ")
        if len(parts) < 4:
            continue
        candidate = parts[3]
        if candidate.startswith("b/"):
            candidate = candidate[2:]
        elif parts[2].startswith("a/"):
            candidate = parts[2][2:]
        if candidate and candidate not in seen:
            seen.add(candidate)
            paths.append(candidate)
    return paths


def _session_id_from_result(result: SessionResult) -> Optional[str]:
    structured = result.raw.get("structuredContent")
    if isinstance(structured, dict):
        session_id = structured.get("sessionId")
        if isinstance(session_id, str) and session_id.strip():
            return session_id
    for key in ("sessionId", "session_id"):
        value = result.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _effective_codex_binary_path(manifest: RunManifest, driver: CodexDriver) -> Optional[str]:
    driver_path = getattr(driver, "codex_bin", None)
    if isinstance(driver_path, str) and driver_path.strip():
        return driver_path
    return manifest.codex_binary_path or manifest.planner.codex_binary_path


def _git_process(
    args: List[str],
    *,
    cwd: Path,
    stdin: Optional[str] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _verification_has_failures(report: VerificationReport) -> bool:
    if not report.results:
        return False
    return any(result.status != "passed" for result in report.results)


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _default_runtime_generation() -> str:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    return f"cli:{os.getpid()}:{now}"
