from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from c_orch.cli import main
from c_orch.codex_discovery import CodexCandidateReport, CodexEnvironmentReport
from c_orch.task_store import TaskStore


class CliQueueTests(unittest.TestCase):
    def test_queue_import_and_status_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            tasks_path = cwd / "tasks.json"
            tasks_path.write_text(
                json.dumps(
                    [
                        {"id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                        {"task_id": "task-002", "title": "Task 2", "prompt": "Do task 2"},
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as import_stdout:
                exit_code = main(
                    [
                        "queue",
                        "import",
                        "tasks.json",
                        "--cwd",
                        str(cwd),
                    ]
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("tasks: 2", import_stdout.getvalue())

            with redirect_stdout(StringIO()) as status_stdout:
                exit_code = main(
                    [
                        "queue",
                        "status",
                        "--cwd",
                        str(cwd),
                        "--json",
                    ]
                )
            self.assertEqual(exit_code, 0)
            payload = json.loads(status_stdout.getvalue())
            self.assertEqual(payload["queue"]["status"], "PENDING")
            self.assertEqual(len(payload["queue"]["tasks"]), 2)

    def test_queue_run_smoke_with_mock_scheduler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cwd = root / "repo"
            cwd.mkdir()
            queue_path = cwd / ".c-orch" / "tasks" / "queue.json"
            task_store = TaskStore(queue_path)
            task_store.import_tasks(
                [
                    {"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"},
                ]
            )
            report = CodexEnvironmentReport(
                candidates=(),
                selected=CodexCandidateReport(
                    path="/Applications/Codex.app/Contents/Resources/codex",
                    source="macos_app",
                    version="codex-cli test",
                    interesting_models=("gpt-5.5", "gpt-5.3-codex-spark"),
                    usable=True,
                ),
            )

            driver = _FakeDriver()
            queue_after_run = task_store.load()
            queue_after_run.status = "APPROVED"
            queue_after_run.tasks[0].status = "APPROVED"

            with mock.patch(
                "c_orch.codex_discovery.inspect_codex_environment",
                return_value=report,
            ), mock.patch(
                "c_orch.mcp_driver.McpCodexDriver",
                return_value=driver,
            ), mock.patch(
                "c_orch.scheduler.TaskScheduler",
            ) as scheduler_cls, redirect_stdout(StringIO()) as stdout:
                scheduler = scheduler_cls.return_value
                scheduler.run.return_value = queue_after_run
                exit_code = main(["queue", "run", "--cwd", str(cwd)])

            self.assertEqual(exit_code, 0)
            self.assertTrue(driver.entered)
            self.assertTrue(driver.exited)
            self.assertEqual(scheduler_cls.call_count, 1)
            self.assertIn("status: APPROVED", stdout.getvalue())

    def test_queue_status_missing_file_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "repo"
            cwd.mkdir()
            with redirect_stderr(StringIO()) as stderr:
                exit_code = main(["queue", "status", "--cwd", str(cwd)])
            self.assertEqual(exit_code, 1)
            self.assertIn("queue file not found", stderr.getvalue())


class _FakeDriver:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    def __enter__(self) -> "_FakeDriver":
        self.entered = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.exited = True


if __name__ == "__main__":
    unittest.main()
