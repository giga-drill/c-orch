from __future__ import annotations

import unittest
from io import StringIO
from typing import Any, Dict, List, Mapping

from c_orch.mcp_client import (
    McpClient,
    McpError,
    McpToolError,
    StdioMcpTransport,
    StdioServerCommand,
)


class McpClientTests(unittest.TestCase):
    def test_start_sends_initialize_and_initialized_notification(self) -> None:
        transport = FakeTransport(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                    },
                }
            ]
        )

        McpClient(transport).start()

        self.assertEqual(transport.outgoing[0]["method"], "initialize")
        self.assertEqual(
            transport.outgoing[0]["params"]["protocolVersion"],
            "2024-11-05",
        )
        self.assertEqual(
            transport.outgoing[0]["params"]["clientInfo"]["name"],
            "c-orch",
        )
        self.assertEqual(
            transport.outgoing[1],
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    def test_call_tool_sends_tools_call_request(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {"tools": {}}}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "structuredContent": {
                            "threadId": "thr_1",
                            "content": "done",
                        }
                    },
                },
            ]
        )
        client = McpClient(transport)

        result = client.call_tool("codex", {"prompt": "hello"})

        self.assertEqual(
            transport.outgoing[2],
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "codex",
                    "arguments": {"prompt": "hello"},
                },
            },
        )
        self.assertEqual(result["structuredContent"]["threadId"], "thr_1")

    def test_list_tools_sends_tools_list_request(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "tools": [
                            {"name": "codex"},
                            {"name": "codex-reply"},
                        ]
                    },
                },
            ]
        )
        client = McpClient(transport)

        tools = client.list_tools()

        self.assertEqual([tool["name"] for tool in tools], ["codex", "codex-reply"])
        self.assertEqual(
            transport.outgoing[2],
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            },
        )

    def test_list_tools_follows_next_cursor(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {"tools": [{"name": "codex"}], "nextCursor": "page-2"},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "result": {"tools": [{"name": "codex-reply"}]},
                },
            ]
        )
        client = McpClient(transport)

        tools = client.list_tools()

        self.assertEqual([tool["name"] for tool in tools], ["codex", "codex-reply"])
        self.assertEqual(transport.outgoing[3]["params"], {"cursor": "page-2"})

    def test_json_rpc_error_raises(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "error": {"code": -32602, "message": "bad params"},
                },
            ]
        )
        client = McpClient(transport)

        with self.assertRaisesRegex(McpError, "bad params"):
            client.call_tool("codex", {"prompt": "hello"})

    def test_tool_is_error_raises_with_text_content(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "result": {
                        "isError": True,
                        "content": [{"type": "text", "text": "tool failed"}],
                    },
                },
            ]
        )
        client = McpClient(transport)

        with self.assertRaisesRegex(McpToolError, "tool failed"):
            client.call_tool("codex", {"prompt": "hello"})

    def test_server_ping_request_gets_response_before_tool_result(self) -> None:
        transport = FakeTransport(
            [
                {"jsonrpc": "2.0", "id": 1, "result": {}},
                {"jsonrpc": "2.0", "id": "server-1", "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/progress"},
                {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}},
            ]
        )
        client = McpClient(transport)

        result = client.call_tool("codex", {"prompt": "hello"})

        self.assertEqual(result, {"ok": True})
        self.assertIn(
            {"jsonrpc": "2.0", "id": "server-1", "result": {}},
            transport.outgoing,
        )

    def test_stdio_transport_writes_and_reads_ndjson_messages(self) -> None:
        processes: List[FakeProcess] = []

        def process_factory(argv: List[str], **kwargs: Any) -> "FakeProcess":
            process = FakeProcess(
                '{"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
            )
            process.argv = argv
            process.kwargs = kwargs
            processes.append(process)
            return process

        transport = StdioMcpTransport(
            StdioServerCommand(command="/codex", args=("mcp-server",)),
            process_factory=process_factory,
        )

        transport.write_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"prompt": "line\nbreak"},
            }
        )
        message = transport.read_message(timeout_seconds=1.0)

        self.assertEqual(processes[0].argv, ["/codex", "mcp-server"])
        self.assertEqual(processes[0].stdin.getvalue().count("\n"), 1)
        self.assertIn('"line\\nbreak"', processes[0].stdin.getvalue())
        self.assertEqual(message["result"], {"ok": True})


class FakeTransport:
    def __init__(self, incoming: List[Dict[str, Any]]) -> None:
        self.incoming = list(incoming)
        self.outgoing: List[Dict[str, Any]] = []
        self.closed = False

    def write_message(self, message: Mapping[str, Any]) -> None:
        self.outgoing.append(dict(message))

    def read_message(self, timeout_seconds: float) -> Dict[str, Any]:
        del timeout_seconds
        if not self.incoming:
            raise AssertionError("unexpected read")
        return self.incoming.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, stdout_text: str) -> None:
        self.stdin = StringIO()
        self.stdout = StringIO(stdout_text)
        self.argv: List[str] = []
        self.kwargs: Dict[str, Any] = {}
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float = 0) -> int:
        del timeout
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


if __name__ == "__main__":
    unittest.main()
