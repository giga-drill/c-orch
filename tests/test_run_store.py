from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from c_orch.run_store import ReviewRecord, RunStore


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
            self.assertEqual(len(loaded.workers), 1)
            self.assertEqual(loaded.workers[0].id, "worker-1")
            self.assertEqual(loaded.workers[0].model, "worker-model")

            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(raw["run_id"], manifest.run_id)

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
            manifest.review = ReviewRecord(
                decision="needs_changes",
                reason="Missing regression test",
                next_worker_prompt="Add coverage",
                evidence_files=["runs/example/evidence/git-diff.patch"],
            )

            store.save(manifest)
            loaded = store.load(manifest.run_id)

            self.assertNotEqual(loaded.updated_at, "2000-01-01T00:00:00+00:00")
            self.assertEqual(loaded.acceptance_criteria, ["Tests pass"])
            self.assertEqual(loaded.verification_commands, ["python -m unittest"])
            self.assertIsNotNone(loaded.review)
            self.assertEqual(loaded.review.decision, "needs_changes")
            self.assertEqual(
                loaded.review.evidence_files,
                ["runs/example/evidence/git-diff.patch"],
            )


if __name__ == "__main__":
    unittest.main()
