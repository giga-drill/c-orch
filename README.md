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
PYTHONPATH=src python3 -m c_orch.cli doctor
```

Prepare a run without calling Codex:

```bash
PYTHONPATH=src python3 -m c_orch.cli run \
  --prepare-only \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

Run the single-worker MVP:

```bash
PYTHONPATH=src python3 -m c_orch.cli run \
  --cwd /path/to/target-repo \
  "Implement feature X"
```

Common options:

```bash
--codex-bin /Applications/Codex.app/Contents/Resources/codex
--planner-model gpt-5.5
--worker-model gpt-5.3-codex
--max-attempts 3
--sandbox workspace-write
--approval-policy never
--runs-dir runs
--worktrees-dir .c-orch/worktrees
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
- The current MVP supports one Worker thread with retry-on-review.
- Planner/Worker sessions are created through Codex MCP and written into Codex
  session logs with `source=mcp`.
- Thread naming and richer live progress are left for a future Codex App Server
  driver.
