from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from c_orch.codex_session_logs import CodexSessionSnapshot
from c_orch.drivers import DriverError, SessionResult
from c_orch.mcp_client import McpTimeoutError, McpToolError
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
            reasoning_effort="high",
            service_tier="fast",
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
                        "config": {
                            "model_reasoning_effort": "high",
                            "service_tier": "fast",
                        },
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

    def test_reply_uses_codex_exec_resume_when_mcp_server_lacks_thread(self) -> None:
        client = FakeMcpClient([])
        client.error_on_call = McpToolError("Session not found for thread_id: thr_planner")
        runner = FakeExecResumeRunner(
            SessionResult(thread_id="thr_planner", content="resumed reply", raw={})
        )
        driver = McpCodexDriver(
            codex_bin="/bin/codex",
            client=client,
            exec_resume_runner=runner,
        )

        result = driver.reply(thread_id="thr_planner", prompt="Continue")

        self.assertEqual(result.thread_id, "thr_planner")
        self.assertEqual(result.content, "resumed reply")
        self.assertEqual(
            client.calls,
            [
                (
                    "codex-reply",
                    {
                        "threadId": "thr_planner",
                        "prompt": "Continue",
                    },
                )
            ],
        )
        self.assertEqual(runner.calls, [("/bin/codex", "thr_planner", "Continue")])

    def test_reply_does_not_resume_for_non_session_not_found_errors(self) -> None:
        client = FakeMcpClient([])
        client.error_on_call = McpToolError("permission denied")
        runner = FakeExecResumeRunner(
            SessionResult(thread_id="thr_planner", content="resumed reply", raw={})
        )
        driver = McpCodexDriver(
            codex_bin="/bin/codex",
            client=client,
            exec_resume_runner=runner,
        )

        with self.assertRaisesRegex(McpToolError, "permission denied"):
            driver.reply(thread_id="thr_planner", prompt="Continue")

        self.assertEqual(runner.calls, [])

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

    def test_start_session_recovers_finished_session_after_mcp_timeout(self) -> None:
        client = FakeMcpClient([])
        client.timeout_on_call = True
        session_logs = FakeSessionLogStore(
            CodexSessionSnapshot(
                thread_id="thr_worker",
                path=Path("/sessions/worker.jsonl"),
                cwd=Path("/repo"),
                source="mcp",
                has_activity_after_started=True,
                final_content="worker done from log",
                final_epoch=1.0,
            )
        )
        driver = McpCodexDriver(
            client=client,
            session_log_store=session_logs,
            session_recovery_poll_seconds=0,
        )

        result = driver.start_session(
            role="worker",
            model="gpt-5.3-codex",
            cwd="/repo",
            prompt="Implement feature",
            sandbox="workspace-write",
            approval_policy="never",
        )

        self.assertEqual(result.thread_id, "thr_worker")
        self.assertEqual(result.content, "worker done from log")
        self.assertEqual(result.raw["recoveredFromSessionLog"], "/sessions/worker.jsonl")
        self.assertFalse(client.closed)
        self.assertEqual(session_logs.calls[0]["cwd"], "/repo")
        self.assertIsNone(session_logs.calls[0]["thread_id"])

    def test_reply_recovers_only_new_finished_session_after_mcp_timeout(self) -> None:
        client = FakeMcpClient([])
        client.timeout_on_call = True
        session_logs = FakeSessionLogStore(
            CodexSessionSnapshot(
                thread_id="thr_planner",
                path=Path("/sessions/planner.jsonl"),
                cwd=Path("/repo"),
                source="mcp",
                has_activity_after_started=True,
                final_content='{"decision":"accepted"}',
                final_epoch=1.0,
            )
        )
        driver = McpCodexDriver(
            client=client,
            session_log_store=session_logs,
            session_recovery_poll_seconds=0,
        )

        result = driver.reply(thread_id="thr_planner", prompt="Review")

        self.assertEqual(result.thread_id, "thr_planner")
        self.assertEqual(result.content, '{"decision":"accepted"}')
        self.assertFalse(client.closed)
        self.assertEqual(session_logs.calls[0]["thread_id"], "thr_planner")
        self.assertIsNone(session_logs.calls[0]["cwd"])

    def test_start_session_recovery_preserves_client_for_planner_reply(self) -> None:
        client = FakeMcpClient(
            [
                {
                    "structuredContent": {
                        "threadId": "thr_planner",
                        "content": '{"decision":"accepted"}',
                    }
                }
            ]
        )
        client.timeout_next_call = True
        session_logs = FakeSessionLogStore(
            CodexSessionSnapshot(
                thread_id="thr_planner",
                path=Path("/sessions/planner.jsonl"),
                cwd=Path("/repo"),
                source="mcp",
                has_activity_after_started=True,
                final_content='{"status":"plan_ready"}',
                final_epoch=1.0,
            )
        )
        driver = McpCodexDriver(
            client=client,
            session_log_store=session_logs,
            session_recovery_poll_seconds=0,
        )

        plan = driver.start_session(
            role="planner",
            model="gpt-5.5",
            cwd="/repo",
            prompt="Plan",
            sandbox="workspace-write",
            approval_policy="never",
        )
        review = driver.reply(thread_id="thr_planner", prompt="Review")

        self.assertEqual(plan.thread_id, "thr_planner")
        self.assertEqual(plan.content, '{"status":"plan_ready"}')
        self.assertEqual(review.content, '{"decision":"accepted"}')
        self.assertFalse(client.closed)
        self.assertEqual([call[0] for call in client.calls], ["codex", "codex-reply"])

    def test_timeout_without_session_activity_still_raises(self) -> None:
        client = FakeMcpClient([])
        client.timeout_on_call = True
        driver = McpCodexDriver(
            client=client,
            session_log_store=FakeSessionLogStore(None),
            session_start_grace_seconds=0,
            session_recovery_poll_seconds=0,
        )

        with self.assertRaises(McpTimeoutError):
            driver.start_session(
                role="worker",
                model="gpt-5.3-codex",
                cwd="/repo",
                prompt="Implement feature",
                sandbox="workspace-write",
                approval_policy="never",
            )

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
        self.timeout_on_call = False
        self.timeout_next_call = False
        self.error_on_call: Optional[Exception] = None

    def start(self) -> None:
        self.started = True

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Dict[str, Any]:
        self.calls.append((name, dict(arguments)))
        if self.timeout_next_call:
            self.timeout_next_call = False
            raise McpTimeoutError("timeout")
        if self.timeout_on_call:
            raise McpTimeoutError("timeout")
        if self.error_on_call is not None:
            raise self.error_on_call
        if not self.results:
            raise AssertionError("unexpected tool call")
        return self.results.pop(0)

    def list_tools(self) -> List[Dict[str, Any]]:
        self.list_tools_calls += 1
        return list(self.tools)

    def close(self) -> None:
        self.closed = True


class FakeSessionLogStore:
    def __init__(self, snapshot: Optional[CodexSessionSnapshot]) -> None:
        self.snapshot = snapshot
        self.calls: List[Dict[str, Any]] = []

    def find_latest_session(
        self,
        *,
        thread_id: Optional[str] = None,
        cwd: Optional[str] = None,
        started_after: Optional[float] = None,
        source: Optional[str] = "mcp",
    ) -> Optional[CodexSessionSnapshot]:
        self.calls.append(
            {
                "thread_id": thread_id,
                "cwd": cwd,
                "started_after": started_after,
                "source": source,
            }
        )
        return self.snapshot


class FakeExecResumeRunner:
    def __init__(self, result: SessionResult) -> None:
        self.result = result
        self.calls: List[tuple[str, str, str]] = []

    def __call__(self, codex_bin: str, thread_id: str, prompt: str) -> SessionResult:
        self.calls.append((codex_bin, thread_id, prompt))
        return self.result


if __name__ == "__main__":
    unittest.main()
