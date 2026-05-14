from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union
from urllib.parse import unquote

from .runtime import COrchRuntime
from .scheduler import SchedulerConfig
from .settings import DEFAULT_PROPOSALS_FILE, DEFAULT_UI_HOST, DEFAULT_UI_PORT


Pathish = Union[str, Path]
WEB_DIST_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"
FALLBACK_INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>c-orch 运行面板</title>
</head>
<body>
  <main style="font: 14px/1.5 -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 720px; margin: 48px auto; padding: 0 20px;">
    <h1>c-orch 前端还没有构建</h1>
    <p>请先在项目根目录运行 <code>pnpm --dir web install --frozen-lockfile</code> 和 <code>pnpm --dir web run build</code>，然后刷新页面。</p>
  </main>
</body>
</html>
"""


def serve_dashboard(
    *,
    runs_dir: Pathish,
    queue_path: Optional[Pathish] = None,
    proposals_path: Optional[Pathish] = None,
    scheduler_config: Optional[SchedulerConfig] = None,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
) -> None:
    server = build_server(
        runs_dir=runs_dir,
        queue_path=queue_path,
        proposals_path=proposals_path,
        scheduler_config=scheduler_config,
        host=host,
        port=port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime = getattr(server, "c_orch_runtime", None)
        if runtime is not None:
            runtime.close()
        server.server_close()


def build_server(
    *,
    runs_dir: Pathish,
    queue_path: Optional[Pathish] = None,
    proposals_path: Optional[Pathish] = None,
    scheduler_config: Optional[SchedulerConfig] = None,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
) -> ThreadingHTTPServer:
    runs_path = Path(runs_dir).expanduser().resolve()
    queue_file = Path(queue_path).expanduser().resolve() if queue_path is not None else None
    proposals_file = (
        Path(proposals_path).expanduser().resolve()
        if proposals_path is not None
        else _default_proposals_path(queue_file)
    )
    runtime = COrchRuntime(
        runs_dir=runs_path,
        queue_path=queue_file,
        proposals_path=proposals_file,
        scheduler_config=scheduler_config,
    )
    handler = make_dashboard_handler(runtime)
    server = ThreadingHTTPServer((host, port), handler)
    setattr(server, "c_orch_runtime", runtime)
    runtime.dispatch_queue_async()
    runtime.dispatch_proposals_async()
    return server


def make_dashboard_handler(runtime: COrchRuntime) -> Type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/api/runs":
                self._send_json(HTTPStatus.OK, runtime.build_runs_payload())
                return
            if path == "/api/queue":
                self._send_json(HTTPStatus.OK, runtime.build_queue_payload())
                return
            if path == "/api/proposals":
                self._send_json(HTTPStatus.OK, runtime.build_proposals_payload())
                return
            if path == "/api/state":
                self._send_json(HTTPStatus.OK, runtime.build_state_payload())
                return
            if path.startswith("/api/runs/"):
                run_id = unquote(path[len("/api/runs/"):])
                payload = runtime.build_run_payload(run_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            if self._send_static(path):
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            run_prefix = "/api/runs/"
            task_prefix = "/api/tasks/"
            proposal_prefix = "/api/proposals/"
            queue_actions_path = "/api/queue/actions"
            suffix = "/actions"
            if path != "/api/proposals" and path != queue_actions_path and (
                not path.endswith(suffix)
                or not (
                    path.startswith(run_prefix)
                    or path.startswith(task_prefix)
                    or path.startswith(proposal_prefix)
                )
            ):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                return
            action = data.get("action")
            if path == "/api/proposals":
                result = runtime.create_proposal(data.get("title"), data.get("prompt"), data.get("cwd"))
            elif path == queue_actions_path:
                confirmed_by = data.get("confirmed_by")
                if not isinstance(confirmed_by, str) or not confirmed_by.strip():
                    confirmed_by = "dashboard"
                result = runtime.queue_action(action, confirmed_by=confirmed_by)
            elif path.startswith(run_prefix):
                run_id = unquote(path[len(run_prefix):-len(suffix)])
                result = runtime.run_action(run_id, action, data.get("feedback"))
            elif path.startswith(proposal_prefix):
                proposal_id = unquote(path[len(proposal_prefix):-len(suffix)])
                result = runtime.proposal_action(proposal_id, action, data.get("feedback"))
            else:
                task_id = unquote(path[len(task_prefix):-len(suffix)])
                result = runtime.task_action(task_id, action)
            if result is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            status, payload = result
            self._send_json(status, payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_static(self, request_path: str) -> bool:
            if request_path == "/" or request_path == "/index.html":
                index_path = WEB_DIST_DIR / "index.html"
                if index_path.exists():
                    self._send_file(index_path, "text/html; charset=utf-8")
                else:
                    self._send_text(HTTPStatus.OK, FALLBACK_INDEX_HTML, "text/html; charset=utf-8")
                return True
            if not WEB_DIST_DIR.exists():
                return False
            relative = unquote(request_path.lstrip("/"))
            static_path = (WEB_DIST_DIR / relative).resolve()
            try:
                static_path.relative_to(WEB_DIST_DIR.resolve())
            except ValueError:
                return False
            if not static_path.is_file():
                return False
            content_type = mimetypes.guess_type(str(static_path))[0] or "application/octet-stream"
            if content_type.startswith("text/"):
                content_type = f"{content_type}; charset=utf-8"
            self._send_file(static_path, content_type)
            return True

        def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: HTTPStatus, body_text: str, content_type: str) -> None:
            self._send_bytes(status, body_text.encode("utf-8"), content_type)

        def _send_file(self, path: Path, content_type: str) -> None:
            self._send_bytes(HTTPStatus.OK, path.read_bytes(), content_type)

        def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def _default_proposals_path(queue_file: Optional[Path]) -> Optional[Path]:
    if queue_file is None:
        return None
    return queue_file.parent / Path(DEFAULT_PROPOSALS_FILE).name
