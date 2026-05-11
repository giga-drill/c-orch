from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch.run_store import ReviewRecord, RunStore
from c_orch.ui import INDEX_HTML, build_run_payload, build_runs_payload


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
            manifest.workers[0].evidence_files = [str(evidence_path)]
            manifest.review = ReviewRecord(
                decision="approved",
                reason="looks good",
                evidence_files=[str(evidence_path)],
            )
            store.save(manifest)

            payload = build_run_payload(root / "runs", manifest.run_id)

            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["manifest"]["run_id"], manifest.run_id)
            self.assertEqual(payload["run"]["review"]["decision"], "approved")
            self.assertEqual(payload["run"]["evidence_count"], 1)
            self.assertEqual(payload["evidence_files"][0]["name"], "git-diff.patch")
            self.assertTrue(payload["evidence_files"][0]["exists"])

    def test_build_run_payload_rejects_path_traversal_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(build_run_payload(Path(tmp) / "runs", "../outside"))

    def test_index_html_contains_dashboard_mount_points(self) -> None:
        self.assertIn('id="runList"', INDEX_HTML)
        self.assertIn("/api/runs", INDEX_HTML)
        self.assertIn('id="detail"', INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
