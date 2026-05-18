from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch import dashboard_payloads
from c_orch.proposal_store import ProposalStore
from c_orch.run_store import ReviewAttemptRecord, ReviewRecord, RunStore


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
            self.assertEqual(payload["proposals"][0]["allowed_actions"], ["retry-plan"])
            self.assertIn("src/main.py", payload["proposals"][0]["blocker"]["status_output"])

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


if __name__ == "__main__":
    unittest.main()
