from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from c_orch import dashboard_payloads
from c_orch.proposal_store import ProposalStore


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


if __name__ == "__main__":
    unittest.main()
