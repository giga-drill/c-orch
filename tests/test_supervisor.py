from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from c_orch.supervisor import (
    DashboardSupervisor,
    SupervisorConfig,
    build_ui_command,
    is_restart_required,
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


class SupervisorTests(unittest.TestCase):
    def test_restart_helpers_detect_restart_gate(self) -> None:
        payload = queue_payload(status="RESTART_REQUIRED", waiting_for="restart")

        self.assertTrue(is_restart_required(payload))
        self.assertEqual(
            restart_gate_key(payload),
            '[{"active_run_id":"run-1","task_id":"task-1"}]',
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


if __name__ == "__main__":
    unittest.main()
