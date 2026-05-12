# c-orch

Codex-native Planner/Worker workflow orchestrator.

`c-orch` uses a deterministic local controller plus Codex MCP. The controller
creates real Codex sessions for a Planner and a Worker, records their thread
ids in a run manifest, isolates Worker edits in a git worktree, collects diff
and verification evidence, and asks the Planner to approve or request changes.

## Current Design

- [MCP-based orchestrator technical design](docs/mcp-orchestrator-design.md)

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

When run events are available, the local run dashboard shows a per-run Timeline.

Common options:

```bash
--codex-bin /Applications/Codex.app/Contents/Resources/codex
--planner-model gpt-5.5
--worker-model gpt-5.3-codex-spark
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
model = "gpt-5.3-codex-spark"
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
  Worker git worktrees.
- The current MVP supports one Worker thread with human plan approval before
  Worker start and retry-on-review after Worker execution.
- Planner/Worker sessions are created through Codex MCP and written into Codex
  session logs with `source=mcp`.
- Thread naming and richer live progress are left for a future Codex App Server
  driver.
