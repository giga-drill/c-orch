from __future__ import annotations

import unittest

from c_orch import dashboard_payloads


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


if __name__ == "__main__":
    unittest.main()
