from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.error import URLError

from c_orch.supervisor import (
    DashboardSupervisor,
    SupervisorConfig,
    build_ui_command,
    is_restart_required,
    restart_drain_reasons,
    restart_gate_key,
)


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def poll(self) -> Optional[int]:
        return 0 if self.terminated or self.killed else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self.poll()

    def kill(self) -> None:
        self.killed = True


class FakeClient:
    def __init__(
        self,
        queue_payloads: Sequence[Mapping[str, Any]],
        *,
        state_payloads: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        self.queue_payloads = list(queue_payloads)
        self.state_payloads = list(state_payloads or [])
        self.confirm_calls = 0

    def get_queue(self) -> Mapping[str, Any]:
        if self.queue_payloads:
            return self.queue_payloads.pop(0)
        return queue_payload(status="PENDING")

    def get_state(self) -> Mapping[str, Any]:
        if self.state_payloads:
            return self.state_payloads.pop(0)
        return state_payload(
            queue=queue_payload(status="PENDING"),
            queue_dispatch_running=False,
            proposal_dispatch_running=False,
        )

    def confirm_runtime_restarted(self) -> Mapping[str, Any]:
        self.confirm_calls += 1
        return queue_payload(status="PENDING")


class LegacyQueueOnlyClient:
    def __init__(self, queue_payloads: Sequence[Mapping[str, Any]]) -> None:
        self.queue_payloads = list(queue_payloads)
        self.confirm_calls = 0

    def get_queue(self) -> Mapping[str, Any]:
        if self.queue_payloads:
            return self.queue_payloads.pop(0)
        return queue_payload(status="PENDING")

    def confirm_runtime_restarted(self) -> Mapping[str, Any]:
        self.confirm_calls += 1
        return queue_payload(status="PENDING")


class StateFailingClient:
    def __init__(self, queue_payloads: Sequence[Mapping[str, Any]]) -> None:
        self.queue_payloads = list(queue_payloads)
        self.confirm_calls = 0

    def get_queue(self) -> Mapping[str, Any]:
        if self.queue_payloads:
            return self.queue_payloads.pop(0)
        return queue_payload(status="PENDING")

    def get_state(self) -> Mapping[str, Any]:
        raise URLError("state endpoint unavailable")

    def confirm_runtime_restarted(self) -> Mapping[str, Any]:
        self.confirm_calls += 1
        return queue_payload(status="PENDING")


def queue_payload(*, status: str, waiting_for: str = "done") -> Mapping[str, Any]:
    return {
        "queue": {"status": status},
        "summary": {"current_waiting_point": waiting_for},
        "tasks": [
            {
                "task_id": "task-1",
                "active_run_id": "run-1",
                "waiting_for": waiting_for,
                "next_action": waiting_for,
            }
        ],
    }


def state_payload(
    *,
    queue: Mapping[str, Any],
    queue_dispatch_running: bool,
    proposal_dispatch_running: bool,
    restart_gate: Optional[Mapping[str, Any]] = None,
    restart_drain: Optional[Mapping[str, Any]] = None,
    queue_lane_active_count: int = 0,
    proposal_lane_active_count: int = 0,
) -> Mapping[str, Any]:
    return {
        "runtime": {
            "dispatch_running": queue_dispatch_running or proposal_dispatch_running,
            "queue_dispatch_running": queue_dispatch_running,
            "proposal_dispatch_running": proposal_dispatch_running,
            "queue_lane_active_count": queue_lane_active_count,
            "proposal_lane_active_count": proposal_lane_active_count,
            "restart_gate": restart_gate,
            "restart_drain": restart_drain,
        },
        "queue": queue,
    }


class SupervisorTests(unittest.TestCase):
    def test_restart_helpers_detect_restart_gate(self) -> None:
        payload = queue_payload(status="RESTART_REQUIRED", waiting_for="restart")

        self.assertTrue(is_restart_required(payload))
        self.assertEqual(
            restart_gate_key(payload),
            '[{"active_run_id":"run-1","task_id":"task-1"}]',
        )

    def test_restart_helpers_prefer_runtime_restart_gate_snapshot(self) -> None:
        payload = state_payload(
            queue=queue_payload(status="PENDING", waiting_for="done"),
            queue_dispatch_running=False,
            proposal_dispatch_running=False,
            restart_gate={
                "active": True,
                "run_ids": ["run-9"],
                "task_ids": ["task-9"],
            },
        )

        self.assertTrue(is_restart_required(payload))
        self.assertEqual(
            restart_gate_key(payload),
            '{"run_ids":["run-9"],"task_ids":["task-9"]}',
        )

    def test_restart_drain_reasons_include_active_dispatch_even_when_gate_active(self) -> None:
        payload = state_payload(
            queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
            queue_dispatch_running=True,
            proposal_dispatch_running=True,
            queue_lane_active_count=0,
            proposal_lane_active_count=0,
            restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
        )

        self.assertEqual(
            restart_drain_reasons(payload),
            ["queue-dispatch-running", "proposal-dispatch-running"],
        )

    def test_restart_drain_reasons_allow_gate_paused_idle_threads(self) -> None:
        payload = state_payload(
            queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
            queue_dispatch_running=False,
            proposal_dispatch_running=False,
            queue_lane_active_count=0,
            proposal_lane_active_count=0,
            restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
        )

        self.assertEqual(restart_drain_reasons(payload), [])

    def test_restart_drain_reasons_prefer_backend_restart_drain_blockers(self) -> None:
        payload = state_payload(
            queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
            queue_dispatch_running=False,
            proposal_dispatch_running=False,
            restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
            restart_drain={
                "stage": "blocked",
                "can_restart": False,
                "blocking_reasons": [
                    "run-1:runner-1 code_review lease missing runner_subprocess_started boundary (got 'code_review_subprocess_launch_inflight')",
                ],
            },
        )

        self.assertEqual(
            restart_drain_reasons(payload),
            [
                "run-1:runner-1 code_review lease missing runner_subprocess_started boundary (got 'code_review_subprocess_launch_inflight')",
            ],
        )

    def test_build_ui_command_uses_resolved_dashboard_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_ui_command(
                cwd=root,
                runs_dir=root / "runs",
                queue_path=root / ".c-orch" / "tasks" / "queue.json",
                host="127.0.0.1",
                port=8765,
                config_path="custom.toml",
            )

        self.assertIn("c_orch.cli", command)
        self.assertIn("ui", command)
        self.assertIn("--cwd", command)
        self.assertIn("--runs-dir", command)
        self.assertIn("--queue-file", command)
        self.assertIn("--config", command)
        self.assertEqual(command[-2:], ["--config", "custom.toml"])

    def test_build_ui_command_can_target_api_only_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = build_ui_command(
                cwd=root,
                runs_dir=root / "runs",
                queue_path=root / ".c-orch" / "tasks" / "queue.json",
                host="127.0.0.1",
                port=8765,
                api_only=True,
            )

        self.assertIn("--api-only", command)

    def test_restart_gate_restarts_runtime_and_confirms_gate(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                ),
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-confirmed")
        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].terminated)
        self.assertEqual(client.confirm_calls, 1)

    def test_restart_gate_waits_for_runtime_drain_when_dispatch_is_active(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=True,
                    proposal_dispatch_running=False,
                ),
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        first = supervisor.check_once()
        second = supervisor.check_once()

        self.assertEqual(first, "restart-draining")
        self.assertEqual(second, "restart-confirmed")
        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].terminated)
        self.assertEqual(client.confirm_calls, 1)

    def test_restart_gate_waits_for_runtime_drain_with_runtime_restart_gate_snapshot(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=True,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                ),
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                ),
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        first = supervisor.check_once()
        second = supervisor.check_once()

        self.assertEqual(first, "restart-draining")
        self.assertEqual(second, "restart-confirmed")
        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].terminated)
        self.assertEqual(client.confirm_calls, 1)

    def test_restart_gate_waits_when_backend_marks_restart_drain_blocked(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                    restart_drain={
                        "stage": "blocked",
                        "can_restart": False,
                        "blocking_reasons": [
                            "run-1:runner-1 active phase 'worker_implement' is runtime-owned or unsupported for restart handoff",
                        ],
                    },
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-draining")
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].terminated)
        self.assertEqual(client.confirm_calls, 0)

    def test_restart_gate_waits_for_active_external_code_review_result_import(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                    restart_drain={
                        "stage": "blocked",
                        "can_restart": False,
                        "blocking_reasons": [
                            "run-1:runner-1 code_review subprocess still active; waiting for completed result manifest import before restart",
                        ],
                    },
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-draining")
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].terminated)
        self.assertEqual(client.confirm_calls, 0)

    def test_restart_gate_allows_backend_marked_ready_restart(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = FakeClient(
            [queue_payload(status="PENDING")],
            state_payloads=[
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                    restart_drain={
                        "stage": "ready_to_restart",
                        "can_restart": True,
                        "blocking_reasons": [],
                    },
                ),
                state_payload(
                    queue=queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                    queue_dispatch_running=False,
                    proposal_dispatch_running=False,
                    queue_lane_active_count=0,
                    proposal_lane_active_count=0,
                    restart_gate={"active": True, "run_ids": ["run-1"], "task_ids": ["task-1"]},
                    restart_drain={
                        "stage": "ready_to_restart",
                        "can_restart": True,
                        "blocking_reasons": [],
                    },
                ),
            ],
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-confirmed")
        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].terminated)
        self.assertEqual(client.confirm_calls, 1)

    def test_restart_gate_supports_queue_only_client_payload(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = LegacyQueueOnlyClient(
            [
                queue_payload(status="PENDING"),
                queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
                queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
            ]
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-confirmed")
        self.assertEqual(len(started), 2)
        self.assertTrue(started[0].terminated)
        self.assertEqual(client.confirm_calls, 1)

    def test_restart_gate_waits_when_state_endpoint_unavailable(self) -> None:
        started: list[FakeProcess] = []

        def process_factory(command: Sequence[str], cwd: Path) -> FakeProcess:
            process = FakeProcess()
            started.append(process)
            return process

        client = StateFailingClient(
            [
                queue_payload(status="PENDING"),
                queue_payload(status="RESTART_REQUIRED", waiting_for="restart"),
            ]
        )
        supervisor = DashboardSupervisor(
            SupervisorConfig(
                command=["python", "-m", "c_orch.cli", "ui"],
                cwd=Path("/tmp"),
                base_url="http://127.0.0.1:8765",
                startup_timeout_seconds=1,
                stop_timeout_seconds=1,
            ),
            client=client,
            process_factory=process_factory,
        )

        supervisor.start()
        outcome = supervisor.check_once()

        self.assertEqual(outcome, "restart-state-unavailable")
        self.assertEqual(len(started), 1)
        self.assertFalse(started[0].terminated)
        self.assertEqual(client.confirm_calls, 0)


if __name__ == "__main__":
    unittest.main()
