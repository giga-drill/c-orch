# c-orch

Codex-native Planner/Worker workflow orchestrator.

`c-orch` uses a deterministic local controller plus Codex MCP. The controller
creates real Codex sessions for a Planner and a Worker, records their thread
ids in a run manifest, isolates Worker edits in a git worktree, collects diff
and verification evidence, and asks the Planner to approve or request changes.

## Current Design

- [MCP-based orchestrator technical design](docs/mcp-orchestrator-design.md)
- [Architecture principles and state model](docs/architecture-principles.md)

## Workflow Shape

c-orch now separates plan review from execution:

```text
Plan proposal pool: task idea -> Planner plan -> human approve/revise
Execution queue: approved plan -> Worker -> Planner review -> apply -> commit
```

The proposal pool is where a human reviews Planner's plan. Approving a proposal
adds an execution task that points at the same run, so the Worker starts from
the approved Planner context. Revising a proposal sends feedback back to the
same Planner thread and waits for a new plan. The execution queue is reserved
for plans that have already been approved and can move automatically until a
restart gate, failure, or task completion.

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

For smoke tests or trusted tiny changes, skip the approval gate:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli run \
  --auto-approve-plan \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

Open the local run dashboard:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli ui \
  --cwd /path/to/target-repo \
  --runs-dir runs
```

For self-modifying Cork runs, prefer the supervised dashboard. It starts the
same UI/runtime child process, watches for `RESTART_REQUIRED`, restarts the
child, and then clears the restart gate through the backend queue action:

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
--worker-reasoning-effort medium
--planner-service-tier fast
--worker-service-tier flex
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
reasoning_effort = "medium"

[run]
max_attempts = 3
sandbox = "workspace-write"
approval_policy = "never"
runs_dir = "runs"
worktrees_dir = ".c-orch/worktrees"

[ui]
host = "127.0.0.1"
port = 8765
```

## Codex Binary Selection

`c-orch doctor` probes Codex binaries in this order:

1. `--codex-bin` / `C_ORCH_CODEX_BIN`
2. `/Applications/Codex.app/Contents/Resources/codex`
3. `codex` on `PATH`

This is intentional. On this machine, the Codex.app embedded CLI is newer and
has `gpt-5.5`; the older Homebrew CLI does not.

## Boundaries

- The target repo must have a committed base ref before `c-orch run` can create
  task git worktrees.
- Each run gets one isolated task workspace. Planner and Worker sessions for
  that run use the same workspace so review can inspect the same files the
  Worker changed.
- The current MVP supports one Worker thread with human plan approval before
  Worker start and retryable review attempts after Worker execution. Dashboard
  task submission uses a proposal pool so `human_plan_review` happens before
  the task enters the execution queue. Queue scheduling can auto-trigger
  `retry_review` from saved evidence. `restart` remains an operational gate when
  running the plain dashboard, and the `supervise-ui` wrapper can clear it after
  restarting the UI/runtime child.
- The current MVP does not support parallel runs that modify the same target
  repo. Run such tasks serially; otherwise patch apply can conflict with commits
  made while a Worker was running.
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
