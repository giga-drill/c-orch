from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from c_orch.drivers import SessionResult
from c_orch.orchestrator import OrchestratorConfig, RunOrchestrator
from c_orch.run_store import PlanRecord, ReviewRecord, RunStore
from c_orch.verification import CommandVerification, VerificationReport
from c_orch.worktrees import ApplyReport, DiffEvidence, GitCommitReport, create_worker_worktree


class FakeDriver:
    def __init__(
        self,
        *,
        start_results: List[SessionResult],
        reply_results: List[SessionResult],
    ) -> None:
        self.start_results = list(start_results)
        self.reply_results = list(reply_results)
        self.start_calls: List[Dict[str, Any]] = []
        self.reply_calls: List[Dict[str, Any]] = []

    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.start_calls.append(
            {
                "role": role,
                "model": model,
                "cwd": cwd,
                "prompt": prompt,
                "sandbox": sandbox,
                "approval_policy": approval_policy,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        return self.start_results.pop(0)

    def reply(
        self,
        *,
        thread_id: str,
        prompt: str,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.reply_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        return self.reply_results.pop(0)


class FakeEvidenceCollector:
    def __init__(self, *, changed_paths: Optional[List[str]] = None) -> None:
        self.calls: List[Dict[str, Path]] = []
        self.changed_paths = list(changed_paths or ["src/example.py"])

    def __call__(self, worktree_path: str, evidence_dir: Path) -> DiffEvidence:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        summary_path = evidence_dir / "git-diff-summary.md"
        patch_path = evidence_dir / "git-diff.patch"
        summary = f"summary for {Path(worktree_path).name}"
        patch = "diff --git a/file b/file\n"
        summary_path.write_text(summary, encoding="utf-8")
        patch_path.write_text(patch, encoding="utf-8")
        self.calls.append(
            {
                "worktree_path": Path(worktree_path),
                "evidence_dir": evidence_dir,
            }
        )
        return DiffEvidence(
            summary_path=summary_path,
            patch_path=patch_path,
            summary=summary,
            patch=patch,
            changed_paths=list(self.changed_paths),
        )


class FakeVerificationRunner:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        commands: List[str],
        *,
        cwd: str,
        evidence_dir: Path,
    ) -> VerificationReport:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        output_path = evidence_dir / "verification-output.txt"
        output_path.write_text("fake verification output\n", encoding="utf-8")
        self.calls.append(
            {
                "commands": list(commands),
                "cwd": Path(cwd),
                "evidence_dir": evidence_dir,
            }
        )
        return VerificationReport(
            summary="All fake verification passed.",
            output_path=output_path,
            results=[
                CommandVerification(
                    command=commands[0] if commands else "",
                    status="passed",
                    returncode=0,
                    summary="fake ok",
                )
            ],
        )


class FakeFailingVerificationRunner:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        commands: List[str],
        *,
        cwd: str,
        evidence_dir: Path,
    ) -> VerificationReport:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        output_path = evidence_dir / "verification-output.txt"
        output_path.write_text("fake verification failed output\n", encoding="utf-8")
        self.calls.append(
            {
                "commands": list(commands),
                "cwd": Path(cwd),
                "evidence_dir": evidence_dir,
            }
        )
        return VerificationReport(
            summary="1 of 1 verification command(s) failed.",
            output_path=output_path,
            results=[
                CommandVerification(
                    command=commands[0] if commands else "python -m unittest",
                    status="failed",
                    returncode=1,
                    summary="fake failed",
                )
            ],
        )


class FakeDiffApplier:
    def __init__(self, *, applied: bool = True, materialize_changes: bool = True) -> None:
        self.applied = applied
        self.materialize_changes = materialize_changes
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        diff: DiffEvidence,
        target_repo_path: str,
        evidence_dir: Path,
    ) -> ApplyReport:
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        output_path = evidence_dir / "git-apply-output.txt"
        output_path.write_text("fake apply output\n", encoding="utf-8")
        self.calls.append(
            {
                "diff": diff,
                "target_repo_path": Path(target_repo_path),
                "evidence_dir": evidence_dir,
            }
        )
        if self.applied and self.materialize_changes:
            target_repo = Path(target_repo_path)
            for relative_path in diff.changed_paths:
                destination = target_repo / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination.write_text(
                        destination.read_text(encoding="utf-8") + "\n# applied by fake applier\n",
                        encoding="utf-8",
                    )
                else:
                    destination.write_text("# applied by fake applier\n", encoding="utf-8")
        return ApplyReport(
            applied=self.applied,
            summary="applied" if self.applied else "failed",
            output_path=output_path,
        )


class FakeGitCommitter:
    def __init__(
        self,
        *,
        status: str = "committed",
        failure_reason: Optional[str] = None,
        commit_hash: str = "abc123",
    ) -> None:
        self.status = status
        self.failure_reason = failure_reason
        self.commit_hash = commit_hash
        self.calls: List[Dict[str, Any]] = []

    def __call__(
        self,
        *,
        target_repo_path: str,
        evidence_dir: Path,
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
        evidence_dir = Path(evidence_dir)
        evidence_dir.mkdir(parents=True, exist_ok=True)
        message_path = evidence_dir / "git-commit-message.txt"
        output_path = evidence_dir / "git-commit-output.txt"
        hash_path = evidence_dir / "git-commit-hash.txt"
        message_path.write_text(
            "\n".join(
                [
                    "Test commit",
                    "",
                    f"Run-ID: {run_id}",
                    f"Worker-ID: {worker_id}",
                    "Planner-Review: accepted",
                    f"Task: {(user_task.splitlines() or [''])[0]}",
                    f"Plan-Summary: {plan_summary or 'n/a'}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        output_path.write_text("fake git commit output\n", encoding="utf-8")
        self.calls.append(
            {
                "target_repo_path": target_repo_path,
                "evidence_dir": evidence_dir,
                "run_id": run_id,
                "worker_id": worker_id,
                "planner_review_decision": planner_review_decision,
                "user_task": user_task,
                "plan_summary": plan_summary,
                "review_reason": review_reason,
                "changed_paths": list(changed_paths),
                "pre_apply_changed_paths": list(pre_apply_changed_paths),
                "pre_apply_staged_paths": list(pre_apply_staged_paths or []),
                "post_apply_changed_paths": list(post_apply_changed_paths or []),
            }
        )
        if self.status == "committed":
            hash_path.write_text(f"{self.commit_hash}\n", encoding="utf-8")
            return GitCommitReport(
                status="committed",
                summary="Committed applied changes.",
                output_path=output_path,
                message_path=message_path,
                commit_hash=self.commit_hash,
                hash_path=hash_path,
            )
        if self.status == "skipped":
            return GitCommitReport(
                status="skipped",
                summary="Skipped: no staged changes after git add.",
                output_path=output_path,
                message_path=message_path,
            )
        return GitCommitReport(
            status="failed",
            summary="Failed: git commit returned a non-zero exit code.",
            output_path=output_path,
            message_path=message_path,
            failure_reason=self.failure_reason or "git_commit_failed",
        )


class RetryEndToEndDriver:
    def __init__(self) -> None:
        self.start_calls: List[Dict[str, Any]] = []
        self.reply_calls: List[Dict[str, Any]] = []
        self.worker_mutations: List[str] = []
        self.review_count = 0
        self.worker_cwd: Optional[Path] = None

    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.start_calls.append(
            {
                "role": role,
                "model": model,
                "cwd": cwd,
                "prompt": prompt,
                "sandbox": sandbox,
                "approval_policy": approval_policy,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if role == "planner":
            return _session("planner-thread", _retry_e2e_planner_plan())
        if role == "worker":
            self.worker_cwd = Path(cwd)
            self._write_subtract_bug(self.worker_cwd)
            return _session("worker-thread", _worker_result(summary="added subtract with a bug"))
        raise AssertionError(f"unexpected role: {role}")

    def reply(
        self,
        *,
        thread_id: str,
        prompt: str,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.reply_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if thread_id == "planner-thread":
            self.review_count += 1
            if self.review_count == 1:
                return _session(
                    "planner-thread",
                    _review("revision_requested", "Fix subtract implementation so tests pass."),
                )
            return _session("planner-thread", _review("accepted"))
        if thread_id == "worker-thread":
            if self.worker_cwd is None:
                raise AssertionError("worker reply happened before worker start")
            self._write_subtract_fix(self.worker_cwd)
            return _session("worker-thread", _worker_result(summary="fixed subtract"))
        raise AssertionError(f"unexpected thread_id: {thread_id}")

    def _write_subtract_bug(self, worktree: Path) -> None:
        _write_calculator(worktree / "calculator.py", subtract_expression="a - b - 1")
        _write_calculator_tests(worktree / "tests" / "test_calculator.py", include_subtract=True)
        self.worker_mutations.append("buggy-subtract")

    def _write_subtract_fix(self, worktree: Path) -> None:
        _write_calculator(worktree / "calculator.py", subtract_expression="a - b")
        _write_calculator_tests(worktree / "tests" / "test_calculator.py", include_subtract=True)
        self.worker_mutations.append("fixed-subtract")


class FallbackReviewDriver:
    def __init__(self, *, fallback_reply: Optional[SessionResult], fallback_error: Optional[Exception] = None) -> None:
        self.fallback_reply = fallback_reply
        self.fallback_error = fallback_error
        self.start_calls: List[Dict[str, Any]] = []
        self.reply_calls: List[Dict[str, Any]] = []

    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.start_calls.append(
            {
                "role": role,
                "model": model,
                "cwd": cwd,
                "prompt": prompt,
                "sandbox": sandbox,
                "approval_policy": approval_policy,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if role == "planner" and "fallback Planner reviewer" in prompt:
            if self.fallback_error is not None:
                raise self.fallback_error
            if self.fallback_reply is None:
                raise AssertionError("missing fallback reply")
            return self.fallback_reply
        if role == "planner":
            return _session("planner-thread", _planner_plan())
        if role == "worker":
            return _session("worker-thread", _worker_result())
        raise AssertionError(f"unexpected role: {role}")

    def reply(
        self,
        *,
        thread_id: str,
        prompt: str,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.reply_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if thread_id == "planner-thread":
            raise RuntimeError(f"Session not found for thread_id: {thread_id}")
        raise AssertionError(f"unexpected thread_id: {thread_id}")


class FallbackWorkerReworkDriver:
    def __init__(self) -> None:
        self.start_calls: List[Dict[str, Any]] = []
        self.reply_calls: List[Dict[str, Any]] = []
        self.worker_start_count = 0
        self.review_count = 0

    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.start_calls.append(
            {
                "role": role,
                "model": model,
                "cwd": cwd,
                "prompt": prompt,
                "sandbox": sandbox,
                "approval_policy": approval_policy,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if role == "planner":
            return _session("planner-thread", _planner_plan())
        if role == "worker":
            self.worker_start_count += 1
            thread_id = "worker-thread" if self.worker_start_count == 1 else "worker-fallback-thread"
            return _session(thread_id, _worker_result(summary=f"worker attempt {self.worker_start_count}"))
        raise AssertionError(f"unexpected role: {role}")

    def reply(
        self,
        *,
        thread_id: str,
        prompt: str,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        self.reply_calls.append(
            {
                "thread_id": thread_id,
                "prompt": prompt,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "service_tier": service_tier,
            }
        )
        if thread_id == "planner-thread":
            self.review_count += 1
            if self.review_count == 1:
                return _session("planner-thread", _review("revision_requested", "Add missing state mapping."))
            return _session("planner-thread", _review("accepted"))
        if thread_id == "worker-thread":
            raise RuntimeError(f"Session not found for thread_id: {thread_id}")
        raise AssertionError(f"unexpected thread_id: {thread_id}")


class OrchestratorTests(unittest.TestCase):
    def test_approved_flow_updates_manifest_and_uses_worker_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, worktree = _create_manifest(root)
            manifest.planner.reasoning_effort = "high"
            manifest.planner.service_tier = "fast"
            manifest.workers[0].reasoning_effort = "medium"
            manifest.workers[0].service_tier = "flex"
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.planner.thread_id, "planner-thread")
            self.assertEqual(result.planner.status, "APPROVED")
            self.assertEqual(result.acceptance_criteria, ["Tests pass", "Scope is tight"])
            self.assertEqual(result.verification_commands, ["python -m unittest"])
            self.assertEqual(result.workers[0].thread_id, "worker-thread")
            self.assertEqual(result.workers[0].status, "APPROVED")
            self.assertEqual(driver.start_calls[0]["role"], "planner")
            self.assertEqual(driver.start_calls[0]["cwd"], str(worktree))
            self.assertEqual(driver.start_calls[0]["reasoning_effort"], "high")
            self.assertEqual(driver.start_calls[0]["service_tier"], "fast")
            self.assertEqual(driver.start_calls[1]["role"], "worker")
            self.assertEqual(driver.start_calls[1]["cwd"], str(worktree))
            self.assertEqual(driver.start_calls[1]["reasoning_effort"], "medium")
            self.assertEqual(driver.start_calls[1]["service_tier"], "flex")
            self.assertIn("Build the feature", driver.start_calls[1]["prompt"])
            self.assertIn("python -m unittest", driver.start_calls[1]["prompt"])
            self.assertEqual(driver.reply_calls[0]["thread_id"], "planner-thread")
            self.assertEqual(driver.reply_calls[0]["model"], "planner-model")
            self.assertEqual(driver.reply_calls[0]["reasoning_effort"], "high")
            self.assertEqual(driver.reply_calls[0]["service_tier"], "fast")
            self.assertIn("summary for worker-1", driver.reply_calls[0]["prompt"])
            self.assertIn("All fake verification passed.", driver.reply_calls[0]["prompt"])
            self.assertIn("verification-output.txt", driver.reply_calls[0]["prompt"])
            self.assertEqual(evidence.calls[0]["worktree_path"], worktree)
            self.assertEqual(
                evidence.calls[0]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence" / "attempt-1",
            )
            self.assertEqual(verification.calls[0]["commands"], ["python -m unittest"])
            self.assertEqual(verification.calls[0]["cwd"], worktree)
            self.assertEqual(
                verification.calls[0]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence" / "attempt-1",
            )
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "accepted")
            self.assertEqual(len(result.review.evidence_files), 7)
            self.assertEqual(result.review_attempts[0].workspace_path, str(worktree))
            self.assertEqual(result.review_attempts[0].worker_attempt, 1)
            self.assertIn("git-commit-hash.txt", result.review.evidence_files[-1])
            self.assertFalse(result.requires_restart)
            self.assertIsNone(result.restart_reason)
            self.assertEqual(result.restart_paths, [])
            self.assertEqual(len(result.workers[0].evidence_files), 7)
            self.assertEqual(len(applier.calls), 1)
            self.assertEqual(applier.calls[0]["target_repo_path"], Path(manifest.cwd))
            self.assertEqual(
                applier.calls[0]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence" / "attempt-1",
            )
            commit_message_path = Path(
                next(
                    path
                    for path in result.review.evidence_files
                    if path.endswith("git-commit-message.txt")
                )
            )
            commit_message = commit_message_path.read_text(encoding="utf-8")
            self.assertIn(f"Run-ID: {manifest.run_id}", commit_message)
            self.assertIn("Worker-ID: worker-1", commit_message)
            self.assertIn("Planner-Review: accepted", commit_message)
            self.assertIn("Plan-Summary: Plan it", commit_message)

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual(loaded.review.decision, "accepted")
            self.assertEqual(len(loaded.review.evidence_files), 7)
            self.assertFalse(loaded.requires_restart)
            self.assertIsNone(loaded.restart_reason)
            self.assertEqual(loaded.restart_paths, [])

            events = store.load_events(manifest.run_id)
            self.assertEqual(
                [event["type"] for event in events],
                [
                    "planner_start",
                    "planner_plan_ready",
                    "worker_start",
                    "worker_done",
                    "evidence_collected",
                    "verification_finished",
                    "planner_review_start",
                    "planner_review_completed",
                    "apply_completed",
                    "git_commit_completed",
                    "run_terminal_status",
                ],
            )
            self.assertEqual(events[2]["attempt"], 1)
            self.assertEqual(events[2]["worker_id"], "worker-1")
            self.assertEqual(events[3]["status"], "DONE")
            self.assertEqual(events[7]["decision"], "accepted")
            self.assertTrue(events[8]["applied"])
            self.assertEqual(events[9]["commit_hash"], _git(Path(manifest.cwd), ["rev-parse", "HEAD"]).strip())
            self.assertEqual(events[10]["status"], "APPROVED")
            self.assertIsNotNone(result.timing)
            phase_names = [segment["phase"] for segment in result.timing.get("segments", [])]
            self.assertIn("planning", phase_names)
            self.assertIn("worker_execution", phase_names)

    def test_plan_review_required_pauses_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[_session("planner-thread", _planner_plan())],
                reply_results=[],
            )

            result = RunOrchestrator(
                store=store,
                driver=driver,
                config=OrchestratorConfig(require_plan_approval=True),
            ).run(manifest)

            self.assertEqual(result.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(result.planner.status, "PLAN_REVIEW_REQUIRED")
            self.assertIsNotNone(result.plan)
            self.assertEqual(result.plan.approval_status, "pending")
            self.assertEqual(result.plan.summary, "Plan it")
            self.assertEqual(result.plan.worker_prompt, "Build the feature")
            self.assertIsNone(result.workers[0].thread_id)
            self.assertEqual(result.workers[0].status, "PENDING")
            self.assertEqual(len(driver.start_calls), 1)
            self.assertEqual(driver.start_calls[0]["role"], "planner")
            self.assertEqual(driver.reply_calls, [])

            events = store.load_events(manifest.run_id)
            self.assertEqual(
                [event["type"] for event in events],
                ["planner_start", "planner_plan_ready", "plan_review_required"],
            )

    def test_approved_plan_resume_reuses_saved_plan_and_starts_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, worktree = _create_manifest(root)
            planner_driver = FakeDriver(
                start_results=[_session("planner-thread", _planner_plan())],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=planner_driver,
                config=OrchestratorConfig(require_plan_approval=True),
            ).run(manifest)
            self.assertEqual(paused.status, "PLAN_REVIEW_REQUIRED")

            worker_driver = FakeDriver(
                start_results=[_session("worker-thread", _worker_result())],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            result = RunOrchestrator(
                store=store,
                driver=worker_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
                config=OrchestratorConfig(require_plan_approval=True, approve_plan=True),
            ).run(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            self.assertIsNotNone(result.plan)
            self.assertEqual(result.plan.approval_status, "approved")
            self.assertEqual(result.plan.approved_by, "human")
            self.assertEqual(len(worker_driver.start_calls), 1)
            self.assertEqual(worker_driver.start_calls[0]["role"], "worker")
            self.assertEqual(worker_driver.start_calls[0]["cwd"], str(worktree))
            self.assertEqual(worker_driver.reply_calls[0]["thread_id"], "planner-thread")

            events = store.load_events(manifest.run_id)
            self.assertIn("plan_approved", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "run_terminal_status")

    def test_revise_plan_reuses_planner_thread_and_replaces_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            planner_driver = FakeDriver(
                start_results=[_session("planner-thread", _planner_plan())],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=planner_driver,
                config=OrchestratorConfig(require_plan_approval=True),
            ).run(manifest)
            self.assertEqual(paused.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(len(planner_driver.start_calls), 1)

            revise_driver = FakeDriver(
                start_results=[],
                reply_results=[_session("planner-thread", _planner_plan_revised())],
            )
            paused.planner.reasoning_effort = "high"
            paused.planner.service_tier = "fast"
            store.save(paused)
            revised = RunOrchestrator(
                store=store,
                driver=revise_driver,
                config=OrchestratorConfig(require_plan_approval=True),
            ).revise_plan(store.load(manifest.run_id), "Please tighten scope.")

            self.assertEqual(revised.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(revised.planner.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(revised.plan.summary, "Revised plan")
            self.assertEqual(revised.plan.worker_prompt, "Build only the scheduler path")
            self.assertEqual(revised.plan.approval_status, "pending")
            self.assertEqual(revised.plan.approved_at, None)
            self.assertEqual(revised.plan.approved_by, None)
            self.assertEqual(revised.acceptance_criteria, ["Queue does not create duplicate runs"])
            self.assertEqual(revised.verification_commands, ["python -m unittest tests/test_scheduler.py"])
            self.assertEqual(len(revise_driver.start_calls), 0)
            self.assertEqual(len(revise_driver.reply_calls), 1)
            self.assertEqual(revise_driver.reply_calls[0]["thread_id"], "planner-thread")
            self.assertEqual(revise_driver.reply_calls[0]["model"], "planner-model")
            self.assertEqual(revise_driver.reply_calls[0]["reasoning_effort"], "high")
            self.assertEqual(revise_driver.reply_calls[0]["service_tier"], "fast")
            self.assertIn("Please tighten scope.", revise_driver.reply_calls[0]["prompt"])
            self.assertEqual(len(revised.plan_revisions), 1)
            self.assertEqual(revised.plan_revisions[0].id, "plan-revision-1")
            self.assertEqual(revised.plan_revisions[0].human_feedback, "Please tighten scope.")
            self.assertEqual(revised.plan_revisions[0].previous_plan["summary"], "Plan it")
            self.assertEqual(revised.plan_revisions[0].new_plan["summary"], "Revised plan")

            events = store.load_events(manifest.run_id)
            self.assertIn("planner_plan_revision_started", [event["type"] for event in events])
            self.assertIn("planner_plan_revised", [event["type"] for event in events])

    def test_revise_plan_failure_preserves_plan_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            paused = RunOrchestrator(
                store=store,
                driver=FakeDriver(
                    start_results=[_session("planner-thread", _planner_plan())],
                    reply_results=[],
                ),
                config=OrchestratorConfig(require_plan_approval=True),
            ).run(manifest)
            self.assertEqual(paused.status, "PLAN_REVIEW_REQUIRED")

            failing_driver = FakeDriver(start_results=[], reply_results=[])
            with self.assertRaises(IndexError):
                RunOrchestrator(
                    store=store,
                    driver=failing_driver,
                    config=OrchestratorConfig(require_plan_approval=True),
                ).revise_plan(store.load(manifest.run_id), "Please tighten scope.")

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(loaded.planner.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(loaded.plan.summary, "Plan it")
            self.assertEqual(loaded.plan_revisions, [])
            events = store.load_events(manifest.run_id)
            self.assertIn("planner_plan_revision_failed", [event["type"] for event in events])

    def test_revision_requested_reuses_worker_thread_then_reviews_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            manifest.planner.reasoning_effort = "high"
            manifest.planner.service_tier = "fast"
            manifest.workers[0].reasoning_effort = "medium"
            manifest.workers[0].service_tier = "flex"
            store.save(manifest)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result(summary="first pass")),
                ],
                reply_results=[
                    _session("planner-thread", _review("revision_requested", "Add coverage")),
                    _session("worker-thread", _worker_result(summary="second pass")),
                    _session("planner-thread", _review("accepted")),
                ],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                verification_runner=verification,
                config=OrchestratorConfig(max_attempts=2),
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.workers[0].thread_id, "worker-thread")
            self.assertEqual(result.workers[0].attempt, 2)
            self.assertEqual(len(driver.start_calls), 2)
            self.assertEqual(
                [call["thread_id"] for call in driver.reply_calls],
                ["planner-thread", "worker-thread", "planner-thread"],
            )
            self.assertEqual(driver.reply_calls[0]["model"], "planner-model")
            self.assertEqual(driver.reply_calls[0]["reasoning_effort"], "high")
            self.assertEqual(driver.reply_calls[0]["service_tier"], "fast")
            self.assertEqual(driver.reply_calls[1]["model"], "worker-model")
            self.assertEqual(driver.reply_calls[1]["reasoning_effort"], "medium")
            self.assertEqual(driver.reply_calls[1]["service_tier"], "flex")
            self.assertEqual(driver.reply_calls[2]["model"], "planner-model")
            self.assertEqual(driver.reply_calls[2]["reasoning_effort"], "high")
            self.assertEqual(driver.reply_calls[2]["service_tier"], "fast")
            self.assertIn("Add coverage", driver.reply_calls[1]["prompt"])
            self.assertEqual(len(evidence.calls), 2)
            self.assertEqual(
                evidence.calls[1]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence" / "attempt-2",
            )
            self.assertEqual(
                [call["evidence_dir"].name for call in evidence.calls],
                ["attempt-1", "attempt-2"],
            )
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "accepted")
            self.assertIn("attempt-1", result.review_attempts[0].evidence_files[0])
            self.assertIn("attempt-2", result.review_attempts[1].evidence_files[0])
            self.assertEqual(len(verification.calls), 2)
            self.assertEqual(len(result.workers[0].evidence_files), 10)
            self.assertEqual(len(applier.calls), 1)
            self.assertIn("git-commit-hash.txt", result.review.evidence_files[-1])

    def test_revision_requested_falls_back_to_new_worker_when_thread_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            manifest.planner.reasoning_effort = "high"
            manifest.planner.service_tier = "fast"
            manifest.workers[0].reasoning_effort = "medium"
            manifest.workers[0].service_tier = "flex"
            store.save(manifest)
            driver = FallbackWorkerReworkDriver()
            applier = FakeDiffApplier()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=applier,
                verification_runner=FakeVerificationRunner(),
                config=OrchestratorConfig(max_attempts=2),
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.workers[0].attempt, 2)
            self.assertEqual(result.workers[0].thread_id, "worker-fallback-thread")
            self.assertEqual([call["role"] for call in driver.start_calls], ["planner", "worker", "worker"])
            self.assertEqual(driver.reply_calls[1]["thread_id"], "worker-thread")
            self.assertEqual(driver.reply_calls[0]["model"], "planner-model")
            self.assertEqual(driver.reply_calls[0]["reasoning_effort"], "high")
            self.assertEqual(driver.reply_calls[0]["service_tier"], "fast")
            self.assertEqual(driver.reply_calls[1]["model"], "worker-model")
            self.assertEqual(driver.reply_calls[1]["reasoning_effort"], "medium")
            self.assertEqual(driver.reply_calls[1]["service_tier"], "flex")
            self.assertEqual(len(applier.calls), 1)
            event_types = [event["type"] for event in store.load_events(manifest.run_id)]
            self.assertIn("worker_rework_fallback_started", event_types)
            self.assertIn("worker_rework_fallback_thread_started", event_types)

    @unittest.skipIf(shutil.which("git") is None, "git is not available")
    def test_retry_flow_end_to_end_from_plan_gate_to_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = _create_retry_e2e_repo(root / "repo")
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=repo,
                user_task="Add subtract and tests, then fix review feedback.",
                planner_model="planner-model",
                worker_model="worker-model",
            )
            manifest.workers[0].worktree_path = str(
                create_worker_worktree(
                    repo,
                    root / "worktrees",
                    manifest.run_id,
                    manifest.workers[0].id,
                )
            )
            store.save(manifest)
            driver = RetryEndToEndDriver()

            paused = RunOrchestrator(
                store=store,
                driver=driver,
                config=OrchestratorConfig(require_plan_approval=True, max_attempts=2),
            ).run(manifest)
            self.assertEqual(paused.status, "PLAN_REVIEW_REQUIRED")
            self.assertEqual(paused.plan.approval_status, "pending")
            self.assertEqual([call["role"] for call in driver.start_calls], ["planner"])
            self.assertEqual(driver.start_calls[0]["cwd"], manifest.workers[0].worktree_path)

            result = RunOrchestrator(
                store=store,
                driver=driver,
                config=OrchestratorConfig(
                    require_plan_approval=True,
                    approve_plan=True,
                    max_attempts=2,
                ),
            ).run(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.plan.approval_status, "approved")
            self.assertEqual(result.workers[0].attempt, 2)
            self.assertEqual(result.workers[0].thread_id, "worker-thread")
            self.assertEqual(driver.worker_mutations, ["buggy-subtract", "fixed-subtract"])
            self.assertEqual([call["role"] for call in driver.start_calls], ["planner", "worker"])
            self.assertEqual(
                [call["thread_id"] for call in driver.reply_calls],
                ["planner-thread", "worker-thread", "planner-thread"],
            )
            self.assertIn("1 of 1 verification command(s) failed.", driver.reply_calls[0]["prompt"])
            self.assertIn("Fix subtract implementation", driver.reply_calls[1]["prompt"])
            self.assertIn("All 1 verification command(s) passed.", driver.reply_calls[2]["prompt"])
            self.assertEqual(
                [attempt.status for attempt in result.review_attempts],
                ["REVISION_REQUESTED", "ACCEPTED"],
            )
            self.assertEqual(result.review.decision, "accepted")
            self.assertIsNotNone(result.timing)
            phase_names = [segment["phase"] for segment in result.timing.get("segments", [])]
            self.assertEqual(phase_names.count("worker_execution"), 2)
            self.assertEqual(phase_names.count("planner_review"), 2)
            self.assertEqual(phase_names.count("revision_wait"), 1)

            self.assertIn("def subtract(a, b):", (repo / "calculator.py").read_text(encoding="utf-8"))
            self.assertIn(
                "self.assertEqual(subtract(5, 3), 2)",
                (repo / "tests" / "test_calculator.py").read_text(encoding="utf-8"),
            )
            self.assertEqual(_git(repo, ["status", "--short"]).strip(), "")
            self.assertEqual(_git(repo, ["rev-list", "--count", "HEAD"]).strip(), "2")
            latest_message = _git(repo, ["log", "-1", "--pretty=%B"])
            self.assertIn(f"Run-ID: {manifest.run_id}", latest_message)
            self.assertIn("Worker-ID: worker-1", latest_message)
            self.assertIn("Planner-Review: accepted", latest_message)
            verification = subprocess.run(
                _retry_e2e_verification_command(),
                cwd=repo,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(verification.returncode, 0, verification.stdout + verification.stderr)

            events = store.load_events(manifest.run_id)
            event_types = [event["type"] for event in events]
            self.assertEqual(
                event_types,
                [
                    "planner_start",
                    "planner_plan_ready",
                    "plan_review_required",
                    "plan_approved",
                    "worker_start",
                    "worker_done",
                    "evidence_collected",
                    "verification_finished",
                    "planner_review_start",
                    "planner_review_completed",
                    "worker_start",
                    "worker_done",
                    "evidence_collected",
                    "verification_finished",
                    "planner_review_start",
                    "planner_review_completed",
                    "apply_completed",
                    "git_commit_completed",
                    "run_terminal_status",
                ],
            )
            self.assertEqual(
                [event.get("attempt") for event in events if event["type"] == "worker_start"],
                [1, 2],
            )
            self.assertEqual(
                [event.get("decision") for event in events if event["type"] == "planner_review_completed"],
                ["revision_requested", "accepted"],
            )
            self.assertEqual(
                [event.get("summary") for event in events if event["type"] == "verification_finished"],
                [
                    "1 of 1 verification command(s) failed.",
                    "All 1 verification command(s) passed.",
                ],
            )

    def test_revision_requested_after_max_attempts_fails_without_extra_worker_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("revision_requested", "Still missing"))],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier()
            committer = FakeGitCommitter()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                git_committer=committer,
                verification_runner=verification,
                config=OrchestratorConfig(max_attempts=1),
            ).run(manifest)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.planner.status, "FAILED")
            self.assertEqual(result.workers[0].status, "FAILED")
            self.assertEqual(result.workers[0].attempt, 1)
            self.assertEqual(
                [call["thread_id"] for call in driver.reply_calls],
                ["planner-thread"],
            )
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "revision_requested")
            self.assertEqual(len(applier.calls), 0)
            self.assertEqual(len(committer.calls), 0)

            events = store.load_events(manifest.run_id)
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "FAILED")
            self.assertEqual(events[-1]["reason"], "max_attempts_reached")

    def test_apply_failure_after_approved_review_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier(applied=False)

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.planner.status, "FAILED")
            self.assertEqual(result.workers[0].status, "FAILED")
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "accepted")
            self.assertEqual(len(applier.calls), 1)
            self.assertIn("git-apply-output.txt", result.workers[0].evidence_files[-1])
            self.assertIn("git-apply-output.txt", result.review.evidence_files[-1])

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.status, "FAILED")
            self.assertEqual(loaded.planner.status, "FAILED")
            self.assertEqual(loaded.workers[0].status, "FAILED")
            self.assertEqual(loaded.review.decision, "accepted")

            events = store.load_events(manifest.run_id)
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "FAILED")
            self.assertEqual(events[-1]["reason"], "apply_failed")
            self.assertFalse(result.requires_restart)
            self.assertIsNone(result.restart_reason)
            self.assertEqual(result.restart_paths, [])

    def test_accepted_review_with_failed_verification_fails_without_apply_or_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeFailingVerificationRunner()
            applier = FakeDiffApplier(applied=True)
            committer = FakeGitCommitter()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                git_committer=committer,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.planner.status, "FAILED")
            self.assertEqual(result.workers[0].status, "FAILED")
            self.assertEqual(len(applier.calls), 0)
            self.assertEqual(len(committer.calls), 0)
            events = store.load_events(manifest.run_id)
            self.assertIn("verification_gate_failed", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["reason"], "verification_failed")

    def test_retry_verification_after_accepted_review_applies_without_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            manifest.status = "FAILED"
            manifest.planner.status = "FAILED"
            manifest.plan = PlanRecord(
                summary="Plan it",
                worker_prompt="Build the feature",
                approval_status="approved",
            )
            manifest.review = ReviewRecord(
                decision="accepted",
                reason="looks good",
                evidence_files=[],
            )
            manifest.workers[0].status = "FAILED"
            manifest.workers[0].result = json.loads(_worker_result())
            manifest.verification_commands = ["python -m unittest"]
            store.save(manifest)
            store.append_event(
                manifest.run_id,
                "verification_gate_failed",
                "Verification gate failed",
                summary="1 of 1 verification command(s) failed.",
            )
            driver = FakeDriver(start_results=[], reply_results=[])
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier(applied=True)
            committer = FakeGitCommitter()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                git_committer=committer,
                verification_runner=verification,
            ).retry_verification(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.planner.status, "APPROVED")
            self.assertEqual(result.workers[0].status, "APPROVED")
            self.assertEqual(len(driver.start_calls), 0)
            self.assertEqual(len(driver.reply_calls), 0)
            self.assertEqual(len(evidence.calls), 1)
            self.assertEqual(len(verification.calls), 1)
            self.assertEqual(len(applier.calls), 1)
            self.assertEqual(len(committer.calls), 1)
            events = store.load_events(manifest.run_id)
            self.assertIn("verification_retry_started", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "APPROVED")

    def test_commit_failure_after_apply_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector()
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier(applied=True)
            committer = FakeGitCommitter(status="failed", failure_reason="git_commit_failed")

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                git_committer=committer,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "FAILED")
            self.assertEqual(result.planner.status, "FAILED")
            self.assertEqual(result.workers[0].status, "FAILED")
            self.assertEqual(len(applier.calls), 1)
            self.assertEqual(len(committer.calls), 1)
            events = store.load_events(manifest.run_id)
            self.assertIn("git_commit_failed", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["reason"], "git_commit_failed")

    def test_commit_skip_after_apply_keeps_run_approved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector(changed_paths=[])
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier(applied=True, materialize_changes=False)
            committer = FakeGitCommitter(status="skipped")

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                git_committer=committer,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.planner.status, "APPROVED")
            self.assertEqual(result.workers[0].status, "APPROVED")
            self.assertEqual(len(committer.calls), 1)
            events = store.load_events(manifest.run_id)
            self.assertIn("git_commit_skipped", [event["type"] for event in events])
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "APPROVED")

    def test_self_modification_marks_restart_required_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector(
                changed_paths=[
                    "src/c_orch/orchestrator.py",
                    "docs/mcp-orchestrator-design.md",
                ]
            )
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier()

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertTrue(result.requires_restart)
            self.assertEqual(
                result.restart_reason,
                "Applied diff touched c-orch runtime code or critical config.",
            )
            self.assertEqual(result.restart_paths, ["src/c_orch/orchestrator.py"])

            events = store.load_events(manifest.run_id)
            self.assertIn("restart_required", [event["type"] for event in events])
            restart_event = next(event for event in events if event["type"] == "restart_required")
            self.assertEqual(
                restart_event["restart_paths"],
                ["src/c_orch/orchestrator.py"],
            )

    def test_apply_failure_does_not_mark_restart_even_for_self_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            evidence = FakeEvidenceCollector(changed_paths=["src/c_orch/run_store.py"])
            verification = FakeVerificationRunner()
            applier = FakeDiffApplier(applied=False)

            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=evidence,
                diff_applier=applier,
                verification_runner=verification,
            ).run(manifest)

            self.assertEqual(result.status, "FAILED")
            self.assertFalse(result.requires_restart)
            self.assertIsNone(result.restart_reason)
            self.assertEqual(result.restart_paths, [])

            events = store.load_events(manifest.run_id)
            self.assertNotIn("restart_required", [event["type"] for event in events])

    def test_review_failure_preserves_worker_result_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)

            self.assertEqual(result.status, "WORK_DONE")
            self.assertEqual(result.planner.status, "PLAN_APPROVED")
            self.assertEqual(result.workers[0].status, "DONE")
            self.assertIsNotNone(result.workers[0].result)
            self.assertEqual(result.review_attempts[-1].status, "FAILED_RETRYABLE")
            self.assertIn("pop from empty list", result.review_attempts[-1].error)
            self.assertIsNotNone(result.review)
            self.assertIn("git-diff.patch", result.review.evidence_files[1])

            events = store.load_events(manifest.run_id)
            self.assertIn("planner_review_failed", [event["type"] for event in events])

    def test_retry_review_reuses_saved_worker_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            first_driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=first_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)
            self.assertEqual(paused.status, "WORK_DONE")

            retry_driver = FakeDriver(
                start_results=[],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            applier = FakeDiffApplier()
            result = RunOrchestrator(
                store=store,
                driver=retry_driver,
                diff_applier=applier,
            ).retry_review(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(retry_driver.start_calls, [])
            self.assertEqual(retry_driver.reply_calls[0]["thread_id"], "planner-thread")
            self.assertEqual(len(applier.calls), 1)
            self.assertEqual(
                [attempt.status for attempt in result.review_attempts],
                ["FAILED_RETRYABLE", "ACCEPTED"],
            )

    def test_retry_review_revision_requested_records_revision_wait_phase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            first_driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=first_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)
            self.assertEqual(paused.status, "WORK_DONE")
            self.assertEqual(paused.review_attempts[-1].status, "FAILED_RETRYABLE")

            retry_driver = FakeDriver(
                start_results=[],
                reply_results=[
                    _session("planner-thread", _review("revision_requested", "Please tighten fix.")),
                    _session("worker-thread", _worker_result(summary="retry rework done")),
                    _session("planner-thread", _review("accepted")),
                ],
            )
            result = RunOrchestrator(
                store=store,
                driver=retry_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).retry_review(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(
                [attempt.status for attempt in result.review_attempts],
                ["FAILED_RETRYABLE", "REVISION_REQUESTED", "ACCEPTED"],
            )
            self.assertEqual(
                [call["thread_id"] for call in retry_driver.reply_calls],
                ["planner-thread", "worker-thread", "planner-thread"],
            )
            self.assertIsNotNone(result.timing)
            phase_names = [segment["phase"] for segment in result.timing.get("segments", [])]
            self.assertGreaterEqual(phase_names.count("planner_review"), 2)
            self.assertEqual(phase_names.count("revision_wait"), 1)
            self.assertGreaterEqual(phase_names.count("worker_execution"), 2)

    def test_retry_review_records_recovery_after_mcp_timeout_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            first_driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=first_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)
            self.assertEqual(paused.status, "WORK_DONE")

            retry_driver = FakeDriver(
                start_results=[],
                reply_results=[
                    SessionResult(
                        thread_id="planner-thread",
                        content=_review("accepted"),
                        raw={"resumedAfterMcpTimeout": True},
                    )
                ],
            )
            result = RunOrchestrator(
                store=store,
                driver=retry_driver,
                diff_applier=FakeDiffApplier(),
            ).retry_review(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            events = store.load_events(manifest.run_id)
            recovery_events = [
                event for event in events if event["type"] == "planner_review_recovered"
            ]
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(
                recovery_events[0]["method"],
                "codex_exec_resume_after_mcp_timeout",
            )
            self.assertEqual(recovery_events[0]["old_thread_id"], "planner-thread")

    def test_review_reply_success_does_not_start_fallback_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("accepted"))],
            )
            result = RunOrchestrator(
                store=store,
                driver=driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual([call["role"] for call in driver.start_calls], ["planner", "worker"])

    def test_retry_review_falls_back_to_new_planner_session_when_thread_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            first_driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=first_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)
            self.assertEqual(paused.status, "WORK_DONE")

            retry_driver = FallbackReviewDriver(
                fallback_reply=_session("planner-fallback-thread", _review("accepted")),
            )
            applier = FakeDiffApplier()
            result = RunOrchestrator(
                store=store,
                driver=retry_driver,
                diff_applier=applier,
            ).retry_review(store.load(manifest.run_id))

            self.assertEqual(result.status, "APPROVED")
            self.assertEqual(result.planner.thread_id, "planner-fallback-thread")
            self.assertEqual(retry_driver.reply_calls[0]["thread_id"], "planner-thread")
            self.assertEqual(retry_driver.start_calls[-1]["role"], "planner")
            self.assertIn("fallback Planner reviewer", retry_driver.start_calls[-1]["prompt"])
            self.assertEqual(len(applier.calls), 1)
            events = store.load_events(manifest.run_id)
            event_types = [event["type"] for event in events]
            self.assertIn("planner_review_fallback_started", event_types)
            self.assertIn("planner_review_fallback_thread_started", event_types)

    def test_retry_review_fallback_failure_preserves_work_done_and_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            first_driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[],
            )
            paused = RunOrchestrator(
                store=store,
                driver=first_driver,
                evidence_collector=FakeEvidenceCollector(),
                diff_applier=FakeDiffApplier(),
                verification_runner=FakeVerificationRunner(),
            ).run(manifest)
            self.assertEqual(paused.status, "WORK_DONE")

            retry_driver = FallbackReviewDriver(
                fallback_reply=None,
                fallback_error=RuntimeError("fallback planner startup failed"),
            )
            result = RunOrchestrator(
                store=store,
                driver=retry_driver,
            ).retry_review(store.load(manifest.run_id))

            self.assertEqual(result.status, "WORK_DONE")
            self.assertEqual(result.planner.status, "PLAN_APPROVED")
            self.assertEqual(result.review_attempts[-1].status, "FAILED_RETRYABLE")
            self.assertIn("fallback planner startup failed", result.review_attempts[-1].error or "")
            self.assertIsNotNone(result.review)
            self.assertGreaterEqual(len(result.review.evidence_files), 1)


def _create_manifest(root: Path):
    store = RunStore(root / "runs")
    repo = root / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "c-orch-test@example.com"])
    _git(repo, ["config", "user.name", "c-orch test"])
    _git(repo, ["add", "."])
    _git(repo, ["commit", "-m", "initial"])
    worktree = root / "worktrees" / "run" / "worker-1"
    worktree.mkdir(parents=True)
    manifest = store.create_run(
        cwd=repo,
        user_task="Implement feature X",
        planner_model="planner-model",
        worker_model="worker-model",
    )
    manifest.workers[0].worktree_path = str(worktree)
    store.save(manifest)
    return store, manifest, worktree


def _create_retry_e2e_repo(repo: Path) -> Path:
    repo.mkdir()
    (repo / "tests").mkdir()
    _write_calculator(repo / "calculator.py", subtract_expression=None)
    _write_calculator_tests(repo / "tests" / "test_calculator.py", include_subtract=False)
    _git(repo, ["init"])
    _git(repo, ["config", "user.email", "c-orch-test@example.com"])
    _git(repo, ["config", "user.name", "c-orch test"])
    _git(repo, ["add", "."])
    _git(repo, ["commit", "-m", "initial"])
    return repo


def _write_calculator(path: Path, *, subtract_expression: Optional[str]) -> None:
    lines = [
        "def add(a, b):",
        "    return a + b",
        "",
    ]
    if subtract_expression is not None:
        lines.extend(
            [
                "",
                "def subtract(a, b):",
                f"    return {subtract_expression}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_calculator_tests(path: Path, *, include_subtract: bool) -> None:
    imports = "from calculator import add"
    if include_subtract:
        imports += ", subtract"
    lines = [
        "import unittest",
        "",
        imports,
        "",
        "",
        "class CalculatorTests(unittest.TestCase):",
        "    def test_add(self):",
        "        self.assertEqual(add(2, 3), 5)",
    ]
    if include_subtract:
        lines.extend(
            [
                "",
                "    def test_subtract(self):",
                "        self.assertEqual(subtract(5, 3), 2)",
            ]
        )
    lines.extend(
        [
            "",
            "",
            "if __name__ == '__main__':",
            "    unittest.main()",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _git(cwd: Path, args: List[str]) -> str:
    completed = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return completed.stdout


def _session(thread_id: str, payload: str) -> SessionResult:
    return SessionResult(thread_id=thread_id, content=payload, raw={})


def _planner_plan() -> str:
    return json.dumps(
        {
            "status": "plan_ready",
            "summary": "Plan it",
            "acceptance_criteria": ["Tests pass", "Scope is tight"],
            "worker_prompt": "Build the feature",
            "verification_commands": ["python -m unittest"],
            "risk_notes": [],
        }
    )


def _planner_plan_revised() -> str:
    return json.dumps(
        {
            "status": "plan_ready",
            "summary": "Revised plan",
            "acceptance_criteria": ["Queue does not create duplicate runs"],
            "worker_prompt": "Build only the scheduler path",
            "verification_commands": ["python -m unittest tests/test_scheduler.py"],
            "risk_notes": ["Avoid touching unrelated queue states"],
        }
    )


def _retry_e2e_planner_plan() -> str:
    return json.dumps(
        {
            "status": "plan_ready",
            "summary": "Add subtract with tests",
            "acceptance_criteria": [
                "subtract(a, b) returns a - b",
                "unit tests cover add and subtract",
            ],
            "worker_prompt": "Add subtract and tests.",
            "verification_commands": [_retry_e2e_verification_command()],
            "risk_notes": [],
        }
    )


def _retry_e2e_verification_command() -> str:
    return f"{shlex.quote(sys.executable)} -m unittest discover -s tests"


def _worker_result(summary: str = "done") -> str:
    return json.dumps(
        {
            "status": "work_done",
            "summary": summary,
            "changed_files": ["src/example.py"],
            "verification": [
                {
                    "command": "python -m unittest",
                    "status": "passed",
                    "summary": "ok",
                }
            ],
            "blockers": [],
        }
    )


def _review(decision: str, next_worker_prompt: Optional[str] = None) -> str:
    return json.dumps(
        {
            "decision": decision,
            "reason": "review reason",
            "next_worker_prompt": next_worker_prompt,
        }
    )


if __name__ == "__main__":
    unittest.main()
