from __future__ import annotations

DEFAULT_PLANNER_MODELS = ("gpt-5.5", "gpt-5.4")
DEFAULT_PLANNER_REASONING_EFFORT = "high"
DEFAULT_WORKER_MODEL = "gpt-5.3-codex-spark"

INTERESTING_MODEL_SLUGS = frozenset(
    {
        *DEFAULT_PLANNER_MODELS,
        "gpt-5.4-mini",
        "gpt-5.3-codex",
        DEFAULT_WORKER_MODEL,
    }
)

REASONING_EFFORT_CHOICES = ("minimal", "low", "medium", "high", "xhigh")
SERVICE_TIER_CHOICES = ("flex", "fast")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_SANDBOX = "workspace-write"
SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_APPROVAL_POLICY = "never"
APPROVAL_POLICY_CHOICES = ("untrusted", "on-failure", "on-request", "never")

DEFAULT_RUNS_DIR = "runs"
DEFAULT_WORKTREES_DIR = ".c-orch/worktrees"
DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8765
