from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from c_orch.drivers import SessionResult
from c_orch.orchestrator import OrchestratorConfig, RunOrchestrator
from c_orch.run_store import RunStore
from c_orch.verification import CommandVerification, VerificationReport
from c_orch.worktrees import ApplyReport, DiffEvidence


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

    def reply(self, *, thread_id: str, prompt: str) -> SessionResult:
        self.reply_calls.append({"thread_id": thread_id, "prompt": prompt})
        return self.reply_results.pop(0)


class FakeEvidenceCollector:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Path]] = []

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


class FakeDiffApplier:
    def __init__(self, *, applied: bool = True) -> None:
        self.applied = applied
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
        return ApplyReport(
            applied=self.applied,
            summary="applied" if self.applied else "failed",
            output_path=output_path,
        )


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
                reply_results=[_session("planner-thread", _review("approved"))],
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
            self.assertEqual(driver.start_calls[0]["cwd"], manifest.cwd)
            self.assertEqual(driver.start_calls[0]["reasoning_effort"], "high")
            self.assertEqual(driver.start_calls[0]["service_tier"], "fast")
            self.assertEqual(driver.start_calls[1]["role"], "worker")
            self.assertEqual(driver.start_calls[1]["cwd"], str(worktree))
            self.assertEqual(driver.start_calls[1]["reasoning_effort"], "medium")
            self.assertEqual(driver.start_calls[1]["service_tier"], "flex")
            self.assertIn("Build the feature", driver.start_calls[1]["prompt"])
            self.assertIn("python -m unittest", driver.start_calls[1]["prompt"])
            self.assertEqual(driver.reply_calls[0]["thread_id"], "planner-thread")
            self.assertIn("summary for worker-1", driver.reply_calls[0]["prompt"])
            self.assertIn("All fake verification passed.", driver.reply_calls[0]["prompt"])
            self.assertIn("verification-output.txt", driver.reply_calls[0]["prompt"])
            self.assertEqual(evidence.calls[0]["worktree_path"], worktree)
            self.assertEqual(
                evidence.calls[0]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence",
            )
            self.assertEqual(verification.calls[0]["commands"], ["python -m unittest"])
            self.assertEqual(verification.calls[0]["cwd"], worktree)
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "approved")
            self.assertEqual(len(result.review.evidence_files), 4)
            self.assertIn("git-apply-output.txt", result.review.evidence_files[-1])
            self.assertEqual(len(result.workers[0].evidence_files), 4)
            self.assertEqual(len(applier.calls), 1)
            self.assertEqual(applier.calls[0]["target_repo_path"], Path(manifest.cwd))
            self.assertEqual(
                applier.calls[0]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence",
            )

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual(loaded.review.decision, "approved")
            self.assertEqual(len(loaded.review.evidence_files), 4)

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
                    "run_terminal_status",
                ],
            )
            self.assertEqual(events[2]["attempt"], 1)
            self.assertEqual(events[2]["worker_id"], "worker-1")
            self.assertEqual(events[3]["status"], "DONE")
            self.assertEqual(events[7]["decision"], "approved")
            self.assertTrue(events[8]["applied"])
            self.assertEqual(events[9]["status"], "APPROVED")

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
                reply_results=[_session("planner-thread", _review("approved"))],
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

    def test_needs_changes_reuses_worker_thread_then_reviews_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result(summary="first pass")),
                ],
                reply_results=[
                    _session("planner-thread", _review("needs_changes", "Add coverage")),
                    _session("worker-thread", _worker_result(summary="second pass")),
                    _session("planner-thread", _review("approved")),
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
            self.assertIn("Add coverage", driver.reply_calls[1]["prompt"])
            self.assertEqual(len(evidence.calls), 2)
            self.assertEqual(
                evidence.calls[1]["evidence_dir"],
                store.run_dir(manifest.run_id) / "evidence",
            )
            self.assertIsNotNone(result.review)
            self.assertEqual(result.review.decision, "approved")
            self.assertEqual(len(verification.calls), 2)
            self.assertEqual(len(result.workers[0].evidence_files), 4)
            self.assertEqual(len(applier.calls), 1)
            self.assertIn("git-apply-output.txt", result.review.evidence_files[-1])

    def test_needs_changes_after_max_attempts_fails_without_extra_worker_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("needs_changes", "Still missing"))],
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
            self.assertEqual(result.review.decision, "needs_changes")
            self.assertEqual(len(applier.calls), 0)

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
                reply_results=[_session("planner-thread", _review("approved"))],
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
            self.assertEqual(result.review.decision, "approved")
            self.assertEqual(len(applier.calls), 1)
            self.assertIn("git-apply-output.txt", result.workers[0].evidence_files[-1])
            self.assertIn("git-apply-output.txt", result.review.evidence_files[-1])

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.status, "FAILED")
            self.assertEqual(loaded.planner.status, "FAILED")
            self.assertEqual(loaded.workers[0].status, "FAILED")
            self.assertEqual(loaded.review.decision, "approved")

            events = store.load_events(manifest.run_id)
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "FAILED")
            self.assertEqual(events[-1]["reason"], "apply_failed")

    def test_blocked_review_marks_run_blocked_and_logs_terminal_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, manifest, _worktree = _create_manifest(root)
            driver = FakeDriver(
                start_results=[
                    _session("planner-thread", _planner_plan()),
                    _session("worker-thread", _worker_result()),
                ],
                reply_results=[_session("planner-thread", _review("blocked"))],
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

            self.assertEqual(result.status, "BLOCKED")
            self.assertEqual(result.planner.status, "BLOCKED")
            self.assertEqual(result.workers[0].status, "BLOCKED")
            self.assertEqual(len(applier.calls), 0)

            events = store.load_events(manifest.run_id)
            self.assertEqual(events[-1]["type"], "run_terminal_status")
            self.assertEqual(events[-1]["status"], "BLOCKED")
            self.assertEqual(events[-1]["reason"], "review_blocked")

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

            self.assertEqual(result.status, "REVIEW_RETRYABLE")
            self.assertEqual(result.planner.status, "REVIEW_RETRYABLE")
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
            self.assertEqual(paused.status, "REVIEW_RETRYABLE")

            retry_driver = FakeDriver(
                start_results=[],
                reply_results=[_session("planner-thread", _review("approved"))],
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
                ["FAILED_RETRYABLE", "APPROVED"],
            )


def _create_manifest(root: Path):
    store = RunStore(root / "runs")
    repo = root / "repo"
    repo.mkdir()
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
