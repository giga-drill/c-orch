from __future__ import annotations

import json
from typing import Iterable, Optional


def planner_initial_prompt(*, user_task: str, cwd: str, worker_model: str) -> str:
    return f"""You are the Planner for c-orch.

You run inside Codex. Do not edit files. Design the plan, acceptance criteria,
and a self-contained Worker prompt only.

Workspace: {cwd}
Worker model: {worker_model}

User task:
{user_task}

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
    diff_summary: str,
    diff_path: str,
    test_summary: str,
    test_output_path: Optional[str] = None,
) -> str:
    test_path_line = f"\nFull test output file: {test_output_path}" if test_output_path else ""
    return f"""You are the Planner reviewing a Worker result for c-orch.

Original plan JSON:
{json.dumps(original_plan_json, ensure_ascii=False, indent=2)}

Worker result JSON:
{json.dumps(worker_result_json, ensure_ascii=False, indent=2)}

Git diff summary:
{diff_summary}

Full git diff file: {diff_path}

Verification summary:
{test_summary}{test_path_line}

Decide whether the Worker satisfies the acceptance criteria. Return exactly one
JSON object with this shape:
{{
  "decision": "approved|needs_changes|blocked|failed",
  "reason": "Why",
  "next_worker_prompt": null
}}

If decision is "needs_changes", next_worker_prompt must be a concrete,
self-contained instruction for the same Worker thread.
"""

