from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from .supervisor import DashboardSupervisor, ProcessHandle, SupervisorConfig, build_ui_command


@dataclass(frozen=True)
class DevUiConfig:
    cwd: Path
    api_command: Sequence[str]
    vite_command: Sequence[str]
    api_base_url: str
    api_target: str
    watch_roots: Sequence[Path]
    poll_interval_seconds: float = 1.0
    startup_timeout_seconds: float = 30.0


class DevUiRunner:
    def __init__(self, config: DevUiConfig, *, api_supervisor: Optional[DashboardSupervisor] = None) -> None:
        self.config = config
        self.api_supervisor = api_supervisor or DashboardSupervisor(
            SupervisorConfig(
                command=config.api_command,
                cwd=config.cwd,
                base_url=config.api_base_url,
                poll_interval_seconds=config.poll_interval_seconds,
                startup_timeout_seconds=config.startup_timeout_seconds,
            ),
            process_factory=_start_api_process,
        )
        self.vite_process: Optional[subprocess.Popen[bytes]] = None
        self._snapshot = source_snapshot(config.watch_roots)

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
        self.start_api()
        self.start_vite()

    def start_api(self) -> None:
        self.api_supervisor.start()

    def start_vite(self) -> None:
        if self.vite_process is not None and self.vite_process.poll() is None:
            return
        env = os.environ.copy()
        env["C_ORCH_API_TARGET"] = self.config.api_target
        print(f"dev-ui: starting Vite: {' '.join(self.config.vite_command)}", flush=True)
        self.vite_process = subprocess.Popen(
            list(self.config.vite_command),
            cwd=self.config.cwd,
            env=env,
            start_new_session=True,
        )

    def stop(self) -> None:
        process = self.vite_process
        if process is not None and process.poll() is None:
            print("dev-ui: stopping Vite", flush=True)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self.vite_process = None
        self.api_supervisor.stop()

    def restart_api(self) -> None:
        print("dev-ui: restarting API runtime after backend source change", flush=True)
        self.api_supervisor.restart()

    def check_once(self) -> None:
        self.api_supervisor.check_once()
        if self.vite_process is not None and self.vite_process.poll() is not None:
            raise RuntimeError("Vite dev server exited")
        snapshot = source_snapshot(self.config.watch_roots)
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self.restart_api()


def build_api_command(
    *,
    cwd: Path,
    config_path: Optional[str],
    runs_dir: Path,
    queue_path: Path,
    host: str,
    port: int,
    python_executable: str = sys.executable,
) -> Sequence[str]:
    command = build_ui_command(
        cwd=cwd,
        runs_dir=runs_dir,
        queue_path=queue_path,
        host=host,
        port=port,
        config_path=config_path,
        api_only=True,
    )
    command[0] = python_executable
    return command


def build_vite_command(*, cwd: Path, host: str, port: int) -> Sequence[str]:
    return [
        "pnpm",
        "--dir",
        str(cwd / "web"),
        "run",
        "dev",
        "--",
        "--host",
        host,
        "--port",
        str(port),
    ]


def source_snapshot(roots: Iterable[Path]) -> Tuple[Tuple[str, int], ...]:
    entries: Dict[str, int] = {}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            try:
                entries[str(path.resolve())] = path.stat().st_mtime_ns
            except OSError:
                continue
    return tuple(sorted(entries.items()))


def _source_env(cwd: Path) -> dict[str, str]:
    env = os.environ.copy()
    src = cwd / "src"
    if src.is_dir():
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(src) if not existing else f"{src}{os.pathsep}{existing}"
    return env


def _start_api_process(command: Sequence[str], cwd: Path) -> ProcessHandle:
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=_source_env(cwd),
        start_new_session=True,
    )
