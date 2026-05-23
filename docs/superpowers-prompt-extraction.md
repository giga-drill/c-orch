# Superpowers Prompt Extraction Manifest

Source repo: `https://github.com/obra/superpowers`

Source commit inspected: `f2cbfbefebbfef77321e4c9abc9e949826bea9d7`

Purpose: adopt Superpowers work-discipline primitives for c-orch prompts
without adopting Superpowers workflow control. c-orch remains the owner of
queueing, state transitions, workspace/worktree setup, retries, apply/commit,
merge/PR decisions, restart gates, and user interaction.

## COrch Control Boundary

Added to every Planner, Worker, and Reviewer prompt.

Used to override any Superpowers instruction that would otherwise tell an agent
to control workflow state:

- create or switch worktrees
- commit, merge, open PRs, or clean up branches
- dispatch subagents
- choose execution mode
- ask the user directly
- run finishing branch workflows

Instead, agents must report concerns through the c-orch JSON contract.

## Planner Discipline

Target c-orch prompt fragment: `SUPERPOWERS_PLANNING_DISCIPLINE`.

Source ranges used:

- `skills/writing-plans/SKILL.md:L10-L12`
  - Used for: write plans for an executor with little repo context.
- `skills/writing-plans/SKILL.md:L21-L34`
  - Used for: scope check, file/responsibility mapping, cohesive task
    boundaries.
- `skills/writing-plans/SKILL.md:L36-L44`
  - Used for: bite-sized, testable task steps.
- `skills/writing-plans/SKILL.md:L63-L97`
  - Used for: exact file paths, test commands, expected outputs, minimal
    implementation steps.
- `skills/writing-plans/SKILL.md:L106-L120`
  - Used for: no placeholders, exact commands, TDD-oriented plan details.
- `skills/writing-plans/SKILL.md:L122-L132`
  - Used for: self-review against spec coverage, placeholders, and type/name
    consistency.

Sanitized out:

- `skills/writing-plans/SKILL.md:L14-L18`
  - Reason: announcing skill usage and saving plans to Superpowers paths are
    Superpowers workflow mechanics, not c-orch Planner output.
- `skills/writing-plans/SKILL.md:L52`
  - Reason: sub-skill handoff is controlled by c-orch phases.
- `skills/writing-plans/SKILL.md:L98-L103`
  - Reason: c-orch owns commit.
- `skills/writing-plans/SKILL.md:L134-L140`
  - Reason: c-orch chooses execution flow; Planner must not ask the user to
    choose Superpowers execution modes.

## Worker Discipline

Target c-orch prompt fragment: `SUPERPOWERS_WORKER_DISCIPLINE`.

Source ranges used:

- `skills/executing-plans/SKILL.md:L18-L30`
  - Used for: critically review the plan, follow bite-sized steps, run
    specified verification.
- `skills/executing-plans/SKILL.md:L39-L55`
  - Used for: stop on blockers, critical plan gaps, unclear instructions, or
    repeated verification failures.
- `skills/executing-plans/SKILL.md:L57-L64`
  - Used for: critical plan review first, do not skip verification, do not
    guess through blockers.
- `skills/test-driven-development/SKILL.md:L10-L12`
  - Used for: test first, observe failure, implement minimal passing code.
- `skills/test-driven-development/SKILL.md:L31-L35`
  - Used for: no production behavior change without a failing test first when
    practical.
- `skills/test-driven-development/SKILL.md:L47-L68`
  - Used for: red-green-refactor loop.
- `skills/test-driven-development/SKILL.md:L108-L132`
  - Used for: tests should check real behavior, fail for the expected reason,
    and drive minimal implementation.

Sanitized out:

- `skills/executing-plans/SKILL.md:L21`
  - Reason: Worker must return blockers in JSON instead of asking the user
    directly.
- `skills/executing-plans/SKILL.md:L22`
  - Reason: TodoWrite is not c-orch state.
- `skills/executing-plans/SKILL.md:L34-L37`
  - Reason: c-orch owns terminalization and finishing gates.
- `skills/executing-plans/SKILL.md:L68-L70`
  - Reason: c-orch owns worktree setup and finishing workflow.
- `skills/test-driven-development/SKILL.md:L24-L29`
  - Reason: asking the user about exceptions is converted into reporting
    blockers/concerns through WorkerResult.

## Reviewer Discipline

Target c-orch prompt fragment: `SUPERPOWERS_REVIEW_DISCIPLINE`.

Source ranges used:

- `skills/requesting-code-review/SKILL.md:L8-L10`
  - Used for: review the work product and evidence, not session history.
- `skills/requesting-code-review/SKILL.md:L24-L46`
  - Used for: review against description, requirements, base/head/diff, and
    classify feedback severity.
- `skills/requesting-code-review/SKILL.md:L75-L88`
  - Used for: review at task/feature/merge checkpoints, adapted to c-orch
    run-level review.
- `skills/requesting-code-review/SKILL.md:L90-L101`
  - Used for: do not skip review or ignore critical/important issues.
- `skills/subagent-driven-development/SKILL.md:L8-L12`
  - Used for: two-stage review, first spec compliance then code quality.
- `skills/subagent-driven-development/SKILL.md:L89-L102`
  - Used for: model routing guidance by task complexity.
- `skills/subagent-driven-development/SKILL.md:L104-L120`
  - Used for: implementer concern/blocker categories, adapted into c-orch
    review guidance.

Sanitized out:

- `skills/requesting-code-review/SKILL.md:L32-L34`
  - Reason: Reviewer must not dispatch subagents.
- `skills/requesting-code-review/SKILL.md:L71-L72`
  - Reason: c-orch decides whether to continue, retry, or terminalize.
- `skills/subagent-driven-development/SKILL.md:L50-L85`
  - Reason: c-orch does not adopt Superpowers subagent dispatch flow in this
    slice.
- `skills/subagent-driven-development/SKILL.md:L114-L118`
  - Reason: blocker routing is a useful rubric, but c-orch owns the actual
    state transition.
