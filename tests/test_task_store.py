from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from c_orch.task_store import (
    TASK_APPROVED,
    TASK_PENDING,
    TaskStore,
)


class TaskStoreTests(unittest.TestCase):
    def test_import_tasks_and_next_pending(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "queue.json"
            store = TaskStore(queue_path)
            queue = store.import_tasks(
                [
                    {"id": "task-001", "title": "First", "prompt": "Do first"},
                    {
                        "task_id": "task-002",
                        "title": "Second",
                        "prompt": "Do second",
                        "cwd": "/tmp/repo-two",
                    },
                ]
            )

            self.assertEqual(queue.status, "PENDING")
            self.assertEqual([task.task_id for task in queue.tasks], ["task-001", "task-002"])
            self.assertIsNone(queue.tasks[0].cwd)
            self.assertEqual(queue.tasks[1].cwd, "/tmp/repo-two")
            self.assertEqual(store.next_pending(queue).task_id, "task-001")

            store.update_task(queue, "task-001", status=TASK_APPROVED)
            self.assertEqual(store.next_pending(queue).task_id, "task-002")

    def test_import_rejects_duplicate_task_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = TaskStore(Path(tmp) / "queue.json")
            with self.assertRaisesRegex(ValueError, "duplicate task_id"):
                store.import_tasks(
                    [
                        {"id": "task-001", "title": "First", "prompt": "Do first"},
                        {"task_id": "task-001", "title": "Second", "prompt": "Do second"},
                    ]
                )

    def test_load_legacy_defaults_active_run_and_run_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "queue.json"
            queue_path.parent.mkdir(parents=True, exist_ok=True)
            queue_path.write_text(
                json.dumps(
                    {
                        "queue_id": "default",
                        "tasks": [
                            {
                                "task_id": "task-001",
                                "title": "Legacy task",
                                "prompt": "Do the thing",
                                "status": TASK_PENDING,
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            queue = TaskStore(queue_path).load()
            self.assertEqual(queue.status, "PENDING")
            self.assertIsNone(queue.tasks[0].active_run_id)
            self.assertEqual(queue.tasks[0].run_ids, [])
            self.assertIsNone(queue.tasks[0].cwd)

    def test_save_and_load_persist_run_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            queue_path = Path(tmp) / "queue.json"
            store = TaskStore(queue_path)
            queue = store.import_tasks(
                [
                    {"task_id": "task-001", "title": "First", "prompt": "Do first"},
                ]
            )
            store.update_task(
                queue,
                "task-001",
                status=TASK_APPROVED,
                cwd="/tmp/repo",
                active_run_id="run-001",
                run_ids=["run-001", "run-002"],
            )
            store.save(queue)

            loaded = store.load()
            self.assertEqual(loaded.tasks[0].status, TASK_APPROVED)
            self.assertEqual(loaded.tasks[0].cwd, "/tmp/repo")
            self.assertEqual(loaded.tasks[0].active_run_id, "run-001")
            self.assertEqual(loaded.tasks[0].run_ids, ["run-001", "run-002"])


if __name__ == "__main__":
    unittest.main()
