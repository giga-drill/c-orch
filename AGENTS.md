# Agent Notes

This file is the repo-local long-term memory for agents working on c-orch.
Keep it concise, public-safe, and focused on reusable project learnings. Do not
record secrets, personal credentials, local tokens, or transient logs here.

## Read First

Before planning or editing c-orch itself, read:

- `README.md`
- `docs/mcp-orchestrator-design.md`
- `docs/architecture-principles.md`

Use those docs as the source of truth for architecture, state semantics, agent
responsibilities, and product direction. Update them when a change alters a
durable project rule.

## Runtime And UI Entry Points

Use supervised dashboard entry points by default:

```bash
PYTHONPATH=src python3.11 -m c_orch.cli dev-ui --cwd /path/to/target-repo --runs-dir runs --queue-file .c-orch/tasks/queue.json
PYTHONPATH=src python3.11 -m c_orch.cli supervise-ui --cwd /path/to/target-repo --runs-dir runs --queue-file .c-orch/tasks/queue.json
```

Treat bare `c-orch ui` as a low-level child runtime/debug entry. It does not
supervise its own restart gate.

## State Boundaries

- The frontend renders backend-authored state and sends user intent back to the
  backend. It must not own business state transitions.
- `GET` endpoints should be pure reads. Queue/proposal/run writes belong in
  backend action entrypoints or scheduler code.
- Proposal planning happens before execution queue entry. The proposal pool owns
  human plan review; the execution queue owns already-approved work.
- Failure recovery metadata belongs in events and `failure_policy.py`, not as
  extra Planner review business outcomes.

## Workspace And Apply Boundaries

- Planner and Worker for one run should use the same isolated task workspace.
- Worker worktrees isolate code changes, not package caches.
- Same-target-repo modifications are currently serialized. If the target repo
  changes while a Worker is running, c-orch may reject the patch during apply
  instead of rebasing it.

## Testing

Use explicit unittest discovery for the Python suite:

```bash
PYTHONPATH=src python3.11 -m unittest discover -s tests
```

Do not use bare `python3.11 -m unittest` as the full-suite command; in this repo
it can report `Ran 0 tests`.

For dashboard frontend changes, also run relevant frontend checks:

```bash
pnpm --dir web run typecheck
pnpm --dir web run build
```

## Recent Learnings

- When a failed task is marked handled/skipped, the runtime must wake both the
  queue dispatcher and the proposal dispatcher. A waiting proposal in the same
  workspace may now be unblocked by that task action.
