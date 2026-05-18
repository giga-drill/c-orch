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
Dashboard startup should use a supervised entry point by default:
`c-orch dev-ui` for local React/Vite development, or `c-orch supervise-ui` for
the checked-in static dashboard build. The bare `c-orch ui` command is a
low-level child runtime/debug entry; it does not supervise its own restart gate.
For self-modifying runs, the supervisor owns process restart, while the
restarted backend still owns clearing the restart gate through the normal queue
action.
Automatic restart should eventually use a drain protocol: stop starting new
lanes, let active lanes reach durable checkpoints or persist leases, restart
the child runtime, reconcile persisted state, and then clear the restart gate.

## Usage Attribution Sidecar

c-orch writes a per-run `usage-attribution.jsonl` sidecar under
`runs/<run_id>/` for business attribution only. It records run/task/proposal/
workspace labels and Codex join keys (thread/session id when available) for
Planner, Worker, and reviewer phases.

c-orch does not collect full session usage facts or token accounting and does
not compute usage totals. External collectors such as CodexUsage remain the
source of truth for session/token facts; they should join with c-orch sidecar
records primarily by thread/session id, with `cwd` and `worktree_path` only as
fallback hints.

Sidecar write failures are non-blocking: orchestration continues, and c-orch
best-effort records a `usage_attribution_failed` event or warning.

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
- The code review gate calls the Codex App embedded CLI explicitly
  (`/Applications/Codex.app/Contents/Resources/codex review --uncommitted`) by
  default. It is not a named Skill that Planner/Worker discover dynamically.
  A configured Codex binary may override this, and `PATH` is only a fallback.

The task workspace is currently implemented as the single Worker's git
worktree. Planner sessions must start in the same workspace before planning, and
Planner review must judge that workspace plus the saved diff/test evidence, not
another checkout of the same repo.

## State Model

Keep normal business progress separate from failure handling.

c-orch separates proposal planning from the executable task queue. New work
enters the plan proposal pool first:

```text
proposal -> Planner plan -> execution queue
```

The proposal pool owns Planner plan generation and plan-related state. In the
default policy, when Planner reaches `PLAN_REVIEW_REQUIRED`, backend runtime
auto-approves that same run and enqueues execution using the same
`active_run_id`, `run_ids`, workspace, and Planner thread context. The
execution queue should contain work that is already approved to run
automatically:

```text
approved plan -> Worker execution -> Planner review -> apply -> commit
```

Proposal planning must pass a backend workspace-clean preflight guard before
Planner generation starts. The guard runs against the proposal/task target
`cwd` (not the controller repo), resolves its Git root, and checks
`git -C <root> status --short`. If the target workspace is dirty, the proposal
stays in the pool as `WAITING_WORKSPACE_CLEAN` with blocker details; c-orch
must not create/advance Planner calls for that proposal until the user cleans
the target repo and retries plan generation.

This boundary keeps the queue pipeline from stopping on plan review by default.
Optional policy `run.require_proposal_plan_review = true` keeps proposals in
`PLAN_REVIEW_REQUIRED` and exposes `approve-plan` / `revise-plan` actions for
human plan review. The queue may still stop at explicit operational gates such
as restart confirmation or failed task retry. When the dashboard is supervised,
the restart confirmation gate can be cleared automatically after the child
runtime has been restarted.

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

Current recovery-policy audit contract (phase 1):

- recovery decisions are persisted as `recovery_decision_recorded` events
- each decision records `phase`, `category`, `reason`, `recovery_action`,
  `source`, `attempt`, `retryable`, `automatic`, and `requires_human`
- normal run/task business states remain unchanged; recovery metadata does not
  add new business states such as `retryable` or `blocked` into Planner review
  outcomes

Not implemented in phase 1:

- runner lease ownership and heartbeat reconciliation
- external runner subprocess execution handoff
- automatic rebase/replay for same-repo parallel apply conflicts

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
`human_plan_review` belongs to the optional proposal-review policy; if an
execution-queue task reaches it, c-orch treats that as a flow-boundary
violation rather than normal queue progress. `task_lifecycle.py` owns task/run
reconciliation, so Scheduler, Runtime, CLI, and UI do not each invent their own
task transition rules.

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

## Development Verification

Use explicit unittest discovery for the Python suite:

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests
```

Do not use bare `python3.11 -m unittest` as the full-suite command for this
repo. It can report `Ran 0 tests` because it does not automatically recurse
into the repo's `tests/` directory in the way this project expects.

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
