from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from c_orch.runtime import build_queue_payload, build_run_payload, build_runs_payload, run_action, task_action
from c_orch.run_store import PlanRecord, PlanRevisionRecord, ReviewRecord, RunStore
from c_orch.task_store import TaskStore
from c_orch.ui import INDEX_HTML


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
            evidence_path.parent.mkdir(parents=True)
            evidence_path.write_text("diff\n", encoding="utf-8")
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
            manifest.workers[0].evidence_files = [str(evidence_path)]
            manifest.workers[0].reasoning_effort = "medium"
            manifest.workers[0].service_tier = "flex"
            manifest.requires_restart = True
            manifest.restart_reason = "Touched runtime code"
            manifest.restart_paths = ["src/c_orch/orchestrator.py"]
            manifest.review = ReviewRecord(
                decision="accepted",
                reason="looks good",
                evidence_files=[str(evidence_path)],
            )
            store.save(manifest)
            store.append_event(
                manifest.run_id,
                "planner_start",
                "Planner started",
                attempt=1,
            )

            payload = build_run_payload(root / "runs", manifest.run_id)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["manifest"]["run_id"], manifest.run_id)
            self.assertEqual(payload["run"]["review"]["decision"], "accepted")
            self.assertEqual(payload["run"]["planner"]["reasoning_effort"], "high")
            self.assertEqual(payload["run"]["plan"]["approval_status"], "approved")
            self.assertEqual(payload["manifest"]["plan"]["worker_prompt"], "Build the feature")
            self.assertEqual(payload["run"]["workers"][0]["service_tier"], "flex")
            self.assertEqual(payload["run"]["review_attempt_count"], 0)
            self.assertEqual(payload["run"]["evidence_count"], 1)
            self.assertEqual(payload["run"]["waiting_for"], "restart")
            self.assertEqual(payload["run"]["next_action"], "restart")
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
            self.assertEqual(payload["events"][0]["type"], "planner_start")
            self.assertEqual(payload["events"][0]["attempt"], 1)

    def test_build_run_payload_rejects_path_traversal_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_run_payload(Path(tmp) / "runs", "../outside"))

    def test_build_queue_payload_includes_task_run_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue_store = TaskStore(root / "queue.json")
            queue = queue_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
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
            self.assertEqual(payload["tasks"][0]["active_run_id"], "run-123")
            self.assertEqual(payload["tasks"][0]["run_ids"], ["run-123"])
            self.assertEqual(payload["tasks"][0]["reason"], "done")
            self.assertEqual(payload["tasks"][0]["error"], "old error")
            self.assertEqual(payload["tasks"][0]["waiting_for"], "done")
            self.assertEqual(payload["tasks"][0]["next_action"], "done")

    def test_build_queue_payload_reconciles_failed_active_run(self) -> None:
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
            self.assertEqual(payload["summary"]["current_waiting_point"], "retry_task")
            self.assertEqual(payload["summary"]["completed_tasks"], 0)
            self.assertEqual(payload["summary"]["running_tasks"], 0)
            self.assertEqual(loaded.status, "FAILED")
            self.assertEqual(loaded.tasks[0].status, "FAILED")

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

    def test_index_html_contains_dashboard_mount_points(self) -> None:
        self.assertIn('id="runList"', INDEX_HTML)
        self.assertIn("/api/runs", INDEX_HTML)
        self.assertIn("/api/queue", INDEX_HTML)
        self.assertIn('id="queueList"', INDEX_HTML)
        self.assertIn('id="queueSummary"', INDEX_HTML)
        self.assertIn("Task Queue", INDEX_HTML)
        self.assertIn("total_tasks", INDEX_HTML)
        self.assertIn("approved_tasks", INDEX_HTML)
        self.assertIn("completed_tasks", INDEX_HTML)
        self.assertIn("pending_tasks", INDEX_HTML)
        self.assertIn("failed_tasks", INDEX_HTML)
        self.assertIn("running_tasks", INDEX_HTML)
        self.assertIn("current_waiting_point", INDEX_HTML)
        self.assertIn("waiting_for", INDEX_HTML)
        self.assertIn("next_action", INDEX_HTML)
        self.assertIn('id="detail"', INDEX_HTML)
        self.assertIn("运行时间线", INDEX_HTML)
        self.assertIn("payload.events", INDEX_HTML)
        self.assertIn("Planner Agent", INDEX_HTML)
        self.assertIn("Worker Agent", INDEX_HTML)
        self.assertIn("plan_revision_count", INDEX_HTML)
        self.assertIn("latest_plan_revision_feedback", INDEX_HTML)
        self.assertIn("reasoning_effort", INDEX_HTML)
        self.assertIn("需要重启", INDEX_HTML)
        self.assertIn("重启原因", INDEX_HTML)
        self.assertIn("影响路径", INDEX_HTML)
        self.assertIn("Worker 指令", INDEX_HTML)
        self.assertIn("Worker 活动", INDEX_HTML)
        self.assertIn("worker_attempt", INDEX_HTML)
        self.assertIn("workspace", INDEX_HTML)
        self.assertIn("通过并启动 Worker", INDEX_HTML)
        self.assertIn("让 Planner 重新生成计划", INDEX_HTML)
        self.assertIn("重新让 Planner 复核", INDEX_HTML)
        self.assertIn("pendingActions", INDEX_HTML)
        self.assertIn("setActionButtonsDisabled", INDEX_HTML)
        self.assertIn("data-run-action=\"approve-plan\"", INDEX_HTML)
        self.assertIn("data-run-action=\"revise-plan\"", INDEX_HTML)
        self.assertIn("data-run-action=\"retry-review\"", INDEX_HTML)

    def test_index_html_contains_timeline_collapse_and_refresh_contracts(self) -> None:
        self.assertIn("newestFirst(events)", INDEX_HTML)
        self.assertIn("items.slice().reverse()", INDEX_HTML)
        self.assertIn("<details><summary>Worker 指令</summary>", INDEX_HTML)
        self.assertIn("<details><summary>验收标准</summary>", INDEX_HTML)
        self.assertIn("<details><summary>验证命令</summary>", INDEX_HTML)
        self.assertIn("<details><summary>风险说明</summary>", INDEX_HTML)
        self.assertIn("<details><summary>证据文件 (", INDEX_HTML)
        self.assertIn("async function refreshAll(options)", INDEX_HTML)
        self.assertIn("await refreshAll({ preferredRunId: runId, forceFirst: false });", INDEX_HTML)
        self.assertIn("const taskSnapshot = findTask(payload, taskId);", INDEX_HTML)
        self.assertIn("let preferredRunId = pickTaskTargetRun(taskSnapshot);", INDEX_HTML)
        self.assertIn("payload.generated_at", INDEX_HTML)
        self.assertIn("queueUpdatedAt", INDEX_HTML)
        self.assertIn("cache: \"no-store\"", INDEX_HTML)

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


if __name__ == "__main__":
    unittest.main()
