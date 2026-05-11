from __future__ import annotations

from typing import Any, Mapping, Optional

from .codex_discovery import inspect_codex_environment
from .drivers import DriverError, SessionResult, coerce_session_result
from .mcp_client import McpClient, build_stdio_mcp_client


class McpCodexDriver:
    def __init__(
        self,
        *,
        codex_bin: Optional[str] = None,
        client: Optional[McpClient] = None,
        timeout_seconds: float = 600.0,
    ) -> None:
        self.codex_bin = codex_bin or _resolve_default_codex_bin(client)
        self._client = client
        self._timeout_seconds = timeout_seconds
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
        payload = self._get_client().call_tool(
            "codex",
            _codex_arguments(
                model=model,
                cwd=cwd,
                prompt=prompt,
                sandbox=sandbox,
                approval_policy=approval_policy,
            ),
        )
        return coerce_session_result(payload)

    def reply(self, *, thread_id: str, prompt: str) -> SessionResult:
        self._ensure_required_tools()
        payload = self._get_client().call_tool(
            "codex-reply",
            {
                "threadId": thread_id,
                "prompt": prompt,
            },
        )
        return coerce_session_result(payload)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def _get_client(self) -> McpClient:
        if self._client is None:
            self._client = build_stdio_mcp_client(
                self.codex_bin,
                ("mcp-server",),
                timeout_seconds=self._timeout_seconds,
            )
        return self._client

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
