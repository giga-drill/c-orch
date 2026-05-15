import { QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchProposals,
  fetchQueue,
  fetchRun,
  fetchRuns,
  fetchState,
  postProposal,
  postProposalAction,
  postQueueAction,
  postRunAction,
  postTaskAction,
} from "./api/client";
import type { AllowedProposalAction, AllowedQueueAction, AllowedRunAction, AllowedTaskAction } from "./api/types";
import type { ActionResponse } from "./api/types";
import type { DashboardStatePayload } from "./api/types";

export const queryKeys = {
  queue: ["queue"] as const,
  proposals: ["proposals"] as const,
  runs: ["runs"] as const,
  state: ["state"] as const,
  run: (runId: string) => ["run", runId] as const,
};

export async function refreshDashboardQueries(queryClient: QueryClient, runId?: string | null) {
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: queryKeys.state }),
    queryClient.invalidateQueries({ queryKey: queryKeys.proposals }),
    queryClient.invalidateQueries({ queryKey: queryKeys.queue }),
    queryClient.invalidateQueries({ queryKey: queryKeys.runs }),
    queryClient.invalidateQueries({ queryKey: ["run"] }),
  ]);
  if (runId) {
    await queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
  }
}

function selectedRunFromAction(payload: ActionResponse): string | null {
  const selected = payload.transition?.selected_run_id ?? payload.transition?.run_id;
  return selected || null;
}

function updateStateFromAction(queryClient: QueryClient, payload: ActionResponse) {
  if (payload.state) {
    queryClient.setQueryData(queryKeys.state, payload.state);
    if (payload.state.selected_run) {
      queryClient.setQueryData(queryKeys.run(payload.state.selected_run.run.run_id), payload.state.selected_run);
    }
  }
}

export function useQueueQuery() {
  return useQuery({ queryKey: queryKeys.queue, queryFn: fetchQueue });
}

export function useProposalsQuery() {
  return useQuery({ queryKey: queryKeys.proposals, queryFn: fetchProposals });
}

export function useRunsQuery() {
  return useQuery({ queryKey: queryKeys.runs, queryFn: fetchRuns });
}

export function useDashboardStateQuery() {
  return useQuery({
    queryKey: queryKeys.state,
    queryFn: fetchState,
    refetchInterval: (query) => {
      const payload = query.state.data as DashboardStatePayload | undefined;
      const activeProposals = payload?.proposals.summary?.active ?? 0;
      const runtimeBusy = Boolean(
        payload?.runtime.dispatch_running || payload?.runtime.proposal_dispatch_running,
      );
      return activeProposals > 0 || runtimeBusy ? 2000 : false;
    },
    refetchIntervalInBackground: true,
  });
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
    onSuccess: (payload, variables) => {
      updateStateFromAction(queryClient, payload);
      refreshDashboardQueries(queryClient, variables.runId);
    },
  });
}

export function useTaskActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ taskId, action }: { taskId: string; action: AllowedTaskAction }) =>
      postTaskAction(taskId, action),
    onSuccess: (payload) => {
      updateStateFromAction(queryClient, payload);
      refreshDashboardQueries(queryClient, selectedRunFromAction(payload));
    },
  });
}

export function useQueueActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ action }: { action: AllowedQueueAction }) => postQueueAction(action),
    onSuccess: (payload) => {
      updateStateFromAction(queryClient, payload);
      refreshDashboardQueries(queryClient, selectedRunFromAction(payload));
    },
  });
}

export function useCreateProposalMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ title, prompt, cwd }: { title: string; prompt: string; cwd?: string | null }) =>
      postProposal(title, prompt, cwd ?? null),
    onSuccess: (payload) => {
      updateStateFromAction(queryClient, payload);
      refreshDashboardQueries(queryClient, selectedRunFromAction(payload));
    },
  });
}

export function useProposalActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      proposalId,
      action,
      payload,
    }: {
      proposalId: string;
      action: AllowedProposalAction;
      payload?: Record<string, unknown>;
    }) => postProposalAction(proposalId, action, payload),
    onSuccess: (payload) => {
      updateStateFromAction(queryClient, payload);
      refreshDashboardQueries(queryClient, selectedRunFromAction(payload));
    },
  });
}
