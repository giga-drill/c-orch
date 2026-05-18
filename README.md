# c-orch

Codex-native Planner/Worker workflow orchestrator.

`c-orch` uses a deterministic local controller plus Codex MCP. The controller
creates real Codex sessions for a Planner and a Worker, records their thread
ids in a run manifest, isolates Worker edits in a git worktree, collects diff
and verification evidence, and asks the Planner to approve or request changes.

## Current Design

- [MCP-based orchestrator technical design](docs/mcp-orchestrator-design.md)
- [Architecture principles and state model](docs/architecture-principles.md)
- [Automation roadmap](docs/automation-roadmap.md)

## Canonical Entry Points

Use a supervised entry point by default. Self-modifying c-orch tasks can apply
runtime changes and set a restart gate; a supervisor is the process that can
restart the runtime and clear that gate through the backend action.

For day-to-day local development with the React/Vite dashboard, start:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli dev-ui \
  --cwd /path/to/target-repo \
  --runs-dir runs \
  --queue-file .c-orch/tasks/queue.json
```

For the checked-in static dashboard build, start:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli supervise-ui \
  --cwd /path/to/target-repo \
  --runs-dir runs \
  --queue-file .c-orch/tasks/queue.json
```

Treat `c-orch ui` as a low-level child runtime/debug entry. Do not use it as
the normal dashboard process for self-bootstrap work; it will not supervise its
own restart gate.

## Workflow Shape

c-orch separates proposal planning from execution, and by default auto-promotes
successful plans into the execution queue:

```text
Plan proposal pool: task idea -> Planner plan -> (default) auto approve + queue
Execution queue: approved plan -> Worker -> Planner review -> apply -> commit
```

The proposal pool still owns Planner plan generation and plan-related states.
In default mode, when Planner reaches `PLAN_REVIEW_REQUIRED`, backend runtime
automatically marks the same run as `PLAN_APPROVED`, enqueues a task that
reuses that run/workspace/thread context, and removes the proposal from the
pool. Optional manual review mode is available through config
(`run.require_proposal_plan_review = true`), where proposals remain in
`PLAN_REVIEW_REQUIRED` with `approve-plan` / `revise-plan`.
Proposal creation returns immediately after persisting proposal/run records;
Planner plan generation continues in a backend proposal dispatcher thread.

Each proposal/task can also bind its own target repository `cwd`. If omitted,
the runtime falls back to the current `--cwd`. This allows one queue file to
hold tasks for different repos while preserving per-task run/worktree binding.

## Runner Lease Sidecar (Phase 1)

c-orch now persists runner lease metadata per run in:

```text
runs/<run_id>/runner-leases.json
```

Phase-1 schema includes:

- `runner_id`, `runtime_generation`, `pid` / `process_hint`
- `run_id`, `task_id`, `proposal_id`
- `phase`, `started_at`, `heartbeat_at`, `lease_expires_at`
- `status` (`active`, `completed`, `failed`, `expired`)
- `checkpoint` (phase boundary metadata)

`stale` is derived on read when `status=active` and `lease_expires_at < now`.
`GET` endpoints do not write lease files.

Phase-1 now includes a supervised reviewer subprocess protocol for
`phase=code_review` only. Runtime writes request manifests under:

```text
runs/<run_id>/runner-subprocess/code_review/<runner_id>.request.json
```

The subprocess writes durable result manifests under:

```text
runs/<run_id>/runner-subprocess/code_review/<runner_id>.result.json
```

Runtime treats result manifests as source-of-truth for review reconstruction.
If subprocess launch/result parsing/result durability fails, runtime records
`runner_subprocess_failed` + `runner_subprocess_fallback_started` and falls
back to direct `code_review_runner`. A completed subprocess report with
`CodexReviewReport.status=error` is preserved as review evidence and does not
trigger infra fallback.

Still follow-up work:

- Planner/Worker subprocess handoff
- runtime-startup lease reconciliation across alive/stale/completed runners

## Quick Start

Run from this repo without installing:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli doctor
```

Prepare a run without calling Codex:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli run \
  --prepare-only \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

Run the single-worker MVP. By default this starts the Planner, saves the plan,
and pauses for human approval before any Worker starts:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli run \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

After reviewing the saved plan in the manifest or dashboard, continue:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli resume \
  --cwd /path/to/target-repo \
  --approve-plan \
  <run_id>
```

For smoke tests or trusted tiny changes, skip the CLI approval gate:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli run \
  --auto-approve-plan \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

Open the local run dashboard from the checked-in frontend build through the
supervised entry point:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli supervise-ui \
  --cwd /path/to/target-repo \
  --runs-dir runs \
  --queue-file .c-orch/tasks/queue.json
```

For frontend development, use Vite as the browser entry point. This keeps HMR
enabled, proxies `/api/*` to the local API runtime, restarts the API child when
backend Python sources change, and uses the same supervisor logic to clear
restart gates after self-modifying runs. Host and port values come from `[ui]`
config and can be overridden with CLI flags:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli dev-ui \
  --cwd /path/to/target-repo \
  --runs-dir runs \
  --queue-file .c-orch/tasks/queue.json
```

Then open the dev UI URL printed by the command. The API process is API-only in
this mode; its root page only points back to the Vite dev UI.

For self-modifying Cork runs, prefer the supervised dashboard. It starts the
same UI/runtime child process, watches for `RESTART_REQUIRED`, restarts the
child after active queue/proposal dispatch drains, and then clears the restart
gate through the backend queue action:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli supervise-ui \
  --cwd /path/to/target-repo \
  --runs-dir runs
```

When run events are available, the local run dashboard shows a per-run Timeline.
When a queue file is configured, the dashboard also shows a plan proposal pool
beside the execution queue. Use the proposal pool for new tasks that should get
a Planner plan before entering the automated queue.

Common options:

```bash
--codex-bin /Applications/Codex.app/Contents/Resources/codex
--planner-model gpt-5.5
--worker-model gpt-5.3-codex
--planner-reasoning-effort high
--worker-reasoning-effort high
--planner-service-tier fast
--worker-service-tier fast
--auto-approve-plan
--max-attempts 3
--sandbox workspace-write
--approval-policy never
--runs-dir runs
--worktrees-dir .c-orch/worktrees
```

Project defaults can be stored in `.c-orch.toml` at the target repo root.
Command-line flags override this file.

```toml
[planner]
preferred_models = ["gpt-5.5", "gpt-5.4"]
reasoning_effort = "high"
service_tier = "fast"

[worker]
model = "gpt-5.3-codex"
reasoning_effort = "high"
service_tier = "fast"

[run]
max_attempts = 3
sandbox = "workspace-write"
approval_policy = "never"
runs_dir = "runs"
worktrees_dir = ".c-orch/worktrees"
require_proposal_plan_review = false
low_cost_mode = false

[ui]
# Optional. Omit these to use built-in defaults.
# host controls the API bind host.
# port controls the API port.
# dev_host controls the Vite bind host.
# dev_port controls the Vite dev UI port.
```

`[run].low_cost_mode = true` forces Planner, Worker, and the Codex review gate
to use `service_tier=flex` for new run/proposal/queue dispatch paths. The
review gate forwards this as `codex review -c service_tier=flex --uncommitted`.
This only overrides `service_tier`; it does not change model selection,
`reasoning_effort`, review loops, state transitions, or failure recovery policy.
It is a cost-control hint, not a hard usage cap: retries, output length, and
model choices can still increase total usage.

## Codex Binary Selection

`c-orch doctor` probes Codex binaries in this order:

1. `--codex-bin` / `C_ORCH_CODEX_BIN`
2. `/Applications/Codex.app/Contents/Resources/codex`
3. `codex` on `PATH`

This is intentional. On this machine, the Codex.app embedded CLI is newer and
has `gpt-5.5`; the older Homebrew CLI does not.

The Planner/Worker sessions and the code review gate use the same resolved
Codex binary. For code review, c-orch explicitly invokes the Codex App embedded
review CLI by default:

```bash
/Applications/Codex.app/Contents/Resources/codex review --uncommitted
```

This is a CLI quality gate, not a named Codex Skill lookup. A configured
`--codex-bin` / `C_ORCH_CODEX_BIN` path can override it; `codex` on `PATH` is
only the last fallback.

## Boundaries

- The target repo must have a committed base ref before `c-orch run` can create
  task git worktrees.
- Each run gets one isolated task workspace. Planner and Worker sessions for
  that run use the same workspace so review can inspect the same files the
  Worker changed.
- Queue/proposal records persist an optional task-level `cwd` (target repo
  root). New run attempts use `task.cwd` first, then fall back to runtime
  `--cwd`.
- The current MVP supports one Worker thread with human plan approval before
  Worker start and retryable review attempts after Worker execution in the
  direct `c-orch run` / `resume` path. Dashboard task submission uses a proposal
  pool: default policy auto-queues Planner-complete proposals into execution;
  optional policy can require explicit human `approve-plan` / `revise-plan`
  before queueing. Queue scheduling can auto-trigger `retry_review` from saved
  evidence. `restart` remains an operational gate when running the plain
  dashboard, and the `supervise-ui` wrapper can clear it after restarting the
  UI/runtime child.
- The current MVP does not support parallel runs that modify the same target
  repo. Even though different tasks may target different repos, tasks that
  target the same repo must still run serially; otherwise patch apply can
  conflict with commits made while a Worker was running.
- Planner review has only two business decisions: `accepted` and
  `revision_requested`. Failure recovery is handled by c-orch separately from
  the business state machine.
- The dashboard UI renders state and sends user intent only. Dashboard actions
  are handled by the backend `COrchRuntime`, which owns action validation,
  Codex driver usage, and Orchestrator state transitions.
- The dashboard's source of truth is the backend state snapshot at
  `GET /api/state`. `GET` endpoints are read-only; queue reconciliation for
  display happens in memory, while persisted state changes happen through
  backend actions or the scheduler. Successful actions return a transition
  result plus a fresh state snapshot so the UI does not have to infer state from
  stale proposal, queue, or run caches.
- Proposal planning dispatch is backend-owned. `POST /api/proposals` only
  creates persistent proposal/run records and triggers async planning; the UI
  does not block on planner completion.
- Restart-gate acknowledgment must go through backend queue action
  `POST /api/queue/actions` with `action=confirm-runtime-restarted`; the UI does
  not directly edit run manifests or queue files. The supervisor also uses this
  backend action after it restarts the dashboard child process.
- The dashboard server keeps one `COrchRuntime` for its process lifetime. The
  runtime may reuse a live MCP driver for speed, but c-orch still treats MCP
  state as volatile and falls back to `codex exec resume` when needed.
- Task status is user-level state. A task can own multiple run attempts via
  `run_ids`; `active_run_id` points to the current attempt. The backend
  reconciles task/queue status from the active run and exposes derived
  `waiting_for` / `next_action` values such as `planner_review_retry`,
  `restart`, and `retry_task`. `human_plan_review` belongs to the proposal
  pool and is treated as an invalid execution-queue waiting point.
- Failed tasks can be requeued with `c-orch queue retry <task_id>` or from the
  dashboard. This preserves prior `run_ids`; when the dashboard backend has
  execution config, a requeued first task is auto-dispatched. `c-orch queue run`
  is still available as the manual scheduler entrypoint.
- Planner/Worker sessions are created through Codex MCP and written into Codex
  session logs with `source=mcp`.
- Continuing a saved session first tries MCP `codex-reply`; if a fresh MCP
  server does not know that thread id, c-orch falls back to `codex exec resume`.
- New Planner/Worker sessions pass model config explicitly through MCP `codex`
  `config` (for example `model_reasoning_effort` and `service_tier`).
- MCP `codex-reply` is used as-is for existing sessions (`threadId` + `prompt`
  only), so c-orch does not send per-reply config overrides there. When
  `codex-reply` must fall back to CLI resume, c-orch explicitly restores saved
  manifest model settings on `codex exec resume`.
- Thread naming and richer live progress are left for a future Codex App Server
  driver.
