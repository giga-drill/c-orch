# c-orch Architecture Principles

Date: 2026-05-12

This document records project-level decisions that should shape future
Planner/Worker tasks. Update it whenever a change alters c-orch architecture,
state semantics, agent responsibilities, or long-term direction.

## Product Direction

c-orch is a Codex-native workflow orchestrator. It keeps Planner and Worker work
inside real Codex sessions so progress remains visible in Codex session history
and can use the user's Codex subscription and model access.

The deterministic c-orch controller owns orchestration mechanics:

- run and task state
- Codex thread ids
- task workspaces
- evidence collection
- verification commands
- apply behavior
- failure recovery policy

The dashboard frontend is not part of the business state machine. It should
render run/task state and send user intent to the backend. Backend runtime code
owns action validation, Codex driver usage, Orchestrator calls, and manifest
updates.

The dashboard reads a backend-authored state snapshot. `GET` endpoints must be
pure reads: they may derive display summaries in memory, but they must not write
queue, proposal, or run files. State changes must happen through backend action
entrypoints or the queue scheduler. Dashboard actions should return an explicit
transition result plus a fresh state snapshot, so the frontend can converge even
when a proposal is removed from the pool or a runtime restart interrupts an
in-flight request.
Run phase timing follows the same boundary: phase segments and transition
timestamps are backend-authored run-manifest data, and UI timing panels must
render backend `run.timing` summary directly instead of inferring business phase
durations from raw event logs in the browser. Legacy runs that predate manifest
timing can use backend best-effort fallback summaries derived from
`events`/`created_at`/`updated_at` with explicit `missing` or `partial` markers.
`POST /api/proposals` is also a control-plane action: it should persist proposal
and run metadata quickly, then let backend async dispatchers continue Planner
work outside the HTTP request.

The dashboard server keeps a process-lifetime runtime object. That runtime may
reuse a live Codex MCP driver as a performance and continuity optimization, but
MCP process memory is volatile cache only. Persisted run manifests, event logs,
Codex thread ids, and Codex disk sessions remain the recovery source of truth.
For self-modifying runs, `c-orch supervise-ui` is the preferred wrapper around
the dashboard server. The supervisor owns process restart, while the restarted
backend still owns clearing the restart gate through the normal queue action.
Automatic restart should eventually use a drain protocol: stop starting new
lanes, let active lanes reach durable checkpoints or persist leases, restart
the child runtime, reconcile persisted state, and then clear the restart gate.

Worker worktrees isolate code changes, not package caches. Verification must run
inside the worker worktree, but dependency setup should follow the target
workspace's lockfile and package manager. For the bundled dashboard frontend,
`web/pnpm-lock.yaml` is the source of truth: c-orch may prepare the worker
worktree with `pnpm --dir web install --frozen-lockfile`, which reuses pnpm's
content-addressed store instead of copying the main checkout's `node_modules`.

Planner and Worker own semantic work:

- Planner designs plans, acceptance criteria, Worker instructions, and review
  decisions.
- Planner and Worker for the same run use one isolated task workspace.
- Worker implements the approved plan inside that task workspace.
- c-orch may insert deterministic quality tools, such as a Codex code review
  gate, inside the Planner review phase. These tools produce evidence for the
  Planner; they do not create an additional business outcome beyond
  `accepted` and `revision_requested`.

The task workspace is currently implemented as the single Worker's git
worktree. Planner sessions must start in the same workspace before planning, and
Planner review must judge that workspace plus the saved diff/test evidence, not
another checkout of the same repo.

## State Model

Keep normal business progress separate from failure handling.

c-orch separates human plan review from the executable task queue. New work
should first enter the plan proposal pool:

```text
proposal -> Planner plan -> human plan review -> execution queue
```

The proposal pool owns ambiguous or still-negotiated work. A proposal may ask
Planner to revise the plan multiple times in the same Planner thread. Only
after a human approves the plan does c-orch enqueue an execution task that
points at the approved run. The execution queue should contain work that is
already approved to run automatically:

```text
approved plan -> Worker execution -> Planner review -> apply -> commit
```

This boundary keeps the queue pipeline from stopping on human plan review. The
queue may still stop at explicit operational gates such as restart confirmation
or failed task retry, but it should not treat "waiting for human plan approval"
as normal executable queue progress. When the dashboard is supervised, the
restart confirmation gate can be cleared automatically after the child runtime
has been restarted.

Business run states describe where the task is in the Planner/Human/Worker
loop:

```text
NEW
PLANNING
PLAN_READY
PLAN_REVIEW_REQUIRED
PLAN_REVISING
PLAN_APPROVED
WORKING
WORK_DONE
REVIEWING
REVISION_REQUESTED
APPROVED
FAILED
```

Worker review has only two business outcomes:

```text
accepted
revision_requested
```

`accepted` means the Worker result satisfies the acceptance criteria. c-orch
may then apply the diff. `revision_requested` means Planner must provide a
concrete next Worker instruction.

Do not add review outcomes such as `blocked`, `failed`, or `retryable` to the
Planner review contract. Those are system concerns, not business review
decisions.

Failure recovery is tracked separately by review attempts, events, and
`failure_policy.py`. Codex MCP `codex-reply` can only reliably address sessions
known to the current MCP server process. When a saved session id exists on disk
but a fresh MCP server reports `Session not found`, c-orch should first recover
the session through `codex exec resume <session-id>`. If that resume path also
fails, c-orch may start a replacement Planner or Worker using saved manifest,
evidence, and the same concrete prompt. The run state should not become a
special exception state.
Longer term, recovery policy should be centralized and phase-durable: runner
subprocesses may keep Agent work alive across runtime restart, but they are not
the source of truth. Run manifests, events, queue/proposal records, evidence,
and phase checkpoints must be persisted before c-orch treats a phase as
complete.

Task state is the user-level lifecycle; run state is one execution attempt.
One task can have multiple run attempts over time. `active_run_id` points to the
current attempt and `run_ids` preserves prior attempts for audit.
Task/proposal records may persist optional `cwd` (target repo root). Queue
scheduler must use `task.cwd` for new attempts when present, and only fall back
to runtime `--cwd` when missing.

A queued task may start with an `active_run_id` that already has an approved
Planner plan. In that case the scheduler must continue the existing run from
`PLAN_APPROVED` instead of creating a replacement run. This is the handoff from
proposal pool to execution queue.
If `active_run_id` already exists, scheduler must continue using the saved run
manifest/worktree context and must not overwrite it from current runtime `--cwd`.

Task status should stay coarse:

```text
PENDING
RUNNING
WAITING
APPROVED
FAILED
```

`BLOCKED` is retained only for legacy or exceptional queue records. Detailed
waiting points such as `planner_review_retry`, `worker_rework`, and
`retry_task` are derived values, not separate task statuses.
`human_plan_review` belongs to the proposal pool; if an execution-queue task
reaches it, c-orch treats that as a flow-boundary violation rather than normal
queue progress. `task_lifecycle.py` owns task/run reconciliation, so Scheduler,
Runtime, CLI, and UI do not each invent their own task transition rules.

When an active run reaches `FAILED`, the owning task must converge to `FAILED`
with `waiting_for=retry_task`; retrying the task should create a new run
attempt rather than mutating the old failed run. The first retry step is
requeueing the failed task: preserve `run_ids`, clear `active_run_id`, and move
the task back to `PENDING`. `PENDING` is an eligible scheduling state: if the
dashboard backend has execution config and the task is the first incomplete
task, the backend scheduler should automatically create the next run attempt.
The CLI `queue run` command remains the explicit manual entrypoint for the same
scheduler path.

## Apply Boundary

The current MVP assumes runs that modify the same target repository are
serialized. It does not support parallel modification of one repo by multiple
task workspaces. If the target repo changes while a Worker is running, c-orch
may reject the reviewed patch during apply instead of rebasing or merging it.
Queue files may mix tasks for different target repos, but same-repo tasks are
still serialized by this boundary.

## Agent Context Contract

Before planning or editing c-orch itself, agents should read:

- `README.md`
- `docs/mcp-orchestrator-design.md`
- `docs/architecture-principles.md`

If an agent is working in another target repo, it should first inspect that
repo's README and relevant docs before proposing or editing architecture-level
changes.

Planner prompts and Worker prompts should preserve this instruction so small
unit tasks remain aligned with the larger project direction.

## Documenting Architecture Changes

Any change that affects architecture, state semantics, agent responsibilities,
failure handling, runtime boundaries, or product direction should update one of:

- `docs/architecture-principles.md` for durable project principles
- `docs/mcp-orchestrator-design.md` for implementation design details
- `README.md` for user-facing capabilities and boundaries

Small bug fixes do not need design documentation unless they expose or change a
durable principle.
