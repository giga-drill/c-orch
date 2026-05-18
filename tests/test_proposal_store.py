from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from c_orch.proposal_store import ProposalStore


class ProposalStoreTests(unittest.TestCase):
    def test_add_and_load_proposal_with_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProposalStore(Path(tmp) / "proposals.json")
            pool = store.create()
            proposal = store.add_proposal(
                pool,
                title="Task 1",
                prompt="Do task 1",
                cwd="/tmp/repo-a",
            )
            store.save(pool)

            loaded = store.load()
            self.assertEqual(loaded.proposals[0].proposal_id, proposal.proposal_id)
            self.assertEqual(loaded.proposals[0].cwd, "/tmp/repo-a")

    def test_load_legacy_proposal_without_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proposals_path = Path(tmp) / "proposals.json"
            proposals_path.parent.mkdir(parents=True, exist_ok=True)
            proposals_path.write_text(
                json.dumps(
                    {
                        "pool_id": "default",
                        "proposals": [
                            {
                                "proposal_id": "proposal-001",
                                "title": "Legacy proposal",
                                "prompt": "Do task 1",
                                "status": "PLAN_REVIEW_REQUIRED",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            pool = ProposalStore(proposals_path).load()
            self.assertEqual(pool.proposals[0].proposal_id, "proposal-001")
            self.assertIsNone(pool.proposals[0].cwd)

    def test_proposal_blocker_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProposalStore(Path(tmp) / "proposals.json")
            pool = store.create()
            proposal = store.add_proposal(
                pool,
                title="Dirty workspace proposal",
                prompt="Do task 1",
                cwd="/tmp/repo-a",
            )
            store.update_proposal(
                pool,
                proposal.proposal_id,
                blocker={
                    "type": "workspace_dirty",
                    "message": "目标工作区存在未提交改动。建议先提交或处理这些改动，再生成计划。",
                    "status_output": " M src/main.py",
                    "command": "git -C /tmp/repo-a status --short",
                },
            )
            store.save(pool)

            loaded = store.load()
            self.assertIsNotNone(loaded.proposals[0].blocker)
            assert loaded.proposals[0].blocker is not None
            self.assertEqual(loaded.proposals[0].blocker["type"], "workspace_dirty")
            self.assertIn("src/main.py", loaded.proposals[0].blocker["status_output"])

    def test_reorder_proposal_persists_array_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ProposalStore(Path(tmp) / "proposals.json")
            pool = store.create()
            first = store.add_proposal(pool, title="Task 1", prompt="Do 1", cwd="/tmp/repo-a")
            second = store.add_proposal(pool, title="Task 2", prompt="Do 2", cwd="/tmp/repo-a")
            third = store.add_proposal(pool, title="Task 3", prompt="Do 3", cwd="/tmp/repo-a")
            store.reorder_proposal(
                pool,
                proposal_id=third.proposal_id,
                target_proposal_id=first.proposal_id,
                position="before",
            )
            store.save(pool)

            loaded = store.load()
            self.assertEqual(
                [proposal.proposal_id for proposal in loaded.proposals],
                [third.proposal_id, first.proposal_id, second.proposal_id],
            )


if __name__ == "__main__":
    unittest.main()
