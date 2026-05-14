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


if __name__ == "__main__":
    unittest.main()
