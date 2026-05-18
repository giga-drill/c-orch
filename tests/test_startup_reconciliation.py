from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from c_orch.run_store import PlanRecord, ReviewAttemptRecord, ReviewRecord, RunStore
from c_orch.runner_leases import (
    LEASE_STATUS_ACTIVE,
    LEASE_STATUS_EXPIRED,
    LEASE_STATUS_FAILED,
)
from c_orch.runner_subprocess import (
    RUNNER_PHASE_CODE_REVIEW,
    RUNNER_RESULT_COMPLETED,
    RunnerSubprocessResult,
)
from c_orch.runtime import COrchRuntime
from c_orch.scheduler import SchedulerConfig
from c_orch.startup_reconciliation import reconcile_runtime_startup_leases
from c_orch.states import (
    RUN_APPROVED,
    REVIEW_ATTEMPT_FAILED_RETRYABLE,
    REVIEW_ATTEMPT_ACTIVE,
    RUN_FAILED,
    RUN_WORKING,
    RUN_REVIEWING,
    RUN_WORK_DONE,
    WORKER_DONE,
)
from c_orch.task_store import TaskStore


class StartupReconciliationTests(unittest.TestCase):
    def test_alive_lease_records_audit_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Alive lease task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_WORKING
            store.save(manifest)
            now = datetime.now(timezone.utc)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": "WORKING"},
                now=now,
                lease_ttl_seconds=3600,
            )

            decisions = reconcile_runtime_startup_leases(
                run_store=store,
                runtime_generation="runtime-2",
                process_hint="runtime:test",
                now=now,
            )

            alive = [item for item in decisions if item["runner_id"] == lease.runner_id]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0]["category"], "runner_alive")
            leases = store.load_runner_leases(manifest.run_id)
            row = next(item for item in leases["leases"] if item["runner_id"] == lease.runner_id)
            self.assertEqual(row["status"], LEASE_STATUS_ACTIVE)
            events = store.load_events(manifest.run_id)
            startup_events = [
                event
                for event in events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(startup_events), 1)
            self.assertEqual(startup_events[0]["reason"], "lease_active_process_alive")

    def test_code_review_subprocess_started_uses_checkpoint_pid_for_liveness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="External code review still alive",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_REVIEWING
            manifest.planner.status = RUN_REVIEWING
            manifest.plan = PlanRecord(
                summary="Plan",
                worker_prompt="Do work",
                approval_status="approved",
                approved_by="human",
            )
            worker = manifest.workers[0]
            worker.status = WORKER_DONE
            manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id=worker.id,
                    status=REVIEW_ATTEMPT_ACTIVE,
                    started_at=store.now_iso(),
                    worker_attempt=worker.attempt,
                    workspace_path=str(root),
                    evidence_files=["evidence/review.patch"],
                )
            ]
            store.save(manifest)
            now = datetime.now(timezone.utc)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:dead-parent",
                pid=999999,  # simulated dead parent runtime pid
                checkpoint={
                    "review_attempt_id": "review-1",
                    "run_status": RUN_REVIEWING,
                    "phase_boundary": "runner_subprocess_started",
                    "runner_phase": RUNNER_PHASE_CODE_REVIEW,
                    "request_path": "runs/request.json",
                    "result_path": "runs/result.json",
                    "pid": os.getpid(),  # simulated live subprocess pid
                    "process_hint": f"pid:{os.getpid()}",
                },
                now=now,
                lease_ttl_seconds=3600,
            )

            decisions = reconcile_runtime_startup_leases(
                run_store=store,
                runtime_generation="runtime-2",
                process_hint="runtime:test",
                now=now,
            )

            alive = [item for item in decisions if item["runner_id"] == lease.runner_id]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0]["category"], "runner_alive")
            self.assertEqual(alive[0]["recovery_action"], "noop_keep_running")
            events = store.load_events(manifest.run_id)
            startup_events = [
                event
                for event in events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(startup_events), 1)
            self.assertEqual(startup_events[0]["reason"], "lease_active_process_alive")

    def test_stale_code_review_subprocess_with_live_checkpoint_pid_is_kept_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="External code review stale heartbeat but subprocess alive",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_REVIEWING
            manifest.planner.status = RUN_REVIEWING
            manifest.plan = PlanRecord(
                summary="Plan",
                worker_prompt="Do work",
                approval_status="approved",
                approved_by="human",
            )
            worker = manifest.workers[0]
            worker.status = WORKER_DONE
            manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id=worker.id,
                    status=REVIEW_ATTEMPT_ACTIVE,
                    started_at=store.now_iso(),
                    worker_attempt=worker.attempt,
                    workspace_path=str(root),
                    evidence_files=["evidence/review.patch"],
                )
            ]
            store.save(manifest)
            now = datetime.now(timezone.utc)
            stale_time = now - timedelta(hours=2)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:dead-parent",
                pid=999999,
                checkpoint={
                    "review_attempt_id": "review-1",
                    "run_status": RUN_REVIEWING,
                    "phase_boundary": "runner_subprocess_started",
                    "runner_phase": RUNNER_PHASE_CODE_REVIEW,
                    "request_path": "runs/request.json",
                    "result_path": "runs/result.json",
                    "pid": os.getpid(),
                    "process_hint": f"pid:{os.getpid()}",
                },
                now=stale_time,
                lease_ttl_seconds=60,
            )

            decisions = reconcile_runtime_startup_leases(
                run_store=store,
                runtime_generation="runtime-2",
                process_hint="runtime:test",
                now=now,
            )

            alive = [item for item in decisions if item["runner_id"] == lease.runner_id]
            self.assertEqual(len(alive), 1)
            self.assertEqual(alive[0]["category"], "runner_alive")
            self.assertEqual(alive[0]["reason"], "lease_stale_but_subprocess_alive")
            self.assertEqual(alive[0]["recovery_action"], "noop_keep_running")
            leases = store.runner_lease_store(manifest.run_id).read(now=now).to_dict()
            row = next(item for item in leases["leases"] if item["runner_id"] == lease.runner_id)
            self.assertEqual(row["status"], LEASE_STATUS_ACTIVE)
            self.assertEqual(row["effective_status"], "stale")

    def test_stale_worker_lease_terminalizes_and_marks_run_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Stale lease task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORKING"
            store.save(manifest)
            now = datetime.now(timezone.utc)
            stale_time = now - timedelta(hours=2)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": "WORKING"},
                now=stale_time,
                lease_ttl_seconds=60,
            )

            decisions = reconcile_runtime_startup_leases(run_store=store, now=now)

            stale = [item for item in decisions if item["runner_id"] == lease.runner_id]
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["effective_status"], "stale")
            leases = store.load_runner_leases(manifest.run_id)
            row = next(item for item in leases["leases"] if item["runner_id"] == lease.runner_id)
            self.assertIn(row["status"], {LEASE_STATUS_EXPIRED, LEASE_STATUS_FAILED})
            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_FAILED)
            events = store.load_events(manifest.run_id)
            self.assertTrue(any(event.get("type") == "run_terminal_status" for event in events))

    def test_completed_code_review_result_is_imported_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Completed code review task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_REVIEWING
            manifest.planner.status = RUN_REVIEWING
            manifest.plan = PlanRecord(
                summary="Plan",
                worker_prompt="Do work",
                approval_status="approved",
                approved_by="human",
            )
            worker = manifest.workers[0]
            worker.status = WORKER_DONE
            worker.result = {"summary": "worker finished"}
            manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id=worker.id,
                    status=REVIEW_ATTEMPT_ACTIVE,
                    started_at=store.now_iso(),
                    worker_attempt=worker.attempt,
                    workspace_path=str(root),
                    evidence_files=["evidence/review.patch"],
                )
            ]
            store.save(manifest)
            now = datetime.now(timezone.utc)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"review_attempt_id": "review-1", "run_status": RUN_REVIEWING},
                now=now - timedelta(minutes=5),
                lease_ttl_seconds=3600,
            )
            attempt_dir = root / "runs" / manifest.run_id / "evidence" / "attempt-1"
            attempt_dir.mkdir(parents=True, exist_ok=True)
            output_path = attempt_dir / "codex-review-output.txt"
            output_path.write_text("ok\n", encoding="utf-8")
            report_result_path = attempt_dir / "codex-review-result.json"
            report_result_path.write_text("{}", encoding="utf-8")
            result_manifest_path = store.runner_subprocess_result_path(
                manifest.run_id,
                RUNNER_PHASE_CODE_REVIEW,
                lease.runner_id,
            )
            result_payload = RunnerSubprocessResult(
                schema_version=1,
                runner_id=lease.runner_id,
                run_id=manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                status=RUNNER_RESULT_COMPLETED,
                started_at=(now - timedelta(minutes=4)).isoformat(timespec="seconds"),
                completed_at=(now - timedelta(minutes=3)).isoformat(timespec="seconds"),
                request_path=str(
                    store.runner_subprocess_request_path(
                        manifest.run_id,
                        RUNNER_PHASE_CODE_REVIEW,
                        lease.runner_id,
                    )
                ),
                result_path=str(result_manifest_path),
                lease_path=str(store.runner_leases_path(manifest.run_id)),
                process_hint="pid:111",
                pid=111,
                lease_status="completed",
                review_status="passed",
                returncode=0,
                error=None,
                report={
                    "summary": "Codex review completed successfully.",
                    "output_path": str(output_path),
                    "result_path": str(report_result_path),
                    "status": "passed",
                    "returncode": 0,
                    "command": "codex review --uncommitted",
                    "service_tier": "fast",
                },
                evidence_files=[str(output_path), str(report_result_path)],
            )
            result_manifest_path.parent.mkdir(parents=True, exist_ok=True)
            result_manifest_path.write_text(
                json.dumps(result_payload.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            decisions = reconcile_runtime_startup_leases(run_store=store, now=now)
            code_review_decision = next(item for item in decisions if item["runner_id"] == lease.runner_id)
            self.assertNotEqual(code_review_decision["category"], "runner_alive")
            self.assertEqual(code_review_decision["reason"], "planner_review_failed")
            first_events = store.load_events(manifest.run_id)
            first_recovery = [
                event
                for event in first_events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(first_recovery), 1)

            # Re-running startup reconciliation should be idempotent.
            reconcile_runtime_startup_leases(run_store=store, now=now + timedelta(seconds=5))
            second_events = store.load_events(manifest.run_id)
            second_recovery = [
                event
                for event in second_events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(second_recovery), 1)

            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_WORK_DONE)
            self.assertEqual(refreshed.review_attempts[-1].status, REVIEW_ATTEMPT_FAILED_RETRYABLE)
            self.assertEqual(refreshed.review_attempts[-1].reason, "planner_review_failed")
            self.assertIn(str(output_path), refreshed.review.evidence_files)
            self.assertIn(str(report_result_path), refreshed.review.evidence_files)
            self.assertTrue(
                any(event.get("type") == "runner_subprocess_imported" for event in second_events)
            )

    def test_terminal_approved_run_ignores_stale_worker_lease_without_state_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Approved terminal task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_APPROVED
            manifest.planner.status = RUN_APPROVED
            manifest.workers[0].status = RUN_APPROVED
            store.save(manifest)
            stale_now = datetime.now(timezone.utc) - timedelta(hours=2)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": RUN_WORKING},
                now=stale_now,
                lease_ttl_seconds=60,
            )

            decisions = reconcile_runtime_startup_leases(run_store=store, now=datetime.now(timezone.utc))

            decision = next(item for item in decisions if item["runner_id"] == lease.runner_id)
            self.assertIn("terminal_run_lease_ignored", decision["reason"])
            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_APPROVED)
            self.assertEqual(refreshed.planner.status, RUN_APPROVED)
            self.assertEqual(refreshed.workers[0].status, RUN_APPROVED)
            leases = store.load_runner_leases(manifest.run_id)
            row = next(item for item in leases["leases"] if item["runner_id"] == lease.runner_id)
            self.assertEqual(row["status"], LEASE_STATUS_EXPIRED)

    def test_terminal_failed_run_ignores_residual_leases_without_polluting_state_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Failed terminal task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_FAILED
            manifest.planner.status = RUN_REVIEWING
            manifest.workers[0].status = RUN_WORKING
            store.save(manifest)
            stale = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="verification",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": RUN_WORKING},
                now=datetime.now(timezone.utc) - timedelta(hours=2),
                lease_ttl_seconds=60,
            )
            failed = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": RUN_WORKING},
                lease_ttl_seconds=3600,
            )
            store.runner_lease_store(manifest.run_id).fail(
                failed.runner_id,
                error="worker died",
            )

            reconcile_runtime_startup_leases(run_store=store, now=datetime.now(timezone.utc))
            first_events = store.load_events(manifest.run_id)
            first_recovery = [
                event
                for event in first_events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(first_recovery), 2)
            self.assertFalse(
                any(
                    event.get("type") in {"run_terminal_status", "verification_gate_failed", "code_review_failed"}
                    and event.get("source") == "runtime_startup"
                    for event in first_events
                )
            )
            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_FAILED)
            self.assertEqual(refreshed.planner.status, RUN_REVIEWING)
            self.assertEqual(refreshed.workers[0].status, RUN_WORKING)

            reconcile_runtime_startup_leases(run_store=store, now=datetime.now(timezone.utc))
            second_events = store.load_events(manifest.run_id)
            second_recovery = [
                event
                for event in second_events
                if event.get("type") == "recovery_decision_recorded"
                and event.get("source") == "runtime_startup"
            ]
            self.assertEqual(len(second_recovery), 2)
            self.assertTrue(any(item["runner_id"] == stale.runner_id for item in second_recovery))
            self.assertTrue(any(item["runner_id"] == failed.runner_id for item in second_recovery))

    def test_old_phase_or_attempt_lease_does_not_override_current_later_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Old lease should not override",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_REVIEWING
            manifest.plan = PlanRecord(summary="plan", worker_prompt="work", approval_status="approved")
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id=manifest.workers[0].id,
                    status=REVIEW_ATTEMPT_FAILED_RETRYABLE,
                    started_at=store.now_iso(),
                    completed_at=store.now_iso(),
                    reason="planner_review_failed",
                    error="old attempt failed",
                    evidence_files=["evidence/review-1.patch"],
                ),
                ReviewAttemptRecord(
                    id="review-2",
                    worker_id=manifest.workers[0].id,
                    status=REVIEW_ATTEMPT_ACTIVE,
                    started_at=store.now_iso(),
                    evidence_files=["evidence/review-2.patch"],
                ),
            ]
            manifest.review = ReviewRecord(evidence_files=["evidence/review-2.patch"])
            store.save(manifest)
            old_lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": RUN_WORKING, "review_attempt_id": "review-1"},
                lease_ttl_seconds=3600,
            )

            decisions = reconcile_runtime_startup_leases(run_store=store)

            decision = next(item for item in decisions if item["runner_id"] == old_lease.runner_id)
            self.assertEqual(decision["recovery_action"], "skip_not_relevant")
            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_REVIEWING)
            self.assertEqual(refreshed.review_attempts[-1].id, "review-2")
            self.assertEqual(refreshed.review_attempts[-1].status, REVIEW_ATTEMPT_ACTIVE)
            self.assertFalse(
                any(event.get("type") == "run_terminal_status" for event in store.load_events(manifest.run_id))
            )

    def test_failed_code_review_lease_is_retryable_review_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Failed code review task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = RUN_REVIEWING
            manifest.plan = PlanRecord(
                summary="Plan",
                worker_prompt="Do work",
                approval_status="approved",
                approved_by="human",
            )
            worker = manifest.workers[0]
            worker.status = WORKER_DONE
            worker.result = {"summary": "worker finished"}
            manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id=worker.id,
                    status=REVIEW_ATTEMPT_ACTIVE,
                    started_at=store.now_iso(),
                    worker_attempt=worker.attempt,
                    workspace_path=str(root),
                    evidence_files=["evidence/review.patch"],
                )
            ]
            store.save(manifest)
            lease = store.runner_lease_store(manifest.run_id).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"review_attempt_id": "review-1", "run_status": RUN_REVIEWING},
                lease_ttl_seconds=3600,
            )
            store.runner_lease_store(manifest.run_id).fail(
                lease.runner_id,
                error="runner subprocess crashed",
            )

            decisions = reconcile_runtime_startup_leases(run_store=store)

            failed = [item for item in decisions if item["runner_id"] == lease.runner_id]
            self.assertEqual(len(failed), 1)
            self.assertEqual(failed[0]["reason"], "code_review_failed")
            refreshed = store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_WORK_DONE)
            self.assertEqual(refreshed.review_attempts[-1].status, REVIEW_ATTEMPT_FAILED_RETRYABLE)
            self.assertEqual(refreshed.review_attempts[-1].reason, "code_review_failed")
            events = store.load_events(manifest.run_id)
            self.assertTrue(
                any(
                    event.get("type") == "recovery_decision_recorded"
                    and event.get("source") == "runtime_startup"
                    and event.get("reason") == "code_review_failed"
                    for event in events
                )
            )

    def test_runtime_startup_reconciliation_converges_queue_from_ghost_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            _init_git_repo(repo)
            runs_dir = root / "runs"
            queue_path = root / "queue.json"
            run_store = RunStore(runs_dir)
            manifest = run_store.create_run(
                cwd=repo,
                user_task="Ghost task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORKING"
            run_store.save(manifest)
            lease_store = run_store.runner_lease_store(manifest.run_id)
            lease_store.create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="worker_implement",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="runtime:test",
                pid=None,
                checkpoint={"run_status": "WORKING"},
                now=datetime.now(timezone.utc) - timedelta(hours=2),
                lease_ttl_seconds=60,
            )
            task_store = TaskStore(queue_path)
            queue = task_store.import_tasks(
                [{"task_id": "task-1", "title": "Ghost task", "prompt": "Ghost task", "cwd": str(repo)}]
            )
            task_store.update_task(
                queue,
                "task-1",
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
                reason="worker",
            )
            task_store.save(queue)

            runtime = COrchRuntime(
                runs_dir=runs_dir,
                queue_path=queue_path,
                scheduler_config=SchedulerConfig(
                    cwd=repo,
                    runs_dir=runs_dir,
                    worktrees_dir=root / "worktrees",
                    planner_model="gpt-5.5",
                    worker_model="gpt-5.3-codex",
                    codex_binary_path="/bin/codex",
                    max_attempts=2,
                    sandbox="workspace-write",
                    approval_policy="never",
                ),
            )
            runtime.wait_for_dispatch(timeout=2)

            queue_payload = runtime.build_queue_payload()
            self.assertEqual(queue_payload["tasks"][0]["status"], "FAILED")
            self.assertNotEqual(queue_payload["tasks"][0]["status"], "RUNNING")
            refreshed = run_store.load(manifest.run_id)
            self.assertEqual(refreshed.status, RUN_FAILED)


def _init_git_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
