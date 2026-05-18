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


if __name__ == "__main__":
    unittest.main()
