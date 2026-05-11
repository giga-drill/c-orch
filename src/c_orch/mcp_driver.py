from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from .codex_session_logs import CodexSessionLogStore, CodexSessionSnapshot
from .codex_discovery import inspect_codex_environment
from .drivers import DriverError, SessionResult, coerce_session_result
from .mcp_client import McpClient, McpTimeoutError, build_stdio_mcp_client


class SessionLogStore(Protocol):
    def find_latest_session(
        self,
        *,
        thread_id: Optional[str] = None,
        cwd: Optional[str] = None,
        started_after: Optional[float] = None,
        source: Optional[str] = "mcp",
    ) -> Optional[CodexSessionSnapshot]:
        ...


class McpCodexDriver:
    def __init__(
        self,
        *,
        codex_bin: Optional[str] = None,
        client: Optional[McpClient] = None,
        timeout_seconds: float = 60.0,
        session_recovery_timeout_seconds: float = 3600.0,
        session_recovery_poll_seconds: float = 2.0,
        session_start_grace_seconds: float = 10.0,
        sessions_root: Optional[str] = None,
        session_log_store: Optional[SessionLogStore] = None,
    ) -> None:
        self.codex_bin = codex_bin or _resolve_default_codex_bin(client)
        self._client = client
        self._timeout_seconds = timeout_seconds
        self._session_recovery_timeout_seconds = session_recovery_timeout_seconds
        self._session_recovery_poll_seconds = session_recovery_poll_seconds
        self._session_start_grace_seconds = session_start_grace_seconds
        self._session_log_store = session_log_store or CodexSessionLogStore(sessions_root)
        self._tools_checked = False

    def __enter__(self) -> "McpCodexDriver":
        self._ensure_required_tools()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def start_session(
        self,
        *,
        role: str,
        model: str,
        cwd: str,
        prompt: str,
        sandbox: str,
        approval_policy: str,
    ) -> SessionResult:
        del role
        self._ensure_required_tools()
        started_at = time.time()
        return self._call_tool_with_session_recovery(
            tool_name="codex",
            arguments=_codex_arguments(
                model=model,
                cwd=cwd,
                prompt=prompt,
                sandbox=sandbox,
                approval_policy=approval_policy,
            ),
            thread_id=None,
            cwd=cwd,
            started_at=started_at,
        )

    def reply(self, *, thread_id: str, prompt: str) -> SessionResult:
        self._ensure_required_tools()
        started_at = time.time()
        return self._call_tool_with_session_recovery(
            tool_name="codex-reply",
            arguments={
                "threadId": thread_id,
                "prompt": prompt,
            },
            thread_id=thread_id,
            cwd=None,
            started_at=started_at,
        )

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._tools_checked = False

    def _get_client(self) -> McpClient:
        if self._client is None:
            self._client = build_stdio_mcp_client(
                self.codex_bin,
                ("mcp-server",),
                timeout_seconds=self._timeout_seconds,
            )
        return self._client

    def _call_tool_with_session_recovery(
        self,
        *,
        tool_name: str,
        arguments: Mapping[str, Any],
        thread_id: Optional[str],
        cwd: Optional[str],
        started_at: float,
    ) -> SessionResult:
        try:
            payload = self._get_client().call_tool(tool_name, arguments)
            return coerce_session_result(payload)
        except McpTimeoutError as exc:
            recovered = self._wait_for_recovered_session(
                thread_id=thread_id,
                cwd=cwd,
                started_at=started_at,
            )
            if recovered is None:
                raise exc
            # Keep the MCP server process alive: Codex MCP resolves codex-reply
            # thread ids against state held by that process.
            return _session_result_from_snapshot(recovered)

    def _wait_for_recovered_session(
        self,
        *,
        thread_id: Optional[str],
        cwd: Optional[str],
        started_at: float,
    ) -> Optional[CodexSessionSnapshot]:
        deadline = time.monotonic() + self._session_recovery_timeout_seconds
        start_grace_deadline = time.monotonic() + self._session_start_grace_seconds
        saw_activity = False
        while True:
            snapshot = self._session_log_store.find_latest_session(
                thread_id=thread_id,
                cwd=cwd,
                started_after=started_at,
                source="mcp",
            )
            if snapshot is not None:
                saw_activity = saw_activity or snapshot.has_activity_after_started
                if snapshot.final_content is not None:
                    return snapshot
            now = time.monotonic()
            if not saw_activity and now >= start_grace_deadline:
                return None
            if now >= deadline:
                return None
            time.sleep(min(self._session_recovery_poll_seconds, deadline - now))

    def _ensure_required_tools(self) -> None:
        if self._tools_checked:
            return
        tools = self._get_client().list_tools()
        tool_names = {tool.get("name") for tool in tools}
        missing = sorted({"codex", "codex-reply"} - tool_names)
        if missing:
            raise DriverError(
                "Codex MCP server is missing required tools: " + ", ".join(missing)
            )
        self._tools_checked = True


def _resolve_default_codex_bin(client: Optional[McpClient]) -> str:
    if client is not None:
        return "<injected-mcp-client>"
    report = inspect_codex_environment()
    if report.selected is None:
        raise RuntimeError("No usable Codex binary found. Run `c-orch doctor` for details.")
    return report.selected.path


def _session_result_from_snapshot(snapshot: CodexSessionSnapshot) -> SessionResult:
    content = snapshot.final_content
    if content is None:
        raise DriverError("Recovered Codex session did not include final content")
    raw = {
        "structuredContent": {
            "threadId": snapshot.thread_id,
            "content": content,
        },
        "recoveredFromSessionLog": str(Path(snapshot.path)),
    }
    return coerce_session_result(raw)


def _codex_arguments(
    *,
    model: str,
    cwd: str,
    prompt: str,
    sandbox: str,
    approval_policy: str,
) -> Mapping[str, Any]:
    return {
        "prompt": prompt,
        "model": model,
        "cwd": cwd,
        "sandbox": sandbox,
        "approval-policy": approval_policy,
    }
