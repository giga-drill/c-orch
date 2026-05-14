from __future__ import annotations

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen


class ProcessHandle(Protocol):
    def poll(self) -> Optional[int]:
        ...

    def terminate(self) -> None:
        ...

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        ...

    def kill(self) -> None:
        ...


class DashboardClient(Protocol):
    def get_queue(self) -> Mapping[str, Any]:
        ...

    def confirm_runtime_restarted(self) -> Mapping[str, Any]:
        ...


ProcessFactory = Callable[[Sequence[str], Path], ProcessHandle]


@dataclass(frozen=True)
class SupervisorConfig:
    command: Sequence[str]
    cwd: Path
    base_url: str
    poll_interval_seconds: float = 2.0
    startup_timeout_seconds: float = 30.0
    stop_timeout_seconds: float = 10.0


class HttpDashboardClient:
    def __init__(self, base_url: str, *, timeout_seconds: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_queue(self) -> Mapping[str, Any]:
        return self._request_json("GET", "/api/queue")

    def confirm_runtime_restarted(self) -> Mapping[str, Any]:
        return self._request_json(
            "POST",
            "/api/queue/actions",
            payload={"action": "confirm-runtime-restarted"},
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            raw = response.read().decode("utf-8")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError(f"dashboard returned non-object JSON for {path}")
        return result


class DashboardSupervisor:
    def __init__(
        self,
        config: SupervisorConfig,
        *,
        client: Optional[DashboardClient] = None,
        process_factory: Optional[ProcessFactory] = None,
    ) -> None:
        self.config = config
        self.client = client or HttpDashboardClient(config.base_url)
        self.process_factory = process_factory or _start_process
        self.process: Optional[ProcessHandle] = None
        self._last_restarted_gate_key: Optional[str] = None

    def run_forever(self) -> int:
        self.start()
        try:
            while True:
                self.check_once()
                time.sleep(self.config.poll_interval_seconds)
        except KeyboardInterrupt:
            return 0
        finally:
            self.stop()

    def start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        print(f"supervisor: starting UI runtime: {' '.join(self.config.command)}", flush=True)
        self.process = self.process_factory(self.config.command, self.config.cwd)
        self.wait_until_ready()

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            self.process = None
            return
        print("supervisor: stopping UI runtime", flush=True)
        process.terminate()
        try:
            process.wait(timeout=self.config.stop_timeout_seconds)
        except subprocess.TimeoutExpired:
            print("supervisor: UI runtime did not stop in time; killing it", flush=True)
            process.kill()
            process.wait(timeout=self.config.stop_timeout_seconds)
        self.process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def wait_until_ready(self) -> None:
        deadline = time.time() + self.config.startup_timeout_seconds
        last_error: Optional[BaseException] = None
        while time.time() < deadline:
            if self.process is not None:
                returncode = self.process.poll()
                if returncode is not None:
                    raise RuntimeError(f"UI runtime exited before becoming ready: {returncode}")
            try:
                self.client.get_queue()
                return
            except (OSError, URLError, TimeoutError, ValueError) as exc:
                last_error = exc
                time.sleep(0.25)
        raise TimeoutError(f"UI runtime did not become ready: {last_error}")

    def check_once(self) -> str:
        self.start()
        if self.process is not None and self.process.poll() is not None:
            print("supervisor: UI runtime exited; restarting", flush=True)
            self.restart()
            return "runtime-restarted"

        payload = self.client.get_queue()
        if not is_restart_required(payload):
            self._last_restarted_gate_key = None
            return "idle"

        gate_key = restart_gate_key(payload)
        if gate_key == self._last_restarted_gate_key:
            print(
                "supervisor: restart gate is still present after a prior restart; "
                "waiting for state to change",
                flush=True,
            )
            return "restart-gate-already-handled"

        print("supervisor: restart gate detected; restarting UI runtime", flush=True)
        self._last_restarted_gate_key = gate_key
        self.restart()
        self.client.confirm_runtime_restarted()
        print("supervisor: restart gate confirmed", flush=True)
        return "restart-confirmed"


def is_restart_required(payload: Mapping[str, Any]) -> bool:
    queue = payload.get("queue")
    if isinstance(queue, dict) and queue.get("status") == "RESTART_REQUIRED":
        return True
    summary = payload.get("summary")
    return isinstance(summary, dict) and summary.get("current_waiting_point") == "restart"


def restart_gate_key(payload: Mapping[str, Any]) -> str:
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        return "restart"
    keys = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if task.get("waiting_for") != "restart" and task.get("next_action") != "restart":
            continue
        keys.append(
            {
                "task_id": task.get("task_id"),
                "active_run_id": task.get("active_run_id"),
            }
        )
    if not keys:
        return "restart"
    return json.dumps(keys, sort_keys=True, separators=(",", ":"))


def build_ui_command(
    *,
    cwd: Path,
    runs_dir: Path,
    queue_path: Path,
    host: str,
    port: int,
    config_path: Optional[str] = None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "c_orch.cli",
        "ui",
        "--cwd",
        str(cwd),
        "--runs-dir",
        str(runs_dir),
        "--queue-file",
        str(queue_path),
        "--host",
        host,
        "--port",
        str(port),
    ]
    if config_path:
        command.extend(["--config", config_path])
    return command


def _start_process(command: Sequence[str], cwd: Path) -> ProcessHandle:
    return subprocess.Popen(list(command), cwd=str(cwd))
