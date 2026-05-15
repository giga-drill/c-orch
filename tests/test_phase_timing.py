from __future__ import annotations

import unittest
from types import SimpleNamespace

from c_orch.phase_timing import build_timing_summary, record_run_status_transition


class PhaseTimingTests(unittest.TestCase):
    def test_persisted_timing_summary_for_completed_run(self) -> None:
        manifest = SimpleNamespace(
            status="NEW",
            timing={"version": 1, "segments": []},
            created_at="2026-05-14T10:00:00+00:00",
            updated_at="2026-05-14T10:09:00+00:00",
        )
        record_run_status_transition(manifest, "PLANNING", "2026-05-14T10:00:00+00:00")
        record_run_status_transition(manifest, "PLAN_REVIEW_REQUIRED", "2026-05-14T10:01:00+00:00")
        record_run_status_transition(manifest, "PLAN_APPROVED", "2026-05-14T10:02:00+00:00")
        record_run_status_transition(
            manifest,
            "WORKING",
            "2026-05-14T10:03:00+00:00",
            metadata={"worker_attempt": 1},
        )
        record_run_status_transition(manifest, "WORK_DONE", "2026-05-14T10:05:00+00:00")
        record_run_status_transition(manifest, "REVIEWING", "2026-05-14T10:06:00+00:00")
        record_run_status_transition(manifest, "APPROVED", "2026-05-14T10:09:00+00:00")

        summary = build_timing_summary(manifest.__dict__, [], now_iso="2026-05-14T10:09:00+00:00")
        phase_map = summary["phase_aggregates"]
        self.assertEqual(summary["source"], "manifest")
        self.assertEqual(summary["total"]["status"], "completed")
        self.assertEqual(phase_map["planning"]["count"], 1)
        self.assertEqual(phase_map["human_plan_review_wait"]["count"], 1)
        self.assertEqual(phase_map["worker_execution"]["count"], 1)
        self.assertEqual(phase_map["planner_review"]["count"], 1)
        self.assertEqual(phase_map["revision_wait"]["status"], "missing")

    def test_rework_keeps_multiple_worker_and_review_segments(self) -> None:
        manifest = SimpleNamespace(
            status="NEW",
            timing={"version": 1, "segments": []},
            created_at="2026-05-14T11:00:00+00:00",
            updated_at="2026-05-14T11:10:00+00:00",
        )
        record_run_status_transition(manifest, "WORKING", "2026-05-14T11:00:00+00:00")
        record_run_status_transition(manifest, "WORK_DONE", "2026-05-14T11:01:00+00:00")
        record_run_status_transition(manifest, "REVIEWING", "2026-05-14T11:02:00+00:00")
        record_run_status_transition(manifest, "REVISION_REQUESTED", "2026-05-14T11:03:00+00:00")
        record_run_status_transition(manifest, "WORKING", "2026-05-14T11:04:00+00:00")
        record_run_status_transition(manifest, "WORK_DONE", "2026-05-14T11:06:00+00:00")
        record_run_status_transition(manifest, "REVIEWING", "2026-05-14T11:07:00+00:00")
        record_run_status_transition(manifest, "APPROVED", "2026-05-14T11:10:00+00:00")

        summary = build_timing_summary(manifest.__dict__, [], now_iso="2026-05-14T11:10:00+00:00")
        phase_map = summary["phase_aggregates"]
        self.assertEqual(phase_map["worker_execution"]["count"], 2)
        self.assertEqual(phase_map["planner_review"]["count"], 2)
        self.assertEqual(phase_map["revision_wait"]["count"], 1)
        self.assertGreaterEqual(phase_map["worker_execution"]["total_duration_seconds"], 3 * 60)

    def test_failure_closes_active_phase(self) -> None:
        manifest = SimpleNamespace(
            status="NEW",
            timing={"version": 1, "segments": []},
            created_at="2026-05-14T12:00:00+00:00",
            updated_at="2026-05-14T12:05:00+00:00",
        )
        record_run_status_transition(manifest, "PLANNING", "2026-05-14T12:00:00+00:00")
        record_run_status_transition(manifest, "PLAN_REVIEW_REQUIRED", "2026-05-14T12:01:00+00:00")
        record_run_status_transition(manifest, "FAILED", "2026-05-14T12:05:00+00:00")

        summary = build_timing_summary(manifest.__dict__, [], now_iso="2026-05-14T12:05:00+00:00")
        self.assertEqual(summary["phase_aggregates"]["human_plan_review_wait"]["status"], "completed")
        self.assertEqual(summary["total"]["status"], "completed")
        self.assertEqual(summary["total"]["duration_seconds"], 5 * 60)

    def test_legacy_fallback_from_events(self) -> None:
        manifest = {
            "run_id": "legacy-run",
            "status": "PLAN_REVIEW_REQUIRED",
            "created_at": "2026-05-14T13:00:00+00:00",
            "updated_at": "2026-05-14T13:02:00+00:00",
        }
        events = [
            {
                "timestamp": "2026-05-14T13:00:00+00:00",
                "type": "planner_start",
                "message": "Planner started",
            },
            {
                "timestamp": "2026-05-14T13:01:00+00:00",
                "type": "planner_plan_ready",
                "message": "Planner plan ready",
            },
            {
                "timestamp": "2026-05-14T13:01:30+00:00",
                "type": "plan_review_required",
                "message": "Human plan review required",
            },
        ]
        summary = build_timing_summary(manifest, events, now_iso="2026-05-14T13:02:00+00:00")
        self.assertEqual(summary["source"], "legacy_fallback")
        self.assertEqual(summary["phase_aggregates"]["planning"]["count"], 1)
        self.assertEqual(summary["phase_aggregates"]["human_plan_review_wait"]["status"], "active")
        self.assertEqual(summary["phase_aggregates"]["worker_execution"]["status"], "missing")

    def test_legacy_fallback_without_events_marks_partial(self) -> None:
        manifest = {
            "run_id": "legacy-run-2",
            "status": "WORKING",
            "created_at": "2026-05-14T14:00:00+00:00",
            "updated_at": "2026-05-14T14:03:00+00:00",
        }
        summary = build_timing_summary(manifest, [], now_iso="2026-05-14T14:04:00+00:00")
        self.assertEqual(summary["source"], "legacy_fallback")
        self.assertEqual(summary["phase_aggregates"]["worker_execution"]["count"], 1)
        self.assertEqual(summary["phase_aggregates"]["worker_execution"]["status"], "partial")


if __name__ == "__main__":
    unittest.main()
