from __future__ import annotations

from contextlib import contextmanager
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest import mock

from c_orch.proposal_store import PROPOSAL_QUEUED, ProposalPool, ProposalRecord, ProposalStore
from c_orch.run_store import PlanRecord
from c_orch.runtime import COrchRuntime, prune_queued_proposals
from c_orch.scheduler import SchedulerConfig
from c_orch.states import RUN_PLAN_APPROVED, RUN_PLAN_REVIEW_REQUIRED
from c_orch.task_store import TaskStore


class _FakeOrchestrator:
    def __init__(self, run_store, status: str) -> None:  # type: ignore[no-untyped-def]
        self.run_store = run_store
        self.status = status
        self.run_ids: List[str] = []
        self.run_calls = 0
        self.input_statuses: List[str] = []

    def run(self, manifest):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        self.input_statuses.append(manifest.status)
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
            fake = _FakeOrchestrator(run_store, "APPROVED")
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
            self.assertEqual(loaded.status, "APPROVED")
            self.assertEqual(loaded.tasks[0].status, "APPROVED")
            self.assertIsNone(loaded.tasks[0].reason)
            self.assertEqual(loaded.tasks[0].run_ids, fake.run_ids)
            self.assertEqual(len(fake.run_ids), 1)

    def test_proposal_approve_dispatches_existing_approved_run_to_worker(self) -> None:
        from c_orch.run_store import RunStore

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            run_store = RunStore(root / "runs")
            manifest = run_store.create_run(
                cwd=repo,
                user_task="Do task 1",
                planner_model="gpt-5.5",
                worker_model="gpt-5.3-codex-spark",
                codex_binary_path="/bin/codex",
            )
            manifest.status = RUN_PLAN_REVIEW_REQUIRED
            manifest.plan = PlanRecord(summary="Plan summary", worker_prompt="Do the work")
            run_store.save(manifest)

            proposal_store = ProposalStore(root / "proposals.json")
            pool = proposal_store.create()
            proposal = proposal_store.add_proposal(pool, title="Task 1", prompt="Do task 1")
            proposal_store.update_proposal(
                pool,
                proposal.proposal_id,
                run_id=manifest.run_id,
                status=RUN_PLAN_REVIEW_REQUIRED,
            )
            proposal_store.save(pool)

            fake = _FakeOrchestrator(run_store, "APPROVED")
            runtime = COrchRuntime(
                runs_dir=root / "runs",
                queue_path=root / "queue.json",
                proposals_path=root / "proposals.json",
                scheduler_config=_scheduler_config(root),
                driver_factory=_fake_driver_factory,
                worktree_factory=_fake_worktree_factory,
                orchestrator_factory=lambda: fake,
            )

            status, payload = runtime.proposal_action(proposal.proposal_id, "approve-plan")

            self.assertEqual(int(status), 200)
            self.assertEqual(payload["transition"]["type"], "proposal_approved_and_queued")
            self.assertEqual(payload["transition"]["run_id"], manifest.run_id)
            self.assertEqual(payload["transition"]["task_id"], proposal.proposal_id)
            self.assertIn("state", payload)
            self.assertEqual(payload["state"]["selected_run"]["run"]["run_id"], manifest.run_id)
            self.assertGreater(payload["state_version"], 0)
            self.assertTrue(runtime.wait_for_dispatch(timeout=2))

            loaded_manifest = run_store.load(manifest.run_id)
            self.assertEqual(loaded_manifest.status, "APPROVED")
            self.assertEqual(fake.input_statuses, [RUN_PLAN_APPROVED])
            self.assertEqual(fake.run_calls, 1)
            self.assertEqual(fake.run_ids, [manifest.run_id])

            loaded_pool = proposal_store.load()
            self.assertEqual(loaded_pool.proposals, [])

            queue = TaskStore(root / "queue.json").load()
            self.assertEqual(queue.status, "APPROVED")
            self.assertEqual(len(queue.tasks), 1)
            self.assertEqual(queue.tasks[0].task_id, proposal.proposal_id)
            self.assertEqual(queue.tasks[0].status, "APPROVED")
            self.assertEqual(queue.tasks[0].active_run_id, manifest.run_id)
            self.assertEqual(queue.tasks[0].run_ids, [manifest.run_id])

            manifests = list((root / "runs").glob("*/manifest.json"))
            self.assertEqual(len(manifests), 1)

    def test_prune_queued_proposals_removes_legacy_plan_pool_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proposal_store = ProposalStore(root / "proposals.json")
            proposal_store.save(
                ProposalPool(
                    proposals=[
                        ProposalRecord(
                            proposal_id="queued",
                            title="Queued",
                            prompt="Already queued",
                            status=PROPOSAL_QUEUED,
                        ),
                        ProposalRecord(
                            proposal_id="review",
                            title="Review",
                            prompt="Needs review",
                            status=RUN_PLAN_REVIEW_REQUIRED,
                        ),
                    ],
                )
            )

            removed = prune_queued_proposals(root / "proposals.json")

            self.assertEqual(removed, 1)
            loaded = proposal_store.load()
            self.assertEqual([proposal.proposal_id for proposal in loaded.proposals], ["review"])

    def test_queue_action_requires_queue_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runtime = COrchRuntime(runs_dir=Path(tmp) / "runs")
            status, payload = runtime.queue_action("confirm-runtime-restarted")
            self.assertEqual(int(status), 400)
            self.assertEqual(payload["error"], "missing queue file")


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
