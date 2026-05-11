from __future__ import annotations

import json
import os
import select
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence

from .drivers import DriverError


MCP_PROTOCOL_VERSION = "2024-11-05"


class McpTransport(Protocol):
    def write_message(self, message: Mapping[str, Any]) -> None:
        ...

    def read_message(self, timeout_seconds: float) -> Dict[str, Any]:
        ...

    def close(self) -> None:
        ...


class McpError(DriverError):
    pass


class McpTimeoutError(McpError):
    pass


class McpToolError(McpError):
    pass


@dataclass(frozen=True)
class StdioServerCommand:
    command: str
    args: Sequence[str]
    cwd: Optional[str] = None
    env: Optional[Mapping[str, str]] = None

    def argv(self) -> List[str]:
        return [self.command] + list(self.args)


class StdioMcpTransport:
    """Newline-delimited JSON-RPC transport used by MCP stdio servers."""

    def __init__(
        self,
        server: StdioServerCommand,
        *,
        process_factory: Any = subprocess.Popen,
    ) -> None:
        self.server = server
        env = None
        if server.env is not None:
            env = dict(os.environ)
            env.update(server.env)
        self._process = process_factory(
            server.argv(),
            cwd=server.cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

    def write_message(self, message: Mapping[str, Any]) -> None:
        if self._process.stdin is None:
            raise McpError("MCP server stdin is closed")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        self._process.stdin.write(line + "\n")
        self._process.stdin.flush()

    def read_message(self, timeout_seconds: float) -> Dict[str, Any]:
        if self._process.stdout is None:
            raise McpError("MCP server stdout is closed")
        line = _readline_with_timeout(self._process.stdout, timeout_seconds)
        if line == "":
            raise McpError("MCP server stdout closed")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise McpError(f"MCP server returned invalid JSON: {exc.msg}") from exc
        if not isinstance(message, dict):
            raise McpError("MCP server returned a non-object JSON-RPC message")
        return message

    def close(self) -> None:
        stdin = self._process.stdin
        if stdin is not None and not stdin.closed:
            stdin.close()
        try:
            self._process.wait(timeout=2.0)
            return
        except subprocess.TimeoutExpired:
            self._process.terminate()
        try:
            self._process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=2.0)


class McpClient:
    def __init__(
        self,
        transport: McpTransport,
        *,
        timeout_seconds: float = 600.0,
        client_name: str = "c-orch",
        client_version: str = "0.1.0",
    ) -> None:
        self._transport = transport
        self._timeout_seconds = timeout_seconds
        self._client_name = client_name
        self._client_version = client_version
        self._next_id = 1
        self._started = False
        self._closed = False

    def __enter__(self) -> "McpClient":
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def start(self) -> None:
        if self._started:
            return
        self.request(
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": self._client_name,
                    "version": self._client_version,
                },
            },
            start_allowed=True,
        )
        self.notify("notifications/initialized")
        self._started = True

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        result = self.request(
            "tools/call",
            {
                "name": name,
                "arguments": dict(arguments),
            },
        )
        if not isinstance(result, dict):
            raise McpError("tools/call returned a non-object result")
        if result.get("isError") is True:
            raise McpToolError(_tool_error_text(result))
        return result

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = self.request("tools/list", params)
            if not isinstance(result, dict):
                raise McpError("tools/list returned a non-object result")
            batch = result.get("tools")
            if not isinstance(batch, list):
                raise McpError("tools/list returned no tools array")
            tools.extend(tool for tool in batch if isinstance(tool, dict))
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                return tools
            cursor = next_cursor

    def request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        start_allowed: bool = False,
    ) -> Any:
        if not self._started and not start_allowed:
            self.start()
        request_id = self._allocate_id()
        message: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        self._transport.write_message(message)
        return self._read_response(request_id)

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        message: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            message["params"] = dict(params)
        self._transport.write_message(message)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()

    def _allocate_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _read_response(self, request_id: int) -> Any:
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeoutError(f"MCP request {request_id} timed out")
            message = self._transport.read_message(remaining)
            if _is_server_request(message):
                self._handle_server_request(message)
                continue
            if _is_notification(message):
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise McpError(_json_rpc_error_text(message["error"]))
            return message.get("result")

    def _handle_server_request(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if method == "ping":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {}}
        else:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Unsupported server request: {method}",
                },
            }
        self._transport.write_message(response)


def build_stdio_mcp_client(
    command: str,
    args: Sequence[str],
    *,
    cwd: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout_seconds: float = 600.0,
) -> McpClient:
    transport = StdioMcpTransport(
        StdioServerCommand(command=command, args=tuple(args), cwd=cwd, env=env)
    )
    return McpClient(transport, timeout_seconds=timeout_seconds)


def _is_server_request(message: Mapping[str, Any]) -> bool:
    return "method" in message and "id" in message and "result" not in message and "error" not in message


def _is_notification(message: Mapping[str, Any]) -> bool:
    return "method" in message and "id" not in message


def _json_rpc_error_text(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message")
        code = error.get("code")
        if isinstance(message, str):
            if code is not None:
                return f"MCP JSON-RPC error {code}: {message}"
            return f"MCP JSON-RPC error: {message}"
    return f"MCP JSON-RPC error: {error!r}"


def _tool_error_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if isinstance(content, list):
        parts = [
            item["text"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        if parts:
            return "\n".join(parts)
    return "MCP tool returned isError=true"


def _readline_with_timeout(stdout: Any, timeout_seconds: float) -> str:
    try:
        file_no = stdout.fileno()
    except (AttributeError, OSError):
        return stdout.readline()

    readable, _, _ = select.select([file_no], [], [], timeout_seconds)
    if not readable:
        raise McpTimeoutError("Timed out waiting for MCP server output")
    return stdout.readline()
