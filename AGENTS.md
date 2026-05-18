# Agent Notes

Repo-local agent memory for c-orch. Keep this file very short: it may be
included in context or reread often. Store durable detail in docs, not here.
Never record secrets, credentials, tokens, or transient logs.

## Read First

- `README.md`
- `docs/mcp-orchestrator-design.md`
- `docs/architecture-principles.md`

Update the docs, not this file, when a change alters architecture, state
semantics, agent responsibilities, failure handling, or product direction.

## Core Rules

- Frontend only renders backend-authored state and sends user intent; backend
  runtime owns business transitions.
- `GET` endpoints are pure reads. Queue/proposal/run writes go through backend
  actions or schedulers.
- Proposal pool owns Planner plan generation and human plan review. Execution
  queue owns already-approved work.
- Failure recovery lives in events and `failure_policy.py`, not extra Planner
  review outcomes.
- Planner and Worker for one run use the same isolated task workspace.
- Worker worktrees isolate code changes, not dependency caches.
- Same-target-repo edits are serialized; apply may fail instead of rebasing if
  the target repo changes during a Worker run.

## Commands

Use supervised dashboard entry points:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli dev-ui --cwd /path/to/repo --runs-dir runs --queue-file .c-orch/tasks/queue.json
PYTHONPATH=src python3.11 -m c_orch.cli supervise-ui --cwd /path/to/repo --runs-dir runs --queue-file .c-orch/tasks/queue.json
```

Treat bare `c-orch ui` as a low-level child runtime/debug entry.

Run Python tests with explicit discovery:

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests
```

Bare `python3.11 -m unittest` can report `Ran 0 tests` in this repo. For
frontend changes, also run `pnpm --dir web run typecheck` and
`pnpm --dir web run build`.

## Current Learning

When a failed task is marked handled/skipped, wake both queue and proposal
dispatchers; waiting proposals in that workspace may become unblocked.
