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
- Worker worktrees
- evidence collection
- verification commands
- apply behavior
- failure recovery policy

Planner and Worker own semantic work:

- Planner designs plans, acceptance criteria, Worker instructions, and review
  decisions.
- Worker implements the approved plan inside its assigned worktree.

## State Model

Keep normal business progress separate from failure handling.

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
