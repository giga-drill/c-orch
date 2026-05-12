from __future__ import annotations

import json
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Type, Union
from urllib.parse import unquote

from .run_store import RunStore


Pathish = Union[str, Path]

STATUS_ORDER = {
    "NEW": 0,
    "PLANNING": 1,
    "PLAN_READY": 2,
    "PLAN_REVIEW_REQUIRED": 3,
    "PLAN_APPROVED": 4,
    "WORKING": 5,
    "WORK_DONE": 6,
    "REVIEWING": 7,
    "NEEDS_CHANGES": 8,
    "APPROVED": 9,
    "BLOCKED": 9,
    "FAILED": 9,
}
TERMINAL_STATUSES = {"APPROVED", "BLOCKED", "FAILED"}


def serve_dashboard(
    *,
    runs_dir: Pathish,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> None:
    server = build_server(runs_dir=runs_dir, host=host, port=port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def build_server(
    *,
    runs_dir: Pathish,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    runs_path = Path(runs_dir).expanduser().resolve()
    handler = make_dashboard_handler(runs_path)
    return ThreadingHTTPServer((host, port), handler)


def make_dashboard_handler(runs_dir: Path) -> Type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send_text(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
                return
            if path == "/api/runs":
                self._send_json(HTTPStatus.OK, build_runs_payload(runs_dir))
                return
            if path.startswith("/api/runs/"):
                run_id = unquote(path[len("/api/runs/"):])
                payload = build_run_payload(runs_dir, run_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: HTTPStatus, body_text: str, content_type: str) -> None:
            body = body_text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return DashboardHandler


def build_runs_payload(runs_dir: Pathish) -> Dict[str, Any]:
    runs_path = Path(runs_dir).expanduser().resolve()
    runs = [_summarize_manifest(manifest) for manifest in _load_manifests(runs_path)]
    runs.sort(key=lambda item: item["sort_key"], reverse=True)
    for run in runs:
        run.pop("sort_key", None)
    return {
        "runs_dir": str(runs_path),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "runs": runs,
    }


def build_run_payload(runs_dir: Pathish, run_id: str) -> Optional[Dict[str, Any]]:
    runs_path = Path(runs_dir).expanduser().resolve()
    if not _valid_run_id(run_id):
        return None
    manifest_path = runs_path / run_id / "manifest.json"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        return None
    events = RunStore(runs_path).load_events(run_id)
    return {
        "run": _summarize_manifest(manifest),
        "manifest": manifest,
        "evidence_files": _evidence_details(manifest),
        "events": events,
    }


def _load_manifests(runs_dir: Path) -> List[Dict[str, Any]]:
    manifests: List[Dict[str, Any]] = []
    if not runs_dir.exists():
        return manifests
    for path in sorted(runs_dir.glob("*/manifest.json")):
        manifest = _load_manifest(path)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def _load_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _summarize_manifest(manifest: Dict[str, Any]) -> Dict[str, Any]:
    planner = _dict_value(manifest.get("planner"))
    workers = [_summarize_worker(worker) for worker in _list_value(manifest.get("workers"))]
    review = _dict_value(manifest.get("review"))
    plan = _dict_value(manifest.get("plan"))
    status = str(manifest.get("status", "UNKNOWN"))
    evidence_files = _unique_strings(
        _flatten(
            [
                worker.get("evidence_files", [])
                for worker in _list_value(manifest.get("workers"))
                if isinstance(worker, dict)
            ]
        )
        + _list_value(review.get("evidence_files"))
    )
    updated_at = str(manifest.get("updated_at", ""))
    created_at = str(manifest.get("created_at", ""))
    return {
        "run_id": str(manifest.get("run_id", "")),
        "status": status,
        "status_index": STATUS_ORDER.get(status, 0),
        "terminal": status in TERMINAL_STATUSES,
        "user_task": str(manifest.get("user_task", "")),
        "cwd": str(manifest.get("cwd", "")),
        "created_at": created_at,
        "updated_at": updated_at,
        "sort_key": updated_at or created_at,
        "planner": {
            "status": str(planner.get("status", "PENDING")),
            "model": str(planner.get("model", "")),
            "thread_id": planner.get("thread_id"),
            "reasoning_effort": planner.get("reasoning_effort"),
            "service_tier": planner.get("service_tier"),
        },
        "workers": workers,
        "review": {
            "decision": review.get("decision"),
            "reason": review.get("reason"),
        } if review else None,
        "plan": {
            "approval_status": plan.get("approval_status"),
            "summary": plan.get("summary"),
        } if plan else None,
        "acceptance_count": len(_list_value(manifest.get("acceptance_criteria"))),
        "verification_count": len(_list_value(manifest.get("verification_commands"))),
        "evidence_count": len(evidence_files),
    }


def _summarize_worker(worker: Any) -> Dict[str, Any]:
    data = _dict_value(worker)
    evidence_files = _list_value(data.get("evidence_files"))
    return {
        "id": str(data.get("id", "")),
        "status": str(data.get("status", "PENDING")),
        "model": str(data.get("model", "")),
        "thread_id": data.get("thread_id"),
        "reasoning_effort": data.get("reasoning_effort"),
        "service_tier": data.get("service_tier"),
        "attempt": data.get("attempt", 1),
        "worktree_path": data.get("worktree_path"),
        "evidence_count": len(evidence_files),
    }


def _evidence_details(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    paths = []
    for worker in _list_value(manifest.get("workers")):
        if isinstance(worker, dict):
            paths.extend(_list_value(worker.get("evidence_files")))
    review = _dict_value(manifest.get("review"))
    paths.extend(_list_value(review.get("evidence_files")))
    details = []
    for value in _unique_strings(paths):
        path = Path(value).expanduser()
        details.append(
            {
                "path": value,
                "name": path.name,
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else None,
            }
        )
    return details


def _valid_run_id(value: str) -> bool:
    return bool(value) and Path(value).name == value and value not in {".", ".."}


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _flatten(values: Sequence[Any]) -> List[Any]:
    result: List[Any] = []
    for value in values:
        if isinstance(value, list):
            result.extend(value)
        else:
            result.append(value)
    return result


def _unique_strings(values: Sequence[Any]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>c-orch Runs</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f2;
      --ink: #17211c;
      --muted: #667067;
      --line: #d8ddd2;
      --panel: #ffffff;
      --panel-soft: #eef3ea;
      --green: #117a55;
      --amber: #b36300;
      --red: #bd2f2f;
      --blue: #256f92;
      --shadow: 0 18px 48px rgba(25, 36, 30, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell {
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(300px, 380px) minmax(0, 1fr);
    }
    .sidebar {
      border-right: 1px solid var(--line);
      background: #fbfcf8;
      padding: 18px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      min-width: 0;
    }
    .topbar, .detailTop {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 18px; font-weight: 760; }
    h2 { font-size: 20px; font-weight: 760; }
    h3 { font-size: 13px; color: var(--muted); font-weight: 700; text-transform: uppercase; }
    button {
      min-height: 34px;
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--ink);
      border-radius: 6px;
      padding: 0 12px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: #aab5a6; }
    .meta { color: var(--muted); font-size: 12px; }
    .list {
      display: grid;
      gap: 8px;
      overflow: auto;
      padding-right: 3px;
    }
    .runItem {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 12px;
      cursor: pointer;
      display: grid;
      gap: 8px;
      min-width: 0;
    }
    .runItem.active {
      border-color: var(--green);
      box-shadow: 0 0 0 2px rgba(17,122,85,.12);
    }
    .runTitle {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: start;
    }
    .runId, code {
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
    }
    .task {
      color: #2c372f;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      border: 1px solid var(--line);
      padding: 2px 8px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }
    .APPROVED { color: var(--green); border-color: rgba(17,122,85,.3); background: rgba(17,122,85,.08); }
    .FAILED { color: var(--red); border-color: rgba(189,47,47,.3); background: rgba(189,47,47,.08); }
    .BLOCKED, .NEEDS_CHANGES { color: var(--amber); border-color: rgba(179,99,0,.3); background: rgba(179,99,0,.10); }
    .WORKING, .REVIEWING, .PLANNING, .WORK_DONE, .PLAN_READY, .PLAN_APPROVED { color: var(--blue); border-color: rgba(37,111,146,.3); background: rgba(37,111,146,.08); }
    .PLAN_REVIEW_REQUIRED { color: var(--amber); border-color: rgba(179,99,0,.3); background: rgba(179,99,0,.10); }
    main {
      padding: 22px;
      min-width: 0;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      gap: 18px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .detail {
      padding: 18px;
      display: grid;
      gap: 16px;
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: var(--panel-soft);
      min-width: 0;
    }
    .metric .value {
      font-size: 15px;
      font-weight: 760;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .steps {
      display: grid;
      grid-template-columns: repeat(10, minmax(42px, 1fr));
      gap: 6px;
    }
    .step {
      height: 8px;
      border-radius: 999px;
      background: #dce3d8;
    }
    .step.on { background: var(--green); }
    .twoCol {
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(280px, .8fr);
      gap: 14px;
      min-width: 0;
    }
    .section {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-width: 0;
      background: #fff;
    }
    .stack {
      display: grid;
      gap: 12px;
      min-width: 0;
    }
    .kv {
      display: grid;
      grid-template-columns: 128px minmax(0, 1fr);
      gap: 8px;
      padding: 6px 0;
      border-bottom: 1px solid #edf0ea;
    }
    .kv:last-child { border-bottom: 0; }
    .mono {
      font-family: "SFMono-Regular", Consolas, monospace;
      overflow-wrap: anywhere;
    }
    .planPrompt {
      margin-top: 8px;
      padding: 10px;
      border: 1px solid #edf0ea;
      border-radius: 6px;
      background: #fafcf8;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    ul {
      margin: 8px 0 0;
      padding-left: 18px;
    }
    li { margin: 5px 0; }
    .timeline {
      list-style: none;
      margin: 8px 0 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }
    .timelineItem {
      border: 1px solid #e7ece3;
      border-radius: 8px;
      padding: 8px;
      background: #fafcf8;
      display: grid;
      gap: 4px;
    }
    .timelineTop {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .empty {
      height: 100%;
      min-height: 320px;
      display: grid;
      place-items: center;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--line);
      border-radius: 8px;
    }
    @media (max-width: 880px) {
      .shell { grid-template-columns: 1fr; }
      .sidebar { border-right: 0; border-bottom: 1px solid var(--line); max-height: 45vh; }
      main { padding: 14px; }
      .grid, .twoCol { grid-template-columns: 1fr; }
      .steps { grid-template-columns: repeat(5, 1fr); }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside class="sidebar">
      <div class="topbar">
        <div>
          <h1>c-orch</h1>
          <p class="meta" id="runsDir"></p>
        </div>
        <button id="refreshBtn" type="button">Refresh</button>
      </div>
      <div class="list" id="runList"></div>
    </aside>
    <main>
      <div class="detailTop">
        <div>
          <h2 id="detailTitle">Runs</h2>
          <p class="meta" id="updatedAt"></p>
        </div>
        <span class="badge" id="detailBadge">WAITING</span>
      </div>
      <div class="steps" id="steps"></div>
      <section class="panel detail" id="detail"></section>
    </main>
  </div>
  <script>
    const statusLabels = [
      "NEW",
      "PLANNING",
      "PLAN_READY",
      "PLAN_REVIEW_REQUIRED",
      "PLAN_APPROVED",
      "WORKING",
      "WORK_DONE",
      "REVIEWING",
      "NEEDS_CHANGES",
      "DONE"
    ];
    let runs = [];
    let selected = null;

    const els = {
      runList: document.getElementById("runList"),
      runsDir: document.getElementById("runsDir"),
      refreshBtn: document.getElementById("refreshBtn"),
      detail: document.getElementById("detail"),
      detailTitle: document.getElementById("detailTitle"),
      detailBadge: document.getElementById("detailBadge"),
      updatedAt: document.getElementById("updatedAt"),
      steps: document.getElementById("steps")
    };

    els.refreshBtn.addEventListener("click", () => loadRuns(true));
    setInterval(() => loadRuns(false), 2500);
    loadRuns(false);

    async function loadRuns(forceFirst) {
      const response = await fetch("/api/runs", { cache: "no-store" });
      const payload = await response.json();
      runs = payload.runs || [];
      els.runsDir.textContent = payload.runs_dir || "";
      if (!selected || forceFirst || !runs.find(run => run.run_id === selected)) {
        selected = runs[0] ? runs[0].run_id : null;
      }
      renderList();
      if (selected) {
        await loadRun(selected);
      } else {
        renderEmpty();
      }
    }

    function renderList() {
      els.runList.innerHTML = "";
      runs.forEach(run => {
        const item = document.createElement("article");
        item.className = "runItem" + (run.run_id === selected ? " active" : "");
        item.onclick = () => { selected = run.run_id; renderList(); loadRun(run.run_id); };
        item.innerHTML = `
          <div class="runTitle">
            <span class="runId">${escapeHtml(run.run_id)}</span>
            <span class="badge ${escapeHtml(run.status)}">${escapeHtml(run.status)}</span>
          </div>
          <div class="task">${escapeHtml(run.user_task || "")}</div>
          <div class="meta">${escapeHtml(run.updated_at || "")}</div>
        `;
        els.runList.appendChild(item);
      });
    }

    async function loadRun(runId) {
      const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
      if (!response.ok) {
        renderEmpty();
        return;
      }
      const payload = await response.json();
      renderDetail(payload);
    }

    function renderDetail(payload) {
      const run = payload.run;
      const manifest = payload.manifest;
      const events = Array.isArray(payload.events) ? payload.events : [];
      els.detailTitle.textContent = run.run_id;
      els.detailBadge.className = `badge ${run.status}`;
      els.detailBadge.textContent = run.status;
      els.updatedAt.textContent = `Updated ${run.updated_at || ""}`;
      renderSteps(run);

      const workerRows = (manifest.workers || []).map(worker => `
        <div class="section">
          <h3>${escapeHtml(worker.id || "worker")}</h3>
          ${kv("Status", worker.status)}
          ${kv("Model", worker.model)}
          ${optionalKv("Reasoning", worker.reasoning_effort)}
          ${optionalKv("Speed", worker.service_tier)}
          ${kv("Thread", worker.thread_id || "")}
          ${kv("Attempt", String(worker.attempt || ""))}
          ${kv("Worktree", worker.worktree_path || "")}
        </div>
      `).join("");

      const evidence = (payload.evidence_files || []).map(file => (
        `<li><span class="mono">${escapeHtml(file.name)}</span> <span class="meta">${file.exists ? `${file.size || 0} bytes` : "missing"}</span></li>`
      )).join("");

      const plan = manifest.plan || null;
      const riskNotes = plan ? (plan.risk_notes || []).map(item => `<li>${escapeHtml(item)}</li>`).join("") : "";
      const criteria = (manifest.acceptance_criteria || []).map(item => `<li>${escapeHtml(item)}</li>`).join("");
      const commands = (manifest.verification_commands || []).map(item => `<li><code>${escapeHtml(item)}</code></li>`).join("");
      const timeline = events.map(event => {
        const details = eventDetails(event);
        return `
          <li class="timelineItem">
            <div class="timelineTop">
              <span class="mono">${escapeHtml(event.timestamp || "")}</span>
              <span class="badge">${escapeHtml(event.type || "")}</span>
            </div>
            <div>${escapeHtml(event.message || "")}</div>
            ${details ? `<div class="meta mono">${escapeHtml(details)}</div>` : ""}
          </li>
        `;
      }).join("");

      els.detail.innerHTML = `
        <div class="grid">
          <div class="metric"><div class="meta">Planner</div><div class="value">${escapeHtml(run.planner.status || "")}</div></div>
          <div class="metric"><div class="meta">Worker</div><div class="value">${escapeHtml((run.workers[0] || {}).status || "")}</div></div>
          <div class="metric"><div class="meta">Review</div><div class="value">${escapeHtml((run.review && run.review.decision) || "none")}</div></div>
          <div class="metric"><div class="meta">Evidence</div><div class="value">${run.evidence_count}</div></div>
        </div>
        <div class="twoCol">
          <div class="section">
            <h3>Task</h3>
            <p>${escapeHtml(manifest.user_task || "")}</p>
            ${plan ? `
              <h3 style="margin-top:14px">Plan</h3>
              ${kv("Approval", plan.approval_status || "")}
              ${plan.approved_at ? kv("Approved", plan.approved_at) : ""}
              ${plan.summary ? `<p>${escapeHtml(plan.summary)}</p>` : ""}
              ${riskNotes ? `<h3 style="margin-top:14px">Risks</h3><ul>${riskNotes}</ul>` : ""}
              ${plan.worker_prompt ? `<h3 style="margin-top:14px">Worker Prompt</h3><div class="planPrompt">${escapeHtml(plan.worker_prompt)}</div>` : ""}
            ` : ""}
            ${criteria ? `<h3 style="margin-top:14px">Acceptance</h3><ul>${criteria}</ul>` : ""}
            ${commands ? `<h3 style="margin-top:14px">Verification</h3><ul>${commands}</ul>` : ""}
          </div>
          <div class="section">
            <h3>Run</h3>
            ${kv("CWD", manifest.cwd)}
            ${kv("Created", manifest.created_at)}
            ${kv("Updated", manifest.updated_at)}
            ${kv("Planner Thread", (manifest.planner || {}).thread_id || "")}
            ${optionalKv("Planner Reasoning", (manifest.planner || {}).reasoning_effort)}
            ${optionalKv("Planner Speed", (manifest.planner || {}).service_tier)}
            ${kv("Codex", manifest.codex_binary_path || "")}
          </div>
        </div>
        <div class="twoCol">
          <div>${workerRows}</div>
          <div class="stack">
            <div class="section">
              <h3>Evidence</h3>
              ${evidence ? `<ul>${evidence}</ul>` : `<p class="meta">No evidence yet.</p>`}
            </div>
            <div class="section">
              <h3>Timeline</h3>
              ${timeline ? `<ul class="timeline">${timeline}</ul>` : `<p class="meta">No events yet.</p>`}
            </div>
          </div>
        </div>
      `;
    }

    function renderSteps(run) {
      els.steps.innerHTML = "";
      const max = run.terminal ? statusLabels.length - 1 : run.status_index;
      statusLabels.forEach((label, index) => {
        const item = document.createElement("div");
        item.className = "step" + (index <= max ? " on" : "");
        item.title = label;
        els.steps.appendChild(item);
      });
    }

    function renderEmpty() {
      els.detailTitle.textContent = "Runs";
      els.detailBadge.className = "badge";
      els.detailBadge.textContent = "EMPTY";
      els.updatedAt.textContent = "";
      els.steps.innerHTML = "";
      els.detail.innerHTML = `<div class="empty">No runs found.</div>`;
    }

    function kv(key, value) {
      return `<div class="kv"><span class="meta">${escapeHtml(key)}</span><span class="mono">${escapeHtml(value || "")}</span></div>`;
    }

    function optionalKv(key, value) {
      return value ? kv(key, value) : "";
    }

    function eventDetails(event) {
      const details = {};
      Object.keys(event || {}).forEach(key => {
        if (key === "timestamp" || key === "type" || key === "message") {
          return;
        }
        const value = event[key];
        if (value === null || value === undefined) {
          return;
        }
        details[key] = value;
      });
      if (!Object.keys(details).length) {
        return "";
      }
      try {
        return JSON.stringify(details);
      } catch (_error) {
        return String(details);
      }
    }

    function escapeHtml(value) {
      return String(value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;");
    }
  </script>
</body>
</html>
"""
