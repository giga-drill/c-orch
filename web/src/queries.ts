import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchQueue, fetchRun, fetchRuns, postQueueAction, postRunAction, postTaskAction } from "./api/client";
import type { AllowedQueueAction, AllowedRunAction, AllowedTaskAction } from "./api/types";

export const queryKeys = {
  queue: ["queue"] as const,
  runs: ["runs"] as const,
  run: (runId: string) => ["run", runId] as const,
};

export function useQueueQuery() {
  return useQuery({ queryKey: queryKeys.queue, queryFn: fetchQueue });
}

export function useRunsQuery() {
  return useQuery({ queryKey: queryKeys.runs, queryFn: fetchRuns });
}

export function useRunQuery(runId: string | null) {
  return useQuery({
    queryKey: runId ? queryKeys.run(runId) : ["run", "none"],
    queryFn: () => fetchRun(runId as string),
    enabled: Boolean(runId),
  });
}

export function useRunActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      runId,
      action,
      payload,
    }: {
      runId: string;
      action: AllowedRunAction;
      payload?: Record<string, unknown>;
    }) => postRunAction(runId, action, payload),
    onSuccess: async (_payload, variables) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.queue }),
        queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
        queryClient.invalidateQueries({ queryKey: queryKeys.run(variables.runId) }),
      ]);
    },
  });
}

export function useTaskActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, action }: { taskId: string; action: AllowedTaskAction }) =>
      postTaskAction(taskId, action),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.queue }),
        queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
      ]);
    },
  });
}

export function useQueueActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ action }: { action: AllowedQueueAction }) => postQueueAction(action),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.queue }),
        queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
        queryClient.invalidateQueries({ queryKey: ["run"] }),
      ]);
    },
  });
}
