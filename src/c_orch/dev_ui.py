from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple
from urllib.error import URLError
from urllib.request import urlopen


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
    def __init__(self, config: DevUiConfig) -> None:
        self.config = config
        self.api_process: Optional[subprocess.Popen[bytes]] = None
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
        if self.api_process is not None and self.api_process.poll() is None:
            return
        print(f"dev-ui: starting API runtime: {' '.join(self.config.api_command)}", flush=True)
        self.api_process = subprocess.Popen(
            list(self.config.api_command),
            cwd=self.config.cwd,
            env=_source_env(self.config.cwd),
            start_new_session=True,
        )
        self.wait_until_api_ready()

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
        for name, process in (("Vite", self.vite_process), ("API runtime", self.api_process)):
            if process is None or process.poll() is not None:
                continue
            print(f"dev-ui: stopping {name}", flush=True)
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        self.api_process = None
        self.vite_process = None

    def restart_api(self) -> None:
        if self.api_process is not None and self.api_process.poll() is None:
            print("dev-ui: restarting API runtime after backend source change", flush=True)
            self.api_process.terminate()
            try:
                self.api_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.api_process.kill()
                self.api_process.wait(timeout=10)
        self.api_process = None
        self.start_api()

    def check_once(self) -> None:
        if self.api_process is not None and self.api_process.poll() is not None:
            print("dev-ui: API runtime exited; restarting", flush=True)
            self.api_process = None
            self.start_api()
        if self.vite_process is not None and self.vite_process.poll() is not None:
            raise RuntimeError("Vite dev server exited")
        snapshot = source_snapshot(self.config.watch_roots)
        if snapshot != self._snapshot:
            self._snapshot = snapshot
            self.restart_api()

    def wait_until_api_ready(self) -> None:
        deadline = time.time() + self.config.startup_timeout_seconds
        last_error: Optional[BaseException] = None
        while time.time() < deadline:
            if self.api_process is not None:
                returncode = self.api_process.poll()
                if returncode is not None:
                    raise RuntimeError(f"API runtime exited before becoming ready: {returncode}")
            try:
                with urlopen(f"{self.config.api_base_url.rstrip('/')}/api/state", timeout=2):
                    return
            except (OSError, URLError, TimeoutError, ValueError) as exc:
                last_error = exc
                time.sleep(0.25)
        raise TimeoutError(f"API runtime did not become ready: {last_error}")


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
    command = [
        python_executable,
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
        "--api-only",
    ]
    if config_path:
        command.extend(["--config", config_path])
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
