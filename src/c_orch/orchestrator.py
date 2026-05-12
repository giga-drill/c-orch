from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Protocol, Union

from .contracts import PlannerPlan, ReviewDecision, WorkerResult
from .drivers import CodexDriver
from .prompts import planner_initial_prompt, planner_review_prompt, worker_prompt
from .run_store import (
    PlanRecord,
    ReviewAttemptRecord,
    ReviewRecord,
    RunManifest,
    RunStore,
    WorkerRecord,
)
from .verification import VerificationReport, run_verification_commands
from .worktrees import (
    ApplyReport,
    DiffEvidence,
    apply_diff_evidence_to_repo,
    collect_diff_evidence,
)


Pathish = Union[str, Path]
TERMINAL_STATUSES = {"APPROVED", "BLOCKED", "FAILED"}
PLAN_REVIEW_REQUIRED = "PLAN_REVIEW_REQUIRED"
REVIEW_RETRYABLE = "REVIEW_RETRYABLE"


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
    sandbox: str = "workspace-write"
    approval_policy: str = "never"
    max_attempts: int = 3
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
            if manifest.status not in TERMINAL_STATUSES:
                manifest.status = "FAILED"
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

            if decision.decision == "approved":
                apply_report = self._apply_reviewed_diff(
                    manifest=manifest,
                    worker=worker,
                    evidence=evidence,
                )
                if apply_report.applied:
                    manifest.status = "APPROVED"
                    manifest.planner.status = "APPROVED"
                    worker.status = "APPROVED"
                    self._record_terminal_status(manifest)
                    self._save(manifest)
                    return manifest
                manifest.status = "FAILED"
                manifest.planner.status = "FAILED"
                worker.status = "FAILED"
                self._record_terminal_status(manifest, reason="apply_failed")
                self._save(manifest)
                return manifest

            if decision.decision == "needs_changes":
                if attempt >= self.config.max_attempts:
                    manifest.status = "FAILED"
                    manifest.planner.status = "FAILED"
                    worker.status = "FAILED"
                    self._record_terminal_status(manifest, reason="max_attempts_reached")
                    self._save(manifest)
                    return manifest
                manifest.status = "NEEDS_CHANGES"
                worker.status = "NEEDS_CHANGES"
                self._save(manifest)
                next_worker_prompt = _rework_worker_prompt(
                    decision.next_worker_prompt or "",
                    plan.acceptance_criteria,
                    plan.verification_commands,
                )
                continue

            if decision.decision == "blocked":
                manifest.status = "BLOCKED"
                manifest.planner.status = "BLOCKED"
                worker.status = "BLOCKED"
                self._record_terminal_status(manifest, reason="review_blocked")
                self._save(manifest)
                return manifest

            manifest.status = "FAILED"
            manifest.planner.status = "FAILED"
            worker.status = "FAILED"
            self._record_terminal_status(manifest, reason="review_failed")
            self._save(manifest)
            return manifest

        if worker_result is None:
            raise OrchestratorError("worker did not run")
        manifest.status = "FAILED"
        self._record_terminal_status(manifest, reason="fallthrough")
        self._save(manifest)
        return manifest

    def _ensure_plan(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        if manifest.plan is not None:
            return _planner_plan_from_record(manifest.plan, manifest)
        return self._start_planner(manifest, worker)

    def retry_review(self, manifest: RunManifest) -> RunManifest:
        worker = _single_worker(manifest)
        if manifest.status != REVIEW_RETRYABLE:
            raise OrchestratorError("run is not waiting for a retryable Planner review")
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
        if decision.decision == "approved":
            apply_report = self._apply_reviewed_diff(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
            )
            manifest.status = "APPROVED" if apply_report.applied else "FAILED"
            manifest.planner.status = manifest.status
            worker.status = manifest.status
            self._record_terminal_status(
                manifest,
                reason=None if apply_report.applied else "apply_failed",
            )
            self._save(manifest)
            return manifest
        if decision.decision == "needs_changes":
            return self._continue_after_needs_changes(manifest, worker, plan, decision)
        if decision.decision == "blocked":
            manifest.status = "BLOCKED"
            manifest.planner.status = "BLOCKED"
            worker.status = "BLOCKED"
            self._record_terminal_status(manifest, reason="review_blocked")
            self._save(manifest)
            return manifest
        manifest.status = "FAILED"
        manifest.planner.status = "FAILED"
        worker.status = "FAILED"
        self._record_terminal_status(manifest, reason="review_failed")
        self._save(manifest)
        return manifest

    def _start_planner(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        manifest.status = "PLANNING"
        manifest.planner.status = "ACTIVE"
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_start",
            "Planner started",
        )

        result = self.driver.start_session(
            role="planner",
            model=manifest.planner.model,
            cwd=manifest.cwd,
            prompt=planner_initial_prompt(
                user_task=manifest.user_task,
                cwd=manifest.cwd,
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
        manifest.status = "PLAN_READY"
        manifest.planner.status = "PLAN_READY"
        self._record_event(
            manifest,
            "planner_plan_ready",
            "Planner plan ready",
            acceptance_count=len(manifest.acceptance_criteria),
            verification_count=len(manifest.verification_commands),
        )
        return plan

    def _require_plan_review(self, manifest: RunManifest) -> None:
        manifest.status = PLAN_REVIEW_REQUIRED
        manifest.planner.status = PLAN_REVIEW_REQUIRED
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
        manifest.status = "PLAN_APPROVED"
        manifest.planner.status = "PLAN_APPROVED"
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
        manifest.status = "WORKING"
        worker.status = "ACTIVE"
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
            result = self.driver.reply(thread_id=worker.thread_id, prompt=prompt)

        worker_result = WorkerResult.parse(result.content)
        worker.result = dict(worker_result.raw)
        manifest.status = "WORK_DONE"
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
        evidence = self.evidence_collector(worktree_path, self._evidence_dir(manifest))
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
            evidence_dir=self._evidence_dir(manifest),
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

        manifest.status = "REVIEWING"
        manifest.planner.status = "REVIEWING"
        self._save(manifest)
        evidence_files = _append_unique(evidence.evidence_files, verification.evidence_files)
        attempt = ReviewAttemptRecord(
            id=f"review-{len(manifest.review_attempts) + 1}",
            worker_id=worker.id,
            status="ACTIVE",
            started_at=self.store.now_iso(),
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
            result = self.driver.reply(
                thread_id=manifest.planner.thread_id,
                prompt=planner_review_prompt(
                    original_plan_json=plan.raw,
                    worker_result_json=worker_result.raw,
                    diff_summary=evidence.summary,
                    diff_path=str(evidence.patch_path),
                    test_summary=verification.summary,
                    test_output_path=str(verification.output_path),
                ),
            )
            decision = ReviewDecision.parse(result.content)
        except Exception as exc:
            attempt.status = "FAILED_RETRYABLE"
            attempt.completed_at = self.store.now_iso()
            attempt.error = str(exc)
            manifest.status = REVIEW_RETRYABLE
            manifest.planner.status = REVIEW_RETRYABLE
            worker.status = "DONE" if worker.status == "DONE" else worker.status
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
        if decision.decision == "needs_changes":
            worker.status = "NEEDS_CHANGES"
        self._save(manifest)
        self._record_event(
            manifest,
            "planner_review_completed",
            "Planner review completed",
            review_attempt_id=attempt.id,
            decision=decision.decision,
        )
        return decision

    def _continue_after_needs_changes(
        self,
        manifest: RunManifest,
        worker: WorkerRecord,
        plan: PlannerPlan,
        decision: ReviewDecision,
    ) -> RunManifest:
        if worker.attempt >= self.config.max_attempts:
            manifest.status = "FAILED"
            manifest.planner.status = "FAILED"
            worker.status = "FAILED"
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
        if next_decision.decision == "needs_changes":
            return self._continue_after_needs_changes(manifest, worker, plan, next_decision)
        if next_decision.decision == "approved":
            apply_report = self._apply_reviewed_diff(
                manifest=manifest,
                worker=worker,
                evidence=evidence,
            )
            manifest.status = "APPROVED" if apply_report.applied else "FAILED"
            manifest.planner.status = manifest.status
            worker.status = manifest.status
            self._record_terminal_status(
                manifest,
                reason=None if apply_report.applied else "apply_failed",
            )
            self._save(manifest)
            return manifest
        if next_decision.decision == "blocked":
            manifest.status = "BLOCKED"
            manifest.planner.status = "BLOCKED"
            worker.status = "BLOCKED"
            self._record_terminal_status(manifest, reason="review_blocked")
        else:
            manifest.status = "FAILED"
            manifest.planner.status = "FAILED"
            worker.status = "FAILED"
            self._record_terminal_status(manifest, reason="review_failed")
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
        if manifest.status not in TERMINAL_STATUSES:
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
            self._evidence_dir(manifest),
        )
        worker.evidence_files = _append_unique(worker.evidence_files, apply_report.evidence_files)
        if manifest.review is not None:
            manifest.review.evidence_files = _append_unique(
                manifest.review.evidence_files,
                apply_report.evidence_files,
            )
        self._save(manifest)
        self._record_event(
            manifest,
            "apply_completed",
            "Apply completed",
            applied=apply_report.applied,
            summary=apply_report.summary,
        )
        return apply_report


def run_single_worker(
    *,
    manifest: RunManifest,
    store: RunStore,
    driver: CodexDriver,
    evidence_collector: EvidenceCollector = collect_diff_evidence,
    verification_runner: VerificationRunner = run_verification_commands,
    max_attempts: int = 3,
    sandbox: str = "workspace-write",
    approval_policy: str = "never",
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
        return "DONE"
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
