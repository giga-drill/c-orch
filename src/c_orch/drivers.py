from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol


@dataclass(frozen=True)
class SessionResult:
    thread_id: str
    content: str
    raw: Dict[str, Any]


class CodexDriver(Protocol):
    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
        reasoning_effort: Optional[str] = None,
        service_tier: Optional[str] = None,
    ) -> SessionResult:
        ...

    def reply(self, *, thread_id: str, prompt: str) -> SessionResult:
        ...


class DriverError(RuntimeError):
    pass


def coerce_session_result(payload: Dict[str, Any]) -> SessionResult:
    """Normalize Codex MCP structuredContent into the internal result shape."""
    structured = payload.get("structuredContent") if isinstance(payload, dict) else None
    if not isinstance(structured, dict):
        structured = payload
    thread_id = structured.get("threadId")
    content = structured.get("content")
    if not isinstance(thread_id, str) or not thread_id:
        raise DriverError("Codex response did not include structuredContent.threadId")
    if not isinstance(content, str):
        content = _content_text(payload)
    if not isinstance(content, str):
        raise DriverError("Codex response did not include text content")
    return SessionResult(thread_id=thread_id, content=content, raw=payload)


def _content_text(payload: Dict[str, Any]) -> Optional[str]:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    return None
