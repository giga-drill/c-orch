import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import type {
  AllowedRunAction,
  AllowedTaskAction,
  AgentSummary,
  EvidenceFile,
  ManifestRecord,
  ProposalPlanDetail,
  ProposalRecord,
  ProposalsPayload,
  QueuePayload,
  ReviewAttempt,
  RunEvent,
  RunListItem,
  RunPayload,
  TaskSummary,
  WorkerActivity,
} from "../api/types";
import {
  useCreateProposalMutation,
  useDashboardStateQuery,
  useProposalActionMutation,
  useQueueActionMutation,
  useRunActionMutation,
  useRunQuery,
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
  const stateQuery = useDashboardStateQuery();
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [manualSelection, setManualSelection] = useState(false);
  const detailRef = useRef<HTMLElement | null>(null);
  const runtimeGenerationRef = useRef<string | null>(null);
  const queuePayload = stateQuery.data?.queue;
  const proposalsPayload = stateQuery.data?.proposals;
  const runsPayload = stateQuery.data?.runs;
  const focusedRunId = stateQuery.data?.focused_run_id ?? null;
  const firstRunId = runsPayload?.runs[0]?.run_id ?? null;
  const selectedRunExists = Boolean(
    selectedRunId && runsPayload?.runs.some((run) => run.run_id === selectedRunId),
  );

  useEffect(() => {
    if (!manualSelection && focusedRunId && selectedRunId !== focusedRunId) {
      setSelectedRunId(focusedRunId);
      return;
    }
    if (!selectedRunId || !selectedRunExists) {
      setSelectedRunId(focusedRunId ?? firstRunId);
    }
  }, [focusedRunId, firstRunId, manualSelection, selectedRunExists, selectedRunId]);

  useEffect(() => {
    const generation = stateQuery.data?.runtime.generation;
    if (!generation) return;
    if (runtimeGenerationRef.current && runtimeGenerationRef.current !== generation) {
      void stateQuery.refetch();
    }
    runtimeGenerationRef.current = generation;
  }, [stateQuery.data?.runtime.generation]);

  const runQuery = useRunQuery(selectedRunId);
  const stateSelectedRun = stateQuery.data?.selected_run;
  const runPayload =
    stateSelectedRun && stateSelectedRun.run.run_id === selectedRunId
      ? stateSelectedRun
      : runQuery.data;
  const isRunLoading =
    Boolean(selectedRunId) &&
    !runPayload &&
    (runQuery.isLoading || runQuery.isFetching || stateQuery.isLoading);

  function selectRun(runId: string) {
    setManualSelection(true);
    setSelectedRunId(runId);
    window.setTimeout(() => {
      detailRef.current?.scrollIntoView({ block: "start", behavior: "smooth" });
    }, 0);
  }

  async function refreshAll() {
    await Promise.all([
      stateQuery.refetch(),
      runQuery.refetch(),
    ]);
  }

  const isLoading = stateQuery.isLoading;
  const error = stateQuery.error ?? runQuery.error;

  return (
    <main className="shell">
      <aside className="sidebar">
        <header className="appHeader">
          <div>
            <h1>c-orch</h1>
            <p className="meta">Planner / Worker 运行控制台</p>
          </div>
          <button
            type="button"
            onClick={refreshAll}
            disabled={stateQuery.isFetching}
          >
            刷新
          </button>
        </header>
        {error ? <div className="error">{error.message}</div> : null}
        {isLoading ? <div className="empty">正在加载运行状态...</div> : null}
        <SystemStatus
          queuePayload={queuePayload}
          runsGeneratedAt={runsPayload?.generated_at}
          runtimeGeneration={stateQuery.data?.runtime.generation}
          stateVersion={stateQuery.data?.version}
        />
        <ProposalPanel
          payload={proposalsPayload}
          onSelectRun={selectRun}
        />
        <QueuePanel
          payload={queuePayload}
          selectedRunId={selectedRunId}
          onSelectRun={selectRun}
        />
        <RunList runs={runsPayload?.runs ?? []} selectedRunId={selectedRunId} onSelectRun={selectRun} />
      </aside>
      <section className="content" ref={detailRef}>
        <RunDetail
          payload={runPayload}
          isLoading={isRunLoading}
          selectedRunId={selectedRunId}
          onSelectRun={selectRun}
        />
      </section>
    </main>
  );
}

function SystemStatus({
  queuePayload,
  runsGeneratedAt,
  runtimeGeneration,
  stateVersion,
}: {
  queuePayload?: QueuePayload;
  runsGeneratedAt?: string;
  runtimeGeneration?: string;
  stateVersion?: number;
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
      <div>
        <span className="label">state_version</span>
        <strong>{displayValue(stateVersion)}</strong>
      </div>
      <div>
        <span className="label">runtime</span>
        <strong>{displayValue(runtimeGeneration)}</strong>
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

function ProposalPanel({
  payload,
  onSelectRun,
}: {
  payload?: ProposalsPayload;
  onSelectRun: (runId: string) => void;
}) {
  const createMutation = useCreateProposalMutation();
  const actionMutation = useProposalActionMutation();
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [feedbackById, setFeedbackById] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const proposalsNewestFirst = newestFirst(payload?.proposals ?? []);
  const summary = payload?.summary;

  async function createProposal() {
    setError(null);
    try {
      await createMutation.mutateAsync({ title: title.trim(), prompt: prompt.trim() });
      setTitle("");
      setPrompt("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function proposalAction(proposal: ProposalRecord, action: "approve-plan" | "revise-plan") {
    setError(null);
    const feedback = feedbackById[proposal.proposal_id] ?? "";
    try {
      await actionMutation.mutateAsync({
        proposalId: proposal.proposal_id,
        action,
        payload: action === "revise-plan" ? { feedback } : {},
      });
      if (action === "revise-plan") {
        setFeedbackById((current) => ({ ...current, [proposal.proposal_id]: "" }));
      }
      if (proposal.run_id) onSelectRun(proposal.run_id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  return (
    <section className="panel">
      <div className="sectionTitle">
        <h2>待审核计划</h2>
        <span className="meta">{displayValue(payload?.proposals_file)}</span>
      </div>
      {summary ? (
        <div className="queueStats" aria-label="计划池统计">
          <span>总数 {summary.total_proposals}</span>
          <span>待审 {summary.review_required}</span>
          <span>失败 {summary.failed}</span>
        </div>
      ) : null}
      <div className="proposalForm">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="任务标题"
        />
        <textarea
          value={prompt}
          onChange={(event) => setPrompt(event.target.value)}
          placeholder="任务说明。Planner 会先生成方案，等待你审核后才进入执行队列。"
        />
        <button
          type="button"
          onClick={createProposal}
          disabled={createMutation.isPending || !title.trim() || !prompt.trim()}
        >
          {createMutation.isPending ? "生成计划中..." : "生成 Planner 方案"}
        </button>
      </div>
      {error ? <div className="error">{error}</div> : null}
      <div className="taskList">
        {proposalsNewestFirst.map((proposal) => (
          <article key={proposal.proposal_id} className="taskItem">
            <button
              type="button"
              className="taskButton"
              onClick={() => proposal.run_id && onSelectRun(proposal.run_id)}
              disabled={!proposal.run_id}
            >
              <span className="itemTop">
                <span className="mono">{proposal.proposal_id}</span>
                <StatusBadge status={proposal.status} />
              </span>
              <span>{proposal.title}</span>
              <span className="meta">waiting_for: {displayValue(proposal.waiting_for)}</span>
              <span className="meta">run_id: {displayValue(proposal.run_id)}</span>
            </button>
            <ProposalPlanPanel plan={proposal.plan_detail} fallbackSummary={proposal.run?.plan?.summary} />
            {proposal.allowed_actions.includes("approve-plan") ? (
              <div className="proposalActions">
                <button
                  type="button"
                  onClick={() => proposalAction(proposal, "approve-plan")}
                  disabled={actionMutation.isPending}
                >
                  {actionMutation.isPending ? "处理中..." : "通过并加入执行队列"}
                </button>
                <textarea
                  value={feedbackById[proposal.proposal_id] ?? ""}
                  onChange={(event) =>
                    setFeedbackById((current) => ({
                      ...current,
                      [proposal.proposal_id]: event.target.value,
                    }))
                  }
                  placeholder="修改意见"
                />
                <button
                  type="button"
                  onClick={() => proposalAction(proposal, "revise-plan")}
                  disabled={actionMutation.isPending || !(feedbackById[proposal.proposal_id] ?? "").trim()}
                >
                  让 Planner 修改方案
                </button>
              </div>
            ) : null}
          </article>
        ))}
        {payload && payload.proposals.length === 0 ? <div className="empty">暂无待审核计划。</div> : null}
      </div>
    </section>
  );
}

function ProposalPlanPanel({
  plan,
  fallbackSummary,
}: {
  plan?: ProposalPlanDetail | null;
  fallbackSummary?: string | null;
}) {
  if (!plan) {
    return fallbackSummary ? <p>{fallbackSummary}</p> : null;
  }
  return (
    <div className="proposalPlan">
      {plan.summary ? <p>{plan.summary}</p> : null}
      <Collapsible title="完整 Planner 方案" open>
        {plan.worker_prompt ? <pre>{plan.worker_prompt}</pre> : <p className="meta">暂无 Worker 指令。</p>}
      </Collapsible>
      <Collapsible title="验收标准">
        <BulletList items={plan.acceptance_criteria} />
      </Collapsible>
      <Collapsible title="验证命令">
        <BulletList items={plan.verification_commands} code />
      </Collapsible>
      <Collapsible title="风险说明">
        <BulletList items={plan.risk_notes} />
      </Collapsible>
    </div>
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
  const tasksNewestFirst = newestFirst(payload?.tasks ?? []);

  async function runTaskAction(task: TaskSummary, action: AllowedTaskAction) {
    setTaskError(null);
    try {
      const result = await taskMutation.mutateAsync({ taskId: task.task_id, action });
      const latest = result.state?.queue.tasks.find((item) => item.task_id === task.task_id);
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
        <div className="queueStats" aria-label="任务队列统计">
          <span>总数 {summary.total_tasks}</span>
          <span>完成 {summary.completed_tasks}</span>
          <span>待执行 {summary.pending_tasks}</span>
          <span>失败 {summary.failed_tasks}</span>
        </div>
      ) : null}
      {taskError ? <div className="error">{taskError}</div> : null}
      <div className="taskList">
        {tasksNewestFirst.map((task) => {
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
  selectedRunId,
}: {
  payload?: RunPayload;
  isLoading: boolean;
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}) {
  const actionMutation = useRunActionMutation();
  const [actionError, setActionError] = useState<string | null>(null);

  if (isLoading) {
    return <div className="empty">正在加载 run 详情: {displayValue(selectedRunId)}</div>;
  }
  if (!payload) return <div className="empty">没有选中的 run。</div>;
  if (selectedRunId && payload.run.run_id !== selectedRunId) {
    return <div className="empty">正在切换 run 详情: {selectedRunId}</div>;
  }

  const { run, manifest } = payload;

  async function runAction(action: AllowedRunAction, extra: Record<string, unknown> = {}) {
    setActionError(null);
    try {
      await actionMutation.mutateAsync({ runId: run.run_id, action, payload: extra });
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
        onAction={runAction}
      />
      {actionError ? <div className="error">{actionError}</div> : null}
      <FailurePanel run={run} events={payload.events} files={payload.evidence_files} />

      <div className="metrics">
        <Metric label="Planner" value={formatStatus(run.planner.status)} />
        <Metric label="Worker" value={formatStatus(run.workers[0]?.status)} />
        <Metric label="复核次数" value={run.review_attempt_count} />
        <Metric label="证据" value={run.evidence_count} />
      </div>

      <div className="detailGrid">
        <section className="section">
          <h2>任务</h2>
          <p>{manifest.user_task}</p>
        </section>
        <section className="section">
          <h2>方案</h2>
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

function FailurePanel({
  run,
  events,
  files,
}: {
  run: RunListItem;
  events: RunEvent[];
  files: EvidenceFile[];
}) {
  const latestFailure =
    run.last_error_event ??
    newestFirst(events).find((event) => String(event.type ?? "").endsWith("_failed"));
  if (run.status !== "FAILED" && !latestFailure) return null;
  const verification = newestFirst(events).find((event) => event.type === "verification_finished");
  const verificationOutput = files.find((file) => file.name === "verification-output.txt" && file.preview);

  return (
    <section className="failurePanel">
      <div className="timelineTop">
        <h2>失败原因</h2>
        <StatusBadge status={latestFailure?.type ?? run.status} />
      </div>
      <p>{displayValue(latestFailure?.message ?? "Run 已失败。")}</p>
      {latestFailure?.summary ? <p className="meta">{displayValue(latestFailure.summary)}</p> : null}
      {latestFailure?.reason ? <p className="meta">reason: {displayValue(latestFailure.reason)}</p> : null}
      {verification?.summary ? <p className="meta">verification: {displayValue(verification.summary)}</p> : null}
      {latestFailure ? <p className="meta mono">{eventDetails(latestFailure)}</p> : null}
      {verificationOutput ? (
        <Collapsible title="verification-output.txt" open>
          <pre>{verificationOutput.preview}</pre>
        </Collapsible>
      ) : null}
    </section>
  );
}

function ActionBar({
  actions,
  isPending,
  onAction,
}: {
  actions: AllowedRunAction[];
  isPending: boolean;
  onAction: (action: AllowedRunAction, extra?: Record<string, unknown>) => void;
}) {
  if (!actions.length) return null;
  return (
    <section className="actionBar">
      {actions.includes("retry-review") ? (
        <button type="button" onClick={() => onAction("retry-review")} disabled={isPending}>
          {isPending ? "处理中..." : "重新让 Planner 复核"}
        </button>
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
        <li key={file.path} className="evidenceItem">
          <div className="evidenceTop">
            <span className="mono">{file.name}</span>
            <span className="meta">{file.exists ? `${file.size ?? 0} bytes` : "缺失"}</span>
          </div>
          {file.preview ? (
            <Collapsible title="预览">
              <pre>{file.preview}</pre>
            </Collapsible>
          ) : null}
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

function Collapsible({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details open={open}>
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
