from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .states import (
    RUN_APPROVED,
    RUN_FAILED,
    RUN_PLAN_APPROVED,
    RUN_PLAN_READY,
    RUN_PLAN_REVIEW_REQUIRED,
    RUN_PLAN_REVISING,
    RUN_PLANNING,
    RUN_REVIEWING,
    RUN_REVISION_REQUESTED,
    RUN_WORK_DONE,
    RUN_WORKING,
    TERMINAL_RUN_STATUSES,
)


TIMING_VERSION = 1

PHASE_PLANNING = "planning"
PHASE_HUMAN_PLAN_REVIEW_WAIT = "human_plan_review_wait"
PHASE_PLAN_REVISION = "plan_revision"
PHASE_WORKER_EXECUTION = "worker_execution"
PHASE_WORK_DONE_WAIT = "work_done_wait"
PHASE_PLANNER_REVIEW = "planner_review"
PHASE_REVISION_WAIT = "revision_wait"
PHASE_TOTAL = "total"

PHASE_ORDER = [
    PHASE_PLANNING,
    PHASE_HUMAN_PLAN_REVIEW_WAIT,
    PHASE_PLAN_REVISION,
    PHASE_WORKER_EXECUTION,
    PHASE_WORK_DONE_WAIT,
    PHASE_PLANNER_REVIEW,
    PHASE_REVISION_WAIT,
]

PHASE_LABELS = {
    PHASE_PLANNING: "Planning",
    PHASE_HUMAN_PLAN_REVIEW_WAIT: "Human Plan Review Wait",
    PHASE_PLAN_REVISION: "Plan Revision",
    PHASE_WORKER_EXECUTION: "Worker Execution",
    PHASE_WORK_DONE_WAIT: "Work Done Wait",
    PHASE_PLANNER_REVIEW: "Planner Review",
    PHASE_REVISION_WAIT: "Revision Wait",
    PHASE_TOTAL: "Total",
}

STATUS_TO_PHASE = {
    RUN_PLANNING: PHASE_PLANNING,
    RUN_PLAN_REVIEW_REQUIRED: PHASE_HUMAN_PLAN_REVIEW_WAIT,
    RUN_PLAN_REVISING: PHASE_PLAN_REVISION,
    RUN_WORKING: PHASE_WORKER_EXECUTION,
    RUN_WORK_DONE: PHASE_WORK_DONE_WAIT,
    RUN_REVIEWING: PHASE_PLANNER_REVIEW,
    RUN_REVISION_REQUESTED: PHASE_REVISION_WAIT,
}

EVENT_TO_STATUS = {
    "planner_start": RUN_PLANNING,
    "planner_plan_ready": RUN_PLAN_READY,
    "plan_review_required": RUN_PLAN_REVIEW_REQUIRED,
    "plan_approved": RUN_PLAN_APPROVED,
    "planner_plan_revision_started": RUN_PLAN_REVISING,
    "planner_plan_revised": RUN_PLAN_REVIEW_REQUIRED,
    "worker_start": RUN_WORKING,
    "worker_done": RUN_WORK_DONE,
    "planner_review_start": RUN_REVIEWING,
    "planner_review_failed": RUN_WORK_DONE,
}


def record_run_status_transition(
    manifest: Any,
    new_status: str,
    now_iso: str,
    metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Persist phase timing on business run-status transitions.

    Returns True when manifest.status changes; False for idempotent no-op.
    """
    previous_status = str(getattr(manifest, "status", ""))
    timing = _ensure_timing_dict(manifest)
    if previous_status == new_status:
        active = _active_segment(timing.get("segments", []))
        if active is not None and str(active.get("start_status", "")) == new_status:
            return False
    segments = timing.setdefault("segments", [])
    active = _active_segment(segments)
    if active is not None and not active.get("completed_at"):
        active["completed_at"] = now_iso
        active["end_status"] = new_status
        active["duration_seconds"] = _duration_seconds(active.get("started_at"), now_iso)
        active["status"] = "completed"
    phase = STATUS_TO_PHASE.get(new_status)
    if phase:
        sequence = len(segments) + 1
        segment: Dict[str, Any] = {
            "id": f"segment-{sequence:04d}",
            "sequence": sequence,
            "phase": phase,
            "label": PHASE_LABELS.get(phase, phase),
            "started_at": now_iso,
            "completed_at": None,
            "duration_seconds": None,
            "status": "active",
            "start_status": new_status,
            "end_status": None,
        }
        for key, value in (metadata or {}).items():
            if value is not None:
                segment[str(key)] = value
        segments.append(segment)
    manifest.status = new_status
    return previous_status != new_status


def build_timing_summary(
    manifest: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    now_iso: str,
) -> Dict[str, Any]:
    timing = manifest.get("timing")
    if isinstance(timing, dict) and isinstance(timing.get("segments"), list):
        summary = _summary_from_segments(
            manifest=manifest,
            segments=timing.get("segments", []),
            now_iso=now_iso,
            source="manifest",
        )
        summary["version"] = int(timing.get("version") or TIMING_VERSION)
        return summary
    fallback_segments = _segments_from_events(manifest=manifest, events=events, now_iso=now_iso)
    summary = _summary_from_segments(
        manifest=manifest,
        segments=fallback_segments,
        now_iso=now_iso,
        source="legacy_fallback",
    )
    summary["version"] = TIMING_VERSION
    return summary


def _segments_from_events(
    *,
    manifest: Mapping[str, Any],
    events: Iterable[Mapping[str, Any]],
    now_iso: str,
) -> List[Dict[str, Any]]:
    transitions: List[Tuple[str, str, Dict[str, Any]]] = []
    for event in events:
        event_type = str(event.get("type", ""))
        timestamp = event.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            continue
        mapped = EVENT_TO_STATUS.get(event_type)
        if event_type == "planner_review_completed":
            if str(event.get("decision", "")) == "revision_requested":
                mapped = RUN_REVISION_REQUESTED
        elif event_type == "run_terminal_status":
            mapped = str(event.get("status", "")) or str(manifest.get("status", ""))
        elif event_type == "proposal_planning_failed":
            mapped = RUN_FAILED
        if mapped:
            transitions.append((timestamp, mapped, {"event_type": event_type}))
    created_at = str(manifest.get("created_at", ""))
    updated_at = str(manifest.get("updated_at", "")) or now_iso
    final_status = str(manifest.get("status", ""))
    if final_status and (not transitions or transitions[-1][1] != final_status):
        transitions.append((updated_at, final_status, {"inferred": True}))
    segments: List[Dict[str, Any]] = []
    active: Optional[Dict[str, Any]] = None
    sequence = 0
    last_status = str(manifest.get("status", ""))
    if transitions:
        last_status = ""
    for timestamp, status, metadata in transitions:
        if status == last_status:
            continue
        if active is not None and not active.get("completed_at"):
            active["completed_at"] = timestamp
            active["end_status"] = status
            active["duration_seconds"] = _duration_seconds(active.get("started_at"), timestamp)
            active["status"] = "completed"
        phase = STATUS_TO_PHASE.get(status)
        if phase:
            sequence += 1
            segment_status = "partial" if bool(metadata.get("inferred")) else "active"
            active = {
                "id": f"legacy-segment-{sequence:04d}",
                "sequence": sequence,
                "phase": phase,
                "label": PHASE_LABELS.get(phase, phase),
                "started_at": timestamp,
                "completed_at": None,
                "duration_seconds": None,
                "status": segment_status,
                "start_status": status,
                "end_status": None,
            }
            for key, value in metadata.items():
                if value is not None:
                    active[str(key)] = value
            segments.append(active)
        else:
            active = None
        last_status = status
    if active is not None and final_status in TERMINAL_RUN_STATUSES and not active.get("completed_at"):
        active["completed_at"] = updated_at
        active["end_status"] = final_status
        active["duration_seconds"] = _duration_seconds(active.get("started_at"), updated_at)
        active["status"] = "completed"
    if not segments and STATUS_TO_PHASE.get(final_status):
        sequence += 1
        inferred_start = created_at or updated_at or now_iso
        duration = _duration_seconds(inferred_start, updated_at or now_iso)
        segments.append(
            {
                "id": f"legacy-segment-{sequence:04d}",
                "sequence": sequence,
                "phase": STATUS_TO_PHASE[final_status],
                "label": PHASE_LABELS.get(STATUS_TO_PHASE[final_status], final_status),
                "started_at": inferred_start,
                "completed_at": updated_at if final_status in TERMINAL_RUN_STATUSES else None,
                "duration_seconds": duration,
                "status": "partial",
                "start_status": final_status,
                "end_status": final_status if final_status in TERMINAL_RUN_STATUSES else None,
                "inferred": True,
            }
        )
    return segments


def _summary_from_segments(
    *,
    manifest: Mapping[str, Any],
    segments: Iterable[Mapping[str, Any]],
    now_iso: str,
    source: str,
) -> Dict[str, Any]:
    normalized_segments = [_normalize_segment(segment, now_iso) for segment in segments]
    phase_summaries = [
        _phase_summary(
            phase=phase,
            label=PHASE_LABELS[phase],
            segments=[segment for segment in normalized_segments if segment.get("phase") == phase],
        )
        for phase in PHASE_ORDER
    ]
    phase_map = {summary["phase"]: summary for summary in phase_summaries}
    created_at = str(manifest.get("created_at", "")) or _first_timestamp(normalized_segments)
    run_status = str(manifest.get("status", ""))
    if run_status in TERMINAL_RUN_STATUSES:
        completed_at = str(manifest.get("updated_at", "")) or _last_completed_timestamp(normalized_segments)
    else:
        completed_at = None
    total_end = completed_at or now_iso
    total_duration = _duration_seconds(created_at, total_end)
    total_status = "completed" if completed_at else "active"
    if not created_at:
        total_status = "partial"
    total = {
        "phase": PHASE_TOTAL,
        "label": PHASE_LABELS[PHASE_TOTAL],
        "count": 1 if created_at else 0,
        "started_at": created_at or None,
        "completed_at": completed_at,
        "duration_seconds": total_duration,
        "total_duration_seconds": total_duration,
        "status": total_status,
    }
    return {
        "source": source,
        "segments": normalized_segments,
        "phases": phase_summaries,
        "phase_aggregates": phase_map,
        "total": total,
    }


def _phase_summary(*, phase: str, label: str, segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not segments:
        return {
            "phase": phase,
            "label": label,
            "count": 0,
            "started_at": None,
            "completed_at": None,
            "duration_seconds": 0,
            "total_duration_seconds": 0,
            "status": "missing",
        }
    durations = [int(segment.get("duration_seconds") or 0) for segment in segments]
    started_at = _first_timestamp(segments)
    completed_at = _last_completed_timestamp(segments)
    has_active = any(str(segment.get("status", "")) == "active" for segment in segments)
    has_partial = any(str(segment.get("status", "")) == "partial" for segment in segments)
    if has_active:
        status = "active"
    elif has_partial:
        status = "partial"
    else:
        status = "completed"
    return {
        "phase": phase,
        "label": label,
        "count": len(segments),
        "started_at": started_at,
        "completed_at": completed_at if not has_active else None,
        "duration_seconds": sum(durations),
        "total_duration_seconds": sum(durations),
        "status": status,
    }


def _normalize_segment(segment: Mapping[str, Any], now_iso: str) -> Dict[str, Any]:
    started_at = str(segment.get("started_at", ""))
    completed_at_raw = segment.get("completed_at")
    completed_at = str(completed_at_raw) if completed_at_raw else None
    if completed_at:
        duration = _duration_seconds(started_at, completed_at)
        status = "completed"
    else:
        duration = _duration_seconds(started_at, now_iso)
        status = "active" if started_at else "partial"
    normalized = dict(segment)
    normalized["started_at"] = started_at or None
    normalized["completed_at"] = completed_at
    normalized["duration_seconds"] = duration
    normalized["status"] = status if str(segment.get("status", "")) != "partial" else "partial"
    return normalized


def _ensure_timing_dict(manifest: Any) -> Dict[str, Any]:
    timing = getattr(manifest, "timing", None)
    if not isinstance(timing, dict):
        timing = {"version": TIMING_VERSION, "segments": []}
        manifest.timing = timing
    else:
        timing.setdefault("version", TIMING_VERSION)
        if not isinstance(timing.get("segments"), list):
            timing["segments"] = []
    return timing


def _active_segment(segments: Iterable[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    for segment in reversed(list(segments)):
        if not segment.get("completed_at"):
            return segment  # type: ignore[return-value]
    return None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_seconds(started_at: Any, completed_at: Any) -> int:
    start = _parse_iso(started_at)
    end = _parse_iso(completed_at)
    if start is None or end is None:
        return 0
    seconds = int((end - start).total_seconds())
    return seconds if seconds >= 0 else 0


def _first_timestamp(records: Iterable[Mapping[str, Any]]) -> Optional[str]:
    timestamps = [str(record.get("started_at", "")) for record in records if record.get("started_at")]
    return min(timestamps) if timestamps else None


def _last_completed_timestamp(records: Iterable[Mapping[str, Any]]) -> Optional[str]:
    timestamps = [str(record.get("completed_at", "")) for record in records if record.get("completed_at")]
    return max(timestamps) if timestamps else None
