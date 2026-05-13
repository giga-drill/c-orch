from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from c_orch.run_store import (
    PlanRecord,
    PlanRevisionRecord,
    ReviewAttemptRecord,
    ReviewRecord,
    RunStore,
)


class RunStoreTests(unittest.TestCase):
    def test_create_run_writes_manifest_with_timezone_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            manifest = store.create_run(
                cwd=root,
                user_task="Implement the thing",
                planner_model="planner-model",
                worker_model="worker-model",
                planner_reasoning_effort="high",
                worker_reasoning_effort="medium",
                planner_service_tier="fast",
                worker_service_tier="flex",
            )

            manifest_path = root / "runs" / manifest.run_id / "manifest.json"
            self.assertTrue(manifest_path.exists())

            created_at = datetime.fromisoformat(manifest.created_at)
            updated_at = datetime.fromisoformat(manifest.updated_at)
            self.assertIsNotNone(created_at.tzinfo)
            self.assertIsNotNone(updated_at.tzinfo)

            loaded = store.load(manifest.run_id)
            self.assertEqual(loaded.run_id, manifest.run_id)
            self.assertEqual(loaded.cwd, str(root.resolve()))
            self.assertEqual(loaded.user_task, "Implement the thing")
            self.assertEqual(loaded.status, "NEW")
            self.assertEqual(loaded.planner.model, "planner-model")
            self.assertEqual(loaded.planner.reasoning_effort, "high")
            self.assertEqual(loaded.planner.service_tier, "fast")
            self.assertEqual(len(loaded.workers), 1)
            self.assertEqual(loaded.workers[0].id, "worker-1")
            self.assertEqual(loaded.workers[0].model, "worker-model")
            self.assertEqual(loaded.workers[0].reasoning_effort, "medium")
            self.assertEqual(loaded.workers[0].service_tier, "flex")
            self.assertIsNone(loaded.plan)
            self.assertFalse(loaded.requires_restart)
            self.assertIsNone(loaded.restart_reason)
            self.assertEqual(loaded.restart_paths, [])

            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["run_id"], manifest.run_id)
            self.assertFalse(raw["requires_restart"])
            self.assertIsNone(raw["restart_reason"])
            self.assertEqual(raw["restart_paths"], [])

    def test_save_updates_review_and_updated_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "runs")
            manifest = store.create_run(
                cwd=tmp,
                user_task="Review me",
                planner_model="planner-model",
                worker_model="worker-model",
            )
            manifest.updated_at = "2000-01-01T00:00:00+00:00"
            manifest.acceptance_criteria.append("Tests pass")
            manifest.verification_commands.append("python -m unittest")
            manifest.plan = PlanRecord(
                summary="Use a focused implementation path",
                worker_prompt="Implement the focused path",
                risk_notes=["Keep scope tight"],
                raw={"status": "plan_ready"},
                approval_status="approved",
                approved_at="2026-05-12T09:00:00+08:00",
                approved_by="human",
            )
            manifest.review = ReviewRecord(
                decision="revision_requested",
                reason="Missing regression test",
                next_worker_prompt="Add coverage",
                evidence_files=["runs/example/evidence/git-diff.patch"],
            )
            manifest.review_attempts.append(
                ReviewAttemptRecord(
                    id="review-1",
                    worker_id="worker-1",
                    status="FAILED_RETRYABLE",
                    started_at="2026-05-12T09:01:00+08:00",
                    completed_at="2026-05-12T09:02:00+08:00",
                    error="Timed out",
                    evidence_files=["runs/example/evidence/git-diff.patch"],
                )
            )
            manifest.plan_revisions.append(
                PlanRevisionRecord(
                    id="plan-revision-1",
                    created_at="2026-05-12T09:00:30+08:00",
                    human_feedback="Tighten scope",
                    previous_plan={"summary": "old"},
                    new_plan={"summary": "new"},
                )
            )
            manifest.workers[0].result = {"status": "work_done", "summary": "done"}

            store.save(manifest)
            loaded = store.load(manifest.run_id)

            self.assertNotEqual(loaded.updated_at, "2000-01-01T00:00:00+00:00")
            self.assertEqual(loaded.acceptance_criteria, ["Tests pass"])
            self.assertEqual(loaded.verification_commands, ["python -m unittest"])
            self.assertIsNotNone(loaded.plan)
            self.assertEqual(loaded.plan.summary, "Use a focused implementation path")
            self.assertEqual(loaded.plan.worker_prompt, "Implement the focused path")
            self.assertEqual(loaded.plan.risk_notes, ["Keep scope tight"])
            self.assertEqual(loaded.plan.raw, {"status": "plan_ready"})
            self.assertEqual(loaded.plan.approval_status, "approved")
            self.assertEqual(loaded.plan.approved_by, "human")
            self.assertEqual(len(loaded.plan_revisions), 1)
            self.assertEqual(loaded.plan_revisions[0].human_feedback, "Tighten scope")
            self.assertEqual(loaded.plan_revisions[0].new_plan["summary"], "new")
            self.assertIsNotNone(loaded.review)
            self.assertEqual(loaded.review.decision, "revision_requested")
            self.assertEqual(
                loaded.review.evidence_files,
                ["runs/example/evidence/git-diff.patch"],
            )
            self.assertEqual(loaded.review_attempts[0].status, "FAILED_RETRYABLE")
            self.assertEqual(loaded.review_attempts[0].error, "Timed out")
            self.assertEqual(loaded.workers[0].result["summary"], "done")

    def test_append_event_and_load_events_preserve_order_and_optional_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            first = store.append_event(
                "run-1",
                "planner_start",
                "Planner started",
                attempt=1,
                worker_id=None,
                worktree=Path("worktrees/run-1/worker-1"),
            )
            second = store.append_event(
                "run-1",
                "worker_done",
                "Worker attempt completed",
                attempt=1,
                status="DONE",
            )

            log_path = root / "runs" / "run-1" / "events.jsonl"
            self.assertTrue(log_path.exists())
            self.assertEqual(len(log_path.read_text(encoding="utf-8").splitlines()), 2)

            loaded = store.load_events("run-1")
            self.assertEqual(loaded, [first, second])
            self.assertEqual([event["type"] for event in loaded], ["planner_start", "worker_done"])
            self.assertEqual(loaded[0]["attempt"], 1)
            self.assertNotIn("worker_id", loaded[0])
            self.assertEqual(loaded[0]["worktree"], "worktrees/run-1/worker-1")
            self.assertEqual(loaded[1]["status"], "DONE")
            self.assertIsNotNone(datetime.fromisoformat(loaded[0]["timestamp"]).tzinfo)
            self.assertIsNotNone(datetime.fromisoformat(loaded[1]["timestamp"]).tzinfo)

    def test_load_events_handles_missing_and_malformed_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")

            self.assertEqual(store.load_events("missing-run"), [])

            log_path = root / "runs" / "run-1" / "events.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                "\n".join(
                    [
                        '{"timestamp":"2026-05-11T10:00:00+00:00","type":"planner_start","message":"Planner started"}',
                        "",
                        "{not-json",
                        '["not-an-object"]',
                        '{"timestamp":"2026-05-11T10:00:05+00:00","type":"worker_start","message":"Worker attempt started","attempt":1}',
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            loaded = store.load_events("run-1")
            self.assertEqual(len(loaded), 2)
            self.assertEqual([event["type"] for event in loaded], ["planner_start", "worker_start"])

    def test_load_legacy_manifest_defaults_restart_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root / "runs")
            manifest = store.create_run(
                cwd=root,
                user_task="Legacy manifest",
                planner_model="planner-model",
                worker_model="worker-model",
            )
            manifest_path = root / "runs" / manifest.run_id / "manifest.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw.pop("requires_restart", None)
            raw.pop("restart_reason", None)
            raw.pop("restart_paths", None)
            manifest_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            loaded = store.load(manifest.run_id)
            self.assertFalse(loaded.requires_restart)
            self.assertIsNone(loaded.restart_reason)
            self.assertEqual(loaded.restart_paths, [])


if __name__ == "__main__":
    unittest.main()
