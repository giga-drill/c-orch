from __future__ import annotations

import json
from typing import Iterable, Optional


PROJECT_CONTEXT_INSTRUCTIONS = """Project context:
Before planning, reviewing, or editing, inspect the target repo's README and
relevant docs when they exist. For c-orch itself, read README.md,
docs/mcp-orchestrator-design.md, and docs/architecture-principles.md first.
Keep architecture, state semantics, failure handling, and agent
responsibilities aligned with those docs.

Choose verification commands from the target workspace's lockfiles. c-orch owns
dependency setup before verification. If a web frontend has pnpm-lock.yaml, use
pnpm commands such as `pnpm --dir web run typecheck` and
`pnpm --dir web run build` instead of npm; do not include a separate pnpm
install command unless the task explicitly requires testing dependency setup.

`verification_commands` are hard gates: c-orch will run each command after
Worker completion, and any non-zero exit blocks apply/commit and fails the run.
Only include commands whose failure should block the task for this specific
workspace and request. Put optional diagnostics, package-build checks, or
environment/tooling probes in the worker_prompt or risk_notes instead of
`verification_commands`."""


PLANNER_REVIEW_INSTRUCTIONS = """Review depth requirements:
- Use the review target workspace as the source of truth. Inspect the changed
  files, surrounding code, and relevant docs/tests in that workspace; do not
  decide from the diff alone.
- Trace affected call sites, UI/API flows, state transitions, and persistence
  paths when the change touches them. Check whether the new behavior preserves
  the original business expectations that still matter.
- Compare the result against the original user task, the approved plan, and
  every acceptance criterion. Verification output is supporting evidence, not a
  substitute for code and behavior review.
- Look for regressions outside the edited lines: stale state, broken retries,
  missing error handling, concurrency/order issues, data compatibility, and
  mismatches with project architecture.
- The `reason` field must briefly state what you inspected and why the work is
  accepted or what concrete gap requires revision."""


def planner_initial_prompt(*, user_task: str, cwd: str, worker_model: str) -> str:
    return f"""You are the Planner for c-orch.

You run inside Codex. Do not edit files. Design the plan, acceptance criteria,
and a self-contained Worker prompt only.

{PROJECT_CONTEXT_INSTRUCTIONS}

Task workspace: {cwd}
Worker model: {worker_model}

User task:
{user_task}

Use Simplified Chinese for all human-readable plan content, including summary,
acceptance_criteria, worker_prompt, and risk_notes. Keep JSON keys, status
values, file paths, and commands unchanged.

Return exactly one JSON object with this shape:
{{
  "status": "plan_ready",
  "summary": "Short plan summary",
  "acceptance_criteria": ["Criterion 1"],
  "worker_prompt": "Self-contained Worker instructions",
  "verification_commands": ["command to run"],
  "risk_notes": ["Risk note"]
}}
"""


def worker_prompt(*, planner_worker_prompt: str, acceptance_criteria: Iterable[str]) -> str:
    criteria = "\n".join(f"- {item}" for item in acceptance_criteria)
    return f"""You are a Worker in a c-orch run.

You are not alone in this codebase. Do not revert unrelated changes. Keep edits
inside the assigned task scope and adapt to existing code.

{PROJECT_CONTEXT_INSTRUCTIONS}

Acceptance criteria:
{criteria}

Planner instructions:
{planner_worker_prompt}

When finished, return exactly one JSON object with this shape:
{{
  "status": "work_done",
  "summary": "What changed",
  "changed_files": ["path"],
  "verification": [
    {{"command": "command", "status": "passed|failed|not_run", "summary": "result"}}
  ],
  "blockers": []
}}
"""


def planner_review_prompt(
    *,
    original_plan_json: dict,
    worker_result_json: dict,
    review_workspace: str,
    diff_summary: str,
    diff_path: str,
    test_summary: str,
    test_output_path: Optional[str] = None,
    code_review_summary: Optional[str] = None,
    code_review_output_path: Optional[str] = None,
) -> str:
    test_path_line = f"\nFull test output file: {test_output_path}" if test_output_path else ""
    review_summary = code_review_summary or "Code review was not available."
    review_path_line = (
        f"\nCode review output file: {code_review_output_path}"
        if code_review_output_path
        else ""
    )
    return f"""You are the Planner reviewing a Worker result for c-orch.

{PROJECT_CONTEXT_INSTRUCTIONS}

Original plan JSON:
{json.dumps(original_plan_json, ensure_ascii=False, indent=2)}

Worker result JSON:
{json.dumps(worker_result_json, ensure_ascii=False, indent=2)}

Review target workspace:
{review_workspace}

Git diff summary:
{diff_summary}

Full git diff file: {diff_path}

Verification summary:
{test_summary}{test_path_line}

Code review summary:
{review_summary}{review_path_line}

{PLANNER_REVIEW_INSTRUCTIONS}

Review the Worker result against the task workspace and the evidence above. Do
not judge by reading another checkout of the same repository. Decide whether the
Worker satisfies the acceptance criteria. There are only two business outcomes:
accept the work, or request a concrete Worker revision. Infrastructure errors
are handled by c-orch, not by this JSON contract.

Return exactly one JSON object with this shape:
{{
  "decision": "accepted|revision_requested",
  "reason": "Why",
  "next_worker_prompt": null
}}

If decision is "accepted", next_worker_prompt must be null.
If decision is "revision_requested", next_worker_prompt must be a concrete,
self-contained instruction for the same Worker thread.
"""


def planner_revision_prompt(*, human_feedback: str) -> str:
    return f"""You are the Planner for c-orch.

The human reviewer asked you to revise your previous plan.

{PROJECT_CONTEXT_INSTRUCTIONS}

Human feedback:
{human_feedback}

Use Simplified Chinese for all human-readable plan content, including summary,
acceptance_criteria, worker_prompt, and risk_notes. Keep JSON keys, status
values, file paths, and commands unchanged.

Return exactly one complete JSON plan object with this shape:
{{
  "status": "plan_ready",
  "summary": "Short plan summary",
  "acceptance_criteria": ["Criterion 1"],
  "worker_prompt": "Self-contained Worker instructions",
  "verification_commands": ["command to run"],
  "risk_notes": ["Risk note"]
}}
"""


def planner_review_fallback_prompt(
    *,
    user_task: str,
    original_plan_json: dict,
    acceptance_criteria: Iterable[str],
    worker_result_json: dict,
    review_workspace: str,
    diff_summary: str,
    diff_path: str,
    test_summary: str,
    test_output_path: Optional[str] = None,
    code_review_summary: Optional[str] = None,
    code_review_output_path: Optional[str] = None,
) -> str:
    criteria = "\n".join(f"- {item}" for item in acceptance_criteria)
    test_path_line = f"\nVerification output path: {test_output_path}" if test_output_path else ""
    review_summary = code_review_summary or "Code review was not available."
    review_path_line = (
        f"\nCode review output path: {code_review_output_path}"
        if code_review_output_path
        else ""
    )
    return f"""You are a fallback Planner reviewer for c-orch.

The original Planner thread is not recoverable. You are only reviewing the
saved Worker result. Do not re-plan. Do not edit files.

{PROJECT_CONTEXT_INSTRUCTIONS}

Original user task:
{user_task}

Approved/original plan JSON:
{json.dumps(original_plan_json, ensure_ascii=False, indent=2)}

Acceptance criteria:
{criteria}

Worker result JSON:
{json.dumps(worker_result_json, ensure_ascii=False, indent=2)}

Review target workspace:
{review_workspace}

Git diff summary:
{diff_summary}

Full git diff file: {diff_path}

Verification summary:
{test_summary}{test_path_line}

Code review summary:
{review_summary}{review_path_line}

{PLANNER_REVIEW_INSTRUCTIONS}

Review the Worker result against the task workspace and the evidence above. Do
not judge by reading another checkout of the same repository. Decide whether the
Worker satisfies the acceptance criteria. There are only two business outcomes:
accept the work, or request a concrete Worker revision.
Infrastructure errors are handled by c-orch, not by this JSON contract.

Return exactly one JSON object with this shape:
{{
  "decision": "accepted|revision_requested",
  "reason": "Why",
  "next_worker_prompt": null
}}

If decision is "accepted", next_worker_prompt must be null.
If decision is "revision_requested", next_worker_prompt must be a concrete,
self-contained instruction for the same Worker thread.
"""
