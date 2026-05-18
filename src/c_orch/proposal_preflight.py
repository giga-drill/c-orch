from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import List


@dataclass(frozen=True)
class ProposalPreflightResult:
    passed: bool
    reason: str | None = None
    message: str | None = None
    suggested_action: str | None = None
    suggestions: List[str] = field(default_factory=list)
    issues: List[str] = field(default_factory=list)


_GENERIC_VAGUE_PATTERNS = {
    "fix",
    "fix it",
    "fix this",
    "optimize",
    "optimize it",
    "improve",
    "improve it",
    "refactor",
    "refactor it",
    "do this",
    "do it",
    "处理一下",
    "修一下",
    "改一下",
    "优化一下",
    "弄一下",
}

_SCOPE_TOO_LARGE_PATTERNS = (
    r"\brewrite (the )?(entire|whole) (system|codebase)\b",
    r"\barchitecture (rewrite|redesign|overhaul)\b",
    r"\b(refactor|rewrite) all modules\b",
    r"\boverhaul (the )?(entire|whole) (project|system)\b",
    r"重写整个系统",
    r"重新设计架构",
    r"大范围架构重构",
    r"全部模块改造",
    r"全量重构",
)
_SCOPE_TOO_LARGE_RE = re.compile("|".join(_SCOPE_TOO_LARGE_PATTERNS), re.IGNORECASE)

_VALIDATION_HINT_RE = re.compile(
    r"\b(acceptance|verify|verification|test|tests|assert|expected|should|when|given|then|api|endpoint|file|path|cli)\b",
    re.IGNORECASE,
)
_CODE_ANCHOR_RE = re.compile(r"([/\\][\w.\-]+)|(\b[\w.\-]+\.(py|ts|tsx|js|go|rs|md|json|yaml|yml)\b)", re.IGNORECASE)


def evaluate_proposal_preflight(prompt: str) -> ProposalPreflightResult:
    text = prompt.strip()
    lowered = text.lower()

    if _is_too_vague(text, lowered):
        return ProposalPreflightResult(
            passed=False,
            reason="proposal_too_vague",
            message="提案信息过少，当前描述不足以让 Planner 生成可执行计划。",
            suggested_action="请补充目标改动对象、预期行为和基本验证方式后重试。",
            suggestions=[
                "说明要改动的模块、文件或接口范围。",
                "描述完成后系统应该表现出的可观察结果。",
                "给出至少一个可执行的验证线索（测试、命令或手工检查）。",
            ],
            issues=["prompt_too_vague"],
        )

    if _SCOPE_TOO_LARGE_RE.search(text):
        return ProposalPreflightResult(
            passed=False,
            reason="proposal_scope_too_large",
            message="提案范围过大，超出单次 commit-sized 任务的安全边界。",
            suggested_action="请拆分为一组较小提案，每个提案聚焦一个可独立验证的目标。",
            suggestions=[
                "先提交最小可交付子任务，再追加后续提案。",
                "每个提案限定在一个明确模块或一个行为改动。",
                "为每个子任务提供独立验收标准。",
            ],
            issues=["scope_too_large"],
        )

    if _likely_missing_outcome_or_acceptance(text):
        return ProposalPreflightResult(
            passed=False,
            reason="proposal_missing_outcome",
            message="提案缺少可判断的目标行为或验收线索，Planner 难以稳定落地。",
            suggested_action="请补充“改完后应该怎样”和“如何验证”的描述后重试。",
            suggestions=[
                "补一句完成条件，例如“接口返回 X”或“页面展示 Y”。",
                "补一个验证方式，例如命令、测试文件或手工检查步骤。",
            ],
            issues=["missing_outcome_or_acceptance"],
        )

    return ProposalPreflightResult(passed=True)


def _is_too_vague(text: str, lowered: str) -> bool:
    compact = re.sub(r"\s+", " ", lowered).strip()
    if compact in _GENERIC_VAGUE_PATTERNS:
        return True
    words = re.findall(r"[a-zA-Z]+", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    if len(words) <= 1 and len(cjk_chars) <= 6 and len(text.strip()) <= 12:
        return True
    return False


def _likely_missing_outcome_or_acceptance(text: str) -> bool:
    if len(text) >= 64:
        return False
    if _VALIDATION_HINT_RE.search(text) or _CODE_ANCHOR_RE.search(text):
        return False
    if re.search(r"[0-9]", text):
        return False
    normalized = text.lower()
    generic_start = re.match(
        r"^\s*(add|update|change|improve|optimize|refactor|fix|implement|create|build|make|处理|优化|改进|调整|完善)",
        normalized,
    )
    return bool(generic_start)
