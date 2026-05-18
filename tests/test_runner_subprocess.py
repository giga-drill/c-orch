from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from c_orch.runner_leases import LEASE_STATUS_COMPLETED, LEASE_STATUS_FAILED, RunnerLeaseStore
from c_orch.runner_subprocess import (
    RUNNER_PHASE_CODE_REVIEW,
    RUNNER_RESULT_COMPLETED,
    RUNNER_RESULT_FAILED,
    RunnerSubprocessRequest,
    load_runner_subprocess_result,
    run_runner_subprocess,
)


class RunnerSubprocessTests(unittest.TestCase):
    def test_run_runner_subprocess_completes_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-1"
            run_dir = root / "runs" / run_id
            worktree = root / "repo"
            worktree.mkdir(parents=True)
            (worktree / "README.md").write_text("hello\n", encoding="utf-8")
            codex_bin = root / "fake-codex.sh"
            _write_fake_codex_binary(codex_bin, exit_code=0)

            lease_path = run_dir / "runner-leases.json"
            lease_store = RunnerLeaseStore(lease_path)
            lease = lease_store.create(
                runtime_generation="runtime-test",
                run_id=run_id,
                phase="code_review",
                task_id="task-1",
                proposal_id=None,
                process_hint="pid:1",
                pid=1,
                checkpoint={"phase_boundary": "before_code_review"},
                lease_ttl_seconds=300,
                now=datetime(2026, 5, 19, 2, 0, tzinfo=timezone.utc),
            )

            request_path = run_dir / "runner-subprocess" / "code_review" / f"{lease.runner_id}.request.json"
            result_path = run_dir / "runner-subprocess" / "code_review" / f"{lease.runner_id}.result.json"
            request = RunnerSubprocessRequest(
                schema_version=1,
                runner_id=lease.runner_id,
                run_id=run_id,
                phase=RUNNER_PHASE_CODE_REVIEW,
                cwd=str(worktree),
                evidence_dir=str(run_dir / "evidence" / "attempt-1" / "review-1"),
                codex_binary_path=str(codex_bin),
                patch_path=None,
                service_tier="flex",
                request_path=str(request_path),
                result_path=str(result_path),
                lease_path=str(lease_path),
                lease_ttl_seconds=300,
            )

            result = run_runner_subprocess(request)
            self.assertEqual(result.status, RUNNER_RESULT_COMPLETED)
            self.assertEqual(result.review_status, "passed")
            self.assertTrue(result_path.exists())

            loaded = load_runner_subprocess_result(result_path)
            report = loaded.to_codex_review_report()
            self.assertEqual(report.status, "passed")
            self.assertTrue(report.output_path.exists())
            self.assertTrue(report.result_path.exists())

            lease_payload = lease_store.read().to_dict()
            code_review_lease = next(item for item in lease_payload["leases"] if item["runner_id"] == lease.runner_id)
            self.assertEqual(code_review_lease["status"], LEASE_STATUS_COMPLETED)

    def test_run_runner_subprocess_failure_persists_failed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run-1"
            run_dir = root / "runs" / run_id
            worktree = root / "repo"
            worktree.mkdir(parents=True)
            lease_path = run_dir / "runner-leases.json"
            lease_store = RunnerLeaseStore(lease_path)
            lease = lease_store.create(
                runtime_generation="runtime-test",
                run_id=run_id,
                phase="code_review",
                task_id=None,
                proposal_id=None,
                process_hint="pid:1",
                pid=1,
                checkpoint={"phase_boundary": "before_code_review"},
                lease_ttl_seconds=300,
                now=datetime(2026, 5, 19, 2, 0, tzinfo=timezone.utc),
            )

            request_path = run_dir / "runner-subprocess" / "code_review" / f"{lease.runner_id}.request.json"
            result_path = run_dir / "runner-subprocess" / "code_review" / f"{lease.runner_id}.result.json"
            request = RunnerSubprocessRequest(
                schema_version=1,
                runner_id=lease.runner_id,
                run_id=run_id,
                phase="invalid_phase",
                cwd=str(worktree),
                evidence_dir=str(run_dir / "evidence" / "attempt-1" / "review-1"),
                codex_binary_path=None,
                patch_path=None,
                service_tier=None,
                request_path=str(request_path),
                result_path=str(result_path),
                lease_path=str(lease_path),
                lease_ttl_seconds=300,
            )

            result = run_runner_subprocess(request)
            self.assertEqual(result.status, RUNNER_RESULT_FAILED)
            self.assertTrue(result.error)
            self.assertTrue(result_path.exists())

            loaded = load_runner_subprocess_result(result_path)
            self.assertEqual(loaded.status, RUNNER_RESULT_FAILED)
            lease_payload = lease_store.read().to_dict()
            code_review_lease = next(item for item in lease_payload["leases"] if item["runner_id"] == lease.runner_id)
            self.assertEqual(code_review_lease["status"], LEASE_STATUS_FAILED)


def _write_fake_codex_binary(path: Path, *, exit_code: int) -> None:
    script = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            'if [ \"$1\" = \"review\" ] && [ \"$2\" = \"--uncommitted\" ]; then',
            "  echo \"fake codex review output\"",
            f"  exit {int(exit_code)}",
            "fi",
            "echo \"unexpected command\" >&2",
            "exit 64",
        ]
    )
    path.write_text(script + "\n", encoding="utf-8")
    path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
