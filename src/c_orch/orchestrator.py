from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Protocol, Union

from .contracts import PlannerPlan, ReviewDecision, WorkerResult
from .drivers import CodexDriver
from .failure_policy import has_retryable_review_failure, should_start_replacement_agent
from .prompts import (
    planner_initial_prompt,
    planner_review_fallback_prompt,
    planner_revision_prompt,
    planner_review_prompt,
    worker_prompt,
)
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
from .verification import VerificationReport, run_verification_commands
from .worktrees import (
    ApplyReport,
    DiffEvidence,
    apply_diff_evidence_to_repo,
    collect_diff_evidence,
)


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


class DiffApplier(Protocol):
    def __call__(
        self,
        diff: DiffEvidence,
        target_repo_path: Pathish,
        evidence_dir: Pathish,
    ) -> ApplyReport:
        ...


@dataclass(frozen=True)
class OrchestratorConfig:
    sandbox: str = DEFAULT_SANDBOX
    approval_policy: str = DEFAULT_APPROVAL_POLICY
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    require_plan_approval: bool = False
    approve_plan: bool = False


class RunOrchestrator:
    """Single-worker Planner/Worker state machine."""

    def __init__(
        self,
        *,
        store: RunStore,
        driver: CodexDriver,
        evidence_collector: EvidenceCollector = collect_diff_evidence,
        diff_applier: DiffApplier = apply_diff_evidence_to_repo,
        verification_runner: VerificationRunner = run_verification_commands,
        config: Optional[OrchestratorConfig] = None,
    ) -> None:
        self.store = store
        self.driver = driver
        self.evidence_collector = evidence_collector
        self.diff_applier = diff_applier
        self.verification_runner = verification_runner
        self.config = config or OrchestratorConfig()
        if self.config.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")

    def run(self, manifest: RunManifest) -> RunManifest:
        try:
            return self._run(manifest)
        except Exception:
            if manifest.status not in TERMINAL_RUN_STATUSES:
                manifest.status = RUN_FAILED
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
                apply_report = self._apply_reviewed_diff(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                )
                if apply_report.applied:
                    manifest.status = RUN_APPROVED
                    manifest.planner.status = RUN_APPROVED
                    worker.status = RUN_APPROVED
                    self._record_terminal_status(manifest)
                    self._save(manifest)
                    return manifest
                manifest.status = RUN_FAILED
                manifest.planner.status = RUN_FAILED
                worker.status = RUN_FAILED
                self._record_terminal_status(manifest, reason="apply_failed")
                self._save(manifest)
                return manifest

            if decision.decision == "revision_requested":
                if attempt >= self.config.max_attempts:
                    manifest.status = RUN_FAILED
                    manifest.planner.status = RUN_FAILED
                    worker.status = RUN_FAILED
                    self._record_terminal_status(manifest, reason="max_attempts_reached")
                    self._save(manifest)
                    return manifest
                manifest.status = RUN_REVISION_REQUESTED
                worker.status = RUN_REVISION_REQUESTED
                self._save(manifest)
                next_worker_prompt = _rework_worker_prompt(
                    decision.next_worker_prompt or "",
                    plan.acceptance_criteria,
                    plan.verification_commands,
                )
                continue

            manifest.status = RUN_FAILED
            manifest.planner.status = RUN_FAILED
            worker.status = RUN_FAILED
            self._record_terminal_status(manifest, reason="unexpected_review_decision")
            self._save(manifest)
            return manifest

        if worker_result is None:
            raise OrchestratorError("worker did not run")
        manifest.status = RUN_FAILED
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
            apply_report = self._apply_reviewed_diff(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
            )
            manifest.status = RUN_APPROVED if apply_report.applied else RUN_FAILED
            manifest.planner.status = manifest.status
            worker.status = manifest.status
            self._record_terminal_status(
                manifest,
                reason=None if apply_report.applied else "apply_failed",
            )
            self._save(manifest)
            return manifest
        if decision.decision == "revision_requested":
            return self._continue_after_revision_requested(manifest, worker, plan, decision)
        manifest.status = RUN_FAILED
        manifest.planner.status = RUN_FAILED
        worker.status = RUN_FAILED
        self._record_terminal_status(manifest, reason="unexpected_review_decision")
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
        manifest.status = RUN_PLAN_REVISING
        manifest.planner.status = RUN_PLAN_REVISING
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_plan_revision_started",
            "Planner plan revision started",
        )

        try:
            result = self.driver.reply(
                thread_id=manifest.planner.thread_id,
                prompt=planner_revision_prompt(human_feedback=normalized_feedback),
            )
            revised = PlannerPlan.parse(result.content)
        except Exception as exc:
            manifest.status = RUN_PLAN_REVIEW_REQUIRED
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
        manifest.status = RUN_PLAN_READY
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
        self._require_plan_review(manifest)
        return manifest

    def _start_planner(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        worktree_path = _required_worktree_path(worker)
        manifest.status = RUN_PLANNING
        manifest.planner.status = WORKER_ACTIVE
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_start",
            "Planner started",
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
        manifest.acceptance_criteria = list(plan.acceptance_criteria)
        manifest.verification_commands = list(plan.verification_commands)
        manifest.plan = PlanRecord(
            summary=plan.summary,
            worker_prompt=plan.worker_prompt,
            risk_notes=list(plan.risk_notes),
            raw=dict(plan.raw),
            approval_status="pending" if self.config.require_plan_approval else "not_required",
        )
        manifest.status = RUN_PLAN_READY
        manifest.planner.status = RUN_PLAN_READY
        self._record_event(
            manifest,
            "planner_plan_ready",
            "Planner plan ready",
            acceptance_count=len(manifest.acceptance_criteria),
            verification_count=len(manifest.verification_commands),
        )
        return plan

    def _require_plan_review(self, manifest: RunManifest) -> None:
        manifest.status = RUN_PLAN_REVIEW_REQUIRED
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
        manifest.status = RUN_PLAN_APPROVED
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
        manifest.status = RUN_WORKING
        worker.status = WORKER_ACTIVE
        self._save(manifest)
        self._record_event(
            manifest,
            "worker_start",
            "Worker attempt started",
            worker_id=worker.id,
            attempt=worker.attempt,
        )

        if is_initial_attempt:
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
                result = self.driver.reply(thread_id=previous_thread_id, prompt=prompt)
            except Exception as exc:
                if not should_start_replacement_agent(exc):
                    raise
                self._record_event(
                    manifest,
                    "worker_rework_fallback_started",
                    "Worker rework fallback started after unrecoverable thread error",
                    worker_id=worker.id,
                    old_thread_id=previous_thread_id,
                    error=str(exc),
                )
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
        worker.result = dict(worker_result.raw)
        manifest.status = RUN_WORK_DONE
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
        return worker_result

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
        return report

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

        manifest.status = RUN_REVIEWING
        manifest.planner.status = RUN_REVIEWING
        self._save(manifest)
        evidence_files = _append_unique(evidence.evidence_files, verification.evidence_files)
        workspace_path = _required_worktree_path(worker)
        attempt = ReviewAttemptRecord(
            id=f"review-{len(manifest.review_attempts) + 1}",
            worker_id=worker.id,
            status=REVIEW_ATTEMPT_ACTIVE,
            started_at=self.store.now_iso(),
            worker_attempt=worker.attempt,
            workspace_path=workspace_path,
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
            decision = self._request_review_decision(
                manifest=manifest,
                plan=plan,
                worker_result=worker_result,
                evidence=evidence,
                verification=verification,
                review_workspace=workspace_path,
            )
        except Exception as exc:
            attempt.status = REVIEW_ATTEMPT_FAILED_RETRYABLE
            attempt.completed_at = self.store.now_iso()
            attempt.error = str(exc)
            manifest.status = RUN_WORK_DONE
            manifest.planner.status = (
                RUN_PLAN_APPROVED
                if manifest.plan and manifest.plan.approval_status in {"approved", "not_required"}
                else RUN_PLAN_READY
            )
            worker.status = WORKER_DONE if worker.status == WORKER_DONE else worker.status
            manifest.review = ReviewRecord(evidence_files=evidence_files)
            self._save(manifest)
            self._record_event(
                manifest,
                "planner_review_failed",
                "Planner review failed after Worker evidence was saved",
                review_attempt_id=attempt.id,
                worker_id=worker.id,
                error=str(exc),
            )
            return None

        attempt.status = decision.decision.upper()
        attempt.completed_at = self.store.now_iso()
        attempt.decision = decision.decision
        attempt.reason = decision.reason
        attempt.next_worker_prompt = decision.next_worker_prompt
        manifest.review = ReviewRecord(
            decision=decision.decision,
            reason=decision.reason,
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

    def _request_review_decision(
        self,
        *,
        manifest: RunManifest,
        plan: PlannerPlan,
        worker_result: WorkerResult,
        evidence: DiffEvidence,
        verification: VerificationReport,
        review_workspace: str,
    ) -> ReviewDecision:
        if not manifest.planner.thread_id:
            raise OrchestratorError("cannot review without planner thread_id")
        primary_prompt = planner_review_prompt(
            original_plan_json=plan.raw,
            worker_result_json=worker_result.raw,
            review_workspace=review_workspace,
            diff_summary=evidence.summary,
            diff_path=str(evidence.patch_path),
            test_summary=verification.summary,
            test_output_path=str(verification.output_path),
        )
        previous_thread_id = manifest.planner.thread_id
        try:
            result = self.driver.reply(
                thread_id=previous_thread_id,
                prompt=primary_prompt,
            )
            return ReviewDecision.parse(result.content)
        except Exception as exc:
            if not should_start_replacement_agent(exc):
                raise
            self._record_event(
                manifest,
                "planner_review_fallback_started",
                "Planner review fallback started after unrecoverable thread error",
                old_thread_id=previous_thread_id,
                error=str(exc),
            )
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
                ),
                sandbox=self.config.sandbox,
                approval_policy=self.config.approval_policy,
                reasoning_effort=manifest.planner.reasoning_effort,
                service_tier=manifest.planner.service_tier,
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
            return ReviewDecision.parse(fallback_result.content)

    def _continue_after_revision_requested(
        self,
        manifest: RunManifest,
        worker: WorkerRecord,
        plan: PlannerPlan,
        decision: ReviewDecision,
    ) -> RunManifest:
        if worker.attempt >= self.config.max_attempts:
            manifest.status = RUN_FAILED
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
            apply_report = self._apply_reviewed_diff(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
            )
            manifest.status = RUN_APPROVED if apply_report.applied else RUN_FAILED
            manifest.planner.status = manifest.status
            worker.status = manifest.status
            self._record_terminal_status(
                manifest,
                reason=None if apply_report.applied else "apply_failed",
            )
            self._save(manifest)
            return manifest
        manifest.status = RUN_FAILED
        manifest.planner.status = RUN_FAILED
        worker.status = RUN_FAILED
        self._record_terminal_status(manifest, reason="unexpected_review_decision")
        self._save(manifest)
        return manifest

    def _save(self, manifest: RunManifest) -> None:
        self.store.save(manifest)

    def _record_event(
        self,
        manifest: RunManifest,
        event_type: str,
        message: str,
        **fields: Any,
    ) -> None:
        self.store.append_event(manifest.run_id, event_type, message, **fields)

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
            restart_paths = self._restart_trigger_paths(evidence.changed_paths)
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
        return apply_report

    def _restart_trigger_paths(self, changed_paths: List[str]) -> List[str]:
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
        }
    return PlannerPlan(
        status="plan_ready",
        summary=record.summary,
        acceptance_criteria=list(manifest.acceptance_criteria),
        worker_prompt=record.worker_prompt,
        verification_commands=list(manifest.verification_commands),
        risk_notes=list(record.risk_notes),
        raw=raw,
    )


def _plan_is_approved(manifest: RunManifest) -> bool:
    return manifest.plan is not None and manifest.plan.approval_status == "approved"


def _worker_status_from_result(result: WorkerResult) -> str:
    if result.status == "work_done":
        return WORKER_DONE
    return result.status.upper()


def _evidence_from_review_record(review: ReviewRecord) -> DiffEvidence:
    summary_path = _first_existing_path(review.evidence_files, "git-diff-summary.md")
    patch_path = _first_existing_path(review.evidence_files, "git-diff.patch")
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
