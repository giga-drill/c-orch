import { useEffect, useMemo, useState } from "react";
import type {
  AllowedRunAction,
  AllowedTaskAction,
  AgentSummary,
  EvidenceFile,
  ManifestRecord,
  QueuePayload,
  ReviewAttempt,
  RunEvent,
  RunListItem,
  RunPayload,
  TaskSummary,
  WorkerActivity,
} from "../api/types";
import {
  useQueueActionMutation,
  useQueueQuery,
  useRunActionMutation,
  useRunQuery,
  useRunsQuery,
  useTaskActionMutation,
} from "../queries";

const statusText: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING: "等待中",
  APPROVED: "已完成",
  FAILED: "失败",
  BLOCKED: "阻塞",
  RESTART_REQUIRED: "需要重启",
  NEW: "新建",
  PLANNING: "Planner 规划中",
  PLAN_READY: "计划已生成",
  PLAN_REVIEW_REQUIRED: "等待人工审核计划",
  PLAN_REVISING: "Planner 修改计划中",
  PLAN_APPROVED: "计划已通过",
  WORKING: "Worker 工作中",
  WORK_DONE: "Worker 已完成",
  REVIEWING: "Planner 复核中",
  REVISION_REQUESTED: "Worker 返工中",
};

function formatStatus(value?: string | null): string {
  return value ? statusText[value] ?? value : "-";
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (Array.isArray(value)) return value.length ? value.join(", ") : "-";
  if (typeof value === "string") return value.trim() || "-";
  return String(value);
}

function newestFirst<T>(items?: T[] | null): T[] {
  return Array.isArray(items) ? items.slice().reverse() : [];
}

function pickTaskRun(task: TaskSummary): string | null {
  if (task.active_run_id) return task.active_run_id;
  return task.run_ids.length ? task.run_ids[task.run_ids.length - 1] : null;
}

export function App() {
  const queueQuery = useQueueQuery();
  const runsQuery = useRunsQuery();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const activeRunId = useMemo(() => {
    const task = queueQuery.data?.tasks.find((item) => item.active_run_id);
    return task?.active_run_id ?? null;
  }, [queueQuery.data]);
  const firstRunId = runsQuery.data?.runs[0]?.run_id ?? null;

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRunId(activeRunId ?? firstRunId);
    }
  }, [activeRunId, firstRunId, selectedRunId]);

  const runQuery = useRunQuery(selectedRunId);

  async function refreshAll() {
    await Promise.all([queueQuery.refetch(), runsQuery.refetch(), runQuery.refetch()]);
  }

  const isLoading = queueQuery.isLoading || runsQuery.isLoading;
  const error = queueQuery.error ?? runsQuery.error ?? runQuery.error;

  return (
    <main className="shell">
      <aside className="sidebar">
        <header className="appHeader">
          <div>
            <h1>c-orch</h1>
            <p className="meta">Planner / Worker 运行控制台</p>
          </div>
          <button type="button" onClick={refreshAll} disabled={queueQuery.isFetching || runsQuery.isFetching}>
            刷新
          </button>
        </header>
        {error ? <div className="error">{error.message}</div> : null}
        {isLoading ? <div className="empty">正在加载运行状态...</div> : null}
        <SystemStatus queuePayload={queueQuery.data} runsGeneratedAt={runsQuery.data?.generated_at} />
        <QueuePanel
          payload={queueQuery.data}
          selectedRunId={selectedRunId}
          onSelectRun={setSelectedRunId}
        />
        <RunList runs={runsQuery.data?.runs ?? []} selectedRunId={selectedRunId} onSelectRun={setSelectedRunId} />
      </aside>
      <section className="content">
        <RunDetail payload={runQuery.data} isLoading={runQuery.isLoading} onSelectRun={setSelectedRunId} />
      </section>
    </main>
  );
}

function SystemStatus({
  queuePayload,
  runsGeneratedAt,
}: {
  queuePayload?: QueuePayload;
  runsGeneratedAt?: string;
}) {
  const queueActionMutation = useQueueActionMutation();
  const [queueActionError, setQueueActionError] = useState<string | null>(null);
  const queue = queuePayload?.queue;
  const restartRequired = queue?.status === "RESTART_REQUIRED";

  async function confirmRuntimeRestarted() {
    setQueueActionError(null);
    try {
      await queueActionMutation.mutateAsync({ action: "confirm-runtime-restarted" });
    } catch (error) {
      setQueueActionError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <section className={restartRequired ? "systemStatus restart" : "systemStatus"}>
      <div>
        <span className="label">Queue</span>
        <strong>{formatStatus(queue?.status)}</strong>
      </div>
      <div>
        <span className="label">waiting_for</span>
        <strong>{displayValue(queuePayload?.summary?.current_waiting_point)}</strong>
      </div>
      <div>
        <span className="label">updated</span>
        <strong>{displayValue(queuePayload?.generated_at ?? runsGeneratedAt)}</strong>
      </div>
      {restartRequired ? (
        <>
          <p>当前队列被 restart gate 暂停。请在 runtime 完成重启后确认继续。</p>
          <button
            type="button"
            onClick={confirmRuntimeRestarted}
            disabled={queueActionMutation.isPending}
          >
            {queueActionMutation.isPending ? "确认中..." : "确认已重启并继续"}
          </button>
          {queueActionError ? <div className="error">{queueActionError}</div> : null}
        </>
      ) : null}
    </section>
  );
}

function QueuePanel({
  payload,
  selectedRunId,
  onSelectRun,
}: {
  payload?: QueuePayload;
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}) {
  const taskMutation = useTaskActionMutation();
  const [taskError, setTaskError] = useState<string | null>(null);
  const summary = payload?.summary;

  async function runTaskAction(task: TaskSummary, action: AllowedTaskAction) {
    setTaskError(null);
    try {
      const result = await taskMutation.mutateAsync({ taskId: task.task_id, action });
      const latest = result.tasks.find((item) => item.task_id === task.task_id);
      const runId = latest ? pickTaskRun(latest) : pickTaskRun(task);
      if (runId) onSelectRun(runId);
    } catch (error) {
      setTaskError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <section className="panel">
      <div className="sectionTitle">
        <h2>任务队列</h2>
        <span className="meta">{displayValue(payload?.queue_file)}</span>
      </div>
      {summary ? (
        <div className="metrics compact">
          <Metric label="总数" value={summary.total_tasks} />
          <Metric label="完成" value={summary.completed_tasks} />
          <Metric label="待执行" value={summary.pending_tasks} />
          <Metric label="失败" value={summary.failed_tasks} />
        </div>
      ) : null}
      {taskError ? <div className="error">{taskError}</div> : null}
      <div className="taskList">
        {(payload?.tasks ?? []).map((task) => {
          const taskRunId = pickTaskRun(task);
          const selected = Boolean(taskRunId && taskRunId === selectedRunId);
          return (
            <article key={task.task_id} className={selected ? "taskItem selected" : "taskItem"}>
              <button
                type="button"
                className="taskButton"
                onClick={() => taskRunId && onSelectRun(taskRunId)}
                disabled={!taskRunId}
              >
                <span className="itemTop">
                  <span className="mono">{task.task_id}</span>
                  <StatusBadge status={task.status} />
                </span>
                <span>{task.title}</span>
                <span className="meta">waiting_for: {displayValue(task.waiting_for)}</span>
                <span className="meta">active_run_id: {displayValue(task.active_run_id)}</span>
              </button>
              {task.allowed_actions.includes("retry-task") ? (
                <button
                  type="button"
                  onClick={() => runTaskAction(task, "retry-task")}
                  disabled={taskMutation.isPending}
                >
                  {taskMutation.isPending ? "重新排队中..." : "重新排队执行"}
                </button>
              ) : null}
            </article>
          );
        })}
        {payload && payload.tasks.length === 0 ? <div className="empty">暂无任务。</div> : null}
      </div>
    </section>
  );
}

function RunList({
  runs,
  selectedRunId,
  onSelectRun,
}: {
  runs: RunListItem[];
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}) {
  return (
    <section className="panel runsPanel">
      <div className="sectionTitle">
        <h2>Runs</h2>
        <span className="meta">最新在上</span>
      </div>
      <div className="runList">
        {runs.map((run) => (
          <button
            type="button"
            key={run.run_id}
            className={run.run_id === selectedRunId ? "runItem selected" : "runItem"}
            onClick={() => onSelectRun(run.run_id)}
          >
            <span className="itemTop">
              <span className="mono">{run.run_id}</span>
              <StatusBadge status={run.status} />
            </span>
            <span>{run.user_task}</span>
            <span className="meta">waiting_for: {displayValue(run.waiting_for)}</span>
            <span className="meta">{displayValue(run.updated_at)}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

function RunDetail({
  payload,
  isLoading,
}: {
  payload?: RunPayload;
  isLoading: boolean;
  onSelectRun: (runId: string) => void;
}) {
  const actionMutation = useRunActionMutation();
  const [feedback, setFeedback] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);

  if (isLoading) return <div className="empty">正在加载 run 详情...</div>;
  if (!payload) return <div className="empty">没有选中的 run。</div>;

  const { run, manifest } = payload;

  async function runAction(action: AllowedRunAction, extra: Record<string, unknown> = {}) {
    setActionError(null);
    try {
      await actionMutation.mutateAsync({ runId: run.run_id, action, payload: extra });
      if (action === "revise-plan") setFeedback("");
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    }
  }

  return (
    <div className="detail">
      <header className="detailHeader">
        <div>
          <p className="meta">Run</p>
          <h1>{run.run_id}</h1>
          <p>{run.user_task}</p>
        </div>
        <StatusBadge status={run.status} />
      </header>

      <div className="stateStrip">
        <div>
          <span className="label">waiting_for</span>
          <strong>{displayValue(run.waiting_for)}</strong>
        </div>
        <div>
          <span className="label">next_action</span>
          <strong>{displayValue(run.next_action)}</strong>
        </div>
        <div>
          <span className="label">allowed_actions</span>
          <strong>{run.allowed_actions.length ? run.allowed_actions.join(", ") : "-"}</strong>
        </div>
      </div>

      <ActionBar
        actions={run.allowed_actions}
        isPending={actionMutation.isPending}
        feedback={feedback}
        onFeedbackChange={setFeedback}
        onAction={runAction}
      />
      {actionError ? <div className="error">{actionError}</div> : null}

      <div className="metrics">
        <Metric label="Planner" value={formatStatus(run.planner.status)} />
        <Metric label="Worker" value={formatStatus(run.workers[0]?.status)} />
        <Metric label="复核次数" value={run.review_attempt_count} />
        <Metric label="证据" value={run.evidence_count} />
      </div>

      <div className="detailGrid">
        <section className="section">
          <h2>任务与方案</h2>
          <p>{manifest.user_task}</p>
          <PlanPanel manifest={manifest} />
        </section>
        <section className="section">
          <h2>运行信息</h2>
          <KeyValue label="CWD" value={manifest.cwd} />
          <KeyValue label="创建时间" value={manifest.created_at} />
          <KeyValue label="更新时间" value={manifest.updated_at} />
          <KeyValue label="Codex" value={manifest.codex_binary_path} />
          <KeyValue label="plan_revision_count" value={run.plan_revision_count} />
          <KeyValue label="latest_plan_revision_feedback" value={run.latest_plan_revision_feedback} />
          <KeyValue label="需要重启" value={manifest.requires_restart ? "是" : "否"} />
          {manifest.requires_restart ? <KeyValue label="重启原因" value={manifest.restart_reason} /> : null}
          {manifest.requires_restart ? <KeyValue label="影响路径" value={manifest.restart_paths} /> : null}
        </section>
      </div>

      <div className="agentGrid">
        <AgentCard title="Planner Agent" agent={run.planner} waitingFor={run.waiting_for} />
        {run.workers.length ? (
          run.workers.map((worker) => (
            <AgentCard
              key={worker.id ?? worker.thread_id ?? worker.model}
              title={`Worker Agent · ${displayValue(worker.id)}`}
              agent={worker}
              waitingFor={run.waiting_for}
            />
          ))
        ) : (
          <AgentCard title="Worker Agent" agent={{ status: "-", model: "-", thread_id: null, reasoning_effort: null, service_tier: null }} waitingFor={run.waiting_for} />
        )}
      </div>

      <div className="detailGrid">
        <section className="section">
          <h2>证据文件</h2>
          <EvidenceList files={payload.evidence_files} />
        </section>
        <section className="section">
          <h2>Worker 活动</h2>
          <ActivityList items={payload.worker_activity} />
        </section>
        <section className="section">
          <h2>复核记录</h2>
          <ReviewAttempts attempts={manifest.review_attempts} />
        </section>
        <section className="section">
          <h2>运行时间线</h2>
          <Timeline events={payload.events} />
        </section>
      </div>
    </div>
  );
}

function ActionBar({
  actions,
  isPending,
  feedback,
  onFeedbackChange,
  onAction,
}: {
  actions: AllowedRunAction[];
  isPending: boolean;
  feedback: string;
  onFeedbackChange: (value: string) => void;
  onAction: (action: AllowedRunAction, extra?: Record<string, unknown>) => void;
}) {
  if (!actions.length) return null;
  return (
    <section className="actionBar">
      {actions.includes("approve-plan") ? (
        <button type="button" onClick={() => onAction("approve-plan")} disabled={isPending}>
          {isPending ? "处理中..." : "通过并启动 Worker"}
        </button>
      ) : null}
      {actions.includes("retry-review") ? (
        <button type="button" onClick={() => onAction("retry-review")} disabled={isPending}>
          {isPending ? "处理中..." : "重新让 Planner 复核"}
        </button>
      ) : null}
      {actions.includes("revise-plan") ? (
        <div className="revisionBox">
          <textarea
            value={feedback}
            onChange={(event) => onFeedbackChange(event.target.value)}
            placeholder="请输入修改意见，Planner 会在同一线程里重写完整方案。"
          />
          <button
            type="button"
            onClick={() => onAction("revise-plan", { feedback })}
            disabled={isPending || !feedback.trim()}
          >
            {isPending ? "提交中..." : "让 Planner 重新生成计划"}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function PlanPanel({ manifest }: { manifest: ManifestRecord }) {
  const plan = manifest.plan;
  return (
    <div className="planPanel">
      {plan ? (
        <>
          <KeyValue label="审批" value={plan.approval_status} />
          {plan.approved_at ? <KeyValue label="通过时间" value={plan.approved_at} /> : null}
          {plan.summary ? <p>{plan.summary}</p> : null}
          <Collapsible title="Worker 指令">
            <pre>{plan.worker_prompt}</pre>
          </Collapsible>
          <Collapsible title="风险说明">
            <BulletList items={plan.risk_notes} />
          </Collapsible>
        </>
      ) : (
        <p className="meta">暂无 Planner 方案。</p>
      )}
      <Collapsible title="验收标准">
        <BulletList items={manifest.acceptance_criteria} />
      </Collapsible>
      <Collapsible title="验证命令">
        <BulletList items={manifest.verification_commands} code />
      </Collapsible>
    </div>
  );
}

function AgentCard({ title, agent, waitingFor }: { title: string; agent: AgentSummary; waitingFor: string }) {
  return (
    <section className="agentCard">
      <h2>{title}</h2>
      <KeyValue label="status" value={agent.status} />
      <KeyValue label="model" value={agent.model} />
      <KeyValue label="thread_id" value={agent.thread_id} />
      <KeyValue label="reasoning_effort" value={agent.reasoning_effort} />
      <KeyValue label="service_tier" value={agent.service_tier} />
      <KeyValue label="attempt" value={agent.attempt} />
      <KeyValue label="worktree_path" value={agent.worktree_path} />
      <KeyValue label="evidence_count" value={agent.evidence_count} />
      <KeyValue label="waiting_for" value={waitingFor} />
    </section>
  );
}

function EvidenceList({ files }: { files: EvidenceFile[] }) {
  if (!files.length) return <p className="meta">暂无证据。</p>;
  return (
    <ul className="plainList">
      {files.map((file) => (
        <li key={file.path}>
          <span className="mono">{file.name}</span>
          <span className="meta">{file.exists ? `${file.size ?? 0} bytes` : "缺失"}</span>
        </li>
      ))}
    </ul>
  );
}

function ActivityList({ items }: { items: WorkerActivity[] }) {
  if (!items.length) return <p className="meta">暂无 Worker 活动。</p>;
  return (
    <ul className="timeline">
      {newestFirst(items).map((item, index) => (
        <li key={`${item.timestamp ?? "activity"}-${index}`} className="timelineItem">
          <div className="timelineTop">
            <span className="mono">{displayValue(item.timestamp)}</span>
            <StatusBadge status={item.label ?? item.kind ?? "-"} />
          </div>
          <p className="meta">{displayValue(item.detail)}</p>
        </li>
      ))}
    </ul>
  );
}

function ReviewAttempts({ attempts }: { attempts: ReviewAttempt[] }) {
  if (!attempts.length) return <p className="meta">暂无复核记录。</p>;
  return (
    <ul className="timeline">
      {newestFirst(attempts).map((attempt) => (
        <li key={attempt.id} className="timelineItem">
          <div className="timelineTop">
            <StatusBadge status={attempt.status} />
            <span className="mono">{attempt.id}</span>
            <span className="meta">{displayValue(attempt.completed_at ?? attempt.started_at)}</span>
          </div>
          <KeyValue label="worker_attempt" value={attempt.worker_attempt} />
          <KeyValue label="workspace" value={attempt.workspace_path} />
          <KeyValue label="decision" value={attempt.decision} />
          {attempt.reason ? <p>{attempt.reason}</p> : null}
          {attempt.error ? <p className="error">{attempt.error}</p> : null}
        </li>
      ))}
    </ul>
  );
}

function Timeline({ events }: { events: RunEvent[] }) {
  if (!events.length) return <p className="meta">暂无事件。</p>;
  return (
    <ul className="timeline">
      {newestFirst(events).map((event, index) => (
        <li key={`${event.timestamp ?? "event"}-${event.type ?? index}`} className="timelineItem">
          <div className="timelineTop">
            <span className="mono">{displayValue(event.timestamp)}</span>
            <StatusBadge status={event.type ?? "-"} />
          </div>
          <p>{displayValue(event.message)}</p>
          <p className="meta mono">{eventDetails(event)}</p>
        </li>
      ))}
    </ul>
  );
}

function eventDetails(event: RunEvent): string {
  const details = Object.fromEntries(
    Object.entries(event).filter(
      ([key, value]) =>
        !["timestamp", "type", "message"].includes(key) && value !== null && value !== undefined,
    ),
  );
  return Object.keys(details).length ? JSON.stringify(details) : "";
}

function Metric({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="metric">
      <span className="label">{label}</span>
      <strong>{displayValue(value)}</strong>
    </div>
  );
}

function KeyValue({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="kv">
      <span className="meta">{label}</span>
      <span className="mono">{displayValue(value)}</span>
    </div>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  return <span className={`badge ${status ?? ""}`}>{formatStatus(status)}</span>;
}

function Collapsible({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <details>
      <summary>{title}</summary>
      <div>{children}</div>
    </details>
  );
}

function BulletList({ items, code = false }: { items: string[]; code?: boolean }) {
  if (!items.length) return <p className="meta">暂无。</p>;
  return (
    <ul>
      {items.map((item) => (
        <li key={item}>{code ? <code>{item}</code> : item}</li>
      ))}
    </ul>
  );
}
