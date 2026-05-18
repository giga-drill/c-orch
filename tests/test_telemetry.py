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

    def test_recent_terminal_retrospective_filters_sample_and_includes_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            old_run = store.create_run(
                cwd=root,
                user_task="Old terminal run",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            old_run.created_at = "2026-05-19T09:00:00+00:00"
            old_run.updated_at = "2026-05-19T09:06:00+00:00"
            record_run_status_transition(old_run, "PLANNING", "2026-05-19T09:00:00+00:00")
            record_run_status_transition(old_run, "PLAN_REVIEW_REQUIRED", "2026-05-19T09:01:00+00:00")
            record_run_status_transition(old_run, "WORKING", "2026-05-19T09:02:00+00:00")
            record_run_status_transition(old_run, "WORK_DONE", "2026-05-19T09:04:00+00:00")
            record_run_status_transition(old_run, "APPROVED", "2026-05-19T09:06:00+00:00")
            store.save(old_run, touch=False)

            legacy_terminal = store.create_run(
                cwd=root,
                user_task="Legacy terminal run",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            legacy_terminal.status = "FAILED"
            legacy_terminal.timing = None
            legacy_terminal.created_at = "2026-05-19T10:00:00+00:00"
            legacy_terminal.updated_at = "2026-05-19T10:06:00+00:00"
            store.save(legacy_terminal, touch=False)
            store.append_event(
                legacy_terminal.run_id,
                "planner_start",
                "Planner started",
                timestamp="2026-05-19T10:00:00+00:00",
            )
            store.append_event(
                legacy_terminal.run_id,
                "plan_review_required",
                "Plan review required",
                timestamp="2026-05-19T10:01:00+00:00",
            )
            store.append_event(
                legacy_terminal.run_id,
                "worker_start",
                "Worker started",
                timestamp="2026-05-19T10:02:00+00:00",
            )
            store.append_event(
                legacy_terminal.run_id,
                "worker_done",
                "Worker done",
                timestamp="2026-05-19T10:04:00+00:00",
            )
            store.append_event(
                legacy_terminal.run_id,
                "run_terminal_status",
                "Run failed",
                timestamp="2026-05-19T10:06:00+00:00",
                status="FAILED",
            )

            newest_terminal = store.create_run(
                cwd=root,
                user_task="Newest terminal run",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            newest_terminal.created_at = "2026-05-19T11:00:00+00:00"
            newest_terminal.updated_at = "2026-05-19T11:12:00+00:00"
            record_run_status_transition(newest_terminal, "WORKING", "2026-05-19T11:00:00+00:00")
            record_run_status_transition(newest_terminal, "WORK_DONE", "2026-05-19T11:04:00+00:00")
            record_run_status_transition(newest_terminal, "REVIEWING", "2026-05-19T11:05:00+00:00")
            record_run_status_transition(newest_terminal, "REVISION_REQUESTED", "2026-05-19T11:06:00+00:00")
            record_run_status_transition(newest_terminal, "WORKING", "2026-05-19T11:07:00+00:00")
            record_run_status_transition(newest_terminal, "WORK_DONE", "2026-05-19T11:09:00+00:00")
            record_run_status_transition(newest_terminal, "REVIEWING", "2026-05-19T11:10:00+00:00")
            record_run_status_transition(newest_terminal, "APPROVED", "2026-05-19T11:12:00+00:00")
            newest_terminal.review_attempts = [
                ReviewAttemptRecord(
                    id="review-revision",
                    worker_id="worker-1",
                    status="REVISION_REQUESTED",
                    started_at="2026-05-19T11:05:00+00:00",
                    completed_at="2026-05-19T11:06:00+00:00",
                    decision="revision_requested",
                    reason="缺少错误分支处理。请补齐。",
                ),
                ReviewAttemptRecord(
                    id="review-retryable",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-19T11:10:00+00:00",
                    completed_at="2026-05-19T11:11:00+00:00",
                    error="Codex review timed out after 900s.",
                ),
            ]
            store.save(newest_terminal, touch=False)
            store.append_usage_attribution(
                newest_terminal.run_id,
                role="worker",
                phase="implement",
                started_at="2026-05-19T11:00:00+00:00",
                updated_at="2026-05-19T11:04:00+00:00",
            )
            store.append_usage_attribution(
                newest_terminal.run_id,
                role="worker",
                phase="rework",
                started_at="2026-05-19T11:07:00+00:00",
                updated_at="2026-05-19T11:09:00+00:00",
            )
            store.append_event(
                newest_terminal.run_id,
                "recovery_decision_recorded",
                "Recovery decision",
                timestamp="2026-05-19T11:06:00+00:00",
                reason="planner_review_failed",
                recovery_action="retry_review",
            )

            active_run = store.create_run(
                cwd=root,
                user_task="Active run should be ignored",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            active_run.status = "WORKING"
            active_run.created_at = "2026-05-19T12:00:00+00:00"
            active_run.updated_at = "2026-05-19T12:02:00+00:00"
            store.save(active_run, touch=False)

            payload = build_project_telemetry(root / "runs", recent_limit=2)
            retrospective = payload["retrospective"]

            self.assertEqual(retrospective["recent_limit"], 2)
            self.assertEqual(retrospective["sampled_terminal_runs"], 2)
            self.assertEqual(retrospective["sample_run_ids"], [newest_terminal.run_id, legacy_terminal.run_id])
            self.assertEqual(retrospective["non_terminal_runs_ignored"], 1)
            self.assertEqual(payload["recent_bottlenecks"], retrospective["top_bottlenecks"])

            top = retrospective["top_bottlenecks"]
            self.assertTrue(top)
            first = top[0]
            self.assertIn("share_of_sample_duration", first)
            self.assertIn("coverage", first)
            self.assertIn("sources", first)
            self.assertTrue(first["evidence_runs"])

            legacy_evidence = None
            newest_evidence = None
            for bottleneck in top:
                for evidence in bottleneck["evidence_runs"]:
                    if evidence["run_id"] == legacy_terminal.run_id:
                        legacy_evidence = evidence
                    if evidence["run_id"] == newest_terminal.run_id:
                        newest_evidence = evidence
            self.assertIsNotNone(legacy_evidence)
            self.assertIsNotNone(newest_evidence)
            assert legacy_evidence is not None
            assert newest_evidence is not None
            self.assertIn(
                legacy_evidence["source"],
                {"legacy_event_timing", "timing_inferred", "events_inferred", "none", "usage_attribution"},
            )
            self.assertIn(legacy_evidence["coverage"], {"exact", "partial", "unavailable"})
            self.assertGreaterEqual(newest_evidence["rework_count"], 1)
            self.assertGreaterEqual(newest_evidence["revision_requested_count"], 1)
            self.assertGreaterEqual(newest_evidence["review_retryable_count"], 1)
            self.assertGreaterEqual(newest_evidence["recovery_decision_count"], 1)
            self.assertTrue(newest_evidence["reason_summaries"])

    def test_recent_terminal_sort_uses_created_at_when_updated_at_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            older_with_updated_at = store.create_run(
                cwd=root,
                user_task="Older run with updated_at",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            older_with_updated_at.status = "APPROVED"
            older_with_updated_at.created_at = "2026-05-19T09:00:00+00:00"
            older_with_updated_at.updated_at = "2026-05-19T09:10:00+00:00"
            store.save(older_with_updated_at, touch=False)

            newer_missing_updated_at = store.create_run(
                cwd=root,
                user_task="Newer legacy run without updated_at",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            newer_missing_updated_at.status = "FAILED"
            newer_missing_updated_at.created_at = "2026-05-19T10:00:00+00:00"
            newer_missing_updated_at.updated_at = ""
            newer_missing_updated_at.timing = None
            store.save(newer_missing_updated_at, touch=False)

            payload = build_project_telemetry(root / "runs", recent_limit=1)
            retrospective = payload["retrospective"]

            self.assertEqual(retrospective["sample_size"], 1)
            self.assertEqual(retrospective["sample_run_ids"], [newer_missing_updated_at.run_id])

    def test_recent_share_denominator_uses_run_level_total_without_double_counting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            run = store.create_run(
                cwd=root,
                user_task="Run-level denominator check",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex",
            )
            run.created_at = "2026-05-19T11:00:00+00:00"
            run.updated_at = "2026-05-19T11:12:00+00:00"
            record_run_status_transition(run, "WORKING", "2026-05-19T11:00:00+00:00")
            record_run_status_transition(run, "WORK_DONE", "2026-05-19T11:04:00+00:00")
            record_run_status_transition(run, "REVIEWING", "2026-05-19T11:05:00+00:00")
            record_run_status_transition(run, "REVISION_REQUESTED", "2026-05-19T11:06:00+00:00")
            record_run_status_transition(run, "WORKING", "2026-05-19T11:07:00+00:00")
            record_run_status_transition(run, "WORK_DONE", "2026-05-19T11:09:00+00:00")
            record_run_status_transition(run, "REVIEWING", "2026-05-19T11:10:00+00:00")
            record_run_status_transition(run, "APPROVED", "2026-05-19T11:12:00+00:00")
            store.save(run, touch=False)
            store.append_usage_attribution(
                run.run_id,
                role="worker",
                phase="implement",
                started_at="2026-05-19T11:00:00+00:00",
                updated_at="2026-05-19T11:04:00+00:00",
            )
            store.append_usage_attribution(
                run.run_id,
                role="worker",
                phase="rework",
                started_at="2026-05-19T11:07:00+00:00",
                updated_at="2026-05-19T11:09:00+00:00",
            )

            payload = build_project_telemetry(root / "runs", recent_limit=1)
            retrospective = payload["retrospective"]
            top = retrospective["top_bottlenecks"]

            self.assertEqual(retrospective["share_basis"], "sample_run_total")
            self.assertEqual(retrospective["total_sample_duration_seconds"], 720.0)

            worker_row = next(item for item in top if item["category"] == "worker_execution")
            self.assertEqual(worker_row["total_duration_seconds"], 360.0)
            self.assertAlmostEqual(worker_row["share_of_sample_duration"], 0.5, places=6)


if __name__ == "__main__":
    unittest.main()
