from __future__ import annotations

import unittest
from typing import Any, Dict, List, Mapping

from c_orch.drivers import DriverError
from c_orch.mcp_driver import McpCodexDriver


class McpCodexDriverTests(unittest.TestCase):
    def test_start_session_calls_codex_tool_and_coerces_result(self) -> None:
        client = FakeMcpClient(
            [
                {
                    "structuredContent": {
                        "threadId": "thr_worker",
                        "content": "worker done",
                    }
                }
            ]
        )
        driver = McpCodexDriver(client=client)

        result = driver.start_session(
            role="worker",
            model="gpt-5.3-codex",
            cwd="/repo",
            prompt="Implement feature",
            sandbox="workspace-write",
            approval_policy="never",
        )

        self.assertEqual(result.thread_id, "thr_worker")
        self.assertEqual(result.content, "worker done")
        self.assertEqual(
            client.calls,
            [
                (
                    "codex",
                    {
                        "prompt": "Implement feature",
                        "model": "gpt-5.3-codex",
                        "cwd": "/repo",
                        "sandbox": "workspace-write",
                        "approval-policy": "never",
                    },
                )
            ],
        )
        self.assertEqual(client.list_tools_calls, 1)

    def test_reply_calls_codex_reply_tool_and_coerces_result(self) -> None:
        client = FakeMcpClient(
            [
                {
                    "structuredContent": {
                        "threadId": "thr_worker",
                        "content": "reply done",
                    }
                }
            ]
        )
        driver = McpCodexDriver(client=client)

        result = driver.reply(thread_id="thr_worker", prompt="Continue")

        self.assertEqual(result.thread_id, "thr_worker")
        self.assertEqual(result.content, "reply done")
        self.assertEqual(
            client.calls,
            [
                (
                    "codex-reply",
                    {
                        "threadId": "thr_worker",
                        "prompt": "Continue",
                    },
                )
            ],
        )

    def test_missing_codex_tool_raises_before_calling_tool(self) -> None:
        client = FakeMcpClient([])
        client.tools = [{"name": "codex"}]
        driver = McpCodexDriver(client=client)

        with self.assertRaisesRegex(DriverError, "codex-reply"):
            driver.start_session(
                role="worker",
                model="gpt-5.3-codex",
                cwd="/repo",
                prompt="Implement feature",
                sandbox="workspace-write",
                approval_policy="never",
            )

        self.assertEqual(client.calls, [])

    def test_close_closes_client(self) -> None:
        client = FakeMcpClient([])
        driver = McpCodexDriver(client=client)

        driver.close()

        self.assertTrue(client.closed)


class FakeMcpClient:
    def __init__(self, results: List[Dict[str, Any]]) -> None:
        self.results = list(results)
        self.calls: List[tuple[str, Dict[str, Any]]] = []
        self.tools = [{"name": "codex"}, {"name": "codex-reply"}]
        self.list_tools_calls = 0
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if not self.results:
            raise AssertionError("unexpected tool call")
        return self.results.pop(0)

    def list_tools(self) -> List[Dict[str, Any]]:
        self.list_tools_calls += 1
        return list(self.tools)

    def close(self) -> None:
        self.closed = True


if __name__ == "__main__":
    unittest.main()
