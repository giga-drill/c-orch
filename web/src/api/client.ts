import type {
  AllowedRunAction,
  AllowedTaskAction,
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

export function fetchRuns(): Promise<RunsPayload> {
  return requestJson<RunsPayload>("/api/runs");
}

export function fetchRun(runId: string): Promise<RunPayload> {
  return requestJson<RunPayload>(`/api/runs/${encodeURIComponent(runId)}`);
}

export function postRunAction(
  runId: string,
  action: AllowedRunAction,
  payload: Record<string, unknown> = {},
): Promise<RunPayload> {
  return requestJson<RunPayload>(`/api/runs/${encodeURIComponent(runId)}/actions`, {
    method: "POST",
    body: JSON.stringify({ action, ...payload }),
  });
}

export function postTaskAction(taskId: string, action: AllowedTaskAction): Promise<QueuePayload> {
  return requestJson<QueuePayload>(`/api/tasks/${encodeURIComponent(taskId)}/actions`, {
    method: "POST",
    body: JSON.stringify({ action }),
  });
}
