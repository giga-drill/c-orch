from __future__ import annotations

from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from c_orch.runtime import COrchRuntime
from c_orch.scheduler import SchedulerConfig
from c_orch.task_store import TASK_WAITING, TaskStore


class _FakeOrchestrator:
    def __init__(self, run_store, status: str) -> None:  # type: ignore[no-untyped-def]
        self.run_store = run_store
        self.status = status
        self.run_ids: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        manifest.status = self.status
        self.run_store.save(manifest)
        self.run_ids.append(manifest.run_id)
        return manifest


class RuntimeTests(unittest.TestCase):
    def test_runtime_reuses_cached_driver_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            driver = driver_cls.return_value
            driver.__enter__.return_value = driver
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with runtime._driver_context("/bin/codex") as first:
                self.assertIs(first, driver)
            with runtime._driver_context("/bin/codex") as second:
                self.assertIs(second, driver)

            driver_cls.assert_called_once_with(codex_bin="/bin/codex")
            driver.close.assert_not_called()

            runtime.close()

            driver.close.assert_called_once()

    def test_runtime_drops_cached_driver_after_action_exception(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "c_orch.mcp_driver.McpCodexDriver"
        ) as driver_cls:
            first_driver = mock.Mock()
            first_driver.__enter__ = mock.Mock(return_value=first_driver)
            first_driver.__exit__ = mock.Mock(return_value=None)
            second_driver = mock.Mock()
            second_driver.__enter__ = mock.Mock(return_value=second_driver)
            second_driver.__exit__ = mock.Mock(return_value=None)
            driver_cls.side_effect = [first_driver, second_driver]
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")

            with self.assertRaises(RuntimeError):
                with runtime._driver_context("/bin/codex"):
                    raise RuntimeError("driver pipe broke")

            first_driver.close.assert_called_once()
            with runtime._driver_context("/bin/codex") as recovered:
                self.assertIs(recovered, second_driver)

            self.assertEqual(driver_cls.call_count, 2)

    def test_retry_task_starts_background_queue_dispatch(self) -> None:
        from c_orch.run_store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            task_store = TaskStore(root / "queue.json")
            queue = task_store.import_tasks(
                [{"task_id": "task-001", "title": "Task 1", "prompt": "Do task 1"}]
            )
            task_store.update_task(queue, "task-001", status="FAILED", reason="active_run_failed")
            task_store.save(queue)
            run_store = RunStore(root / "runs")
            fake = _FakeOrchestrator(run_store, "PLAN_REVIEW_REQUIRED")
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                scheduler_config=_scheduler_config(root),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            status, _payload = runtime.task_action("task-001", "retry-task")

            self.assertEqual(int(status), 200)
            self.assertTrue(runtime.wait_for_dispatch(timeout=2))
            loaded = task_store.load()
            self.assertEqual(loaded.status, "RUNNING")
            self.assertEqual(loaded.tasks[0].status, TASK_WAITING)
            self.assertEqual(loaded.tasks[0].reason, "human_plan_review")
            self.assertEqual(loaded.tasks[0].run_ids, fake.run_ids)
            self.assertEqual(len(fake.run_ids), 1)


def _scheduler_config(root: Path) -> SchedulerConfig:
    return SchedulerConfig(
        cwd=root / "repo",
        runs_dir=root / "runs",
        worktrees_dir=root / "worktrees",
        planner_model="gpt-5.5",
        worker_model="gpt-5.3-codex-spark",
        codex_binary_path="/bin/codex",
        max_attempts=2,
        sandbox="workspace-write",
        approval_policy="never",
    )


@contextmanager
def _fake_driver_factory(_codex_path: str):  # type: ignore[no-untyped-def]
    yield object()


def _fake_worktree_factory(*, repo_path: Path, worktrees_dir: Path, run_id: str, worker_id: str) -> Path:
    _ = repo_path
    worktree = worktrees_dir / run_id / worker_id
    worktree.mkdir(parents=True, exist_ok=True)
    return worktree


if __name__ == "__main__":
    unittest.main()
