import { QueryClient, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  fetchProposals,
  fetchQueue,
  fetchRun,
  fetchRuns,
  postProposal,
  postProposalAction,
  postQueueAction,
  postRunAction,
  postTaskAction,
} from "./api/client";
import type { AllowedProposalAction, AllowedQueueAction, AllowedRunAction, AllowedTaskAction } from "./api/types";

export const queryKeys = {
  queue: ["queue"] as const,
  proposals: ["proposals"] as const,
  runs: ["runs"] as const,
  run: (runId: string) => ["run", runId] as const,
};

function refreshDashboardQueries(queryClient: QueryClient, runId?: string | null) {
  void queryClient.invalidateQueries({ queryKey: queryKeys.proposals });
  void queryClient.invalidateQueries({ queryKey: queryKeys.queue });
  void queryClient.invalidateQueries({ queryKey: queryKeys.runs });
  void queryClient.invalidateQueries({ queryKey: ["run"] });
  if (runId) {
    void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId) });
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
      queryClient.setQueryData(queryKeys.run(variables.runId), payload);
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
      queryClient.setQueryData(queryKeys.queue, payload);
      refreshDashboardQueries(queryClient);
    },
  });
}

export function useQueueActionMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ action }: { action: AllowedQueueAction }) => postQueueAction(action),
    onSuccess: (payload) => {
      queryClient.setQueryData(queryKeys.queue, payload);
      refreshDashboardQueries(queryClient);
    },
  });
}

export function useCreateProposalMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ title, prompt }: { title: string; prompt: string }) => postProposal(title, prompt),
    onSuccess: (payload) => {
      queryClient.setQueryData(queryKeys.proposals, payload);
      refreshDashboardQueries(queryClient);
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
    onSuccess: (payload, variables) => {
      queryClient.setQueryData(queryKeys.proposals, payload);
      const proposal = payload.proposals.find((item) => item.proposal_id === variables.proposalId);
      refreshDashboardQueries(queryClient, proposal?.run_id);
    },
  });
}
