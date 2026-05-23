# Strategy Experiment Arena

The experiment arena runs multiple candidate execution strategies for the same
task, then preserves their isolated worktrees, patches, verification output, and
summary metadata for human comparison.

It is intentionally separate from the proposal pool and execution queue:

- It does not approve, enqueue, apply, merge, commit, or open PRs.
- Each strategy arm starts from the same base commit in its own worktree.
- Candidate failures are recorded per arm and do not block other arms.
- The evaluator is external: a human or future evaluator compares the produced
  patches and evidence.

## Strategies

Default arms:

- `codex-direct`: Codex implements the task directly.
- `codex-corch-workflow`: Codex follows a compact COrch-style
  plan/implement/self-review/verify discipline in one session.
- `codex-superpowers`: Codex uses the imported Superpowers work-discipline
  prompt fragments without adopting Superpowers workflow control.
- `corch-plain`: Codex simulates COrch Planner/Worker/Reviewer phases inside
  one candidate run.
- `corch-superpowers`: COrch phase boundaries plus Superpowers-style work
  discipline.

## CLI

```bash
PYTHONPATH=src python3.11 -m c_orch.cli experiment run \
  --cwd /path/to/repo \
  --max-parallel 2 \
  --verification-command "PYTHONPATH=src python3.11 -m unittest discover -s tests" \
  "Implement the task to compare"
```

Use repeated `--strategy` flags to compare a subset.

Results are written under `.c-orch/experiments/<experiment-id>/` by default.
The experiment worktrees use the configured `run.worktrees_dir` unless
`--worktrees-dir` is provided.
