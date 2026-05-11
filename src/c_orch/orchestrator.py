from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Protocol, Union

from .contracts import PlannerPlan, ReviewDecision, WorkerResult
from .drivers import CodexDriver
from .prompts import planner_initial_prompt, planner_review_prompt, worker_prompt
from .run_store import ReviewRecord, RunManifest, RunStore, WorkerRecord
from .verification import VerificationReport, run_verification_commands
from .worktrees import (
    ApplyReport,
    DiffEvidence,
    apply_diff_evidence_to_repo,
    collect_diff_evidence,
)


Pathish = Union[str, Path]


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
            if manifest.status not in {"APPROVED", "BLOCKED", "FAILED"}:
                manifest.status = "FAILED"
            self._save(manifest)
            raise

    def _run(self, manifest: RunManifest) -> RunManifest:
        worker = _single_worker(manifest)
        worktree_path = _required_worktree_path(worker)

        plan = self._start_planner(manifest, worker)
        self._save(manifest)

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
                    self._save(manifest)
                    return manifest
                manifest.status = "FAILED"
                manifest.planner.status = "FAILED"
                worker.status = "FAILED"
                self._save(manifest)
                return manifest

            if decision.decision == "needs_changes":
                if attempt >= self.config.max_attempts:
                    manifest.status = "FAILED"
                    manifest.planner.status = "FAILED"
                    worker.status = "FAILED"
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
                self._save(manifest)
                return manifest

            manifest.status = "FAILED"
            manifest.planner.status = "FAILED"
            worker.status = "FAILED"
            self._save(manifest)
            return manifest

        if worker_result is None:
            raise OrchestratorError("worker did not run")
        manifest.status = "FAILED"
        self._save(manifest)
        return manifest

    def _start_planner(self, manifest: RunManifest, worker: WorkerRecord) -> PlannerPlan:
        manifest.status = "PLANNING"
        manifest.planner.status = "ACTIVE"
        self._save(manifest)

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
        )
        manifest.planner.thread_id = result.thread_id

        plan = PlannerPlan.parse(result.content)
        manifest.acceptance_criteria = list(plan.acceptance_criteria)
        manifest.verification_commands = list(plan.verification_commands)
        manifest.status = "PLAN_READY"
        manifest.planner.status = "PLAN_READY"
        return plan

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

        if is_initial_attempt:
            result = self.driver.start_session(
                role="worker",
                model=worker.model,
                cwd=worktree_path,
                prompt=prompt,
                sandbox=self.config.sandbox,
                approval_policy=self.config.approval_policy,
            )
            worker.thread_id = result.thread_id
        else:
            if not worker.thread_id:
                raise OrchestratorError("cannot continue worker without thread_id")
            result = self.driver.reply(thread_id=worker.thread_id, prompt=prompt)

        worker_result = WorkerResult.parse(result.content)
        manifest.status = "WORK_DONE"
        worker.status = _worker_status_from_result(worker_result)
        self._save(manifest)
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
    ) -> ReviewDecision:
        if not manifest.planner.thread_id:
            raise OrchestratorError("cannot review without planner thread_id")

        manifest.status = "REVIEWING"
        manifest.planner.status = "REVIEWING"
        self._save(manifest)

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
        evidence_files = _append_unique(evidence.evidence_files, verification.evidence_files)
        decision = ReviewDecision.parse(result.content)
        manifest.review = ReviewRecord(
            decision=decision.decision,
            reason=decision.reason,
            next_worker_prompt=decision.next_worker_prompt,
            evidence_files=evidence_files,
        )
        if decision.decision == "needs_changes":
            worker.status = "NEEDS_CHANGES"
        self._save(manifest)
        return decision

    def _save(self, manifest: RunManifest) -> None:
        self.store.save(manifest)

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


def _worker_status_from_result(result: WorkerResult) -> str:
    if result.status == "work_done":
        return "DONE"
    return result.status.upper()


def _append_unique(existing: List[str], new_values: List[str]) -> List[str]:
    values = list(existing)
    seen = set(values)
    for value in new_values:
        if value not in seen:
            values.append(value)
            seen.add(value)
    return values
