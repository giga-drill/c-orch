from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch.phase_timing import record_run_status_transition
from c_orch.run_store import ReviewAttemptRecord, RunStore
from c_orch.telemetry import build_project_telemetry


class ProjectTelemetryTests(unittest.TestCase):
    def test_empty_runs_dir_has_no_bottlenecks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = build_project_telemetry(root / "runs")
            self.assertEqual(payload["summary"]["total_runs"], 0)
            self.assertEqual(payload["bottlenecks"], [])

    def test_aggregates_success_run_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.created_at = "2026-05-19T10:00:00+00:00"
            manifest.updated_at = "2026-05-19T10:12:00+00:00"
            record_run_status_transition(manifest, "PLANNING", "2026-05-19T10:00:00+00:00")
            record_run_status_transition(manifest, "PLAN_REVIEW_REQUIRED", "2026-05-19T10:01:00+00:00")
            record_run_status_transition(manifest, "PLAN_APPROVED", "2026-05-19T10:02:00+00:00")
            record_run_status_transition(manifest, "WORKING", "2026-05-19T10:03:00+00:00")
            record_run_status_transition(manifest, "WORK_DONE", "2026-05-19T10:07:00+00:00")
            record_run_status_transition(manifest, "REVIEWING", "2026-05-19T10:08:00+00:00")
            record_run_status_transition(manifest, "APPROVED", "2026-05-19T10:12:00+00:00")
            store.save(manifest, touch=False)

            store.append_event(
                manifest.run_id,
                "evidence_collected",
                "Evidence collected",
                timestamp="2026-05-19T10:07:30+00:00",
            )
            store.append_event(
                manifest.run_id,
                "verification_finished",
                "Verification finished",
                timestamp="2026-05-19T10:08:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "apply_started",
                "Apply started",
                timestamp="2026-05-19T10:10:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "apply_completed",
                "Apply completed",
                timestamp="2026-05-19T10:10:20+00:00",
            )
            store.append_event(
                manifest.run_id,
                "git_commit_started",
                "Commit started",
                timestamp="2026-05-19T10:10:30+00:00",
            )
            store.append_event(
                manifest.run_id,
                "git_commit_completed",
                "Commit completed",
                timestamp="2026-05-19T10:10:50+00:00",
            )

            store.append_usage_attribution(
                manifest.run_id,
                role="planner",
                phase="review",
                started_at="2026-05-19T10:08:00+00:00",
                updated_at="2026-05-19T10:09:00+00:00",
            )
            store.append_usage_attribution(
                manifest.run_id,
                role="reviewer",
                phase="review",
                started_at="2026-05-19T10:09:00+00:00",
                updated_at="2026-05-19T10:10:00+00:00",
            )

            payload = build_project_telemetry(root / "runs")
            stage_map = {stage["category"]: stage for stage in payload["stages"]}

            self.assertEqual(payload["summary"]["total_runs"], 1)
            self.assertEqual(payload["summary"]["approved_runs"], 1)
            self.assertEqual(payload["summary"]["failed_runs"], 0)
            self.assertEqual(stage_map["planning"]["status"], "complete")
            self.assertEqual(stage_map["worker_execution"]["status"], "complete")
            self.assertEqual(stage_map["verification"]["count"], 1)
            self.assertEqual(stage_map["codex_review"]["count"], 1)
            self.assertEqual(stage_map["planner_review"]["count"], 1)
            self.assertGreater(stage_map["apply_commit"]["total_duration_seconds"], 0)

    def test_aggregates_rework_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.created_at = "2026-05-19T11:00:00+00:00"
            manifest.updated_at = "2026-05-19T11:20:00+00:00"
            record_run_status_transition(manifest, "WORKING", "2026-05-19T11:00:00+00:00")
            record_run_status_transition(manifest, "WORK_DONE", "2026-05-19T11:02:00+00:00")
            record_run_status_transition(manifest, "REVIEWING", "2026-05-19T11:03:00+00:00")
            record_run_status_transition(manifest, "REVISION_REQUESTED", "2026-05-19T11:04:00+00:00")
            record_run_status_transition(manifest, "WORKING", "2026-05-19T11:05:00+00:00")
            record_run_status_transition(manifest, "WORK_DONE", "2026-05-19T11:08:00+00:00")
            record_run_status_transition(manifest, "REVIEWING", "2026-05-19T11:09:00+00:00")
            record_run_status_transition(manifest, "APPROVED", "2026-05-19T11:20:00+00:00")
            manifest.review_attempts = [
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-19T11:03:00+00:00",
                    completed_at="2026-05-19T11:04:00+00:00",
                    reason="planner_review_failed",
                    error="timed out",
                )
            ]
            store.save(manifest, touch=False)

            store.append_event(
                manifest.run_id,
                "recovery_decision_recorded",
                "Recovery",
                timestamp="2026-05-19T11:04:00+00:00",
                category="transient_infrastructure",
                recovery_action="retry_review",
            )
            store.append_event(
                manifest.run_id,
                "worker_start",
                "Worker start",
                timestamp="2026-05-19T11:05:00+00:00",
            )

            store.append_usage_attribution(
                manifest.run_id,
                role="worker",
                phase="implement",
                started_at="2026-05-19T11:00:00+00:00",
                updated_at="2026-05-19T11:02:00+00:00",
            )
            store.append_usage_attribution(
                manifest.run_id,
                role="worker",
                phase="rework",
                started_at="2026-05-19T11:05:00+00:00",
                updated_at="2026-05-19T11:08:00+00:00",
            )

            payload = build_project_telemetry(root / "runs")
            stage_map = {stage["category"]: stage for stage in payload["stages"]}

            self.assertEqual(payload["summary"]["rework_count"], 1)
            self.assertEqual(payload["summary"]["review_retry_count"], 1)
            self.assertEqual(stage_map["rework"]["count"], 1)
            self.assertEqual(stage_map["recovery"]["count"], 1)
            self.assertEqual(stage_map["recovery"]["status"], "complete")

    def test_marks_legacy_data_as_partial_or_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            manifest.status = "WORKING"
            manifest.timing = None
            manifest.created_at = "2026-05-19T12:00:00+00:00"
            manifest.updated_at = "2026-05-19T12:05:00+00:00"
            store.save(manifest, touch=False)

            store.append_event(
                manifest.run_id,
                "verification_finished",
                "Verification finished",
                timestamp="2026-05-19T12:03:00+00:00",
            )

            payload = build_project_telemetry(root / "runs")
            stage_map = {stage["category"]: stage for stage in payload["stages"]}

            self.assertEqual(stage_map["verification"]["status"], "partial")
            self.assertEqual(stage_map["codex_review"]["status"], "unavailable")
            self.assertEqual(stage_map["planner_review"]["status"], "unavailable")
            self.assertTrue(payload["limitations"])

    def test_legacy_timing_uses_legacy_event_sources_for_planning_and_worker(self) -> None:
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
            manifest.timing = None
            manifest.created_at = "2026-05-19T13:00:00+00:00"
            manifest.updated_at = "2026-05-19T13:06:00+00:00"
            store.save(manifest, touch=False)

            store.append_event(
                manifest.run_id,
                "planner_start",
                "Planner started",
                timestamp="2026-05-19T13:00:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "planner_plan_ready",
                "Planner plan ready",
                timestamp="2026-05-19T13:01:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "plan_review_required",
                "Plan review required",
                timestamp="2026-05-19T13:02:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "worker_start",
                "Worker started",
                timestamp="2026-05-19T13:03:00+00:00",
            )
            store.append_event(
                manifest.run_id,
                "worker_done",
                "Worker done",
                timestamp="2026-05-19T13:05:00+00:00",
            )

            payload = build_project_telemetry(root / "runs")
            stage_map = {stage["category"]: stage for stage in payload["stages"]}

            planning_sources = stage_map["planning"]["sources"]
            worker_sources = stage_map["worker_execution"]["sources"]
            self.assertIn("legacy_event_timing", planning_sources)
            self.assertIn("legacy_event_timing", worker_sources)
            self.assertNotIn("manifest_timing", planning_sources)
            self.assertNotIn("manifest_timing", worker_sources)

    def test_all_unavailable_stages_have_no_bottlenecks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Task",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            # Keep the run in NEW with empty timing/events/usage to force unavailable metrics.
            manifest.status = "NEW"
            store.save(manifest, touch=False)

            payload = build_project_telemetry(root / "runs")

            self.assertEqual(payload["summary"]["total_runs"], 1)
            self.assertEqual(payload["bottlenecks"], [])


if __name__ == "__main__":
    unittest.main()
