from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import Request, urlopen
from unittest import mock

from c_orch.runtime import (
    build_proposals_payload,
    build_queue_payload,
    build_run_payload,
    build_runs_payload,
    build_state_payload,
    proposal_action,
    queue_action,
    run_action,
    task_action,
)
from c_orch.run_store import PlanRecord, PlanRevisionRecord, ReviewAttemptRecord, ReviewRecord, RunStore
from c_orch.proposal_store import ProposalStore
from c_orch.task_store import TaskStore
from c_orch.ui import FALLBACK_INDEX_HTML, build_server


class UiTests(unittest.TestCase):
    def test_build_runs_payload_lists_manifests_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            older = store.create_run(
                cwd=root,
                user_task="Older task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            older.updated_at = "2026-05-11T10:00:00+00:00"
            store.save(older, touch=False)
            newer = store.create_run(
                cwd=root,
                user_task="Newer task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            newer.status = "WORKING"
            newer.updated_at = "2026-05-11T11:00:00+00:00"
            newer.planner.thread_id = "planner-thread"
            newer.workers[0].thread_id = "worker-thread"
            store.save(newer, touch=False)

            payload = build_runs_payload(root / "runs")

            self.assertEqual([run["run_id"] for run in payload["runs"]], [newer.run_id, older.run_id])
            self.assertEqual(payload["runs"][0]["status"], "WORKING")
            self.assertEqual(payload["runs"][0]["planner"]["thread_id"], "planner-thread")
            self.assertEqual(payload["runs"][0]["workers"][0]["thread_id"], "worker-thread")
            self.assertIn("timing", payload["runs"][0])
            self.assertIn("total", payload["runs"][0]["timing"])

    def test_build_run_payload_includes_manifest_and_evidence_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task with evidence",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            evidence_path = root / "runs" / manifest.run_id / "evidence" / "git-diff.patch"
            verification_path = root / "runs" / manifest.run_id / "evidence" / "verification-output.txt"
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text("diff\n", encoding="utf-8")
            verification_path.write_text("pytest failed\nNo module named pytest\n", encoding="utf-8")
            manifest.status = "APPROVED"
            manifest.planner.reasoning_effort = "high"
            manifest.planner.service_tier = "fast"
            manifest.plan = PlanRecord(
                summary="Plan summary",
                worker_prompt="Build the feature",
                risk_notes=["Risk note"],
                approval_status="approved",
            )
            manifest.plan_revisions.append(
                PlanRevisionRecord(
                    id="plan-revision-1",
                    created_at="2026-05-11T12:00:00+00:00",
                    human_feedback="Please narrow the scope.",
                    previous_plan={"summary": "old"},
                    new_plan={"summary": "new"},
                )
            )
            manifest.workers[0].evidence_files = [str(evidence_path), str(verification_path)]
            manifest.workers[0].reasoning_effort = "medium"
            manifest.workers[0].service_tier = "flex"
            manifest.requires_restart = True
            manifest.restart_reason = "Touched runtime code"
            manifest.restart_paths = ["src/c_orch/orchestrator.py"]
            manifest.review = ReviewRecord(
                decision="accepted",
                reason="looks good",
                evidence_files=[str(evidence_path), str(verification_path)],
            )
            store.save(manifest)
            store.append_event(
                manifest.run_id,
                "planner_start",
                "Planner started",
                attempt=1,
            )
            store.append_event(
                manifest.run_id,
                "verification_gate_failed",
                "Verification gate failed",
                summary="2 of 3 verification command(s) failed.",
                reason="verification_failed",
            )

            payload = build_run_payload(root / "runs", manifest.run_id)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["manifest"]["run_id"], manifest.run_id)
            self.assertEqual(payload["run"]["review"]["decision"], "accepted")
            self.assertIn("timing", payload["run"])
            self.assertIn("phases", payload["run"]["timing"])
            self.assertEqual(payload["run"]["planner"]["reasoning_effort"], "high")
            self.assertEqual(payload["run"]["plan"]["approval_status"], "approved")
            self.assertEqual(payload["manifest"]["plan"]["worker_prompt"], "Build the feature")
            self.assertEqual(payload["run"]["workers"][0]["service_tier"], "flex")
            self.assertEqual(payload["run"]["review_attempt_count"], 0)
            self.assertEqual(payload["run"]["evidence_count"], 2)
            self.assertEqual(payload["run"]["waiting_for"], "restart")
            self.assertEqual(payload["run"]["next_action"], "restart")
            self.assertEqual(payload["run"]["allowed_actions"], [])
            self.assertEqual(payload["run"]["plan_revision_count"], 1)
            self.assertEqual(payload["run"]["latest_plan_revision_id"], "plan-revision-1")
            self.assertEqual(payload["run"]["latest_plan_revision_created_at"], "2026-05-11T12:00:00+00:00")
            self.assertEqual(
                payload["run"]["latest_plan_revision_feedback"],
                "Please narrow the scope.",
            )
            self.assertTrue(payload["run"]["requires_restart"])
            self.assertEqual(payload["run"]["restart_reason"], "Touched runtime code")
            self.assertEqual(payload["run"]["restart_paths"], ["src/c_orch/orchestrator.py"])
            self.assertTrue(payload["manifest"]["requires_restart"])
            self.assertEqual(payload["manifest"]["restart_reason"], "Touched runtime code")
            self.assertEqual(payload["manifest"]["restart_paths"], ["src/c_orch/orchestrator.py"])
            self.assertEqual(payload["evidence_files"][0]["name"], "git-diff.patch")
            self.assertTrue(payload["evidence_files"][0]["exists"])
            verification_file = next(
                file for file in payload["evidence_files"] if file["name"] == "verification-output.txt"
            )
            self.assertIn("No module named pytest", verification_file["preview"])
            self.assertEqual(payload["events"][0]["type"], "planner_start")
            self.assertEqual(payload["events"][0]["attempt"], 1)
            self.assertEqual(payload["run"]["last_error_event"]["type"], "verification_gate_failed")
            self.assertEqual(
                payload["run"]["last_error_event"]["summary"],
                "2 of 3 verification command(s) failed.",
            )

    def test_build_run_payload_rejects_path_traversal_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_run_payload(Path(tmp) / "runs", "../outside"))

    def test_legacy_manifest_without_timing_uses_fallback_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Legacy timing fallback",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            run_store.save(manifest)
            run_store.append_event(manifest.run_id, "planner_start", "Planner started")
            run_store.append_event(manifest.run_id, "planner_plan_ready", "Planner plan ready")
            run_store.append_event(manifest.run_id, "plan_review_required", "Human plan review required")

            manifest_path = root / "runs" / manifest.run_id / "manifest.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw.pop("timing", None)
            manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            run_payload = build_run_payload(root / "runs", manifest.run_id)
            runs_payload = build_runs_payload(root / "runs")
            state_payload = build_state_payload(
                runs_dir=root / "runs",
                queue_path=None,
                proposals_path=None,
                runtime_generation="legacy-test",
                selected_run_id=manifest.run_id,
            )

            assert run_payload is not None
            self.assertEqual(run_payload["run"]["timing"]["source"], "legacy_fallback")
            self.assertIn("phases", run_payload["run"]["timing"])
            self.assertEqual(runs_payload["runs"][0]["timing"]["source"], "legacy_fallback")
            self.assertEqual(
                state_payload["selected_run"]["run"]["timing"]["source"],
                "legacy_fallback",
            )

    def test_build_queue_payload_includes_task_run_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1", "cwd": "/tmp/repo-a"}]
            )
            queue_store.update_task(
                queue,
                "task-001",
                status="APPROVED",
                active_run_id="run-123",
                run_ids=["run-123"],
                reason="done",
                error="old error",
            )
            queue_store.save(queue)

            payload = build_queue_payload(root / "queue.json")

            self.assertEqual(payload["queue"]["status"], "PENDING")
            self.assertEqual(payload["summary"]["total_tasks"], 1)
            self.assertEqual(payload["summary"]["approved_tasks"], 1)
            self.assertEqual(payload["summary"]["completed_tasks"], 1)
            self.assertEqual(payload["summary"]["running_tasks"], 0)
            self.assertEqual(payload["summary"]["current_waiting_point"], "done")
            self.assertEqual(payload["tasks"][0]["task_id"], "task-001")
            self.assertEqual(payload["tasks"][0]["cwd"], "/tmp/repo-a")
            self.assertEqual(payload["tasks"][0]["active_run_id"], "run-123")
            self.assertEqual(payload["tasks"][0]["run_ids"], ["run-123"])
            self.assertEqual(payload["tasks"][0]["reason"], "done")
            self.assertEqual(payload["tasks"][0]["error"], "old error")
            self.assertEqual(payload["tasks"][0]["waiting_for"], "done")
            self.assertEqual(payload["tasks"][0]["next_action"], "done")
            self.assertEqual(payload["tasks"][0]["allowed_actions"], [])

    def test_build_queue_payload_derives_failed_active_run_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "FAILED"
            run_store.save(manifest)
            run_store.append_event(
                manifest.run_id,
                "worker_failed",
                "Worker failed",
                summary="Worker crashed before review.",
            )
            queue_store.update_task(
                queue,
                "task-001",
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            queue_store.save(queue)

            payload = build_queue_payload(root / "queue.json", runs_dir=root / "runs")
            loaded = queue_store.load()

            self.assertEqual(payload["queue"]["status"], "FAILED")
            self.assertEqual(payload["tasks"][0]["status"], "FAILED")
            self.assertEqual(payload["tasks"][0]["waiting_for"], "retry_task")
            self.assertEqual(payload["tasks"][0]["next_action"], "retry_task")
            self.assertEqual(payload["tasks"][0]["allowed_actions"], ["retry-task"])
            self.assertEqual(payload["tasks"][0]["failure_summary"], "Worker crashed before review.")
            self.assertEqual(payload["tasks"][0]["last_error_event"]["type"], "worker_failed")
            self.assertEqual(payload["summary"]["current_waiting_point"], "retry_task")
            self.assertEqual(payload["summary"]["completed_tasks"], 0)
            self.assertEqual(payload["summary"]["running_tasks"], 0)
            self.assertEqual(loaded.status, "PENDING")
            self.assertEqual(loaded.tasks[0].status, "RUNNING")

    def test_build_queue_payload_exposes_retry_ci_for_verification_gate_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "FAILED"
            manifest.review = ReviewRecord(decision="accepted", reason="looks good")
            run_store.save(manifest)
            run_store.append_event(
                manifest.run_id,
                "verification_gate_failed",
                "Verification gate failed; refusing apply and commit.",
                summary="1 of 4 verification command(s) failed.",
            )
            run_store.append_event(
                manifest.run_id,
                "apply_completed",
                "Apply completed",
                applied=False,
                summary="Failed: git apply --check rejected the patch.",
            )
            queue_store.update_task(
                queue,
                "task-001",
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            queue_store.save(queue)

            payload = build_queue_payload(root / "queue.json", runs_dir=root / "runs")

            self.assertEqual(payload["tasks"][0]["status"], "FAILED")
            self.assertEqual(
                payload["tasks"][0]["allowed_actions"],
                ["retry-verification", "retry-task"],
            )
            self.assertEqual(
                payload["tasks"][0]["failure_summary"],
                "Failed: git apply --check rejected the patch.",
            )
            self.assertEqual(
                payload["tasks"][0]["last_error_event"]["type"],
                "apply_completed",
            )

    def test_build_queue_payload_derives_planner_review_retry_without_saving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORK_DONE"
            manifest.review = ReviewRecord(evidence_files=["evidence/review.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-13T00:00:00+00:00",
                    completed_at="2026-05-13T00:01:00+00:00",
                    evidence_files=["evidence/review.patch"],
                    error="Timed out waiting for MCP server output",
                )
            ]
            run_store.save(manifest)
            queue_store.update_task(
                queue,
                "task-001",
                status="RUNNING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            queue_store.save(queue)

            payload = build_queue_payload(root / "queue.json", runs_dir=root / "runs")
            loaded = queue_store.load()

            self.assertEqual(payload["queue"]["status"], "RUNNING")
            self.assertEqual(payload["tasks"][0]["status"], "WAITING")
            self.assertEqual(payload["tasks"][0]["waiting_for"], "planner_review_retry")
            self.assertEqual(payload["tasks"][0]["next_action"], "planner_review_retry")
            self.assertEqual(payload["tasks"][0]["allowed_actions"], [])
            self.assertEqual(payload["summary"]["current_waiting_point"], "planner_review_retry")
            self.assertEqual(loaded.tasks[0].status, "RUNNING")
            self.assertIsNone(loaded.tasks[0].reason)

    def test_build_state_payload_includes_version_and_runtime_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            run_store.save(manifest)
            proposal_store = ProposalStore(root / "proposals.json")
            proposal_store.create()
            queue_store = TaskStore(root / "queue.json")
            queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )

            payload = build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                runtime_generation="test-generation",
                selected_run_id=manifest.run_id,
            )

            self.assertEqual(payload["runtime"]["generation"], "test-generation")
            self.assertGreater(payload["version"], 0)
            self.assertEqual(payload["queue"]["summary"]["total_tasks"], 1)
            self.assertEqual(payload["proposals"]["summary"]["total_proposals"], 0)
            self.assertEqual(payload["runs"]["runs"][0]["run_id"], manifest.run_id)
            self.assertEqual(payload["focused_run_id"], manifest.run_id)
            self.assertEqual(payload["selected_run"]["run"]["run_id"], manifest.run_id)

    def test_build_state_payload_focuses_attention_run_without_selected_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_store = RunStore(root / "runs")
            completed = run_store.create_run(
                cwd=root,
                user_task="Completed task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            completed.status = "APPROVED"
            run_store.save(completed)
            failed = run_store.create_run(
                cwd=root,
                user_task="Failed task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            failed.status = "FAILED"
            run_store.save(failed)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [
                    {"task_id": "done", "title": "Done", "prompt": "Done"},
                    {"task_id": "failed", "title": "Failed", "prompt": "Failed"},
                ]
            )
            queue_store.update_task(
                queue,
                "done",
                status="APPROVED",
                active_run_id=completed.run_id,
                run_ids=[completed.run_id],
            )
            queue_store.update_task(
                queue,
                "failed",
                status="FAILED",
                active_run_id=failed.run_id,
                run_ids=[failed.run_id],
            )
            queue_store.save(queue)
            ProposalStore(root / "proposals.json").create()

            payload = build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                runtime_generation="test-generation",
            )

            self.assertEqual(payload["focused_run_id"], failed.run_id)
            self.assertEqual(payload["selected_run"]["run"]["run_id"], failed.run_id)

    def test_build_proposals_payload_exposes_plan_review_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal = proposal_store.add_proposal(pool, title="Task 1", prompt="Do task 1")
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.acceptance_criteria = ["Acceptance 1"]
            manifest.verification_commands = ["pytest"]
            manifest.plan = PlanRecord(
                summary="Plan summary",
                worker_prompt="Do the work",
                risk_notes=["Risk 1"],
            )
            run_store.save(manifest)
            proposal.run_id = manifest.run_id
            proposal.cwd = str(root / "repo-a")
            proposal.status = "PLAN_REVIEW_REQUIRED"
            proposal_store.save(pool)

            payload = build_proposals_payload(root / "proposals.json", runs_dir=root / "runs")

            self.assertEqual(payload["summary"]["total_proposals"], 1)
            self.assertEqual(payload["summary"]["review_required"], 1)
            self.assertEqual(payload["proposals"][0]["run_id"], manifest.run_id)
            self.assertEqual(payload["proposals"][0]["cwd"], str(root / "repo-a"))
            self.assertEqual(payload["proposals"][0]["allowed_actions"], ["approve-plan", "revise-plan"])
            self.assertEqual(payload["proposals"][0]["run"]["plan"]["summary"], "Plan summary")
            self.assertEqual(payload["proposals"][0]["plan_detail"]["summary"], "Plan summary")
            self.assertEqual(payload["proposals"][0]["plan_detail"]["worker_prompt"], "Do the work")
            self.assertEqual(payload["proposals"][0]["plan_detail"]["risk_notes"], ["Risk 1"])
            self.assertEqual(payload["proposals"][0]["plan_detail"]["acceptance_criteria"], ["Acceptance 1"])
            self.assertEqual(payload["proposals"][0]["plan_detail"]["verification_commands"], ["pytest"])

    def test_proposal_approve_queues_approved_plan_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal = proposal_store.add_proposal(pool, title="Task 1", prompt="Do task 1")
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(summary="Plan summary", worker_prompt="Do the work")
            run_store.save(manifest)
            proposal.run_id = manifest.run_id
            proposal.cwd = str(root / "repo-a")
            proposal.status = "PLAN_REVIEW_REQUIRED"
            proposal_store.save(pool)

            status, payload = proposal_action(
                root / "proposals.json",
                proposal.proposal_id,
                "approve-plan",
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                driver_factory=_unused_driver_factory,
            )
            queue = TaskStore(root / "queue.json").load()
            loaded_manifest = run_store.load(manifest.run_id)

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["summary"]["total_proposals"], 0)
            self.assertEqual(payload["proposals"], [])
            self.assertEqual(payload["transition"]["type"], "proposal_approved_and_queued")
            self.assertEqual(payload["transition"]["run_id"], manifest.run_id)
            self.assertEqual(payload["transition"]["task_id"], proposal.proposal_id)
            self.assertEqual(queue.tasks[0].cwd, str(root / "repo-a"))
            self.assertTrue(payload["transition"]["removed_from_pool"])
            self.assertEqual(queue.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(queue.tasks[0].run_ids, [manifest.run_id])
            self.assertEqual(loaded_manifest.status, "PLAN_APPROVED")
            self.assertEqual(loaded_manifest.plan.approval_status, "approved")  # type: ignore[union-attr]

    def test_task_action_retry_requeues_failed_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            queue_store.update_task(
                queue,
                "task-001",
                status="FAILED",
                active_run_id="run-1",
                run_ids=["run-1"],
                reason="active_run_failed",
            )
            queue_store.save(queue)

            status, payload = task_action(root / "queue.json", "task-001", "retry-task")
            loaded = queue_store.load()

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["tasks"][0]["status"], "PENDING")
            self.assertIsNone(payload["tasks"][0]["active_run_id"])
            self.assertEqual(payload["tasks"][0]["run_ids"], ["run-1"])
            self.assertEqual(loaded.tasks[0].status, "PENDING")
            self.assertIsNone(loaded.tasks[0].active_run_id)

    def test_queue_action_confirm_runtime_restarted_clears_restart_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "APPROVED"
            manifest.requires_restart = True
            manifest.restart_reason = "Runtime files changed"
            manifest.restart_paths = ["src/c_orch/runtime.py", "src/c_orch/ui.py"]
            run_store.save(manifest)
            queue_store.update_task(
                queue,
                "task-001",
                status="APPROVED",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            queue.status = "RESTART_REQUIRED"
            queue_store.save(queue)

            status, payload = queue_action(
                root / "queue.json",
                "confirm-runtime-restarted",
                runs_dir=root / "runs",
                confirmed_by="dashboard",
            )

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["queue"]["status"], "APPROVED")
            loaded_manifest = run_store.load(manifest.run_id)
            self.assertFalse(loaded_manifest.requires_restart)
            self.assertEqual(loaded_manifest.restart_reason, "Runtime files changed")
            self.assertEqual(
                loaded_manifest.restart_paths,
                ["src/c_orch/runtime.py", "src/c_orch/ui.py"],
            )
            events = run_store.load_events(manifest.run_id)
            self.assertTrue(events)
            event = events[-1]
            self.assertEqual(event["type"], "runtime_restart_confirmed")
            self.assertEqual(event["action"], "confirm-runtime-restarted")
            self.assertEqual(event["confirmed_by"], "dashboard")
            self.assertEqual(event["source"], "dashboard")
            self.assertEqual(event["previous_restart_reason"], "Runtime files changed")
            self.assertEqual(
                event["previous_restart_paths"],
                ["src/c_orch/runtime.py", "src/c_orch/ui.py"],
            )

    def test_queue_action_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            status, payload = queue_action(
                root / "queue.json",
                "unknown-action",
                runs_dir=root / "runs",
            )
            self.assertEqual(int(status), 400)
            self.assertEqual(payload["error"], "unsupported action")

    def test_queue_action_returns_none_when_queue_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = queue_action(
                root / "missing-queue.json",
                "confirm-runtime-restarted",
                runs_dir=root / "runs",
            )
            self.assertIsNone(result)

    def test_queue_action_does_not_clear_non_approved_restart_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                    {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                    {"task_id": "task-003", "title": "Task 3", "prompt": "Do task 3"},
                ]
            )
            run_store = RunStore(root / "runs")
            status_by_task = {
                "task-001": "FAILED",
                "task-002": "RUNNING",
                "task-003": "PLAN_REVIEW_REQUIRED",
            }
            run_ids = {}
            for task_id, run_status in status_by_task.items():
                manifest = run_store.create_run(
                    cwd=root,
                    user_task=task_id,
                    planner_model="gpt-5.5",
                    worker_model="gpt-5.3-codex",
                )
                manifest.status = run_status
                manifest.requires_restart = True
                manifest.restart_reason = f"{task_id} reason"
                manifest.restart_paths = [f"{task_id}.txt"]
                run_store.save(manifest)
                run_ids[task_id] = manifest.run_id
                queue_store.update_task(
                    queue,
                    task_id,
                    status="RUNNING",
                    active_run_id=manifest.run_id,
                    run_ids=[manifest.run_id],
                )
            queue.status = "RESTART_REQUIRED"
            queue_store.save(queue)

            status, _payload = queue_action(
                root / "queue.json",
                "confirm-runtime-restarted",
                runs_dir=root / "runs",
                confirmed_by="dashboard",
            )

            self.assertEqual(int(status), 200)
            for task_id in status_by_task:
                loaded_manifest = run_store.load(run_ids[task_id])
                self.assertTrue(loaded_manifest.requires_restart)
                self.assertEqual(loaded_manifest.restart_reason, f"{task_id} reason")
                self.assertEqual(loaded_manifest.restart_paths, [f"{task_id}.txt"])
                self.assertEqual(run_store.load_events(run_ids[task_id]), [])

    def test_dashboard_queue_actions_route_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "APPROVED"
            manifest.requires_restart = True
            manifest.restart_reason = "Restart before continue"
            manifest.restart_paths = ["src/c_orch/runtime.py"]
            run_store.save(manifest)
            queue_store.update_task(
                queue,
                "task-001",
                status="APPROVED",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            queue.status = "RESTART_REQUIRED"
            queue_store.save(queue)

            server = build_server(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                host="127.0.0.1",
                port=0,
            )
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                request = Request(
                    f"http://127.0.0.1:{server.server_port}/api/queue/actions",
                    data=json.dumps({"action": "confirm-runtime-restarted"}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))
            finally:
                runtime = getattr(server, "c_orch_runtime", None)
                if runtime is not None:
                    runtime.close()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(body["transition"]["type"], "runtime_restart_confirmed")
            self.assertEqual(body["state"]["queue"]["queue"]["status"], "APPROVED")
            self.assertFalse(run_store.load(manifest.run_id).requires_restart)

    def test_dashboard_state_route_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            TaskStore(root / "queue.json").import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            ProposalStore(root / "proposals.json").create()

            server = build_server(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                host="127.0.0.1",
                port=0,
            )
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/api/state", timeout=2) as response:
                    body = json.loads(response.read().decode("utf-8"))
            finally:
                runtime = getattr(server, "c_orch_runtime", None)
                if runtime is not None:
                    runtime.close()
                server.server_close()
                thread.join(timeout=2)

            self.assertIn("runtime", body)
            self.assertIn("version", body)
            self.assertEqual(body["queue"]["summary"]["total_tasks"], 1)
            self.assertEqual(body["proposals"]["summary"]["total_proposals"], 0)
            self.assertIn("runs", body)

    def test_plan_review_run_does_not_expose_dashboard_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(summary="Plan", worker_prompt="Prompt")
            store.save(manifest)

            payload = build_run_payload(root / "runs", manifest.run_id)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["run"]["allowed_actions"], [])

    def test_react_frontend_sources_contain_dashboard_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app_source = (root / "web" / "src" / "components" / "App.tsx").read_text(encoding="utf-8")
        client_source = (root / "web" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
        main_source = (root / "web" / "src" / "main.tsx").read_text(encoding="utf-8")
        query_source = (root / "web" / "src" / "queries.ts").read_text(encoding="utf-8")
        style_source = (root / "web" / "src" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("/api/runs", client_source)
        self.assertIn("/api/queue", client_source)
        self.assertIn("/api/proposals", client_source)
        self.assertIn("cwd", client_source)
        self.assertIn("/api/state", client_source)
        self.assertIn("/api/queue/actions", client_source)
        self.assertIn("/actions", client_source)
        self.assertIn("allowed_actions", app_source)
        self.assertIn("待审核计划", app_source)
        self.assertIn("生成 Planner 方案", app_source)
        self.assertIn("创建中...", app_source)
        self.assertIn("目标仓库路径", app_source)
        self.assertIn("Planner 方案生成中", app_source)
        self.assertIn("Planner 方案生成中...", app_source)
        self.assertIn("规划失败：", app_source)
        self.assertIn("通过并加入执行队列", app_source)
        self.assertIn("pendingProposalAction", app_source)
        self.assertIn("observedRevision", app_source)
        self.assertIn("pendingTaskAction", app_source)
        self.assertIn("pendingRunAction", app_source)
        self.assertNotIn("actionMutation.isPending ? \"处理中...\" : \"通过并加入执行队列\"", app_source)
        self.assertIn("ProposalPlanPanel", app_source)
        self.assertIn("完整 Planner 方案", app_source)
        self.assertIn("FailurePanel", app_source)
        self.assertIn("失败原因", app_source)
        self.assertIn("verification-output.txt", app_source)
        self.assertIn("confirm-runtime-restarted", app_source)
        self.assertIn("确认已重启并继续", app_source)
        self.assertIn("确认中...", app_source)
        self.assertIn("queueActionError", app_source)
        self.assertIn("QUEUE_PREVIEW_LIMIT = 5", app_source)
        self.assertIn("显示全部", app_source)
        self.assertIn("收起", app_source)
        self.assertIn("隐藏", app_source)
        self.assertIn("queueTaskList", app_source)
        self.assertNotIn("通过并启动 Worker", app_source)
        self.assertNotIn("让 Planner 重新生成计划", app_source)
        self.assertIn("重新让 Planner 复核", app_source)
        self.assertIn("运行时间线", app_source)
        self.assertIn("Worker 指令", app_source)
        self.assertIn("验收标准", app_source)
        self.assertIn("验证命令", app_source)
        self.assertIn("newestFirst", app_source)
        self.assertIn("focusedRunId", app_source)
        self.assertIn("focused_run_id", app_source)
        self.assertIn("manualSelection", app_source)
        self.assertIn("stateSelectedRun", app_source)
        self.assertIn("runQuery.data ??", app_source)
        self.assertIn("stateQuery.refetch()", app_source)
        self.assertIn("runQuery.refetch()", app_source)
        self.assertNotIn("isManualRefresh", app_source)
        self.assertNotIn("MANUAL_REFRESH_TIMEOUT_MS", app_source)
        self.assertNotIn("刷新中...", app_source)
        self.assertNotIn("disabled={stateQuery.isFetching}", app_source)
        self.assertIn("scrollIntoView", app_source)
        self.assertIn("正在切换 run 详情", app_source)
        self.assertIn("useMutation", query_source)
        self.assertIn("invalidateQueries", query_source)
        self.assertIn("export async function refreshDashboardQueries", query_source)
        self.assertIn("refreshDashboardQueries", query_source)
        self.assertIn("useDashboardStateQuery", query_source)
        self.assertIn("activeProposals", query_source)
        self.assertIn("runningTasks", query_source)
        self.assertIn("queue_dispatch_running", query_source)
        self.assertIn("runtimeBusy", query_source)
        self.assertIn("2000", query_source)
        self.assertIn("updateStateFromAction", query_source)
        self.assertIn("selectedRunFromAction", query_source)
        self.assertIn("queryKey: [\"run\"]", query_source)
        self.assertIn("state_version", app_source)
        self.assertIn("runtimeGeneration", app_source)
        self.assertNotIn("refetchInterval: 1000", main_source)
        self.assertIn("refetchIntervalInBackground: true", main_source)
        self.assertIn(".queueTaskList.expanded", style_source)
        self.assertIn("max-height: 48vh", style_source)

    def test_fallback_html_explains_missing_frontend_build(self) -> None:
        self.assertIn("c-orch 前端还没有构建", FALLBACK_INDEX_HTML)
        self.assertIn("pnpm --dir web run build", FALLBACK_INDEX_HTML)

    def test_dashboard_server_serves_root_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = build_server(runs_dir=root / "runs", host="127.0.0.1", port=0)
            thread = threading.Thread(target=server.handle_request, daemon=True)
            thread.start()
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
                    body = response.read().decode("utf-8")
                    content_type = response.headers["Content-Type"]
            finally:
                runtime = getattr(server, "c_orch_runtime", None)
                if runtime is not None:
                    runtime.close()
                server.server_close()
                thread.join(timeout=2)

            self.assertIn("c-orch", body)
            self.assertIn("text/html", content_type)

    def test_run_action_revise_plan_passes_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                codex_binary_path="/bin/codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.planner.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(summary="Plan", worker_prompt="Prompt")
            store.save(manifest)

            with mock.patch("c_orch.mcp_driver.McpCodexDriver") as driver_cls, mock.patch(
                "c_orch.orchestrator.RunOrchestrator"
            ) as orchestrator_cls:
                driver = driver_cls.return_value.__enter__.return_value
                orchestrator = orchestrator_cls.return_value
                orchestrator.revise_plan.return_value = manifest
                status, _payload = run_action(
                    root / "runs",
                    manifest.run_id,
                    "revise-plan",
                    "Please update the plan",
                )

            self.assertEqual(int(status), 200)
            self.assertEqual(orchestrator.revise_plan.call_count, 1)
            self.assertEqual(orchestrator.revise_plan.call_args.args[1], "Please update the plan")
            driver_cls.assert_called_once()

    def test_run_action_rejects_approve_plan_outside_plan_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                codex_binary_path="/bin/codex",
            )
            manifest.status = "WORKING"
            manifest.plan = PlanRecord(summary="Plan", worker_prompt="Prompt")
            store.save(manifest)

            with mock.patch("c_orch.mcp_driver.McpCodexDriver") as driver_cls:
                status, payload = run_action(root / "runs", manifest.run_id, "approve-plan")

            self.assertEqual(int(status), 409)
            self.assertEqual(payload["status"], "WORKING")
            self.assertIn("requires PLAN_REVIEW_REQUIRED", payload["error"])
            driver_cls.assert_not_called()

    def test_run_action_rejects_retry_review_without_saved_failed_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                codex_binary_path="/bin/codex",
            )
            manifest.status = "PLAN_REVIEW_REQUIRED"
            manifest.plan = PlanRecord(summary="Plan", worker_prompt="Prompt")
            store.save(manifest)

            with mock.patch("c_orch.mcp_driver.McpCodexDriver") as driver_cls:
                status, payload = run_action(root / "runs", manifest.run_id, "retry-review")

            self.assertEqual(int(status), 409)
            self.assertEqual(payload["status"], "PLAN_REVIEW_REQUIRED")
            self.assertIn("requires saved failed review evidence", payload["error"])
            driver_cls.assert_not_called()


class _UnusedDriverContext:
    def __enter__(self) -> object:
        raise AssertionError("driver should not be used")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


def _unused_driver_factory(_codex_path: str) -> _UnusedDriverContext:
    return _UnusedDriverContext()


if __name__ == "__main__":
    unittest.main()
