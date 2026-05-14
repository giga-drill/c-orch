import type {
  ActionResponse,
  AllowedQueueAction,
  AllowedProposalAction,
  AllowedRunAction,
  AllowedTaskAction,
  DashboardStatePayload,
  ProposalsPayload,
  QueuePayload,
  RunPayload,
  RunsPayload,
} from "./types";

async function requestJson<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    const message = typeof payload.error === "string" ? payload.error : `请求失败: ${response.status}`;
    throw new Error(message);
  }
  return payload as T;
}

export function fetchQueue(): Promise<QueuePayload> {
  return requestJson<QueuePayload>("/api/queue");
}

export function fetchProposals(): Promise<ProposalsPayload> {
  return requestJson<ProposalsPayload>("/api/proposals");
}

export function fetchRuns(): Promise<RunsPayload> {
  return requestJson<RunsPayload>("/api/runs");
}

export function fetchRun(runId: string): Promise<RunPayload> {
  return requestJson<RunPayload>(`/api/runs/${encodeURIComponent(runId)}`);
}

export function fetchState(): Promise<DashboardStatePayload> {
  return requestJson<DashboardStatePayload>("/api/state");
}

export function postRunAction(
  runId: string,
  action: AllowedRunAction,
  payload: Record<string, unknown> = {},
): Promise<ActionResponse> {
  return requestJson<ActionResponse>(`/api/runs/${encodeURIComponent(runId)}/actions`, {
    method: "POST",
    body: JSON.stringify({ action, ...payload }),
  });
}

export function postTaskAction(taskId: string, action: AllowedTaskAction): Promise<ActionResponse> {
  return requestJson<ActionResponse>(`/api/tasks/${encodeURIComponent(taskId)}/actions`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export function postQueueAction(action: AllowedQueueAction): Promise<ActionResponse> {
  return requestJson<ActionResponse>("/api/queue/actions", {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}

export function postProposal(
  title: string,
  prompt: string,
  cwd: string | null = null,
): Promise<ActionResponse> {
  return requestJson<ActionResponse>("/api/proposals", {
    method: "POST",
    body: JSON.stringify({ title, prompt, cwd }),
  });
}

export function postProposalAction(
  proposalId: string,
  action: AllowedProposalAction,
  payload: Record<string, unknown> = {},
): Promise<ActionResponse> {
  return requestJson<ActionResponse>(`/api/proposals/${encodeURIComponent(proposalId)}/actions`, {
    method: "POST",
    body: JSON.stringify({ action, ...payload }),
  });
}
