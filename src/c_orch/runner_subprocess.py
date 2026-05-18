from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Union

from .codex_review import CodexReviewReport, run_codex_uncommitted_review
from .runner_leases import RunnerLeaseStore


Pathish = Union[str, Path]
RUNNER_SUBPROCESS_SCHEMA_VERSION = 1
RUNNER_PHASE_CODE_REVIEW = "code_review"
RUNNER_RESULT_COMPLETED = "completed"
RUNNER_RESULT_FAILED = "failed"


@dataclass(frozen=True)
class RunnerSubprocessRequest:
    schema_version: int
    runner_id: str
    run_id: str
    phase: str
    cwd: str
    evidence_dir: str
    codex_binary_path: Optional[str]
    patch_path: Optional[str]
    service_tier: Optional[str]
    request_path: str
    result_path: str
    lease_path: str
    lease_ttl_seconds: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "runner_id": self.runner_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "cwd": self.cwd,
            "evidence_dir": self.evidence_dir,
            "codex_binary_path": self.codex_binary_path,
            "patch_path": self.patch_path,
            "service_tier": self.service_tier,
            "request_path": self.request_path,
            "result_path": self.result_path,
            "lease_path": self.lease_path,
            "lease_ttl_seconds": int(self.lease_ttl_seconds),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunnerSubprocessRequest":
        return cls(
            schema_version=int(data.get("schema_version", RUNNER_SUBPROCESS_SCHEMA_VERSION)),
            runner_id=str(data.get("runner_id", "")).strip(),
            run_id=str(data.get("run_id", "")).strip(),
            phase=str(data.get("phase", "")).strip(),
            cwd=str(data.get("cwd", "")).strip(),
            evidence_dir=str(data.get("evidence_dir", "")).strip(),
            codex_binary_path=_string_or_none(data.get("codex_binary_path")),
            patch_path=_string_or_none(data.get("patch_path")),
            service_tier=_string_or_none(data.get("service_tier")),
            request_path=str(data.get("request_path", "")).strip(),
            result_path=str(data.get("result_path", "")).strip(),
            lease_path=str(data.get("lease_path", "")).strip(),
            lease_ttl_seconds=max(1, int(data.get("lease_ttl_seconds", 300))),
        )


@dataclass(frozen=True)
class RunnerSubprocessResult:
    schema_version: int
    runner_id: str
    run_id: str
    phase: str
    status: str
    started_at: str
    completed_at: str
    request_path: str
    result_path: str
    lease_path: str
    process_hint: str
    pid: Optional[int]
    lease_status: Optional[str]
    review_status: Optional[str]
    returncode: Optional[int]
    error: Optional[str]
    report: Optional[Dict[str, Any]]
    evidence_files: list[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "runner_id": self.runner_id,
            "run_id": self.run_id,
            "phase": self.phase,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_path": self.request_path,
            "result_path": self.result_path,
            "lease_path": self.lease_path,
            "process_hint": self.process_hint,
            "pid": self.pid,
            "lease_status": self.lease_status,
            "review_status": self.review_status,
            "returncode": self.returncode,
            "error": self.error,
            "report": dict(self.report) if isinstance(self.report, dict) else None,
            "evidence_files": list(self.evidence_files),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunnerSubprocessResult":
        pid_raw = data.get("pid")
        pid: Optional[int]
        if isinstance(pid_raw, int):
            pid = pid_raw
        elif isinstance(pid_raw, str) and pid_raw.isdigit():
            pid = int(pid_raw)
        else:
            pid = None
        report = data.get("report") if isinstance(data.get("report"), dict) else None
        return cls(
            schema_version=int(data.get("schema_version", RUNNER_SUBPROCESS_SCHEMA_VERSION)),
            runner_id=str(data.get("runner_id", "")).strip(),
            run_id=str(data.get("run_id", "")).strip(),
            phase=str(data.get("phase", "")).strip(),
            status=str(data.get("status", "")).strip(),
            started_at=str(data.get("started_at", "")).strip(),
            completed_at=str(data.get("completed_at", "")).strip(),
            request_path=str(data.get("request_path", "")).strip(),
            result_path=str(data.get("result_path", "")).strip(),
            lease_path=str(data.get("lease_path", "")).strip(),
            process_hint=str(data.get("process_hint", "")).strip(),
            pid=pid,
            lease_status=_string_or_none(data.get("lease_status")),
            review_status=_string_or_none(data.get("review_status")),
            returncode=_int_or_none(data.get("returncode")),
            error=_string_or_none(data.get("error")),
            report=dict(report) if isinstance(report, dict) else None,
            evidence_files=[str(item) for item in data.get("evidence_files", []) if isinstance(item, str)],
        )

    def to_codex_review_report(self) -> CodexReviewReport:
        payload = self.report
        if not isinstance(payload, dict):
            raise ValueError("runner result does not contain report payload")
        output_path = payload.get("output_path")
        result_path = payload.get("result_path")
        if not isinstance(output_path, str) or not output_path.strip():
            raise ValueError("runner report missing output_path")
        if not isinstance(result_path, str) or not result_path.strip():
            raise ValueError("runner report missing result_path")
        summary = str(payload.get("summary", "")).strip()
        status = str(payload.get("status", "")).strip()
        command = str(payload.get("command", "")).strip()
        if not summary:
            raise ValueError("runner report missing summary")
        if not status:
            raise ValueError("runner report missing status")
        if not command:
            raise ValueError("runner report missing command")
        return CodexReviewReport(
            summary=summary,
            output_path=Path(output_path).expanduser().resolve(),
            result_path=Path(result_path).expanduser().resolve(),
            status=status,
            returncode=_int_or_none(payload.get("returncode")),
            command=command,
            service_tier=_string_or_none(payload.get("service_tier")),
        )


def run_runner_subprocess(request: RunnerSubprocessRequest) -> RunnerSubprocessResult:
    started_at = _now_iso()
    pid = os.getpid()
    process_hint = f"pid:{pid}"
    lease_store = RunnerLeaseStore(Path(request.lease_path))

    report: Optional[CodexReviewReport] = None
    status = RUNNER_RESULT_COMPLETED
    infra_error: Optional[str] = None
    lease_status = "failed"

    try:
        lease_store.heartbeat(
            request.runner_id,
            checkpoint={
                "phase_boundary": "runner_subprocess_started",
                "runner_phase": request.phase,
                "request_path": request.request_path,
                "result_path": request.result_path,
                "pid": pid,
                "process_hint": process_hint,
            },
            lease_ttl_seconds=request.lease_ttl_seconds,
        )
        report = _run_phase(request)
        lease_status = "completed"
        lease_store.complete(
            request.runner_id,
            checkpoint={
                "phase_boundary": "runner_subprocess_completed",
                "runner_phase": request.phase,
                "request_path": request.request_path,
                "result_path": request.result_path,
                "pid": pid,
                "process_hint": process_hint,
                "code_review_status": report.status,
            },
        )
    except Exception as exc:
        status = RUNNER_RESULT_FAILED
        infra_error = str(exc)
        lease_status = "failed"
        try:
            lease_store.fail(
                request.runner_id,
                checkpoint={
                    "phase_boundary": "runner_subprocess_failed",
                    "runner_phase": request.phase,
                    "request_path": request.request_path,
                    "result_path": request.result_path,
                    "pid": pid,
                    "process_hint": process_hint,
                },
                error=infra_error,
            )
        except Exception:
            pass

    completed_at = _now_iso()
    report_payload = _report_to_dict(report) if report is not None else None
    evidence_files = list(report.evidence_files) if report is not None else []
    review_status = report.status if report is not None else None
    returncode = report.returncode if report is not None else None
    result = RunnerSubprocessResult(
        schema_version=RUNNER_SUBPROCESS_SCHEMA_VERSION,
        runner_id=request.runner_id,
        run_id=request.run_id,
        phase=request.phase,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        request_path=request.request_path,
        result_path=request.result_path,
        lease_path=request.lease_path,
        process_hint=process_hint,
        pid=pid,
        lease_status=lease_status,
        review_status=review_status,
        returncode=returncode,
        error=infra_error,
        report=report_payload,
        evidence_files=evidence_files,
    )
    _write_json(Path(request.result_path), result.to_dict())
    return result


def load_runner_subprocess_result(path: Pathish) -> RunnerSubprocessResult:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("runner subprocess result payload must be an object")
    return RunnerSubprocessResult.from_dict(data)


def write_runner_subprocess_request(path: Pathish, request: RunnerSubprocessRequest) -> Path:
    target = Path(path)
    _write_json(target, request.to_dict())
    return target


def load_runner_subprocess_request(path: Pathish) -> RunnerSubprocessRequest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("runner subprocess request payload must be an object")
    request = RunnerSubprocessRequest.from_dict(data)
    if request.phase != RUNNER_PHASE_CODE_REVIEW:
        raise ValueError(f"unsupported runner phase: {request.phase}")
    if not request.runner_id:
        raise ValueError("runner_id is required")
    if not request.run_id:
        raise ValueError("run_id is required")
    if not request.cwd:
        raise ValueError("cwd is required")
    if not request.evidence_dir:
        raise ValueError("evidence_dir is required")
    if not request.request_path:
        raise ValueError("request_path is required")
    if not request.result_path:
        raise ValueError("result_path is required")
    if not request.lease_path:
        raise ValueError("lease_path is required")
    return request


def _run_phase(request: RunnerSubprocessRequest) -> CodexReviewReport:
    if request.phase != RUNNER_PHASE_CODE_REVIEW:
        raise ValueError(f"unsupported runner phase: {request.phase}")
    return run_codex_uncommitted_review(
        cwd=request.cwd,
        evidence_dir=request.evidence_dir,
        codex_binary_path=request.codex_binary_path,
        patch_path=request.patch_path,
        service_tier=request.service_tier,
    )


def _report_to_dict(report: CodexReviewReport) -> Dict[str, Any]:
    return {
        "summary": report.summary,
        "output_path": str(report.output_path),
        "result_path": str(report.result_path),
        "status": report.status,
        "returncode": report.returncode,
        "command": report.command,
        "service_tier": report.service_tier,
        "evidence_files": list(report.evidence_files),
    }


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="c-orch runner subprocess")
    parser.add_argument("--request", required=True, help="Path to runner request JSON")
    parser.add_argument("--result", required=True, help="Path to runner result JSON")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    request = load_runner_subprocess_request(args.request)
    request_path = str(Path(args.request).expanduser().resolve())
    result_path = str(Path(args.result).expanduser().resolve())
    if request.request_path != request_path or request.result_path != result_path:
        request = RunnerSubprocessRequest(
            schema_version=request.schema_version,
            runner_id=request.runner_id,
            run_id=request.run_id,
            phase=request.phase,
            cwd=request.cwd,
            evidence_dir=request.evidence_dir,
            codex_binary_path=request.codex_binary_path,
            patch_path=request.patch_path,
            service_tier=request.service_tier,
            request_path=request_path,
            result_path=result_path,
            lease_path=request.lease_path,
            lease_ttl_seconds=request.lease_ttl_seconds,
        )
        write_runner_subprocess_request(request_path, request)
    result = run_runner_subprocess(request)
    return 0 if result.status == RUNNER_RESULT_COMPLETED else 1


if __name__ == "__main__":
    raise SystemExit(main())
