from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from c_orch.codex_session_logs import CodexSessionLogStore


class CodexSessionLogStoreTests(unittest.TestCase):
    def test_finds_finished_mcp_session_by_cwd_after_start_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            sessions_root = root / "sessions"
            session_path = (
                sessions_root
                / "2026"
                / "05"
                / "11"
                / "rollout-2026-05-11T18-15-00-thread-worker.jsonl"
            )
            _write_jsonl(
                session_path,
                [
                    _session_meta(
                        timestamp="2026-05-11T10:15:14.660Z",
                        thread_id="thread-worker",
                        cwd=str(repo),
                    ),
                    _task_complete(
                        timestamp="2026-05-11T10:21:12.785Z",
                        message='{"status":"work_done"}',
                    ),
                ],
            )

            snapshot = CodexSessionLogStore(sessions_root).find_latest_session(
                cwd=repo,
                started_after=_epoch("2026-05-11T10:15:13.000Z"),
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.thread_id, "thread-worker")
            self.assertEqual(snapshot.final_content, '{"status":"work_done"}')
            self.assertTrue(snapshot.has_activity_after_started)

    def test_ignores_old_final_answer_for_existing_thread_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_root = root / "sessions"
            session_path = (
                sessions_root
                / "2026"
                / "05"
                / "11"
                / "rollout-2026-05-11T18-11-44-thread-planner.jsonl"
            )
            _write_jsonl(
                session_path,
                [
                    _session_meta(
                        timestamp="2026-05-11T10:11:44.000Z",
                        thread_id="thread-planner",
                        cwd=str(root / "repo"),
                    ),
                    _task_complete(
                        timestamp="2026-05-11T10:14:59.000Z",
                        message='{"status":"plan_ready"}',
                    ),
                    {
                        "timestamp": "2026-05-11T10:21:30.000Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "review this"}],
                        },
                    },
                ],
            )

            snapshot = CodexSessionLogStore(sessions_root).find_latest_session(
                thread_id="thread-planner",
                started_after=_epoch("2026-05-11T10:21:00.000Z"),
            )

            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.has_activity_after_started)
            self.assertIsNone(snapshot.final_content)

    def test_finds_new_final_answer_for_existing_thread_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_root = root / "sessions"
            session_path = (
                sessions_root
                / "2026"
                / "05"
                / "11"
                / "rollout-2026-05-11T18-11-44-thread-planner.jsonl"
            )
            _write_jsonl(
                session_path,
                [
                    _session_meta(
                        timestamp="2026-05-11T10:11:44.000Z",
                        thread_id="thread-planner",
                        cwd=str(root / "repo"),
                    ),
                    _task_complete(
                        timestamp="2026-05-11T10:14:59.000Z",
                        message='{"status":"plan_ready"}',
                    ),
                    _task_complete(
                        timestamp="2026-05-11T10:22:00.000Z",
                        message='{"decision":"accepted"}',
                    ),
                ],
            )

            snapshot = CodexSessionLogStore(sessions_root).find_latest_session(
                thread_id="thread-planner",
                started_after=_epoch("2026-05-11T10:21:00.000Z"),
            )

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.final_content, '{"decision":"accepted"}')

    def test_load_recent_activity_summarizes_worker_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sessions_root = root / "sessions"
            session_path = (
                sessions_root
                / "2026"
                / "05"
                / "12"
                / "rollout-2026-05-12T10-00-00-thread-worker.jsonl"
            )
            _write_jsonl(
                session_path,
                [
                    _session_meta(
                        timestamp="2026-05-12T10:00:00Z",
                        thread_id="thread-worker",
                        cwd="/repo",
                    ),
                    {
                        "timestamp": "2026-05-12T10:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "reading files",
                        },
                    },
                    {
                        "timestamp": "2026-05-12T10:00:02Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                        },
                    },
                    {
                        "timestamp": "2026-05-12T10:00:03Z",
                        "type": "event_msg",
                        "payload": {"type": "turn_aborted"},
                    },
                ],
            )

            activity = CodexSessionLogStore(sessions_root).load_recent_activity(
                thread_id="thread-worker",
                limit=10,
            )

            self.assertEqual([item.kind for item in activity], ["message", "tool_call", "aborted"])
            self.assertEqual(activity[0].detail, "reading files")
            self.assertEqual(activity[1].label, "调用工具: exec_command")
            self.assertEqual(activity[2].label, "回合中断")


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _session_meta(*, timestamp: str, thread_id: str, cwd: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "cwd": cwd,
            "source": "mcp",
        },
    }


def _task_complete(*, timestamp: str, message: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "last_agent_message": message,
        },
    }


def _epoch(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


if __name__ == "__main__":
    unittest.main()
