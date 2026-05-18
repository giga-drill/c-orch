# c-orch Automation Roadmap

Date: 2026-05-17

This document records the near-term path toward a pipeline where the human only
publishes intent, while c-orch handles planning, implementation, review,
recovery, apply, commit, and self-restart automatically.

## Target Direction

The ideal workflow is:

```text
human intent -> Planner plan -> automated gates -> Worker loop -> apply -> commit
```

Human involvement should become the exception after intent publication. c-orch
should still expose clear evidence and recovery controls, but ordinary queue
progress should not depend on a human watching the dashboard.

The current system still has three important automation gaps:

- code quality review is only part of Planner semantic review
- failure recovery is not yet centralized or durable enough
- self-bootstrap restart needs a stronger runtime protocol

## 1. Code Review Gate

### Goal

Add a mandatory code-quality step inside the Planner review phase. The Planner
review phase should gather code-review evidence first, then make the single
final business decision for that Worker attempt.

The gate should use Codex's non-interactive review command. This is an
explicit CLI gate, not a Codex Skill lookup. c-orch should invoke the
Codex.app embedded CLI by default so review uses the same desktop-authenticated
Codex binary and does not accidentally pick an older `codex` from `PATH`:

```bash
/Applications/Codex.app/Contents/Resources/codex review --uncommitted
/Applications/Codex.app/Contents/Resources/codex review --base <branch>
/Applications/Codex.app/Contents/Resources/codex review --commit <sha>
```

For c-orch's worker worktree flow, the first implementation should review the
Worker's captured patch inside an isolated review worktree. A user-provided
`--codex-bin` / `C_ORCH_CODEX_BIN` override may still be used, but the default
must be the App embedded CLI before falling back to `codex` on `PATH`.

### Proposed Flow

```text
Worker attempt completed
-> collect diff evidence
-> run verification commands
-> enter Planner review phase
-> c-orch runs Codex code review for the Worker diff
-> save code-review evidence
-> send plan + diff + test + code-review evidence to Planner
-> Planner returns accepted or revision_requested
-> if Planner requests changes:
     include code-review findings and Planner semantic feedback
     in the next Worker prompt
     Worker reworks in the same task workspace
     repeat diff/test/Planner-review-with-code-review loop
-> if Planner accepts:
     apply diff to target repo
     commit applied changes
```

The code review gate is not a separate rework decision before Planner review
and not a third Planner business outcome. It is tool evidence gathered during
Planner review. Planner combines code-review findings, verification results,
diff evidence, and acceptance criteria into the only two semantic outcomes:

```text
accepted
revision_requested
```

### Evidence Contract

Each Worker attempt should persist:

- raw Codex review output
- normalized review findings, for example blocking findings, advisory notes,
  or no blocking findings
- actionable findings included in Planner's next Worker prompt when Planner
  returns `revision_requested`
- command, cwd, resolved Codex binary path/source, model/config if available
- review output path in the run manifest evidence list

Suggested evidence files:

```text
evidence/attempt-N/review-M/codex-review-output.txt
evidence/attempt-N/review-M/codex-review-result.json
```

The Planner review prompt should include the current code-review evidence every
time it reviews a Worker attempt. If prior attempts had code-review findings,
Planner should also see a compact history of those findings and the Worker
rework loop.

### First Implementation Shape

Add a `CodeReviewRunner` abstraction beside verification:

```text
orchestrator.py
  Worker attempt
  collect evidence
  run verification
  Planner review phase
    run code review
    ask Planner for accepted or revision_requested
  maybe rework Worker from Planner's next_worker_prompt

code_review.py
  run_codex_review(...)
  resolve App embedded Codex review CLI
  parse/normalize review output
  persist review evidence
```

The first version can be conservative:

- run after verification as the first step of Planner review
- retry infra failures through failure policy
- include actionable findings in the Planner review prompt
- let Planner decide whether those findings require Worker rework
- cap total Worker attempts with the existing max-attempts budget
- expose the code-review report in the dashboard evidence panel

### Open Questions

- Whether `codex review` can be prompted to emit strict JSON reliably enough, or
  whether c-orch needs a small parser/normalizer step.
- Whether minor comments should always block the Worker, or whether the review
  result should distinguish blocking findings from advisory notes.
- Which model/reasoning settings `codex review` should use by default.

## 2. Failure Recovery Policy

### Goal

Move retry, resume, replacement-agent, verification rerun, task retry, and
human-escalation decisions into one explicit recovery policy.

The important point is durability. Splitting Agents into independent runner
subprocesses can prevent a runtime restart from killing them, but it does not
solve correctness by itself. c-orch must persist phase-level state before and
after every externally visible action.

### Principle

The source of truth must be persisted state, not runtime memory:

- run manifest
- event log
- task/proposal queue records
- attempt evidence
- phase timing/checkpoints
- active runner lease/owner metadata

If a Planner, Worker, reviewer, or runner completes a phase but c-orch does not
persist that phase result, then a restart can still lose the state even if the
underlying Agent process survived.

### Proposed Recovery Layers

Use one policy module to classify failures:

```text
transient infrastructure
  -> retry with backoff

MCP session lost
  -> codex exec resume
  -> replacement Planner/Worker from saved manifest/evidence if resume fails

verification failure
  -> rerun once if likely flaky
  -> otherwise feed failure evidence back to Worker

code review findings
  -> Worker rework, then rerun verification and code review

apply conflict
  -> short term: fail with explicit evidence
  -> future: recreate/rebase task workspace and retry

git commit failure
  -> classify dirty repo, hook failure, no changes, or git error
  -> auto-handle safe cases, otherwise fail with recovery action

max attempts exceeded
  -> mark failed and expose evidence for human decision
```

### State Checkpoint Contract

Each phase should be idempotent and resumable from persisted state:

```text
phase_started event
phase output evidence persisted
manifest updated
phase_completed event
owning task/proposal reconciled
```

For long-running or subprocess-based execution, persist a lease:

```text
runner_id
runtime_generation
pid or process handle if local
started_at
heartbeat_at
lease_expires_at
phase
run_id/task_id/proposal_id
```

A restarted runtime can then decide whether a lane is still alive, stale, done,
or safe to retry.

### First Implementation Shape

- Expand `failure_policy.py` into the single classification point.
- Teach Orchestrator and Runtime to ask policy for recovery action instead of
  directly choosing UI/manual retry paths.
- Persist recovery attempts and decisions as events.
- Keep dashboard actions as manual overrides, not the primary recovery path.

## 3. Self-Bootstrap Restart Protocol

### Goal

Let c-orch safely modify itself, apply and commit those changes, restart the
runtime when needed, and continue the queue without human confirmation.

### Key Risk

The risky case is not only "restart kills Worker/Planner." The deeper risk is:

```text
an Agent or runner reaches a phase boundary
but c-orch has not durably recorded that boundary
then runtime restarts
then c-orch cannot know what really happened
```

Therefore self-bootstrap needs both process supervision and durable phase
state.

### Proposed Protocol

```text
self-modifying run accepted
-> apply diff
-> commit applied changes
-> mark restart_required with affected paths
-> runtime enters drain mode
-> do not start new proposal or queue lanes
-> let active lanes reach safe checkpoint or persist resumable leases
-> supervisor restarts child runtime
-> new runtime reconciles proposals, queue, runs, leases, and evidence
-> backend confirms runtime restarted through queue action
-> scheduler resumes eligible work
```

### Drain Mode

Drain mode should mean:

- no new proposal planning lanes
- no new queue execution lanes
- existing lanes may finish their current durable phase
- if a lane cannot finish soon, its lease and phase state must be persisted
  before restart

The first implementation can be simpler: only auto-restart when no active lanes
exist. Later, runner subprocesses plus leases can allow restart while external
work is still running.

### Runner Subprocesses

Moving Planner/Worker work into runner subprocesses is useful, but only as one
part of the solution:

- it can keep work alive across UI/API runtime restart
- it can isolate long-running Agent calls from HTTP/server lifecycle
- it can expose process-level heartbeat and cancellation

It does not replace manifest/event/evidence persistence. Runner completion must
write durable phase output before c-orch considers the phase complete.

### Open Design Work

- Define the exact lane lease schema.
- Decide which phases are safe restart checkpoints.
- Decide whether a runner writes manifests directly or reports results back to
  runtime through a small local protocol.
- Decide how to recover a runner that finishes while runtime is restarting.
- Decide when self-bootstrap restart is allowed automatically and when it must
  fail safe.

## Suggested Order

1. Implement Code Review Gate.
2. Centralize Failure Recovery Policy around durable recovery decisions.
3. Implement Self-Bootstrap Restart Protocol in stages:
   first drain-and-restart when idle, then runner leases, then restart while
   external lanes are active.
