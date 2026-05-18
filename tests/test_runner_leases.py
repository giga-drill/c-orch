from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from c_orch.runner_leases import (
    LEASE_STATUS_ACTIVE,
    LEASE_STATUS_COMPLETED,
    LEASE_STATUS_EXPIRED,
    LEASE_STATUS_FAILED,
    RunnerLeaseStore,
)


class RunnerLeaseStoreTests(unittest.TestCase):
    def test_create_and_read_active_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "run-1" / "runner-leases.json"
            store = RunnerLeaseStore(path)
            now = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
            lease = store.create(
                runtime_generation="runtime-1",
                run_id="run-1",
                phase="planner_plan",
                task_id="task-1",
                proposal_id="proposal-1",
                process_hint="pid:123",
                pid=123,
                checkpoint={"run_status": "PLANNING"},
                lease_ttl_seconds=60,
                now=now,
            )

            payload = store.read(now=now + timedelta(seconds=30)).to_dict()
            self.assertEqual(payload["summary"]["total"], 1)
            self.assertEqual(payload["summary"]["active"], 1)
            self.assertEqual(payload["summary"]["stale"], 0)
            self.assertEqual(payload["leases"][0]["runner_id"], lease.runner_id)
            self.assertEqual(payload["leases"][0]["status"], LEASE_STATUS_ACTIVE)
            self.assertEqual(payload["leases"][0]["effective_status"], LEASE_STATUS_ACTIVE)
            self.assertEqual(payload["leases"][0]["checkpoint"]["run_status"], "PLANNING")

    def test_heartbeat_update_complete_and_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "run-1" / "runner-leases.json"
            store = RunnerLeaseStore(path)
            start = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
            lease = store.create(
                runtime_generation="runtime-1",
                run_id="run-1",
                phase="worker_implement",
                task_id="task-1",
                proposal_id=None,
                process_hint="pid:456",
                pid=456,
                checkpoint={"run_status": "WORKING"},
                lease_ttl_seconds=30,
                now=start,
            )

            beat = store.heartbeat(
                lease.runner_id,
                checkpoint={"run_status": "WORKING", "worker_attempt": 2},
                lease_ttl_seconds=90,
                now=start + timedelta(seconds=10),
            )
            self.assertIsNotNone(beat)
            self.assertEqual(beat.checkpoint["worker_attempt"], 2)
            updated = store.update(
                lease.runner_id,
                checkpoint={"run_status": "WORK_DONE", "phase_boundary": "post_worker"},
                lease_ttl_seconds=120,
                now=start + timedelta(seconds=12),
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated.checkpoint["phase_boundary"], "post_worker")

            completed = store.complete(
                lease.runner_id,
                checkpoint={"run_status": "WORK_DONE"},
                now=start + timedelta(seconds=20),
            )
            self.assertIsNotNone(completed)
            self.assertEqual(completed.status, LEASE_STATUS_COMPLETED)

            second = store.create(
                runtime_generation="runtime-1",
                run_id="run-1",
                phase="planner_review",
                task_id="task-1",
                proposal_id=None,
                process_hint="pid:456",
                pid=456,
                checkpoint={"run_status": "REVIEWING"},
                lease_ttl_seconds=30,
                now=start,
            )
            failed = store.fail(
                second.runner_id,
                checkpoint={"run_status": "FAILED"},
                error="planner crashed",
                now=start + timedelta(seconds=5),
            )
            self.assertIsNotNone(failed)
            self.assertEqual(failed.status, LEASE_STATUS_FAILED)
            self.assertEqual(failed.error, "planner crashed")

            payload = store.read(now=start + timedelta(seconds=40)).to_dict()
            self.assertEqual(payload["summary"]["completed"], 1)
            self.assertEqual(payload["summary"]["failed"], 1)

    def test_expire_and_stale_derivation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "run-1" / "runner-leases.json"
            store = RunnerLeaseStore(path)
            start = datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
            lease = store.create(
                runtime_generation="runtime-1",
                run_id="run-1",
                phase="verification",
                task_id=None,
                proposal_id=None,
                process_hint=None,
                pid=None,
                checkpoint={"run_status": "WORK_DONE"},
                lease_ttl_seconds=5,
                now=start,
            )

            stale_payload = store.read(now=start + timedelta(seconds=10)).to_dict()
            self.assertEqual(stale_payload["leases"][0]["status"], LEASE_STATUS_ACTIVE)
            self.assertEqual(stale_payload["leases"][0]["effective_status"], "stale")
            self.assertEqual(stale_payload["summary"]["stale"], 1)

            expired_ids = store.expire(now=start + timedelta(seconds=10))
            self.assertEqual(expired_ids, [lease.runner_id])
            expired_payload = store.read(now=start + timedelta(seconds=11)).to_dict()
            self.assertEqual(expired_payload["leases"][0]["status"], LEASE_STATUS_EXPIRED)
            self.assertEqual(expired_payload["leases"][0]["effective_status"], LEASE_STATUS_EXPIRED)
            self.assertEqual(expired_payload["summary"]["expired"], 1)

    def test_read_handles_empty_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runs" / "run-1" / "runner-leases.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n", encoding="utf-8")
            store = RunnerLeaseStore(path)
            payload = store.read().to_dict()
            self.assertEqual(payload["summary"]["total"], 0)

            path.write_text("{invalid json", encoding="utf-8")
            payload = store.read().to_dict()
            self.assertEqual(payload["summary"]["total"], 0)

            path.write_text(json.dumps({"schema_version": 1, "leases": "bad"}), encoding="utf-8")
            payload = store.read().to_dict()
            self.assertEqual(payload["summary"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
