export type AllowedRunAction = "retry-review" | "retry-verification";
export type AllowedTaskAction = "retry-task" | "retry-verification" | "mark-handled-skipped";
export type AllowedQueueAction = "confirm-runtime-restarted";
export type AllowedProposalAction = "approve-plan" | "revise-plan";

export interface QueueSummary {
  total_tasks: number;
  approved_tasks: number;
  completed_tasks: number;
  skipped_tasks: number;
  pending_tasks: number;
  failed_tasks: number;
  running_tasks: number;
  current_waiting_point: string;
}

export interface QueueRecord {
  queue_id: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface TaskSummary {
  task_id: string;
  title: string;
  cwd: string | null;
  workspace_id?: string | null;
  status: string;
  active_run_id: string | null;
  run_ids: string[];
  updated_at: string;
  completed_at: string | null;
  reason: string | null;
  error: string | null;
  failure_summary: string | null;
  last_error_event: RunEvent | null;
  waiting_for: string;
  next_action: string;
  blocked_by?: { type?: string; id?: string; title?: string; status?: string } | null;
  allowed_actions: AllowedTaskAction[];
}

export interface QueuePayload {
  queue_file: string | null;
  generated_at: string;
  queue: QueueRecord | null;
  summary?: QueueSummary;
  tasks: TaskSummary[];
  transition?: TransitionResult;
}

export interface ProposalSummary {
  total_proposals: number;
  review_required: number;
  queued: number;
  failed: number;
  active: number;
  waiting_workspace?: number;
}

export interface ProposalPoolRecord {
  pool_id: string;
  created_at: string;
  updated_at: string;
}

export interface ProposalPlanDetail {
  approval_status: string | null;
  summary: string | null;
  worker_prompt: string | null;
  risk_notes: string[];
  acceptance_criteria: string[];
  verification_commands: string[];
}

export interface ProposalRecord {
  proposal_id: string;
  title: string;
  prompt: string;
  cwd: string | null;
  workspace_id?: string | null;
  status: string;
  run_id: string | null;
  task_id: string | null;
  created_at: string;
  updated_at: string;
  error: string | null;
  reason: string | null;
  waiting_for: string;
  allowed_actions: AllowedProposalAction[];
  run: RunListItem | null;
  plan_detail: ProposalPlanDetail | null;
}

export interface WorkspaceLaneSummary {
  workspace_id: string;
  workspace_root: string;
  workspace_name: string;
  status: string;
  active_item: { type?: string; id?: string; title?: string } | null;
  blocked_by: { type?: string; id?: string; title?: string; status?: string } | null;
  queued: number;
  failed: number;
  proposals: number;
}

export interface WorkspaceLanesPayload {
  lanes: WorkspaceLaneSummary[];
  total_lanes: number;
  active_lanes: number;
  failed_lanes: number;
  waiting_review_lanes: number;
  pending_lanes: number;
}

export interface ProposalsPayload {
  proposals_file: string | null;
  generated_at: string;
  pool: ProposalPoolRecord | null;
  summary?: ProposalSummary;
  proposals: ProposalRecord[];
  transition?: TransitionResult;
}

export interface AgentSummary {
  id?: string;
  status: string;
  model: string;
  thread_id: string | null;
  reasoning_effort: string | null;
  service_tier: string | null;
  attempt?: number;
  worktree_path?: string | null;
  evidence_count?: number;
}

export interface RunTimingSegment {
  id: string;
  sequence: number;
  phase: string;
  label: string;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  status: string;
  start_status?: string | null;
  end_status?: string | null;
  [key: string]: unknown;
}

export interface RunTimingPhaseSummary {
  phase: string;
  label: string;
  count: number;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number;
  total_duration_seconds: number;
  status: string;
}

export interface RunTimingSummary {
  version: number;
  source: string;
  segments: RunTimingSegment[];
  phases: RunTimingPhaseSummary[];
  phase_aggregates: Record<string, RunTimingPhaseSummary>;
  total: RunTimingPhaseSummary;
}

export interface RunListItem {
  run_id: string;
  status: string;
  status_index: number;
  terminal: boolean;
  waiting_for: string;
  next_action: string;
  allowed_actions: AllowedRunAction[];
  requires_restart: boolean;
  restart_reason: string | null;
  restart_paths: string[];
  user_task: string;
  cwd: string;
  created_at: string;
  updated_at: string;
  planner: AgentSummary;
  workers: AgentSummary[];
  review: { decision: string | null; reason: string | null } | null;
  review_attempt_count: number;
  last_event: RunEvent | null;
  last_error_event: RunEvent | null;
  can_retry_review: boolean;
  timing: RunTimingSummary;
  plan: { approval_status?: string | null; summary?: string | null } | null;
  plan_revision_count: number;
  latest_plan_revision_id: string | null;
  latest_plan_revision_created_at: string | null;
  latest_plan_revision_feedback: string | null;
  acceptance_count: number;
  verification_count: number;
  evidence_count: number;
}

export interface RunsPayload {
  runs_dir: string;
  generated_at: string;
  runs: RunListItem[];
}

export interface PlanRecord {
  summary: string;
  worker_prompt: string;
  risk_notes: string[];
  raw: Record<string, unknown>;
  approval_status: string;
  approved_at: string | null;
  approved_by: string | null;
}

export interface ManifestRecord {
  run_id: string;
  cwd: string;
  user_task: string;
  status: string;
  created_at: string;
  updated_at: string;
  timing: Record<string, unknown> | null;
  codex_binary_path: string | null;
  requires_restart: boolean;
  restart_reason: string | null;
  restart_paths: string[];
  acceptance_criteria: string[];
  verification_commands: string[];
  plan: PlanRecord | null;
  review_attempts: ReviewAttempt[];
}

export interface EvidenceFile {
  path: string;
  name: string;
  exists: boolean;
  size: number | null;
  preview: string | null;
}

export interface RunEvent {
  timestamp?: string;
  type?: string;
  message?: string;
  [key: string]: unknown;
}

export interface ReviewAttempt {
  id: string;
  worker_id: string;
  status: string;
  started_at: string;
  worker_attempt: number | null;
  workspace_path: string | null;
  completed_at: string | null;
  decision: string | null;
  reason: string | null;
  next_worker_prompt: string | null;
  error: string | null;
  evidence_files: string[];
}

export interface WorkerActivity {
  timestamp?: string;
  label?: string;
  kind?: string;
  worker_id?: string;
  detail?: string;
}

export interface RunPayload {
  run: RunListItem;
  manifest: ManifestRecord;
  evidence_files: EvidenceFile[];
  events: RunEvent[];
  worker_activity: WorkerActivity[];
  transition?: TransitionResult;
}

export interface RuntimeState {
  generation: string;
  pid: number;
  dispatch_running: boolean;
  queue_dispatch_running?: boolean;
  proposal_dispatch_running?: boolean;
  last_dispatch_error: string | null;
  last_proposal_dispatch_error?: string | null;
}

export interface DashboardStatePayload {
  generated_at: string;
  version: number;
  runtime: RuntimeState;
  proposals: ProposalsPayload;
  queue: QueuePayload;
  workspace_lanes?: WorkspaceLanesPayload;
  runs: RunsPayload;
  focused_run_id: string | null;
  selected_run: RunPayload | null;
}

export interface TransitionResult {
  type: string;
  proposal_id?: string;
  task_id?: string;
  run_id?: string;
  selected_run_id?: string;
  removed_from_pool?: boolean;
  queued?: boolean;
  confirmed_by?: string;
}

export interface ActionResponse {
  transition?: TransitionResult;
  state?: DashboardStatePayload;
  state_version?: number;
  [key: string]: unknown;
}
