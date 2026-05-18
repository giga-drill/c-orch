import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type {
  AllowedProposalAction,
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
  RunTimingPhaseSummary,
  RunTimingSegment,
  TaskSummary,
  WorkspaceLanesPayload,
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
  refreshDashboardQueries,
} from "../queries";

const statusText: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING: "等待中",
  APPROVED: "已完成",
  FAILED: "失败",
  BLOCKED: "阻塞",
  SKIPPED: "已跳过",
  RESTART_REQUIRED: "需要重启",
  NEW: "新建",
  PLANNING: "Planner 方案生成中",
  PLAN_READY: "计划已生成",
  PLAN_REVIEW_REQUIRED: "等待计划审核",
  PLAN_REVISING: "Planner 修改计划中",
  WAITING_WORKSPACE: "等待 workspace",
  WAITING_WORKSPACE_CLEAN: "等待工作区清理",
  PLAN_APPROVED: "计划已通过",
  WORKING: "Worker 工作中",
  WORK_DONE: "Worker 已完成",
  REVIEWING: "Planner 复核中",
  REVISION_REQUESTED: "Worker 返工中",
  active: "进行中",
  completed: "已完成",
  missing: "未发生",
  partial: "数据不完整",
  failed: "失败",
  waiting_review: "等待审核",
  idle: "空闲",
};

const phaseText: Record<string, string> = {
  planning: "生成方案",
  human_plan_review_wait: "等待审核方案",
  plan_revision: "修改方案",
  worker_execution: "Worker 执行",
  work_done_wait: "等待复核",
  planner_review: "Planner 复核",
  revision_wait: "等待返工",
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

function formatDurationSeconds(value: unknown): string {
  const total = Number(value);
  if (!Number.isFinite(total) || total < 0) return "-";
  const whole = Math.floor(total);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;
  if (hours > 0) return `${hours}h ${minutes}m ${seconds}s`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function numericDurationSeconds(value: unknown): number {
  const duration = Number(value);
  return Number.isFinite(duration) && duration > 0 ? duration : 0;
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0%";
  if (value < 1) return "<1%";
  return `${Math.round(value)}%`;
}

function phaseDisplayLabel(phase: RunTimingPhaseSummary | RunTimingSegment): string {
  return phaseText[phase.phase] ?? displayValue(phase.label || phase.phase);
}

function phaseDurationShare(phase: RunTimingPhaseSummary, totalSeconds: number): string {
  if (totalSeconds <= 0 || phase.status === "missing") return "0%";
  const duration = numericDurationSeconds(phase.total_duration_seconds ?? phase.duration_seconds);
  return formatPercent((duration / totalSeconds) * 100);
}

function timingSourceLabel(source?: string | null): string {
  if (source === "legacy_fallback") return "由事件推断";
  if (source === "manifest") return "Manifest 记录";
  return displayValue(source);
}

function reviewStageLabel(run: RunListItem): string {
  if (run.waiting_for === "planner_review_retry") return "Review 基础设施失败，可重试";
  if (run.waiting_for === "planner_review") return "Planner 正在复核";
  if (run.waiting_for === "worker_rework") return "Worker 返工中";
  if (run.waiting_for === "worker") return "Worker 工作中";
  return "-";
}

function segmentsByPhase(segments: RunTimingSegment[]): Record<string, RunTimingSegment[]> {
  return segments.reduce<Record<string, RunTimingSegment[]>>((groups, segment) => {
    groups[segment.phase] = [...(groups[segment.phase] ?? []), segment];
    return groups;
  }, {});
}

function newestFirst<T>(items?: T[] | null): T[] {
  return Array.isArray(items) ? items.slice().reverse() : [];
}

const QUEUE_PREVIEW_LIMIT = 5;
const WORKSPACE_CLEAN_HINT_MESSAGE = "目标工作区存在未提交改动。建议先提交或处理这些改动，再生成计划。";

type ProposalAction = AllowedProposalAction;

type PendingEntityAction<Action extends string> = {
  entityId: string;
  action: Action;
  observedRevision: string;
};

type PendingProposalAction = PendingEntityAction<ProposalAction>;
type PendingTaskAction = PendingEntityAction<AllowedTaskAction>;
type PendingRunAction = PendingEntityAction<AllowedRunAction>;

type PendingQueueAction = {
  action: "confirm-runtime-restarted";
  observedRevision: string;
};

function actionRevision(actions: readonly string[]): string {
  return actions.slice().sort().join(",");
}

function proposalRevision(proposal: ProposalRecord): string {
  return [
    proposal.updated_at,
    proposal.status,
    proposal.waiting_for,
    actionRevision(proposal.allowed_actions),
    proposal.run_id ?? "",
    proposal.task_id ?? "",
    proposal.error ?? "",
    proposal.reason ?? "",
    proposal.blocker?.message ?? "",
    proposal.blocker?.status_output ?? "",
  ].join("|");
}

function queueRevision(payload?: QueuePayload): string {
  return [
    payload?.queue?.updated_at ?? "",
    payload?.queue?.status ?? "",
    payload?.summary?.current_waiting_point ?? "",
  ].join("|");
}

function taskRevision(task: TaskSummary): string {
  return [
    task.updated_at,
    task.status,
    task.waiting_for,
    actionRevision(task.allowed_actions),
    task.active_run_id ?? "",
    task.error ?? "",
    task.reason ?? "",
  ].join("|");
}

function runRevision(run: RunListItem): string {
  return [
    run.updated_at,
    run.status,
    run.waiting_for,
    actionRevision(run.allowed_actions),
    run.last_event?.timestamp ?? "",
    run.last_event?.type ?? "",
    run.last_error_event?.timestamp ?? "",
    run.last_error_event?.type ?? "",
  ].join("|");
}

function pickTaskRun(task: TaskSummary): string | null {
  if (task.active_run_id) return task.active_run_id;
  return task.run_ids.length ? task.run_ids[task.run_ids.length - 1] : null;
}

function usePendingActionRefresh(enabled: boolean, runId?: string | null) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;
    const refresh = () => {
      void refreshDashboardQueries(queryClient, runId);
    };
    const initialRefresh = window.setTimeout(refresh, 1000);
    const interval = window.setInterval(refresh, 2000);
    return () => {
      window.clearTimeout(initialRefresh);
      window.clearInterval(interval);
    };
  }, [enabled, queryClient, runId]);
}

function parseTimestamp(value?: string | null): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatShortTime(value?: string | null): string {
  const parsed = parseTimestamp(value);
  if (parsed === null) return displayValue(value);
  return new Date(parsed).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function sortQueueTasks(tasks: TaskSummary[]): TaskSummary[] {
  return tasks
    .map((task, index) => ({ task, index }))
    .sort((left, right) => {
      const leftUpdated = parseTimestamp(left.task.updated_at);
      const rightUpdated = parseTimestamp(right.task.updated_at);
      if (leftUpdated !== null && rightUpdated !== null && leftUpdated !== rightUpdated) {
        return rightUpdated - leftUpdated;
      }
      if (leftUpdated !== null && rightUpdated === null) return -1;
      if (leftUpdated === null && rightUpdated !== null) return 1;
      return left.index - right.index;
    })
    .map((entry) => entry.task);
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
  const runtimeBusy = Boolean(
    stateQuery.data?.runtime.dispatch_running ||
      stateQuery.data?.runtime.queue_dispatch_running ||
      stateQuery.data?.runtime.proposal_dispatch_running,
  );
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
    runQuery.data ??
    (stateSelectedRun && stateSelectedRun.run.run_id === selectedRunId
      ? stateSelectedRun
      : undefined);
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

  function refreshAll() {
    void stateQuery.refetch();
    if (selectedRunId) {
      void runQuery.refetch();
    }
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
          >
            刷新
          </button>
        </header>
        {error ? <div className="error">{error.message}</div> : null}
        {isLoading ? <div className="empty">正在加载运行状态...</div> : null}
        <SystemStatus
          queuePayload={queuePayload}
          runsGeneratedAt={runsPayload?.generated_at}
          runtimeBusy={runtimeBusy}
        />
        <WorkspaceLanePanel payload={stateQuery.data?.workspace_lanes} />
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
  runtimeBusy,
}: {
  queuePayload?: QueuePayload;
  runsGeneratedAt?: string;
  runtimeBusy?: boolean;
}) {
  const queueActionMutation = useQueueActionMutation();
  const [queueActionError, setQueueActionError] = useState<string | null>(null);
  const [pendingQueueAction, setPendingQueueAction] = useState<PendingQueueAction | null>(null);
  const queue = queuePayload?.queue;
  const restartRequired = queue?.status === "RESTART_REQUIRED";
  const currentQueueRevision = queueRevision(queuePayload);
  usePendingActionRefresh(Boolean(pendingQueueAction));

  useEffect(() => {
    if (pendingQueueAction && currentQueueRevision !== pendingQueueAction.observedRevision) {
      setPendingQueueAction(null);
    }
  }, [currentQueueRevision, pendingQueueAction]);

  async function confirmRuntimeRestarted() {
    setQueueActionError(null);
    setPendingQueueAction({
      action: "confirm-runtime-restarted",
      observedRevision: currentQueueRevision,
    });
    try {
      await queueActionMutation.mutateAsync({ action: "confirm-runtime-restarted" });
    } catch (error) {
      setQueueActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setPendingQueueAction((current) =>
        current?.action === "confirm-runtime-restarted" ? null : current,
      );
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
        <strong>{formatShortTime(queuePayload?.generated_at ?? runsGeneratedAt)}</strong>
      </div>
      <div>
        <span className="label">runtime</span>
        <strong>{restartRequired ? "需重启" : runtimeBusy ? "运行中" : "空闲"}</strong>
      </div>
      {restartRequired ? (
        <>
          <p>当前队列被 restart gate 暂停。请在 runtime 完成重启后确认继续。</p>
          <button
            type="button"
            onClick={confirmRuntimeRestarted}
            disabled={Boolean(pendingQueueAction)}
          >
            {pendingQueueAction ? "确认中..." : "确认已重启并继续"}
          </button>
          {queueActionError ? <div className="error">{queueActionError}</div> : null}
        </>
      ) : null}
    </section>
  );
}

function WorkspaceLanePanel({ payload }: { payload?: WorkspaceLanesPayload }) {
  if (!payload || payload.total_lanes === 0) return null;
  return (
    <section className="panel">
      <div className="sectionTitle">
        <h2>Workspace lanes</h2>
        <span className="meta">总数 {payload.total_lanes}</span>
      </div>
      <div className="queueStats" aria-label="workspace lane 统计">
        <span>进行中 {payload.active_lanes}</span>
        <span>失败 {payload.failed_lanes}</span>
        <span>待审 {payload.waiting_review_lanes}</span>
        <span>待执行 {payload.pending_lanes}</span>
      </div>
      <div className="laneList">
        {payload.lanes.map((lane) => (
          <div key={lane.workspace_id} className="laneItem">
            <div className="itemTop">
              <strong>{displayValue(lane.workspace_name)}</strong>
              <StatusBadge status={lane.status} />
            </div>
            <span className="meta">root: {displayValue(lane.workspace_root)}</span>
            {lane.active_item ? (
              <span className="meta">
                active: {displayValue(lane.active_item.type)} {displayValue(lane.active_item.title ?? lane.active_item.id)}
              </span>
            ) : null}
            {lane.blocked_by ? (
              <span className="meta">
                blocked_by: {displayValue(lane.blocked_by.type)} {displayValue(lane.blocked_by.title ?? lane.blocked_by.id)}
              </span>
            ) : null}
            <span className="meta">
              queued {lane.queued} · failed {lane.failed} · proposals {lane.proposals}
            </span>
          </div>
        ))}
      </div>
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
  const [cwd, setCwd] = useState("");
  const [feedbackById, setFeedbackById] = useState<Record<string, string>>({});
  const [pendingProposalAction, setPendingProposalAction] = useState<PendingProposalAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const proposalsNewestFirst = newestFirst(payload?.proposals ?? []);
  const summary = payload?.summary;
  usePendingActionRefresh(Boolean(pendingProposalAction));

  useEffect(() => {
    if (!pendingProposalAction) return;
    const proposal = payload?.proposals.find(
      (item) => item.proposal_id === pendingProposalAction.entityId,
    );
    if (!proposal || proposalRevision(proposal) !== pendingProposalAction.observedRevision) {
      if (pendingProposalAction.action === "revise-plan") {
        setFeedbackById((current) => ({ ...current, [pendingProposalAction.entityId]: "" }));
      }
      setPendingProposalAction(null);
    }
  }, [payload?.proposals, pendingProposalAction]);

  async function createProposal() {
    setError(null);
    try {
      await createMutation.mutateAsync({
        title: title.trim(),
        prompt: prompt.trim(),
        cwd: cwd.trim() || null,
      });
      setTitle("");
      setPrompt("");
      setCwd("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }

  async function proposalAction(proposal: ProposalRecord, action: ProposalAction) {
    setError(null);
    const feedback = feedbackById[proposal.proposal_id] ?? "";
    setPendingProposalAction({
      entityId: proposal.proposal_id,
      action,
      observedRevision: proposalRevision(proposal),
    });
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
    } finally {
      setPendingProposalAction((current) =>
        current?.entityId === proposal.proposal_id && current.action === action ? null : current,
      );
    }
  }

  return (
    <section className="panel">
      <div className="sectionTitle">
        <h2>计划提案池</h2>
        <span className="meta">{displayValue(payload?.proposals_file)}</span>
      </div>
      {summary ? (
        <div className="queueStats" aria-label="计划池统计">
          <span>总数 {summary.total_proposals}</span>
          <span>待审 {summary.review_required}</span>
          <span>待清理 {summary.waiting_workspace_clean ?? 0}</span>
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
          placeholder="任务说明。默认会在 Planner 生成方案后自动进入执行队列。"
        />
        <input
          value={cwd}
          onChange={(event) => setCwd(event.target.value)}
          placeholder="目标仓库路径（可选，默认使用当前运行目录）"
        />
        <button
          type="button"
          onClick={createProposal}
          disabled={createMutation.isPending || !title.trim() || !prompt.trim()}
        >
          {createMutation.isPending ? "创建中..." : "提交提案"}
        </button>
      </div>
      {error ? <div className="error">{error}</div> : null}
      <div className="taskList">
        {proposalsNewestFirst.map((proposal) => (
          <article key={proposal.proposal_id} className="taskItem">
            {(() => {
              const proposalActionPending = pendingProposalAction?.entityId === proposal.proposal_id;
              const pendingAction = proposalActionPending ? pendingProposalAction?.action : null;
              return (
                <>
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
              <span className="meta">workspace: {displayValue(proposal.workspace_id)}</span>
              <span className="meta">cwd: {displayValue(proposal.cwd)}</span>
              <span className="meta">waiting_for: {displayValue(proposal.waiting_for)}</span>
              <span className="meta">run_id: {displayValue(proposal.run_id)}</span>
            </button>
            <ProposalPlanPanel
              status={proposal.status}
              error={proposal.error}
              reason={proposal.reason}
              blocker={proposal.blocker}
              plan={proposal.plan_detail}
              fallbackSummary={proposal.run?.plan?.summary}
            />
            {proposal.allowed_actions.includes("approve-plan") ? (
              <div className="proposalActions">
                <button
                  type="button"
                  onClick={() => proposalAction(proposal, "approve-plan")}
                  disabled={proposalActionPending}
                >
                  {pendingAction === "approve-plan" ? "处理中..." : "通过并加入执行队列"}
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
                  disabled={proposalActionPending || !(feedbackById[proposal.proposal_id] ?? "").trim()}
                >
                  {pendingAction === "revise-plan" ? "处理中..." : "让 Planner 修改方案"}
                </button>
              </div>
            ) : null}
            {proposal.allowed_actions.includes("retry-plan") ? (
              <div className="proposalActions">
                <button
                  type="button"
                  onClick={() => proposalAction(proposal, "retry-plan")}
                  disabled={proposalActionPending}
                >
                  {pendingAction === "retry-plan" ? "处理中..." : "重试生成计划"}
                </button>
              </div>
            ) : null}
                </>
              );
            })()}
          </article>
        ))}
        {payload && payload.proposals.length === 0 ? <div className="empty">暂无计划提案。</div> : null}
      </div>
    </section>
  );
}

function ProposalPlanPanel({
  status,
  error,
  reason,
  blocker,
  plan,
  fallbackSummary,
}: {
  status: string;
  error?: string | null;
  reason?: string | null;
  blocker?: {
    message?: string | null;
    suggested_action?: string | null;
    status_output?: string | null;
    command?: string | null;
  } | null;
  plan?: ProposalPlanDetail | null;
  fallbackSummary?: string | null;
}) {
  if (!plan) {
    if (status === "WAITING_WORKSPACE_CLEAN") {
      return (
        <div className="proposalPlan">
          <p>{displayValue(blocker?.message ?? WORKSPACE_CLEAN_HINT_MESSAGE)}</p>
          {blocker?.suggested_action ? <p className="meta">{displayValue(blocker.suggested_action)}</p> : null}
          {blocker?.command ? <p className="meta mono">{displayValue(blocker.command)}</p> : null}
          {blocker?.status_output ? (
            <Collapsible title="git status --short 输出" open>
              <pre>{blocker.status_output}</pre>
            </Collapsible>
          ) : null}
        </div>
      );
    }
    if (status === "PLANNING") {
      return <p className="meta">Planner 方案生成中...</p>;
    }
    if (status === "FAILED") {
      return (
        <p className="error">
          规划失败：{displayValue(error)}
          {reason ? `（${reason}）` : ""}
        </p>
      );
    }
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
  const [showAllTasks, setShowAllTasks] = useState(false);
  const [pendingTaskAction, setPendingTaskAction] = useState<PendingTaskAction | null>(null);
  const summary = payload?.summary;
  const queueTasks = payload?.tasks ?? [];
  const sortedTasks = sortQueueTasks(queueTasks);
  const hasHiddenTasks = sortedTasks.length > QUEUE_PREVIEW_LIMIT;
  const visibleTasks =
    hasHiddenTasks && !showAllTasks ? sortedTasks.slice(0, QUEUE_PREVIEW_LIMIT) : sortedTasks;
  const hiddenTaskCount = sortedTasks.length - visibleTasks.length;
  usePendingActionRefresh(Boolean(pendingTaskAction), selectedRunId);

  useEffect(() => {
    if (!hasHiddenTasks && showAllTasks) {
      setShowAllTasks(false);
    }
  }, [hasHiddenTasks, showAllTasks]);

  useEffect(() => {
    if (!pendingTaskAction) return;
    const task = queueTasks.find((item) => item.task_id === pendingTaskAction.entityId);
    if (!task || taskRevision(task) !== pendingTaskAction.observedRevision) {
      setPendingTaskAction(null);
    }
  }, [queueTasks, pendingTaskAction]);

  async function runTaskAction(task: TaskSummary, action: AllowedTaskAction) {
    setTaskError(null);
    setPendingTaskAction({
      entityId: task.task_id,
      action,
      observedRevision: taskRevision(task),
    });
    try {
      const result = await taskMutation.mutateAsync({ taskId: task.task_id, action });
      const latest = result.state?.queue.tasks.find((item) => item.task_id === task.task_id);
      const runId = latest ? pickTaskRun(latest) : pickTaskRun(task);
      if (runId) onSelectRun(runId);
    } catch (error) {
      setTaskError(error instanceof Error ? error.message : String(error));
    } finally {
      setPendingTaskAction((current) =>
        current?.entityId === task.task_id && current.action === action ? null : current,
      );
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
          <span>跳过 {summary.skipped_tasks}</span>
          <span>待执行 {summary.pending_tasks}</span>
          <span>失败 {summary.failed_tasks}</span>
        </div>
      ) : null}
      {hasHiddenTasks ? (
        <div className="queueDisplayMeta">
          <span>
            显示 {visibleTasks.length} / 总数 {sortedTasks.length}
            {hiddenTaskCount > 0 ? `，隐藏 ${hiddenTaskCount} 条` : ""}
          </span>
          <button type="button" onClick={() => setShowAllTasks((current) => !current)}>
            {showAllTasks ? "收起" : "显示全部"}
          </button>
        </div>
      ) : null}
      {taskError ? <div className="error">{taskError}</div> : null}
      <div className={showAllTasks ? "taskList queueTaskList expanded" : "taskList queueTaskList"}>
        {visibleTasks.map((task) => {
          const taskRunId = pickTaskRun(task);
          const selected = Boolean(taskRunId && taskRunId === selectedRunId);
          const taskActionPending = pendingTaskAction?.entityId === task.task_id;
          const pendingAction = taskActionPending ? pendingTaskAction?.action : null;
          const failureReason =
            task.failure_summary ??
            task.last_error_event?.summary ??
            task.last_error_event?.message ??
            task.error ??
            task.reason;
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
                <span className="meta">workspace: {displayValue(task.workspace_id)}</span>
                <span className="meta">cwd: {displayValue(task.cwd)}</span>
                <span className="meta">waiting_for: {displayValue(task.waiting_for)}</span>
                {task.blocked_by ? (
                  <span className="meta">blocked_by: {displayValue(task.blocked_by.title ?? task.blocked_by.id)}</span>
                ) : null}
                <span className="meta">active_run_id: {displayValue(task.active_run_id)}</span>
              </button>
              {task.status === "FAILED" && failureReason ? (
                <div className="taskFailure">
                  <span className="label">失败原因</span>
                  <span>{displayValue(failureReason)}</span>
                  {task.last_error_event?.type ? (
                    <span className="meta">event: {displayValue(task.last_error_event.type)}</span>
                  ) : null}
                </div>
              ) : null}
              {task.allowed_actions.length ? (
                <div className="taskActions">
                  {task.allowed_actions.includes("retry-verification") ? (
                    <button
                      type="button"
                      onClick={() => runTaskAction(task, "retry-verification")}
                      disabled={taskActionPending}
                    >
                      {pendingAction === "retry-verification" ? "重跑 CI 中..." : "重新跑 CI"}
                    </button>
                  ) : null}
                  {task.allowed_actions.includes("retry-task") ? (
                    <button
                      type="button"
                      onClick={() => runTaskAction(task, "retry-task")}
                      disabled={taskActionPending}
                    >
                      {pendingAction === "retry-task" ? "重新排队中..." : "重新排队执行"}
                    </button>
                  ) : null}
                  {task.allowed_actions.includes("mark-handled-skipped") ? (
                    <button
                      type="button"
                      onClick={() => runTaskAction(task, "mark-handled-skipped")}
                      disabled={taskActionPending}
                    >
                      {pendingAction === "mark-handled-skipped" ? "标记中..." : "标记为已处理并跳过"}
                    </button>
                  ) : null}
                </div>
              ) : null}
            </article>
          );
        })}
        {payload && queueTasks.length === 0 ? <div className="empty">暂无任务。</div> : null}
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
  const [pendingRunAction, setPendingRunAction] = useState<PendingRunAction | null>(null);
  const currentRun = payload?.run;
  usePendingActionRefresh(Boolean(pendingRunAction), currentRun?.run_id ?? selectedRunId);

  useEffect(() => {
    if (!pendingRunAction || !currentRun) return;
    if (
      pendingRunAction.entityId !== currentRun.run_id ||
      runRevision(currentRun) !== pendingRunAction.observedRevision
    ) {
      setPendingRunAction(null);
    }
  }, [currentRun, pendingRunAction]);

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
    setPendingRunAction({
      entityId: run.run_id,
      action,
      observedRevision: runRevision(run),
    });
    try {
      await actionMutation.mutateAsync({ runId: run.run_id, action, payload: extra });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : String(error));
    } finally {
      setPendingRunAction((current) =>
        current?.entityId === run.run_id && current.action === action ? null : current,
      );
    }
  }

  return (
    <div className="detail">
      <header className="detailHeader">
        <div>
          <p className="meta">Run</p>
          <h1>{run.run_id}</h1>
        </div>
      </header>

      <RunSummaryStrip run={run} />

      <ActionBar
        actions={run.allowed_actions}
        pendingAction={pendingRunAction?.entityId === run.run_id ? pendingRunAction.action : null}
        onAction={runAction}
      />
      {actionError ? <div className="error">{actionError}</div> : null}
      <FailurePanel run={run} events={payload.events} files={payload.evidence_files} />
      <section className="section">
        <h2>阶段耗时</h2>
        <PhaseTimingPanel run={run} />
      </section>

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
          <Collapsible title={`证据文件（${payload.evidence_files.length}）`}>
            <EvidenceList files={payload.evidence_files} />
          </Collapsible>
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

function RunSummaryStrip({ run }: { run: RunListItem }) {
  const latestRevisionSummary =
    run.latest_revision_request?.summary ?? run.latest_revision_request?.reason;
  const isActiveReviewInfraFailure =
    run.waiting_for === "planner_review_retry" ||
    run.can_retry_review ||
    run.allowed_actions.includes("retry-review");
  const showReviewFailure = Boolean(run.latest_review_failure) && isActiveReviewInfraFailure;
  const reviewRetryCount = run.review_retry_count ?? 0;
  return (
    <section className="runSummaryStrip">
      <div className="summaryCell">
        <span className="label">status</span>
        <StatusBadge status={run.status} />
      </div>
      <div className="summaryCell">
        <span className="label">waiting_for</span>
        <strong>{displayValue(run.waiting_for)}</strong>
      </div>
      <div className="summaryCell">
        <span className="label">next_action</span>
        <strong>{displayValue(run.next_action)}</strong>
      </div>
      <div className="summaryCell">
        <span className="label">allowed_actions</span>
        <strong>{run.allowed_actions.length ? run.allowed_actions.join(", ") : "-"}</strong>
      </div>
      <div className="summaryCell">
        <span className="label">Planner</span>
        <strong>{formatStatus(run.planner.status)}</strong>
      </div>
      <div className="summaryCell">
        <span className="label">Worker</span>
        <strong>{formatStatus(run.workers[0]?.status ?? "PENDING")}</strong>
      </div>
      <div className="summaryCell">
        <span className="label">复核/重试次数</span>
        <strong>
          {run.review_attempt_count} / {reviewRetryCount}
        </strong>
      </div>
      <div className="summaryCell">
        <span className="label">复核状态</span>
        <strong>{reviewStageLabel(run)}</strong>
      </div>
      {latestRevisionSummary ? (
        <div className="summaryNote revisionNote">
          <span className="label">最近一次 Planner 打回原因</span>
          <strong>{latestRevisionSummary}</strong>
          {run.latest_revision_request?.review_attempt_id ? (
            <span className="meta mono">attempt: {run.latest_revision_request.review_attempt_id}</span>
          ) : null}
        </div>
      ) : null}
      {showReviewFailure ? (
        <div className="summaryNote failureNote">
          <span className="label">Review 基础设施失败</span>
          <strong>
            {run.latest_review_failure?.summary ??
              run.latest_review_failure?.error ??
              run.latest_review_failure?.reason ??
              "review infra failure"}
          </strong>
          {run.latest_review_failure?.review_attempt_id ? (
            <span className="meta mono">attempt: {run.latest_review_failure.review_attempt_id}</span>
          ) : null}
        </div>
      ) : null}
    </section>
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
  pendingAction,
  onAction,
}: {
  actions: AllowedRunAction[];
  pendingAction: AllowedRunAction | null;
  onAction: (action: AllowedRunAction, extra?: Record<string, unknown>) => void;
}) {
  if (!actions.length) return null;
  return (
    <section className="actionBar">
      {actions.includes("retry-review") ? (
        <button type="button" onClick={() => onAction("retry-review")} disabled={Boolean(pendingAction)}>
          {pendingAction === "retry-review" ? "处理中..." : "重新让 Planner 复核"}
        </button>
      ) : null}
      {actions.includes("retry-verification") ? (
        <button type="button" onClick={() => onAction("retry-verification")} disabled={Boolean(pendingAction)}>
          {pendingAction === "retry-verification" ? "重跑 CI 中..." : "重新跑 CI"}
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
      <KeyValue label="reasoning_effort" value={agent.reasoning_effort} />
      <KeyValue label="service_tier" value={agent.service_tier} />
      <Collapsible title="技术详情">
        <KeyValue label="thread_id" value={agent.thread_id} />
        <KeyValue label="attempt" value={agent.attempt} />
        <KeyValue label="worktree_path" value={agent.worktree_path} />
        <KeyValue label="evidence_count" value={agent.evidence_count} />
        <KeyValue label="waiting_for" value={waitingFor} />
      </Collapsible>
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

function PhaseTimingPanel({ run }: { run: RunListItem }) {
  const timing = run.timing;
  const phases = timing?.phases ?? [];
  if (!timing || !phases.length) return <p className="meta">暂无阶段耗时数据。</p>;
  const totalSeconds = numericDurationSeconds(timing.total?.duration_seconds);
  const groupedSegments = segmentsByPhase(timing.segments ?? []);
  const activePhase = phases.find((phase) => phase.status === "active");
  const isInferred = timing.source === "legacy_fallback";

  return (
    <div className="timingPanel">
      <div className={isInferred ? "timingSummary inferred" : "timingSummary"}>
        <div>
          <span className="label">总耗时</span>
          <strong>{formatDurationSeconds(timing.total?.duration_seconds)}</strong>
        </div>
        <div>
          <span className="label">当前阶段</span>
          <strong>{activePhase ? phaseDisplayLabel(activePhase) : "-"}</strong>
        </div>
        <div>
          <span className="label">状态</span>
          <strong>{formatStatus(timing.total?.status)}</strong>
        </div>
        <div>
          <span className="label">来源</span>
          <strong>{timingSourceLabel(timing.source)}</strong>
        </div>
      </div>

      <div className="timingTable" role="table" aria-label="阶段耗时诊断表">
        <div className="timingHeader" role="row">
          <span>阶段</span>
          <span>状态</span>
          <span>耗时</span>
          <span>占比</span>
          <span>次数</span>
          <span>开始时间</span>
          <span>结束时间</span>
        </div>
        {phases.map((phase) => (
          <PhaseTimingRow
            key={phase.phase}
            phase={phase}
            segments={groupedSegments[phase.phase] ?? []}
            totalSeconds={totalSeconds}
          />
        ))}
      </div>
    </div>
  );
}

function PhaseTimingRow({
  phase,
  segments,
  totalSeconds,
}: {
  phase: RunTimingPhaseSummary;
  segments: RunTimingSegment[];
  totalSeconds: number;
}) {
  const defaultOpen = phase.status === "active" || phase.count > 1;
  const rowClassName = `timingPhaseRow ${phase.status}`;
  const duration = formatDurationSeconds(phase.total_duration_seconds);
  return (
    <details className={rowClassName} open={defaultOpen}>
      <summary className="timingPhaseSummary">
        <span className="timingPhaseName">{phaseDisplayLabel(phase)}</span>
        <span>
          <StatusBadge status={phase.status} />
        </span>
        <span>{duration}</span>
        <span>{phaseDurationShare(phase, totalSeconds)}</span>
        <span>{phase.count}</span>
        <span className="mono">{displayValue(phase.started_at)}</span>
        <span className="mono">{displayValue(phase.completed_at)}</span>
      </summary>
      <div className="timingSegments">
        {segments.length ? (
          segments.map((segment) => (
            <div className="timingSegmentRow" key={segment.id || `${segment.phase}-${segment.sequence}`}>
              <span className="mono">#{displayValue(segment.sequence || segment.id)}</span>
              <span>{phaseDisplayLabel(segment)}</span>
              <span className="mono">
                {displayValue(segment.start_status)} → {displayValue(segment.end_status)}
              </span>
              <span className="mono">{displayValue(segment.started_at)}</span>
              <span className="mono">{displayValue(segment.completed_at)}</span>
              <span>{formatDurationSeconds(segment.duration_seconds)}</span>
              <StatusBadge status={segment.status} />
            </div>
          ))
        ) : (
          <p className="meta">暂无 segment 明细。</p>
        )}
      </div>
    </details>
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
