from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union


Pathish = Union[str, Path]


@dataclass(frozen=True)
class CodexSessionSnapshot:
    thread_id: str
    path: Path
    cwd: Optional[Path]
    source: Optional[str]
    has_activity_after_started: bool
    final_content: Optional[str]
    final_epoch: Optional[float]


class CodexSessionLogStore:
    def __init__(self, sessions_root: Optional[Pathish] = None) -> None:
        self.sessions_root = (
            Path(sessions_root).expanduser()
            if sessions_root is not None
            else Path.home() / ".codex" / "sessions"
        )

    def find_latest_session(
        self,
        *,
        thread_id: Optional[str] = None,
        cwd: Optional[Pathish] = None,
        started_after: Optional[float] = None,
        source: Optional[str] = "mcp",
    ) -> Optional[CodexSessionSnapshot]:
        target_cwd = _resolve_optional_path(cwd)
        candidates: List[CodexSessionSnapshot] = []
        for path in self._candidate_paths(thread_id=thread_id, started_after=started_after):
            snapshot = _read_session_snapshot(
                path,
                started_after=started_after,
            )
            if snapshot is None:
                continue
            if thread_id is not None and snapshot.thread_id != thread_id:
                continue
            if target_cwd is not None and snapshot.cwd != target_cwd:
                continue
            if source is not None and snapshot.source != source:
                continue
            candidates.append(snapshot)
        if not candidates:
            return None
        return max(candidates, key=_snapshot_sort_key)

    def _candidate_paths(
        self,
        *,
        thread_id: Optional[str],
        started_after: Optional[float],
    ) -> Iterable[Path]:
        root = self.sessions_root.expanduser()
        if not root.exists():
            return []
        if thread_id:
            return root.rglob(f"*{thread_id}.jsonl")
        if started_after is None:
            return root.rglob("*.jsonl")
        dates = _candidate_dates(started_after)
        paths: List[Path] = []
        for day in dates:
            day_dir = root / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
            if day_dir.exists():
                paths.extend(day_dir.glob("*.jsonl"))
        return paths


def _read_session_snapshot(
    path: Path,
    *,
    started_after: Optional[float],
) -> Optional[CodexSessionSnapshot]:
    thread_id: Optional[str] = None
    cwd: Optional[Path] = None
    source: Optional[str] = None
    has_activity_after_started = False
    final_content: Optional[str] = None
    final_epoch: Optional[float] = None

    try:
        with path.open("r", encoding="utf-8") as file_obj:
            for line in file_obj:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                line_epoch = _record_epoch(record)
                if _is_after_started(line_epoch, started_after):
                    has_activity_after_started = True
                if record.get("type") == "session_meta":
                    payload = record.get("payload")
                    if isinstance(payload, dict):
                        if isinstance(payload.get("id"), str):
                            thread_id = payload["id"]
                        if isinstance(payload.get("cwd"), str):
                            cwd = _resolve_optional_path(payload["cwd"])
                        if isinstance(payload.get("source"), str):
                            source = payload["source"]
                content = _final_content(record)
                if content is None or not _is_after_started(line_epoch, started_after):
                    continue
                if final_epoch is None or line_epoch is None or line_epoch >= final_epoch:
                    final_content = content
                    final_epoch = line_epoch
    except OSError:
        return None

    if thread_id is None:
        return None
    return CodexSessionSnapshot(
        thread_id=thread_id,
        path=path,
        cwd=cwd,
        source=source,
        has_activity_after_started=has_activity_after_started,
        final_content=final_content,
        final_epoch=final_epoch,
    )


def _final_content(record: Dict[str, Any]) -> Optional[str]:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if record.get("type") == "event_msg":
        if payload.get("type") == "task_complete":
            message = payload.get("last_agent_message")
            return message if isinstance(message, str) else None
        if payload.get("type") == "agent_message" and payload.get("phase") == "final_answer":
            message = payload.get("message")
            return message if isinstance(message, str) else None
    if record.get("type") == "response_item":
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return None
        if payload.get("phase") != "final_answer":
            return None
        return _content_text(payload.get("content"))
    return None


def _content_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, dict) and isinstance(item.get("output_text"), str):
            parts.append(item["output_text"])
    return "\n".join(parts) if parts else None


def _candidate_dates(epoch: float) -> List[datetime]:
    day = datetime.fromtimestamp(epoch).astimezone()
    return [day - timedelta(days=1), day, day + timedelta(days=1)]


def _record_epoch(record: Dict[str, Any]) -> Optional[float]:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _is_after_started(
    epoch: Optional[float],
    started_after: Optional[float],
    *,
    slack_seconds: float = 1.0,
) -> bool:
    if started_after is None:
        return True
    if epoch is None:
        return False
    return epoch >= started_after - slack_seconds


def _resolve_optional_path(value: Optional[Pathish]) -> Optional[Path]:
    if value is None:
        return None
    try:
        return Path(value).expanduser().resolve()
    except OSError:
        return Path(value).expanduser().absolute()


def _snapshot_sort_key(snapshot: CodexSessionSnapshot) -> float:
    if snapshot.final_epoch is not None:
        return snapshot.final_epoch
    try:
        return snapshot.path.stat().st_mtime
    except OSError:
        return 0.0
