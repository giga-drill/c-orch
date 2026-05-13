from __future__ import annotations

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union
from urllib.parse import unquote

from .runtime import COrchRuntime
from .scheduler import SchedulerConfig
from .settings import DEFAULT_UI_HOST, DEFAULT_UI_PORT


Pathish = Union[str, Path]


def serve_dashboard(
    *,
    runs_dir: Pathish,
    queue_path: Optional[Pathish] = None,
    scheduler_config: Optional[SchedulerConfig] = None,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
) -> None:
    server = build_server(
        runs_dir=runs_dir,
        queue_path=queue_path,
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
    scheduler_config: Optional[SchedulerConfig] = None,
    host: str = DEFAULT_UI_HOST,
    port: int = DEFAULT_UI_PORT,
) -> ThreadingHTTPServer:
    runs_path = Path(runs_dir).expanduser().resolve()
    queue_file = Path(queue_path).expanduser().resolve() if queue_path is not None else None
    runtime = COrchRuntime(
        runs_dir=runs_path,
        queue_path=queue_file,
        scheduler_config=scheduler_config,
    )
    handler = make_dashboard_handler(runtime)
    server = ThreadingHTTPServer((host, port), handler)
    setattr(server, "c_orch_runtime", runtime)
    runtime.dispatch_queue_async()
    return server


def make_dashboard_handler(runtime: COrchRuntime) -> Type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/":
                self._send_text(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
                return
            if path == "/api/runs":
                self._send_json(HTTPStatus.OK, runtime.build_runs_payload())
                return
            if path == "/api/queue":
                self._send_json(HTTPStatus.OK, runtime.build_queue_payload())
                return
            if path.startswith("/api/runs/"):
                run_id = unquote(path[len("/api/runs/"):])
                payload = runtime.build_run_payload(run_id)
                if payload is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "run not found"})
                    return
                self._send_json(HTTPStatus.OK, payload)
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            run_prefix = "/api/runs/"
            task_prefix = "/api/tasks/"
            suffix = "/actions"
            if not path.endswith(suffix) or not (
                path.startswith(run_prefix) or path.startswith(task_prefix)
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
            if path.startswith(run_prefix):
                run_id = unquote(path[len(run_prefix):-len(suffix)])
                result = runtime.run_action(run_id, action, data.get("feedback"))
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


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>c-orch 运行面板</title>
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
    .queueBlock {
      border-top: 1px solid var(--line);
      padding-top: 10px;
      display: grid;
      gap: 8px;
    }
    .queueList {
      display: grid;
      gap: 6px;
      max-height: 32vh;
      overflow: auto;
      padding-right: 3px;
    }
    .queueItem {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      padding: 8px;
      display: grid;
      gap: 4px;
    }
    .queueTop {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
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
    .REVISION_REQUESTED { color: var(--amber); border-color: rgba(179,99,0,.3); background: rgba(179,99,0,.10); }
    .WORKING, .REVIEWING, .PLANNING, .WORK_DONE, .PLAN_READY, .PLAN_APPROVED { color: var(--blue); border-color: rgba(37,111,146,.3); background: rgba(37,111,146,.08); }
    .PLAN_REVIEW_REQUIRED { color: var(--amber); border-color: rgba(179,99,0,.3); background: rgba(179,99,0,.10); }
    .PLAN_REVISING { color: var(--amber); border-color: rgba(179,99,0,.3); background: rgba(179,99,0,.10); }
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
    .planFeedback {
      margin-top: 8px;
      width: 100%;
      min-height: 108px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      resize: vertical;
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
        <button id="refreshBtn" type="button">刷新</button>
      </div>
      <div class="list" id="runList"></div>
      <section class="queueBlock">
        <h3>Task Queue</h3>
        <p class="meta" id="queueMeta"></p>
        <div class="queueList" id="queueList"></div>
      </section>
    </aside>
    <main>
      <div class="detailTop">
        <div>
          <h2 id="detailTitle">运行</h2>
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
      "PLAN_REVISING",
      "PLAN_APPROVED",
      "WORKING",
      "WORK_DONE",
      "REVIEWING",
      "REVISION_REQUESTED",
      "DONE"
    ];
    const statusText = {
      PENDING: "待执行",
      RUNNING: "执行中",
      WAITING: "等待中",
      NEW: "新建",
      PLANNING: "规划中",
      PLAN_READY: "方案已生成",
      PLAN_REVIEW_REQUIRED: "等待人工审方案",
      PLAN_REVISING: "Planner 修订方案中",
      PLAN_APPROVED: "方案已通过",
      WORKING: "Worker 执行中",
      WORK_DONE: "Worker 已完成，等待复核",
      REVIEWING: "Planner 复核中",
      REVISION_REQUESTED: "等待 Worker 修改",
      APPROVED: "已通过",
      FAILED: "失败",
      RESTART_REQUIRED: "需要重启"
    };
    let runs = [];
    let queueData = null;
    let selected = null;
    const pendingActions = new Set();

    const els = {
      runList: document.getElementById("runList"),
      queueMeta: document.getElementById("queueMeta"),
      queueList: document.getElementById("queueList"),
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
      await loadQueue();
      if (selected) {
        await loadRun(selected);
      } else {
        renderEmpty();
      }
    }

    async function loadQueue() {
      const response = await fetch("/api/queue", { cache: "no-store" });
      const payload = await response.json();
      queueData = payload;
      const queue = payload.queue;
      if (!queue) {
        els.queueMeta.textContent = payload.queue_file ? `未加载: ${payload.queue_file}` : "未配置 queue file";
        els.queueList.innerHTML = `<div class="meta">暂无任务队列。</div>`;
        return;
      }
      const summary = payload.summary || {};
      const summaryText = `total ${summary.total_tasks ?? 0} · done ${summary.approved_tasks ?? 0} · pending ${summary.pending_tasks ?? 0} · failed ${summary.failed_tasks ?? 0} · waiting ${summary.current_waiting_point || "-"}`;
      els.queueMeta.textContent = `${queue.queue_id} · ${formatStatus(queue.status)} · ${summaryText} · ${payload.queue_file || ""}`;
      const tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      els.queueList.innerHTML = tasks.map(task => `
        <article class="queueItem">
          <div class="queueTop">
            <span class="runId">${escapeHtml(task.task_id || "")}</span>
            <span class="badge ${escapeHtml(task.status || "")}">${escapeHtml(formatStatus(task.status || ""))}</span>
          </div>
          <div>${escapeHtml(task.title || "")}</div>
          <div class="meta">waiting: ${escapeHtml(task.waiting_for || "-")}</div>
          <div class="meta">run: ${escapeHtml(task.active_run_id || "-")}</div>
          <div class="meta">runs: ${escapeHtml((task.run_ids || []).join(", "))}</div>
          ${task.reason ? `<div class="meta">reason: ${escapeHtml(task.reason)}</div>` : ""}
          ${task.error ? `<div class="meta">error: ${escapeHtml(task.error)}</div>` : ""}
          ${task.status === "FAILED" ? `<button data-task-action="retry-task" data-task-id="${escapeHtml(task.task_id || "")}">重新排队</button>` : ""}
        </article>
      `).join("") || `<div class="meta">暂无任务。</div>`;
      document.querySelectorAll("[data-task-action]").forEach(button => {
        button.onclick = () => runTaskAction(button.dataset.taskId, button.dataset.taskAction);
      });
    }

    async function runTaskAction(taskId, action) {
      if (!taskId || !action) return;
      const response = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action })
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        window.alert(payload.error || `任务操作失败: ${response.status}`);
        return;
      }
      await loadQueue();
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
            <span class="badge ${escapeHtml(run.status)}">${escapeHtml(formatStatus(run.status))}</span>
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

    async function runAction(runId, action) {
      return runActionWithPayload(runId, action, {});
    }

    async function runActionWithPayload(runId, action, actionPayload) {
      const key = `${runId}:${action}`;
      if (pendingActions.has(key)) {
        return;
      }
      pendingActions.add(key);
      setActionButtonsDisabled(true);
      try {
        const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/actions`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          cache: "no-store",
          body: JSON.stringify({ action, ...actionPayload })
        });
        if (!response.ok) {
          const text = await response.text();
          alert(text);
          return;
        }
        const resultPayload = await response.json();
        renderDetail(resultPayload);
        await loadRuns(false);
      } finally {
        pendingActions.delete(key);
        setActionButtonsDisabled(false);
      }
    }

    async function runRevisePlan(runId) {
      const input = document.getElementById(`planFeedback-${runId}`);
      const feedback = input && typeof input.value === "string" ? input.value.trim() : "";
      if (!feedback) {
        alert("请先填写修改意见。");
        return;
      }
      await runActionWithPayload(runId, "revise-plan", { feedback });
    }

    function renderDetail(payload) {
      const run = payload.run;
      const manifest = payload.manifest;
      const events = Array.isArray(payload.events) ? payload.events : [];
      const restartRequired = Boolean(manifest.requires_restart);
      const restartPaths = Array.isArray(manifest.restart_paths) ? manifest.restart_paths : [];
      els.detailTitle.textContent = run.run_id;
      els.detailBadge.className = `badge ${run.status}`;
      els.detailBadge.textContent = formatStatus(run.status);
      els.updatedAt.textContent = `更新于 ${run.updated_at || ""}`;
      renderSteps(run);

      const workerRows = (manifest.workers || []).map(worker => `
        <div class="section">
          <h3>${escapeHtml(worker.id || "worker")}</h3>
          ${kv("状态", formatStatus(worker.status))}
          ${kv("模型", worker.model)}
          ${optionalKv("推理强度", worker.reasoning_effort)}
          ${optionalKv("响应速度", worker.service_tier)}
          ${kv("线程", worker.thread_id || "")}
          ${kv("尝试次数", String(worker.attempt || ""))}
          ${kv("工作区", worker.worktree_path || "")}
        </div>
      `).join("");

      const evidence = (payload.evidence_files || []).map(file => (
        `<li><span class="mono">${escapeHtml(file.name)}</span> <span class="meta">${file.exists ? `${file.size || 0} bytes` : "缺失"}</span></li>`
      )).join("");

      const plan = manifest.plan || null;
      const revisePanel = run.status === "PLAN_REVIEW_REQUIRED" && plan ? `
        <div style="margin-top:10px">
          <textarea class="planFeedback" id="planFeedback-${escapeHtml(run.run_id)}" placeholder="请输入修改意见，Planner 会在同一线程里重写完整方案 JSON"></textarea>
          <div style="margin-top:8px">
            <button type="button" data-run-action="revise-plan" onclick="runRevisePlan('${escapeJs(run.run_id)}')">让 Planner 重新生成计划</button>
          </div>
        </div>
      ` : "";
      const actionButtons = [
        run.status === "PLAN_REVIEW_REQUIRED" && plan
          ? `<button type="button" data-run-action="approve-plan" onclick="runAction('${escapeJs(run.run_id)}', 'approve-plan')">通过并启动 Worker</button>${revisePanel}`
          : "",
        run.can_retry_review
          ? `<button type="button" data-run-action="retry-review" onclick="runAction('${escapeJs(run.run_id)}', 'retry-review')">重新让 Planner 复核</button>`
          : ""
      ].filter(Boolean).join(" ");
      const reviewAttempts = (manifest.review_attempts || []).map(attempt => `
        <li class="timelineItem">
          <div class="timelineTop">
            <span class="badge">${escapeHtml(attempt.status || "")}</span>
            <span class="mono">${escapeHtml(attempt.id || "")}</span>
            <span class="meta">${escapeHtml(attempt.completed_at || attempt.started_at || "")}</span>
          </div>
          <div class="meta">worker_attempt: ${escapeHtml(String(attempt.worker_attempt || "-"))}</div>
          <div class="meta">workspace: ${escapeHtml(attempt.workspace_path || "-")}</div>
          <div class="meta">evidence: ${escapeHtml(String((attempt.evidence_files || []).length))}</div>
          ${attempt.reason ? `<div>${escapeHtml(attempt.reason)}</div>` : ""}
          ${attempt.error ? `<div class="meta mono">${escapeHtml(attempt.error)}</div>` : ""}
        </li>
      `).join("");
      const activity = (payload.worker_activity || []).map(item => `
        <li class="timelineItem">
          <div class="timelineTop">
            <span class="mono">${escapeHtml(item.timestamp || "")}</span>
            <span class="badge">${escapeHtml(item.label || item.kind || "")}</span>
            <span class="meta">${escapeHtml(item.worker_id || "")}</span>
          </div>
          ${item.detail ? `<div class="meta mono">${escapeHtml(item.detail)}</div>` : ""}
        </li>
      `).join("");
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
        ${actionButtons ? `<div class="section">${actionButtons}</div>` : ""}
        <div class="grid">
          <div class="metric"><div class="meta">Planner</div><div class="value">${escapeHtml(formatStatus(run.planner.status || ""))}</div></div>
          <div class="metric"><div class="meta">Worker</div><div class="value">${escapeHtml(formatStatus((run.workers[0] || {}).status || ""))}</div></div>
          <div class="metric"><div class="meta">复核</div><div class="value">${escapeHtml((run.review && run.review.decision) || "暂无")}</div></div>
          <div class="metric"><div class="meta">证据</div><div class="value">${run.evidence_count}</div></div>
        </div>
        <div class="twoCol">
          <div class="section">
            <h3>任务</h3>
            <p>${escapeHtml(manifest.user_task || "")}</p>
            ${plan ? `
              <h3 style="margin-top:14px">方案</h3>
              ${kv("审批", plan.approval_status || "")}
              ${plan.approved_at ? kv("通过时间", plan.approved_at) : ""}
              ${plan.summary ? `<p>${escapeHtml(plan.summary)}</p>` : ""}
              ${riskNotes ? `<h3 style="margin-top:14px">风险</h3><ul>${riskNotes}</ul>` : ""}
              ${plan.worker_prompt ? `<h3 style="margin-top:14px">Worker 指令</h3><div class="planPrompt">${escapeHtml(plan.worker_prompt)}</div>` : ""}
            ` : ""}
            ${criteria ? `<h3 style="margin-top:14px">验收标准</h3><ul>${criteria}</ul>` : ""}
            ${commands ? `<h3 style="margin-top:14px">验证命令</h3><ul>${commands}</ul>` : ""}
          </div>
          <div class="section">
            <h3>运行信息</h3>
            ${kv("CWD", manifest.cwd)}
            ${kv("创建时间", manifest.created_at)}
            ${kv("更新时间", manifest.updated_at)}
            ${kv("Planner 线程", (manifest.planner || {}).thread_id || "")}
            ${optionalKv("Planner 推理强度", (manifest.planner || {}).reasoning_effort)}
            ${optionalKv("Planner 响应速度", (manifest.planner || {}).service_tier)}
            ${kv("需要重启", restartRequired ? "是" : "否")}
            ${restartRequired && manifest.restart_reason ? kv("重启原因", manifest.restart_reason) : ""}
            ${restartRequired && restartPaths.length ? kv("影响路径", restartPaths.join(", ")) : ""}
            ${kv("Codex", manifest.codex_binary_path || "")}
          </div>
        </div>
        <div class="twoCol">
          <div>${workerRows}</div>
          <div class="stack">
            <div class="section">
              <h3>证据文件</h3>
              ${evidence ? `<ul>${evidence}</ul>` : `<p class="meta">暂无证据。</p>`}
            </div>
            <div class="section">
              <h3>Worker 活动</h3>
              ${activity ? `<ul class="timeline">${activity}</ul>` : `<p class="meta">暂无 Worker 活动。</p>`}
            </div>
            <div class="section">
              <h3>复核记录</h3>
              ${reviewAttempts ? `<ul class="timeline">${reviewAttempts}</ul>` : `<p class="meta">暂无复核记录。</p>`}
            </div>
            <div class="section">
              <h3>运行时间线</h3>
              ${timeline ? `<ul class="timeline">${timeline}</ul>` : `<p class="meta">暂无事件。</p>`}
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
      els.detailTitle.textContent = "运行";
      els.detailBadge.className = "badge";
      els.detailBadge.textContent = "空";
      els.updatedAt.textContent = "";
      els.steps.innerHTML = "";
      els.detail.innerHTML = `<div class="empty">没有找到运行记录。</div>`;
    }

    function kv(key, value) {
      return `<div class="kv"><span class="meta">${escapeHtml(key)}</span><span class="mono">${escapeHtml(value || "")}</span></div>`;
    }

    function optionalKv(key, value) {
      return value ? kv(key, value) : "";
    }

    function setActionButtonsDisabled(disabled) {
      document.querySelectorAll("[data-run-action]").forEach(button => {
        button.disabled = Boolean(disabled);
      });
    }

    function formatStatus(value) {
      return statusText[value] || value || "";
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

    function escapeJs(value) {
      return String(value).replace(/\\\\/g, "\\\\\\\\").replace(/'/g, "\\\\'");
    }
  </script>
</body>
</html>
"""
