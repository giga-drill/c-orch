from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from c_orch import dashboard_payloads
from c_orch.proposal_store import ProposalStore
from c_orch.runner_subprocess import RUNNER_PHASE_CODE_REVIEW
from c_orch.run_store import PlanRecord, ReviewAttemptRecord, ReviewRecord, RunStore
from c_orch.runner_leases import RunnerLeaseStore
from c_orch.task_store import TaskStore


class DashboardPayloadBoundaryTests(unittest.TestCase):
    def test_public_payload_builders_live_in_dashboard_payloads_module(self) -> None:
        for name in (
            "build_proposals_payload",
            "build_runs_payload",
            "build_state_payload",
            "build_queue_payload",
            "build_run_payload",
        ):
            builder = getattr(dashboard_payloads, name)
            self.assertEqual(builder.__module__, "c_orch.dashboard_payloads")

    def test_waiting_workspace_clean_payload_exposes_retry_and_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proposals_path = Path(tmp) / "proposals.json"
            store = ProposalStore(proposals_path)
            pool = store.create()
            proposal = store.add_proposal(pool, title="Task", prompt="Do task", cwd="/tmp/repo")
            proposal.status = "WAITING_WORKSPACE_CLEAN"
            proposal.blocker = {
                "message": "目标工作区存在未提交改动。建议先提交或处理这些改动，再生成计划。",
                "status_output": " M src/main.py",
            }
            store.save(pool)

            payload = dashboard_payloads.build_proposals_payload(proposals_path)

            self.assertEqual(payload["summary"]["waiting_workspace_clean"], 1)
            self.assertEqual(payload["proposals"][0]["waiting_for"], "workspace_clean")
            self.assertEqual(
                payload["proposals"][0]["allowed_actions"],
                ["move-before", "move-after", "retry-plan"],
            )
            self.assertIn("src/main.py", payload["proposals"][0]["blocker"]["status_output"])

    def test_waiting_proposal_input_payload_exposes_preflight_reason_and_suggestions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proposals_path = Path(tmp) / "proposals.json"
            store = ProposalStore(proposals_path)
            pool = store.create()
            proposal = store.add_proposal(pool, title="Task", prompt="fix it", cwd="/tmp/repo")
            proposal.status = "WAITING_PROPOSAL_INPUT"
            proposal.reason = "proposal_too_vague"
            proposal.blocker = {
                "type": "proposal_preflight_blocked",
                "reason": "proposal_too_vague",
                "message": "提案信息过少，当前描述不足以让 Planner 生成可执行计划。",
                "suggested_action": "请补充目标改动对象、预期行为和基本验证方式后重试。",
                "suggestions": ["说明要改动的模块、文件或接口范围。"],
                "issues": ["prompt_too_vague"],
            }
            store.save(pool)

            payload = dashboard_payloads.build_proposals_payload(proposals_path)

            self.assertEqual(payload["summary"]["waiting_proposal_input"], 1)
            self.assertEqual(payload["proposals"][0]["waiting_for"], "proposal_input")
            self.assertEqual(
                payload["proposals"][0]["allowed_actions"],
                ["move-before", "move-after", "retry-plan"],
            )
            self.assertEqual(payload["proposals"][0]["blocker"]["reason"], "proposal_too_vague")
            self.assertEqual(
                payload["proposals"][0]["blocker"]["suggestions"],
                ["说明要改动的模块、文件或接口范围。"],
            )
            self.assertEqual(payload["proposals"][0]["blocker"]["issues"], ["prompt_too_vague"])

    def test_proposal_plan_detail_includes_decomposition_suggestion(self) -> None:
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
                decomposition_suggestion={
                    "recommended": True,
                    "reason": "范围较大，建议拆分",
                    "subtasks": [
                        {
                            "id": "subtask-1",
                            "title": "先补契约",
                            "goal": "补齐契约并加测试",
                            "acceptance_criteria": ["契约测试通过"],
                        }
                    ],
                },
            )
            run_store.save(manifest)

            proposal.run_id = manifest.run_id
            proposal.status = "PLAN_REVIEW_REQUIRED"
            proposal_store.save(pool)

            payload = dashboard_payloads.build_proposals_payload(root / "proposals.json", runs_dir=root / "runs")
            detail = payload["proposals"][0]["plan_detail"]

            self.assertIsNotNone(detail)
            self.assertEqual(detail["decomposition_suggestion"]["recommended"], True)
            self.assertEqual(detail["decomposition_suggestion"]["subtasks"][0]["id"], "subtask-1")

    def test_proposal_plan_detail_sets_null_when_suggestion_missing(self) -> None:
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
            manifest.plan = PlanRecord(summary="Plan summary", worker_prompt="Do the work")
            run_store.save(manifest)

            proposal.run_id = manifest.run_id
            proposal.status = "PLAN_REVIEW_REQUIRED"
            proposal_store.save(pool)

            payload = dashboard_payloads.build_proposals_payload(root / "proposals.json", runs_dir=root / "runs")
            detail = payload["proposals"][0]["plan_detail"]

            self.assertIsNotNone(detail)
            self.assertIsNone(detail["decomposition_suggestion"])

    def test_run_payload_distinguishes_latest_revision_reason_from_review_infra_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "REVISION_REQUESTED"
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-18T10:00:00+00:00",
                    completed_at="2026-05-18T10:01:00+00:00",
                    decision="revision_requested",
                    reason="旧打回原因。需要补日志。",
                    worker_attempt=1,
                    service_tier="fast",
                ),
                ReviewAttemptRecord(
                    id="review-2",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-18T10:02:00+00:00",
                    completed_at="2026-05-18T10:03:00+00:00",
                    reason="code_review_error",
                    error="status=error returncode=None summary=Codex review timed out after 900s.",
                    worker_attempt=2,
                    service_tier="fast",
                ),
                ReviewAttemptRecord(
                    id="review-3",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-18T10:04:00+00:00",
                    completed_at="2026-05-18T10:05:00+00:00",
                    decision="revision_requested",
                    summary="最新核心打回：缺少失败路径测试。",
                    reason="最新核心打回：缺少失败路径测试。另外需要补边界条件。",
                    worker_attempt=3,
                    service_tier="flex",
                ),
            ]
            manifest.review = ReviewRecord(
                decision="revision_requested",
                reason="最新核心打回：缺少失败路径测试。另外需要补边界条件。",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            run = payload["run"]
            self.assertEqual(run["review_attempt_count"], 3)
            self.assertEqual(run["review_retry_count"], 1)
            self.assertEqual(run["last_review_attempt"]["id"], "review-3")
            self.assertEqual(run["last_review_attempt"]["summary"], "最新核心打回：缺少失败路径测试。")
            self.assertEqual(run["last_review_attempt"]["service_tier"], "flex")
            self.assertEqual(run["latest_revision_request"]["review_attempt_id"], "review-3")
            self.assertEqual(
                run["latest_revision_request"]["summary"],
                "最新核心打回：缺少失败路径测试。",
            )
            self.assertEqual(run["latest_review_failure"]["review_attempt_id"], "review-2")
            self.assertEqual(run["latest_review_failure"]["reason"], "code_review_error")
            self.assertIn("timed out after 900s", run["latest_review_failure"]["summary"])

    def test_run_payload_uses_reason_first_sentence_when_revision_summary_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "REVISION_REQUESTED"
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-18T11:00:00+00:00",
                    completed_at="2026-05-18T11:01:00+00:00",
                    decision="revision_requested",
                    reason="First sentence only.\nSecond line details.",
                ),
            ]
            manifest.review = ReviewRecord(
                decision="revision_requested",
                reason="First sentence only.\nSecond line details.",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            summary = payload["run"]["latest_revision_request"]["summary"]
            self.assertEqual(summary, "First sentence only.")

    def test_run_payload_retry_backoff_state_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORK_DONE"
            manifest.review = ReviewRecord(evidence_files=["runs/example/evidence/git-diff.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-18T11:00:00+00:00",
                    completed_at="2026-05-18T11:01:00+00:00",
                    reason="planner_review_failed",
                    error="Timed out waiting for MCP server output",
                    retry_attempt=1,
                    retry_budget=3,
                    next_retry_at="2099-01-01T00:00:00+00:00",
                    backoff_reason="retry_backoff_30s",
                    retry_budget_exhausted=False,
                )
            ]
            store.save(manifest)
            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            run = payload["run"]
            self.assertEqual(run["waiting_for"], "retry_backoff")
            self.assertEqual(run["retry_state"]["next_retry_at"], "2099-01-01T00:00:00+00:00")
            self.assertEqual(run["retry_state"]["backoff_reason"], "retry_backoff_30s")
            self.assertFalse(run["retry_state"]["budget_exhausted"])
            self.assertEqual(run["latest_review_failure"]["next_retry_at"], "2099-01-01T00:00:00+00:00")

    def test_queue_payload_retry_budget_exhausted_wait_reason_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-1", "title": "Task", "prompt": "Do task", "cwd": str(root)}]
            )
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORK_DONE"
            manifest.review = ReviewRecord(evidence_files=["runs/example/evidence/git-diff.patch"])
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-18T11:00:00+00:00",
                    completed_at="2026-05-18T11:01:00+00:00",
                    reason="planner_review_failed",
                    error="Timed out waiting for MCP server output",
                    retry_attempt=3,
                    retry_budget=3,
                    backoff_reason="retry_budget_exhausted",
                    retry_budget_exhausted=True,
                )
            ]
            run_store.save(manifest)
            task_store.update_task(
                queue,
                "task-1",
                status="WAITING",
                active_run_id=manifest.run_id,
                run_ids=[manifest.run_id],
            )
            task_store.save(queue)

            payload = dashboard_payloads.build_queue_payload(root / "queue.json", runs_dir=root / "runs")

            self.assertEqual(payload["tasks"][0]["waiting_for"], "retry_budget_exhausted")
            self.assertTrue(payload["tasks"][0]["retry_state"]["budget_exhausted"])

    def test_run_payload_uses_chinese_first_sentence_without_whitespace_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "REVISION_REQUESTED"
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-18T12:00:00+00:00",
                    completed_at="2026-05-18T12:01:00+00:00",
                    decision="revision_requested",
                    reason="缺少失败路径测试。另外需要补边界条件。",
                ),
            ]
            manifest.review = ReviewRecord(
                decision="revision_requested",
                reason="缺少失败路径测试。另外需要补边界条件。",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            self.assertEqual(
                payload["run"]["latest_revision_request"]["summary"],
                "缺少失败路径测试。",
            )

    def test_state_payload_includes_low_cost_mode_and_effective_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=None,
                proposals_path=None,
                runtime_generation="test-generation",
                cost_mode={
                    "low_cost_mode": True,
                    "mode_label": "low_cost",
                    "effective_service_tiers": {
                        "planner": "flex",
                        "worker": "flex",
                        "reviewer": "flex",
                    },
                    "toggle_available": False,
                },
            )

            self.assertTrue(payload["cost_mode"]["low_cost_mode"])
            self.assertEqual(payload["cost_mode"]["mode_label"], "low_cost")
            self.assertEqual(payload["cost_mode"]["effective_service_tiers"]["planner"], "flex")
            self.assertEqual(payload["cost_mode"]["effective_service_tiers"]["worker"], "flex")
            self.assertEqual(payload["cost_mode"]["effective_service_tiers"]["reviewer"], "flex")
            self.assertFalse(payload["cost_mode"]["toggle_available"])

    def test_state_payload_cost_mode_defaults_to_unavailable_without_scheduler_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=None,
                proposals_path=None,
                runtime_generation="test-generation",
            )

            self.assertFalse(payload["cost_mode"]["low_cost_mode"])
            self.assertEqual(payload["cost_mode"]["mode_label"], "unavailable")
            self.assertIsNone(payload["cost_mode"]["effective_service_tiers"]["planner"])
            self.assertIsNone(payload["cost_mode"]["effective_service_tiers"]["worker"])
            self.assertIsNone(payload["cost_mode"]["effective_service_tiers"]["reviewer"])
            self.assertFalse(payload["cost_mode"]["toggle_available"])

    def test_state_payload_exposes_restart_gate_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [
                    {"task_id": "gate-task", "title": "Gate task", "prompt": "Gate task", "cwd": str(repo)},
                    {"task_id": "task-b", "title": "Task B", "prompt": "Task B", "cwd": str(repo)},
                ]
            )
            run_store = RunStore(root / "runs")
            gate_manifest = run_store.create_run(
                cwd=repo,
                user_task="Gate task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            gate_manifest.status = "APPROVED"
            gate_manifest.requires_restart = True
            gate_manifest.restart_reason = "Runtime changed"
            gate_manifest.restart_paths = ["src/c_orch/runtime.py"]
            run_store.save(gate_manifest)
            queue_store.update_task(
                queue,
                "gate-task",
                status="APPROVED",
                active_run_id=gate_manifest.run_id,
                run_ids=[gate_manifest.run_id],
            )
            queue_store.save(queue)

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=None,
                runtime_generation="test-generation",
            )

            restart_gate = payload["runtime"]["restart_gate"]
            self.assertTrue(restart_gate["active"])
            self.assertEqual(restart_gate["waiting_for"], "restart")
            self.assertEqual(restart_gate["task_ids"], ["gate-task"])
            self.assertEqual(restart_gate["run_ids"], [gate_manifest.run_id])
            self.assertEqual(restart_gate["items"][0]["task_id"], "gate-task")
            self.assertEqual(restart_gate["items"][0]["run_id"], gate_manifest.run_id)

    def test_state_payload_restart_drain_blocks_unsafe_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "gate-task", "title": "Gate task", "prompt": "Gate task", "cwd": str(repo)}]
            )
            run_store = RunStore(root / "runs")
            gate_manifest = run_store.create_run(
                cwd=repo,
                user_task="Gate task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            gate_manifest.status = "APPROVED"
            gate_manifest.requires_restart = True
            run_store.save(gate_manifest)
            queue_store.update_task(
                queue,
                "gate-task",
                status="APPROVED",
                active_run_id=gate_manifest.run_id,
                run_ids=[gate_manifest.run_id],
            )
            queue_store.save(queue)

            RunnerLeaseStore(run_store.runner_leases_path(gate_manifest.run_id)).create(
                runtime_generation="runtime-1",
                run_id=gate_manifest.run_id,
                phase="worker_implement",
                task_id=gate_manifest.task_id,
                proposal_id=gate_manifest.proposal_id,
                process_hint="runtime:123",
                pid=123,
                checkpoint={"phase_boundary": "worker_call_inflight", "run_status": "WORKING"},
            )

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=None,
                runtime_generation="test-generation",
            )

            restart_drain = payload["runtime"]["restart_drain"]
            self.assertEqual(restart_drain["stage"], "blocked")
            self.assertFalse(restart_drain["can_restart"])
            self.assertEqual(restart_drain["unsafe_active_runner_lease_count"], 1)
            self.assertIn("worker_implement", restart_drain["blocking_reasons"][0])

    def test_state_payload_restart_drain_blocks_active_external_code_review_until_result_imported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "gate-task", "title": "Gate task", "prompt": "Gate task", "cwd": str(repo)}]
            )
            run_store = RunStore(root / "runs")
            gate_manifest = run_store.create_run(
                cwd=repo,
                user_task="Gate task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            gate_manifest.status = "APPROVED"
            gate_manifest.requires_restart = True
            run_store.save(gate_manifest)
            queue_store.update_task(
                queue,
                "gate-task",
                status="APPROVED",
                active_run_id=gate_manifest.run_id,
                run_ids=[gate_manifest.run_id],
            )
            queue_store.save(queue)

            RunnerLeaseStore(run_store.runner_leases_path(gate_manifest.run_id)).create(
                runtime_generation="runtime-1",
                run_id=gate_manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=gate_manifest.task_id,
                proposal_id=gate_manifest.proposal_id,
                process_hint="pid:456",
                pid=456,
                checkpoint={
                    "phase_boundary": "runner_subprocess_started",
                    "runner_phase": RUNNER_PHASE_CODE_REVIEW,
                    "request_path": "runs/request.json",
                    "result_path": "runs/result.json",
                    "pid": 456,
                },
            )

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=None,
                runtime_generation="test-generation",
            )

            restart_drain = payload["runtime"]["restart_drain"]
            self.assertEqual(restart_drain["stage"], "blocked")
            self.assertFalse(restart_drain["can_restart"])
            self.assertEqual(restart_drain["safe_external_runner_lease_count"], 0)
            self.assertEqual(restart_drain["unsafe_active_runner_lease_count"], 1)
            self.assertIn("waiting for completed result manifest import", restart_drain["blocking_reasons"][0])

    def test_state_payload_restart_drain_blocks_code_review_lease_without_checkpoint_process_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir(parents=True, exist_ok=True)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "gate-task", "title": "Gate task", "prompt": "Gate task", "cwd": str(repo)}]
            )
            run_store = RunStore(root / "runs")
            gate_manifest = run_store.create_run(
                cwd=repo,
                user_task="Gate task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            gate_manifest.status = "APPROVED"
            gate_manifest.requires_restart = True
            run_store.save(gate_manifest)
            queue_store.update_task(
                queue,
                "gate-task",
                status="APPROVED",
                active_run_id=gate_manifest.run_id,
                run_ids=[gate_manifest.run_id],
            )
            queue_store.save(queue)

            # Only top-level process hint exists; subprocess checkpoint metadata is incomplete.
            RunnerLeaseStore(run_store.runner_leases_path(gate_manifest.run_id)).create(
                runtime_generation="runtime-1",
                run_id=gate_manifest.run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                task_id=gate_manifest.task_id,
                proposal_id=gate_manifest.proposal_id,
                process_hint="runtime:123",
                pid=123,
                checkpoint={
                    "phase_boundary": "runner_subprocess_started",
                    "runner_phase": RUNNER_PHASE_CODE_REVIEW,
                    "request_path": "runs/request.json",
                    "result_path": "runs/result.json",
                },
            )

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=None,
                runtime_generation="test-generation",
            )

            restart_drain = payload["runtime"]["restart_drain"]
            self.assertEqual(restart_drain["stage"], "blocked")
            self.assertFalse(restart_drain["can_restart"])
            self.assertEqual(restart_drain["safe_external_runner_lease_count"], 0)
            self.assertEqual(restart_drain["unsafe_active_runner_lease_count"], 1)
            self.assertIn("missing request/result/process metadata", restart_drain["blocking_reasons"][0])

    def test_state_payload_includes_telemetry_without_writing_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-1", "title": "Task 1", "prompt": "Do task", "cwd": str(root)}]
            )
            queue_store.save(queue)

            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal_store.add_proposal(pool, title="P1", prompt="Do plan", cwd=str(root))
            proposal_store.save(pool)

            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "APPROVED"
            run_store.save(manifest, touch=False)
            run_store.append_event(manifest.run_id, "worker_done", "Worker done")
            run_store.append_usage_attribution(
                manifest.run_id,
                role="planner",
                phase="plan",
                started_at="2026-05-19T10:00:00+00:00",
                updated_at="2026-05-19T10:01:00+00:00",
            )
            RunnerLeaseStore(run_store.runner_leases_path(manifest.run_id)).create(
                runtime_generation="runtime-1",
                run_id=manifest.run_id,
                phase="planner_plan",
                task_id=manifest.task_id,
                proposal_id=manifest.proposal_id,
                process_hint="pid:123",
                pid=123,
                checkpoint={"run_status": "PLANNING"},
            )

            manifest_path = root / "runs" / manifest.run_id / "manifest.json"
            events_path = root / "runs" / manifest.run_id / "events.jsonl"
            usage_path = root / "runs" / manifest.run_id / "usage-attribution.jsonl"
            leases_path = root / "runs" / manifest.run_id / "runner-leases.json"
            before = {
                "queue": (root / "queue.json").stat().st_mtime_ns,
                "proposals": (root / "proposals.json").stat().st_mtime_ns,
                "manifest": manifest_path.stat().st_mtime_ns,
                "events": events_path.stat().st_mtime_ns,
                "usage": usage_path.stat().st_mtime_ns,
                "leases": leases_path.stat().st_mtime_ns,
            }

            payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                runtime_generation="test-generation",
            )

            self.assertIn("telemetry", payload)
            self.assertEqual(payload["telemetry"]["summary"]["total_runs"], 1)
            self.assertIn("retrospective", payload["telemetry"])
            self.assertIn("recent_bottlenecks", payload["telemetry"])
            self.assertEqual(payload["telemetry"]["retrospective"]["recent_limit"], 5)
            self.assertEqual(
                payload["telemetry"]["recent_bottlenecks"],
                payload["telemetry"]["retrospective"]["top_bottlenecks"],
            )

            after = {
                "queue": (root / "queue.json").stat().st_mtime_ns,
                "proposals": (root / "proposals.json").stat().st_mtime_ns,
                "manifest": manifest_path.stat().st_mtime_ns,
                "events": events_path.stat().st_mtime_ns,
                "usage": usage_path.stat().st_mtime_ns,
                "leases": leases_path.stat().st_mtime_ns,
            }
            self.assertEqual(before, after)

    def test_state_and_run_payload_include_runner_lease_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            now = datetime.now(timezone.utc)
            active_manifest = store.create_run(
                cwd=root,
                user_task="Task A",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            stale_manifest = store.create_run(
                cwd=root,
                user_task="Task B",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            active_leases = RunnerLeaseStore(store.runner_leases_path(active_manifest.run_id))
            active_leases.create(
                runtime_generation="runtime-1",
                run_id=active_manifest.run_id,
                phase="worker_implement",
                task_id=active_manifest.task_id,
                proposal_id=active_manifest.proposal_id,
                process_hint="pid:123",
                pid=123,
                checkpoint={"run_status": "WORKING"},
                now=now,
                lease_ttl_seconds=3600,
            )
            stale_leases = RunnerLeaseStore(store.runner_leases_path(stale_manifest.run_id))
            stale_leases.create(
                runtime_generation="runtime-1",
                run_id=stale_manifest.run_id,
                phase="planner_review",
                task_id=stale_manifest.task_id,
                proposal_id=stale_manifest.proposal_id,
                process_hint="pid:123",
                pid=123,
                checkpoint={"run_status": "REVIEWING"},
                now=now - timedelta(hours=2),
                lease_ttl_seconds=60,
            )

            state_payload = dashboard_payloads.build_state_payload(
                runs_dir=root / "runs",
                queue_path=None,
                proposals_path=None,
                runtime_generation="runtime-1",
            )
            runtime_leases = state_payload["runtime"]["runner_leases"]
            self.assertEqual(runtime_leases["summary"]["total"], 2)
            self.assertEqual(runtime_leases["summary"]["active"], 1)
            self.assertEqual(runtime_leases["summary"]["stale"], 1)

            run_payload = dashboard_payloads.build_run_payload(root / "runs", stale_manifest.run_id)
            assert run_payload is not None
            self.assertEqual(run_payload["runner_leases"]["summary"]["stale"], 1)
            self.assertEqual(run_payload["runner_leases"]["leases"][0]["phase"], "planner_review")
            self.assertIn("lease_expires_at", run_payload["runner_leases"]["leases"][0])

    def test_run_payload_service_tiers_uses_manifest_reviewer_tier_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                planner_service_tier="fast",
                worker_service_tier="fast",
                reviewer_service_tier="flex",
            )
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="APPROVED",
                    started_at="2026-05-18T12:00:00+00:00",
                    completed_at="2026-05-18T12:01:00+00:00",
                    service_tier="fast",
                )
            ]
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            tiers = payload["run"]["service_tiers"]
            self.assertEqual(tiers["planner"], "fast")
            self.assertEqual(tiers["worker"], "fast")
            self.assertEqual(tiers["reviewer"], "flex")
            self.assertEqual(tiers["reviewer_source"], "manifest")

    def test_run_payload_service_tiers_fallbacks_reviewer_tier_to_latest_review_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
                planner_service_tier="flex",
                worker_service_tier="flex",
                reviewer_service_tier=None,
            )
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-18T11:00:00+00:00",
                    completed_at="2026-05-18T11:01:00+00:00",
                    service_tier="fast",
                ),
                ReviewAttemptRecord(
                    id="review-2",
                    worker_id="worker-1",
                    status="APPROVED",
                    started_at="2026-05-18T11:02:00+00:00",
                    completed_at="2026-05-18T11:03:00+00:00",
                    service_tier="flex",
                ),
            ]
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            tiers = payload["run"]["service_tiers"]
            self.assertEqual(tiers["planner"], "flex")
            self.assertEqual(tiers["worker"], "flex")
            self.assertEqual(tiers["reviewer"], "flex")
            self.assertEqual(tiers["reviewer_source"], "review_attempt")

    def test_run_payload_activity_summary_for_working_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_root = root / "sessions"
            review_output = root / "codex-review-output.txt"
            review_output.write_text("Codex review clean.\nNo findings.", encoding="utf-8")
            verification_output = root / "verification-output.txt"
            verification_output.write_text("pytest passed.\n2 tests.", encoding="utf-8")

            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORKING"
            manifest.planner.thread_id = "planner-thread"
            manifest.workers[0].thread_id = "worker-thread"
            manifest.workers[0].evidence_files = [str(verification_output)]
            manifest.review = ReviewRecord(evidence_files=[str(review_output)])
            store.save(manifest)
            store.append_event(manifest.run_id, "worker_started", "Worker started")
            store.append_event(
                manifest.run_id,
                "verification_finished",
                "Verification finished",
                summary="All verification commands passed.",
            )
            _write_jsonl(
                sessions_root / "2026" / "05" / "19" / "rollout-worker-thread.jsonl",
                [
                    _session_meta("2026-05-19T10:00:00Z", "worker-thread"),
                    _session_tool_call("2026-05-19T10:00:05Z", "exec_command", "pytest -q"),
                ],
            )
            _write_jsonl(
                sessions_root / "2026" / "05" / "19" / "rollout-planner-thread.jsonl",
                [
                    _session_meta("2026-05-19T10:00:01Z", "planner-thread"),
                    _session_tool_output("2026-05-19T10:00:06Z", '{"status":"ok"}'),
                ],
            )

            payload = dashboard_payloads.build_run_payload(
                root / "runs",
                manifest.run_id,
                session_logs_root=sessions_root,
            )

            assert payload is not None
            summary = payload["activity_summary"]
            self.assertEqual(summary["current_phase"]["status"], "WORKING")
            self.assertIsNotNone(summary["latest_activity"])
            self.assertEqual(summary["latest_verification"]["kind"], "verification")
            self.assertIn(summary["latest_tool"]["kind"], {"tool_call", "tool_output"})
            self.assertGreaterEqual(len(summary["items"]), 4)
            self.assertEqual(summary["items"][0]["source"], "session_log")

    def test_run_payload_activity_summary_for_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "FAILED"
            store.save(manifest)
            store.append_event(
                manifest.run_id,
                "verification_gate_failed",
                "Verification failed",
                summary="1 command failed",
                reason="verification_failed",
            )

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            summary = payload["activity_summary"]
            self.assertEqual(summary["current_phase"]["status"], "FAILED")
            self.assertEqual(summary["latest_activity"]["kind"], "verification")
            self.assertEqual(summary["latest_activity"]["severity"], "error")
            self.assertEqual(summary["latest_verification"]["kind"], "verification")

    def test_run_payload_activity_summary_for_revision_requested_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "REVISION_REQUESTED"
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-19T10:10:00+00:00",
                    completed_at="2026-05-19T10:11:00+00:00",
                    decision="revision_requested",
                    reason="Need missing failure-path tests.",
                    summary="Need missing failure-path tests.",
                    worker_attempt=1,
                )
            ]
            manifest.review = ReviewRecord(
                decision="revision_requested",
                reason="Need missing failure-path tests.",
                summary="Need missing failure-path tests.",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            summary = payload["activity_summary"]
            self.assertEqual(summary["current_phase"]["status"], "REVISION_REQUESTED")
            self.assertEqual(
                summary["latest_revision_request"]["summary"],
                "Need missing failure-path tests.",
            )
            self.assertTrue(any(item["kind"] == "revision" for item in summary["items"]))

    def test_run_payload_activity_summary_for_approved_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            verification_output = root / "verification-output.txt"
            verification_output.write_text("all checks passed", encoding="utf-8")
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "APPROVED"
            manifest.workers[0].evidence_files = [str(verification_output)]
            store.save(manifest)
            store.append_event(manifest.run_id, "apply_completed", "Apply completed", applied=True)
            store.append_event(
                manifest.run_id,
                "git_commit_completed",
                "Git commit completed",
                summary="Committed applied changes.",
            )
            store.append_event(
                manifest.run_id,
                "run_terminal_status",
                "Run finished with APPROVED",
                status="APPROVED",
            )

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            summary = payload["activity_summary"]
            self.assertEqual(summary["current_phase"]["status"], "APPROVED")
            self.assertEqual(summary["current_phase"]["waiting_for"], "done")
            self.assertIsNotNone(summary["latest_activity"])
            self.assertEqual(summary["latest_verification"]["kind"], "verification")
            self.assertTrue(
                any(item["kind"] in {"apply", "commit"} for item in summary["items"]),
            )

    def test_run_payload_activity_summary_keeps_latest_verification_when_session_items_exceed_display_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_root = root / "sessions"
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORKING"
            manifest.planner.thread_id = "planner-thread"
            manifest.workers[0].thread_id = "worker-thread"
            store.save(manifest)
            store.append_event(
                manifest.run_id,
                "verification_finished",
                "Verification finished",
                summary="Verification completed successfully.",
            )
            planner_records = [_session_meta(_iso_timestamp(0), "planner-thread")]
            worker_records = [_session_meta(_iso_timestamp(0), "worker-thread")]
            for index in range(40):
                planner_records.append(
                    _session_tool_call(
                        _iso_timestamp(1 + index),
                        f"planner_tool_{index}",
                        "{}",
                    )
                )
                worker_records.append(
                    _session_tool_call(
                        _iso_timestamp(41 + index),
                        f"worker_tool_{index}",
                        "{}",
                    )
                )
            _write_jsonl(
                sessions_root / "2026" / "05" / "19" / "rollout-planner-thread.jsonl",
                planner_records,
            )
            _write_jsonl(
                sessions_root / "2026" / "05" / "19" / "rollout-worker-thread.jsonl",
                worker_records,
            )

            payload = dashboard_payloads.build_run_payload(
                root / "runs",
                manifest.run_id,
                session_logs_root=sessions_root,
            )

            assert payload is not None
            summary = payload["activity_summary"]
            self.assertEqual(len(summary["items"]), 80)
            self.assertIsNotNone(summary["latest_verification"])
            self.assertEqual(summary["latest_verification"]["kind"], "verification")

    def test_run_payload_approved_run_with_historical_review_failure_keeps_history_but_not_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "APPROVED"
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-19T10:00:00+00:00",
                    completed_at="2026-05-19T10:01:00+00:00",
                    reason="code_review_error",
                    error="review timeout",
                ),
                ReviewAttemptRecord(
                    id="review-2",
                    worker_id="worker-1",
                    status="APPROVED",
                    started_at="2026-05-19T10:02:00+00:00",
                    completed_at="2026-05-19T10:03:00+00:00",
                    decision="accepted",
                ),
            ]
            manifest.review = ReviewRecord(
                decision="accepted",
                reason="Looks good.",
            )
            store.save(manifest)

            payload = dashboard_payloads.build_run_payload(root / "runs", manifest.run_id)

            assert payload is not None
            run = payload["run"]
            self.assertFalse(run["can_retry_review"])
            self.assertEqual(run["allowed_actions"], [])
            self.assertIsNotNone(payload["activity_summary"]["latest_review_failure"])


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _session_meta(timestamp: str, thread_id: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": "/repo",
            "source": "mcp",
        },
    }


def _session_tool_call(timestamp: str, name: str, arguments: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": name,
            "arguments": arguments,
        },
    }


def _session_tool_output(timestamp: str, output: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "response_item",
        "payload": {
            "type": "function_call_output",
            "output": output,
        },
    }


def _iso_timestamp(minutes: int) -> str:
    base = datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


if __name__ == "__main__":
    unittest.main()
