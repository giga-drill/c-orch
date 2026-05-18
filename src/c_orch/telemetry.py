from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .phase_timing import (
    PHASE_HUMAN_PLAN_REVIEW_WAIT,
    PHASE_PLANNER_REVIEW,
    PHASE_PLAN_REVISION,
    PHASE_PLANNING,
    PHASE_REVISION_WAIT,
    PHASE_WORK_DONE_WAIT,
    PHASE_WORKER_EXECUTION,
    build_timing_summary,
)
from .run_store import RunManifest, RunStore
from .states import TERMINAL_RUN_STATUSES


Pathish = Union[str, Path]

CATEGORY_PLANNING = "planning"
CATEGORY_WORKER_EXECUTION = "worker_execution"
CATEGORY_VERIFICATION = "verification"
CATEGORY_CODEX_REVIEW = "codex_review"
CATEGORY_PLANNER_REVIEW = "planner_review"
CATEGORY_REWORK = "rework"
CATEGORY_APPLY_COMMIT = "apply_commit"
CATEGORY_RECOVERY = "recovery"
CATEGORY_UNKNOWN = "unknown"
DEFAULT_RECENT_RUN_LIMIT = 5

_SENTENCE_BOUNDARY_PATTERN = re.compile(r"[。！？]|[.?!](?=\s|$)|(?:\r?\n)")

_CATEGORIES = [
    (CATEGORY_PLANNING, "Proposal planning"),
    (CATEGORY_WORKER_EXECUTION, "Worker execution"),
    (CATEGORY_VERIFICATION, "Verification"),
    (CATEGORY_CODEX_REVIEW, "Codex review"),
    (CATEGORY_PLANNER_REVIEW, "Planner review"),
    (CATEGORY_REWORK, "Rework"),
    (CATEGORY_APPLY_COMMIT, "Apply/commit"),
    (CATEGORY_RECOVERY, "Recovery"),
    (CATEGORY_UNKNOWN, "Unknown"),
]


@dataclass
class StageMetric:
    category: str
    duration_seconds: float
    count: int
    coverage: str
    source: str
    limitations: List[str]


def build_project_telemetry(runs_dir: Pathish, recent_limit: Optional[int] = None) -> Dict[str, Any]:
    runs_path = Path(runs_dir).expanduser().resolve()
    run_store = RunStore(runs_path)
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")

    manifests = _load_manifests(runs_path)
    total_runs = len(manifests)

    stage_aggregates: Dict[str, Dict[str, Any]] = {
        category: {
            "category": category,
            "label": label,
            "total_duration_seconds": 0.0,
            "count": 0,
            "run_count": 0,
            "coverage": {"exact": 0, "partial": 0, "unavailable": 0},
            "sources": {},
            "limitations": [],
            "status": "unavailable",
        }
        for category, label in _CATEGORIES
    }

    approved_runs = 0
    failed_runs = 0
    terminal_runs = 0
    rework_count = 0
    review_retry_count = 0
    run_samples: List[Dict[str, Any]] = []

    for index, manifest in enumerate(manifests):
        status = str(manifest.get("status", ""))
        if status == "APPROVED":
            approved_runs += 1
        if status == "FAILED":
            failed_runs += 1
        if status in TERMINAL_RUN_STATUSES:
            terminal_runs += 1

        run_id = str(manifest.get("run_id", ""))
        events = run_store.load_events(run_id)
        usage_records = run_store.load_usage_attribution(run_id)
        timing = build_timing_summary(manifest, events, now_iso=now_iso)
        metrics = _run_stage_metrics(
            manifest=manifest,
            events=events,
            usage_records=usage_records,
            timing=timing,
        )
        review_rework_evidence = _run_review_rework_evidence(
            manifest=manifest,
            events=events,
            usage_records=usage_records,
            timing=timing,
        )

        run_rework_count = int(review_rework_evidence.get("rework_count", 0) or 0)
        rework_count += run_rework_count

        review_attempts = _list_value(manifest.get("review_attempts"))
        run_review_retry_count = sum(
            1
            for attempt in review_attempts
            if _string_value(_dict_value(attempt).get("status")) == "FAILED_RETRYABLE"
        )
        if run_review_retry_count == 0:
            run_review_retry_count = int(review_rework_evidence.get("review_retryable_count", 0) or 0)
        review_retry_count += run_review_retry_count

        for metric in metrics:
            aggregate = stage_aggregates[metric.category]
            aggregate["total_duration_seconds"] += max(0.0, metric.duration_seconds)
            aggregate["count"] += max(0, metric.count)
            aggregate["coverage"][metric.coverage] += 1
            if metric.coverage != "unavailable":
                aggregate["run_count"] += 1
            sources = _dict_value(aggregate.get("sources"))
            sources[metric.source] = int(sources.get(metric.source, 0)) + 1
            aggregate["sources"] = sources
            if metric.limitations:
                _extend_unique(aggregate["limitations"], metric.limitations)

        run_samples.append(
            {
                "index": index,
                "manifest": manifest,
                "events": events,
                "usage_records": usage_records,
                "timing": timing,
                "metrics": metrics,
                "review_rework_evidence": review_rework_evidence,
            }
        )

    stages = [_finalize_stage(stage_aggregates[category], total_runs) for category, _ in _CATEGORIES]

    bottlenecks = sorted(
        [
            {
                "category": stage["category"],
                "label": stage["label"],
                "status": stage["status"],
                "total_duration_seconds": stage["total_duration_seconds"],
                "avg_duration_seconds": stage["avg_duration_seconds"],
                "count": stage["count"],
                "run_count": stage["run_count"],
            }
            for stage in stages
            if (
                stage["category"] != CATEGORY_UNKNOWN
                and stage["status"] != "unavailable"
                and int(stage.get("count", 0)) > 0
                and int(stage.get("run_count", 0)) > 0
            )
        ],
        key=lambda item: (float(item.get("total_duration_seconds", 0.0)), int(item.get("count", 0))),
        reverse=True,
    )

    limitations = _project_limitations(stages)
    retrospective = _build_recent_terminal_retrospective(
        run_samples=run_samples,
        recent_limit=recent_limit,
    )
    return {
        "runs_dir": str(runs_path),
        "generated_at": now_iso,
        "summary": {
            "total_runs": total_runs,
            "terminal_runs": terminal_runs,
            "approved_runs": approved_runs,
            "failed_runs": failed_runs,
            "rework_count": rework_count,
            "review_retry_count": review_retry_count,
        },
        "stages": stages,
        "bottlenecks": bottlenecks,
        "recent_bottlenecks": list(retrospective.get("top_bottlenecks", [])),
        "retrospective": retrospective,
        "limitations": limitations,
    }


def _run_stage_metrics(
    *,
    manifest: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    usage_records: Sequence[Dict[str, Any]],
    timing: Dict[str, Any],
) -> List[StageMetric]:
    planning = _planning_metric(timing=timing, usage_records=usage_records)
    worker = _worker_metric(timing=timing, usage_records=usage_records)
    verification = _verification_metric(events=events)
    codex_review = _usage_duration_metric(
        category=CATEGORY_CODEX_REVIEW,
        records=usage_records,
        role="reviewer",
        phase="review",
        source="usage_attribution",
        missing_limitations=[
            "Codex review split needs usage-attribution role=reviewer phase=review records.",
        ],
    )
    planner_review = _planner_review_metric(timing=timing, usage_records=usage_records)
    apply_commit = _apply_commit_metric(events=events)
    recovery = _recovery_metric(events=events, manifest=manifest)
    rework = _rework_metric(timing=timing, usage_records=usage_records)
    unknown = StageMetric(
        category=CATEGORY_UNKNOWN,
        duration_seconds=0.0,
        count=0,
        coverage="unavailable",
        source="none",
        limitations=[],
    )

    return [planning, worker, verification, codex_review, planner_review, rework, apply_commit, recovery, unknown]


def _planning_metric(*, timing: Dict[str, Any], usage_records: Sequence[Dict[str, Any]]) -> StageMetric:
    phase = _timing_phase(timing, PHASE_PLANNING)
    if phase is not None and _string_value(phase.get("status")) != "missing":
        duration = _number_value(phase.get("total_duration_seconds") or phase.get("duration_seconds"))
        count = int(_number_value(phase.get("count")))
        status = _coverage_from_timing_status(_string_value(phase.get("status")))
        timing_source = _string_value(timing.get("source"))
        if timing_source == "manifest":
            metric_source = "manifest_timing"
            base_limitation = "Planning duration from manifest timing is incomplete for some runs."
        else:
            metric_source = "legacy_event_timing"
            base_limitation = (
                "Planning duration is inferred from events/created_at/updated_at for legacy runs, "
                "not from persisted manifest timing."
            )
            if status == "exact":
                status = "partial"
        limitations = []
        if status != "exact":
            limitations.append(base_limitation)
        elif metric_source == "legacy_event_timing":
            limitations.append(base_limitation)
        return StageMetric(
            category=CATEGORY_PLANNING,
            duration_seconds=duration,
            count=max(0, count),
            coverage=status,
            source=metric_source,
            limitations=limitations,
        )
    usage = _usage_duration_metric(
        category=CATEGORY_PLANNING,
        records=usage_records,
        role="planner",
        phase="plan",
        source="usage_attribution",
        missing_limitations=[
            "Planning duration unavailable when both manifest timing and planner plan usage-attribution are missing.",
        ],
    )
    return usage


def _worker_metric(*, timing: Dict[str, Any], usage_records: Sequence[Dict[str, Any]]) -> StageMetric:
    phase = _timing_phase(timing, PHASE_WORKER_EXECUTION)
    if phase is not None and _string_value(phase.get("status")) != "missing":
        duration = _number_value(phase.get("total_duration_seconds") or phase.get("duration_seconds"))
        count = int(_number_value(phase.get("count")))
        status = _coverage_from_timing_status(_string_value(phase.get("status")))
        timing_source = _string_value(timing.get("source"))
        if timing_source == "manifest":
            metric_source = "manifest_timing"
            base_limitation = "Worker execution duration from manifest timing is incomplete for some runs."
        else:
            metric_source = "legacy_event_timing"
            base_limitation = (
                "Worker execution duration is inferred from events/created_at/updated_at for legacy runs, "
                "not from persisted manifest timing."
            )
            if status == "exact":
                status = "partial"
        limitations = []
        if status != "exact":
            limitations.append(base_limitation)
        elif metric_source == "legacy_event_timing":
            limitations.append(base_limitation)
        return StageMetric(
            category=CATEGORY_WORKER_EXECUTION,
            duration_seconds=duration,
            count=max(0, count),
            coverage=status,
            source=metric_source,
            limitations=limitations,
        )
    return _usage_duration_metric(
        category=CATEGORY_WORKER_EXECUTION,
        records=usage_records,
        role="worker",
        phases=("implement", "rework"),
        source="usage_attribution",
        missing_limitations=[
            "Worker execution duration unavailable when both manifest timing and worker usage-attribution are missing.",
        ],
    )


def _planner_review_metric(*, timing: Dict[str, Any], usage_records: Sequence[Dict[str, Any]]) -> StageMetric:
    usage_metric = _usage_duration_metric(
        category=CATEGORY_PLANNER_REVIEW,
        records=usage_records,
        role="planner",
        phase="review",
        source="usage_attribution",
        missing_limitations=[],
    )
    if usage_metric.coverage != "unavailable":
        return usage_metric

    phase = _timing_phase(timing, PHASE_PLANNER_REVIEW)
    if phase is None or _string_value(phase.get("status")) == "missing":
        return StageMetric(
            category=CATEGORY_PLANNER_REVIEW,
            duration_seconds=0.0,
            count=0,
            coverage="unavailable",
            source="none",
            limitations=[
                "Planner review split is unavailable without usage-attribution role=planner phase=review records.",
            ],
        )

    duration = _number_value(phase.get("total_duration_seconds") or phase.get("duration_seconds"))
    count = int(_number_value(phase.get("count")))
    return StageMetric(
        category=CATEGORY_PLANNER_REVIEW,
        duration_seconds=duration,
        count=max(0, count),
        coverage="partial",
        source="timing_inferred",
        limitations=[
            "Planner review duration is inferred from aggregate review phase and may include codex review overhead.",
        ],
    )


def _rework_metric(*, timing: Dict[str, Any], usage_records: Sequence[Dict[str, Any]]) -> StageMetric:
    usage_durations = _usage_durations(usage_records, role="worker", phase="rework")
    if usage_durations:
        status = "exact" if all(item[1] for item in usage_durations) else "partial"
        return StageMetric(
            category=CATEGORY_REWORK,
            duration_seconds=sum(item[0] for item in usage_durations),
            count=len(usage_durations),
            coverage=status,
            source="usage_attribution",
            limitations=(
                []
                if status == "exact"
                else ["Some worker rework usage-attribution rows are missing timestamps."]
            ),
        )

    phase = _timing_phase(timing, PHASE_WORKER_EXECUTION)
    count = max(0, int(_number_value(_dict_value(phase).get("count"))) - 1)
    if count > 0:
        return StageMetric(
            category=CATEGORY_REWORK,
            duration_seconds=0.0,
            count=count,
            coverage="partial",
            source="timing_inferred",
            limitations=["Rework duration requires worker usage-attribution phase=rework; timing-only fallback reports count."],
        )
    return StageMetric(
        category=CATEGORY_REWORK,
        duration_seconds=0.0,
        count=0,
        coverage="unavailable",
        source="none",
        limitations=["Rework telemetry unavailable when no rework loop or usage-attribution evidence exists."],
    )


def _verification_metric(*, events: Sequence[Dict[str, Any]]) -> StageMetric:
    finished_events = [event for event in events if _string_value(event.get("type")) == "verification_finished"]
    if not finished_events:
        return StageMetric(
            category=CATEGORY_VERIFICATION,
            duration_seconds=0.0,
            count=0,
            coverage="unavailable",
            source="none",
            limitations=["Verification duration needs verification_finished events."],
        )

    event_times = [(_string_value(event.get("type")), _parse_iso(_string_value(event.get("timestamp")))) for event in events]
    durations: List[Tuple[float, bool]] = []
    for finished in finished_events:
        end_ts = _parse_iso(_string_value(finished.get("timestamp")))
        if end_ts is None:
            durations.append((0.0, False))
            continue
        start_ts = _latest_preceding_time(
            events=event_times,
            end=end_ts,
            event_types={"evidence_collected", "worker_done"},
        )
        if start_ts is None:
            durations.append((0.0, False))
            continue
        delta = (end_ts - start_ts).total_seconds()
        durations.append((max(0.0, delta), True))

    covered = sum(1 for _, ok in durations if ok)
    total_duration = sum(value for value, ok in durations if ok)
    if covered == 0:
        return StageMetric(
            category=CATEGORY_VERIFICATION,
            duration_seconds=0.0,
            count=len(finished_events),
            coverage="partial",
            source="events_inferred",
            limitations=[
                "Verification finished events exist but start boundary is missing; duration is not measurable.",
            ],
        )
    coverage = "exact" if covered == len(finished_events) else "partial"
    limitations = []
    if coverage == "partial":
        limitations.append("Some verification durations are inferred without precise start boundaries.")
    return StageMetric(
        category=CATEGORY_VERIFICATION,
        duration_seconds=total_duration,
        count=len(finished_events),
        coverage=coverage,
        source="events_inferred",
        limitations=limitations,
    )


def _apply_commit_metric(*, events: Sequence[Dict[str, Any]]) -> StageMetric:
    apply_duration, apply_count, apply_partial = _event_pair_duration(
        events,
        start_event="apply_started",
        end_events={"apply_completed"},
    )
    commit_duration, commit_count, commit_partial = _event_pair_duration(
        events,
        start_event="git_commit_started",
        end_events={"git_commit_completed", "git_commit_skipped", "git_commit_failed"},
    )
    count = apply_count + commit_count
    total_duration = apply_duration + commit_duration
    if count == 0:
        return StageMetric(
            category=CATEGORY_APPLY_COMMIT,
            duration_seconds=0.0,
            count=0,
            coverage="unavailable",
            source="none",
            limitations=["Apply/commit duration requires apply_started and git_commit_started event pairs."],
        )
    coverage = "partial" if (apply_partial or commit_partial) else "exact"
    limitations: List[str] = []
    if coverage == "partial":
        limitations.append("Some apply/commit events are missing start/end boundaries.")
    return StageMetric(
        category=CATEGORY_APPLY_COMMIT,
        duration_seconds=total_duration,
        count=count,
        coverage=coverage,
        source="events",
        limitations=limitations,
    )


def _recovery_metric(*, events: Sequence[Dict[str, Any]], manifest: Dict[str, Any]) -> StageMetric:
    decision_events = [event for event in events if _string_value(event.get("type")) == "recovery_decision_recorded"]
    if not decision_events:
        return StageMetric(
            category=CATEGORY_RECOVERY,
            duration_seconds=0.0,
            count=0,
            coverage="unavailable",
            source="none",
            limitations=["Recovery duration needs recovery_decision_recorded events."],
        )

    parsed_events: List[Tuple[Optional[datetime], Dict[str, Any]]] = [
        (_parse_iso(_string_value(event.get("timestamp"))), event)
        for event in events
    ]
    run_updated_at = _parse_iso(_string_value(manifest.get("updated_at")))
    durations: List[float] = []
    infer_partial = False

    for event in decision_events:
        start = _parse_iso(_string_value(event.get("timestamp")))
        if start is None:
            infer_partial = True
            continue
        next_timestamp = _next_event_timestamp(parsed_events, start)
        if next_timestamp is None:
            if run_updated_at is not None and run_updated_at >= start:
                durations.append((run_updated_at - start).total_seconds())
                infer_partial = True
            else:
                infer_partial = True
            continue
        durations.append(max(0.0, (next_timestamp - start).total_seconds()))

    coverage = "exact"
    if infer_partial:
        coverage = "partial"
    if not durations:
        coverage = "partial"
    return StageMetric(
        category=CATEGORY_RECOVERY,
        duration_seconds=sum(durations),
        count=len(decision_events),
        coverage=coverage,
        source="events_inferred",
        limitations=[
            "Recovery duration is inferred from recovery_decision_recorded to the next observable event boundary.",
        ],
    )


def _usage_duration_metric(
    *,
    category: str,
    records: Sequence[Dict[str, Any]],
    role: str,
    phase: Optional[str] = None,
    phases: Optional[Sequence[str]] = None,
    source: str,
    missing_limitations: Sequence[str],
) -> StageMetric:
    durations = _usage_durations(records, role=role, phase=phase, phases=phases)
    if not durations:
        return StageMetric(
            category=category,
            duration_seconds=0.0,
            count=0,
            coverage="unavailable",
            source="none",
            limitations=list(missing_limitations),
        )

    measured = [duration for duration, complete in durations if complete]
    coverage = "exact" if len(measured) == len(durations) else "partial"
    limitations = []
    if coverage == "partial":
        limitations.append("Some usage-attribution rows are missing started_at or updated_at timestamps.")
    return StageMetric(
        category=category,
        duration_seconds=sum(measured),
        count=len(durations),
        coverage=coverage,
        source=source,
        limitations=limitations,
    )


def _usage_durations(
    records: Sequence[Dict[str, Any]],
    *,
    role: str,
    phase: Optional[str] = None,
    phases: Optional[Sequence[str]] = None,
) -> List[Tuple[float, bool]]:
    normalized_phases = {value for value in phases or () if value}
    if phase:
        normalized_phases.add(phase)
    durations: List[Tuple[float, bool]] = []
    for record in records:
        if not _usage_match(record, role=role, phase=phase, phases=normalized_phases):
            continue
        started_at = _parse_iso(_string_value(record.get("started_at")))
        updated_at = _parse_iso(_string_value(record.get("updated_at")))
        if started_at is None or updated_at is None:
            durations.append((0.0, False))
            continue
        durations.append((max(0.0, (updated_at - started_at).total_seconds()), True))
    return durations


def _usage_match(
    record: Dict[str, Any],
    *,
    role: str,
    phase: Optional[str] = None,
    phases: Optional[Sequence[str]] = None,
) -> bool:
    if _string_value(record.get("role")) != role:
        return False
    value = _string_value(record.get("phase"))
    if phase is not None:
        return value == phase
    if phases:
        return value in set(phases)
    return True


def _timing_phase(timing: Dict[str, Any], phase_name: str) -> Optional[Dict[str, Any]]:
    phases = _list_value(timing.get("phases"))
    for phase in phases:
        phase_dict = _dict_value(phase)
        if _string_value(phase_dict.get("phase")) == phase_name:
            return phase_dict
    return None


def _phase_count(timing: Dict[str, Any], phase_name: str) -> int:
    phase = _timing_phase(timing, phase_name)
    if phase is None:
        return 0
    return max(0, int(_number_value(phase.get("count"))))


def _event_pair_duration(
    events: Sequence[Dict[str, Any]],
    *,
    start_event: str,
    end_events: Sequence[str] | set[str],
) -> Tuple[float, int, bool]:
    end_set = set(end_events)
    start_times: List[datetime] = []
    duration = 0.0
    count = 0
    partial = False

    for event in events:
        event_type = _string_value(event.get("type"))
        timestamp = _parse_iso(_string_value(event.get("timestamp")))
        if event_type == start_event:
            if timestamp is None:
                partial = True
                continue
            start_times.append(timestamp)
            continue
        if event_type not in end_set:
            continue
        if timestamp is None:
            partial = True
            continue
        if not start_times:
            partial = True
            continue
        started = start_times.pop()
        duration += max(0.0, (timestamp - started).total_seconds())
        count += 1

    if start_times:
        partial = True
    return duration, count, partial


def _latest_preceding_time(
    *,
    events: Iterable[Tuple[Optional[str], Optional[datetime]]],
    end: datetime,
    event_types: set[str],
) -> Optional[datetime]:
    latest: Optional[datetime] = None
    for event_type, timestamp in events:
        if event_type not in event_types or timestamp is None:
            continue
        if timestamp > end:
            continue
        if latest is None or timestamp > latest:
            latest = timestamp
    return latest


def _next_event_timestamp(events: Sequence[Tuple[Optional[datetime], Dict[str, Any]]], start: datetime) -> Optional[datetime]:
    next_ts: Optional[datetime] = None
    for timestamp, _event in events:
        if timestamp is None or timestamp <= start:
            continue
        if next_ts is None or timestamp < next_ts:
            next_ts = timestamp
    return next_ts


def _coverage_from_timing_status(status: str) -> str:
    if status == "completed":
        return "exact"
    if status in {"active", "partial"}:
        return "partial"
    return "unavailable"


def _finalize_stage(aggregate: Dict[str, Any], total_runs: int) -> Dict[str, Any]:
    coverage = _dict_value(aggregate.get("coverage"))
    exact = int(coverage.get("exact", 0))
    partial = int(coverage.get("partial", 0))
    unavailable = int(coverage.get("unavailable", 0))
    if total_runs == 0:
        exact = 0
        partial = 0
        unavailable = 0

    if exact == total_runs and total_runs > 0:
        status = "complete"
    elif exact > 0 or partial > 0:
        status = "partial"
    else:
        status = "unavailable"

    count = int(aggregate.get("count", 0))
    total_duration = float(aggregate.get("total_duration_seconds", 0.0))
    avg_duration = total_duration / count if count > 0 else None

    return {
        "category": aggregate.get("category"),
        "label": aggregate.get("label"),
        "status": status,
        "count": count,
        "run_count": int(aggregate.get("run_count", 0)),
        "total_duration_seconds": total_duration,
        "avg_duration_seconds": avg_duration,
        "coverage": {
            "exact_runs": exact,
            "partial_runs": partial,
            "unavailable_runs": unavailable,
            "total_runs": total_runs,
        },
        "sources": _dict_value(aggregate.get("sources")),
        "limitations": list(aggregate.get("limitations", [])),
    }


def _project_limitations(stages: Sequence[Dict[str, Any]]) -> List[str]:
    limitations: List[str] = []
    for stage in stages:
        if _string_value(stage.get("status")) == "complete":
            continue
        label = _string_value(stage.get("label")) or _string_value(stage.get("category"))
        for limitation in _list_value(stage.get("limitations")):
            text = _string_value(limitation)
            if not text:
                continue
            _append_unique(limitations, f"{label}: {text}")
    return limitations


def _build_recent_terminal_retrospective(
    *,
    run_samples: Sequence[Dict[str, Any]],
    recent_limit: Optional[int],
) -> Dict[str, Any]:
    normalized_limit = max(1, int(recent_limit or DEFAULT_RECENT_RUN_LIMIT))
    terminal_samples = [
        sample
        for sample in run_samples
        if _string_value(_dict_value(sample.get("manifest")).get("status")) in TERMINAL_RUN_STATUSES
    ]
    ordered_samples = sorted(terminal_samples, key=_terminal_sample_sort_key, reverse=True)
    selected_samples = ordered_samples[:normalized_limit]
    sample_size = len(selected_samples)

    stage_rows: Dict[str, Dict[str, Any]] = {
        category: {
            "category": category,
            "phase": category,
            "label": label,
            "total_duration_seconds": 0.0,
            "count": 0,
            "run_count": 0,
            "coverage": {"exact_runs": 0, "partial_runs": 0, "unavailable_runs": 0, "total_runs": sample_size},
            "sources": {},
            "limitations": [],
            "status": "unavailable",
            "evidence_runs": [],
        }
        for category, label in _CATEGORIES
    }

    for sample in selected_samples:
        manifest = _dict_value(sample.get("manifest"))
        status = _string_value(manifest.get("status"))
        run_id = _string_value(manifest.get("run_id"))
        updated_at = _string_value(manifest.get("updated_at")) or _string_value(manifest.get("created_at"))
        review_rework_evidence = _dict_value(sample.get("review_rework_evidence"))
        metrics = sample.get("metrics")
        for metric in metrics if isinstance(metrics, list) else []:
            if not isinstance(metric, StageMetric):
                continue
            row = stage_rows[metric.category]
            row["total_duration_seconds"] += max(0.0, metric.duration_seconds)
            row["count"] += max(0, metric.count)
            if metric.coverage != "unavailable" and metric.count > 0:
                row["run_count"] += 1
            coverage = _dict_value(row.get("coverage"))
            coverage_key = f"{metric.coverage}_runs"
            coverage[coverage_key] = int(coverage.get(coverage_key, 0)) + 1
            row["coverage"] = coverage
            sources = _dict_value(row.get("sources"))
            sources[metric.source] = int(sources.get(metric.source, 0)) + 1
            row["sources"] = sources
            if metric.limitations:
                _extend_unique(row["limitations"], metric.limitations)

            if metric.coverage == "unavailable" and metric.count <= 0:
                continue
            row["evidence_runs"].append(
                {
                    "run_id": run_id,
                    "status": status,
                    "updated_at": updated_at,
                    "duration_seconds": max(0.0, metric.duration_seconds),
                    "count": max(0, metric.count),
                    "coverage": metric.coverage,
                    "source": metric.source,
                    "limitations": list(metric.limitations),
                    "revision_requested_count": int(review_rework_evidence.get("revision_requested_count", 0) or 0),
                    "review_retryable_count": int(review_rework_evidence.get("review_retryable_count", 0) or 0),
                    "rework_count": int(review_rework_evidence.get("rework_count", 0) or 0),
                    "recovery_decision_count": int(review_rework_evidence.get("recovery_decision_count", 0) or 0),
                    "reason_summaries": _list_value(review_rework_evidence.get("reason_summaries")),
                }
            )

    denominator_limitations: List[str] = []
    measurable_total_duration = 0.0
    for sample in selected_samples:
        wall_clock, source = _sample_wall_clock_duration_seconds(sample)
        measurable_total_duration += wall_clock
        if source != "timing_total":
            _append_unique(
                denominator_limitations,
                (
                    "Some retrospective share denominator rows fallback to non-overlapping phase totals "
                    "because run-level timing total is missing."
                ),
            )

    top_bottlenecks: List[Dict[str, Any]] = []
    for category, _label in _CATEGORIES:
        row = stage_rows[category]
        count = int(_number_value(row.get("count")))
        total_duration = float(_number_value(row.get("total_duration_seconds")))
        avg_duration = total_duration / count if count > 0 else None
        status = _coverage_status(_dict_value(row.get("coverage")), sample_size)
        row["status"] = status
        row["avg_duration_seconds"] = avg_duration
        row["share_of_sample_duration"] = (
            total_duration / measurable_total_duration if measurable_total_duration > 0 else 0.0
        )
        row["evidence_runs"] = sorted(
            _list_value(row.get("evidence_runs")),
            key=_evidence_sort_key,
            reverse=True,
        )
        if (
            category != CATEGORY_UNKNOWN
            and status != "unavailable"
            and count > 0
            and int(_number_value(row.get("run_count"))) > 0
        ):
            top_bottlenecks.append(
                {
                    "phase": category,
                    "category": category,
                    "label": row.get("label"),
                    "status": status,
                    "coverage": _dict_value(row.get("coverage")),
                    "sources": _dict_value(row.get("sources")),
                    "total_duration_seconds": total_duration,
                    "avg_duration_seconds": avg_duration,
                    "count": count,
                    "run_count": int(_number_value(row.get("run_count"))),
                    "share_of_sample_duration": row["share_of_sample_duration"],
                    "limitations": list(row.get("limitations", [])),
                    "evidence_runs": _list_value(row.get("evidence_runs")),
                }
            )

    top_bottlenecks.sort(
        key=lambda item: (
            float(_number_value(item.get("total_duration_seconds"))),
            int(_number_value(item.get("count"))),
            _string_value(item.get("category")),
        ),
        reverse=True,
    )

    return {
        "recent_limit": normalized_limit,
        "sample_size": sample_size,
        "sampled_terminal_runs": sample_size,
        "total_terminal_runs": len(terminal_samples),
        "non_terminal_runs_ignored": max(0, len(run_samples) - len(terminal_samples)),
        "share_basis": "sample_run_total",
        "limitations": denominator_limitations,
        "sample_run_ids": [
            _string_value(_dict_value(sample.get("manifest")).get("run_id"))
            for sample in selected_samples
            if _string_value(_dict_value(sample.get("manifest")).get("run_id"))
        ],
        "total_sample_duration_seconds": measurable_total_duration,
        "top_bottlenecks": top_bottlenecks[:5],
    }


def _sample_wall_clock_duration_seconds(sample: Dict[str, Any]) -> Tuple[float, str]:
    timing = _dict_value(sample.get("timing"))
    total = _timing_total_duration_seconds(timing)
    if total is not None:
        return total, "timing_total"
    return _non_overlapping_phase_total_duration_seconds(timing), "phase_fallback"


def _timing_total_duration_seconds(timing: Dict[str, Any]) -> Optional[float]:
    total = _dict_value(timing.get("total"))
    if not total:
        return None
    for key in ("total_duration_seconds", "duration_seconds"):
        raw = total.get(key)
        if isinstance(raw, (int, float)):
            return max(0.0, float(raw))
        if isinstance(raw, str):
            try:
                return max(0.0, float(raw))
            except ValueError:
                continue
    return None


def _non_overlapping_phase_total_duration_seconds(timing: Dict[str, Any]) -> float:
    target_phases = {
        PHASE_PLANNING,
        PHASE_HUMAN_PLAN_REVIEW_WAIT,
        PHASE_PLAN_REVISION,
        PHASE_WORKER_EXECUTION,
        PHASE_WORK_DONE_WAIT,
        PHASE_PLANNER_REVIEW,
        PHASE_REVISION_WAIT,
    }
    total = 0.0
    for phase in _list_value(timing.get("phases")):
        phase_dict = _dict_value(phase)
        phase_name = _string_value(phase_dict.get("phase"))
        if phase_name not in target_phases:
            continue
        if _string_value(phase_dict.get("status")) == "missing":
            continue
        duration = _number_value(phase_dict.get("total_duration_seconds") or phase_dict.get("duration_seconds"))
        total += max(0.0, duration)
    return total


def _terminal_sample_sort_key(sample: Dict[str, Any]) -> Tuple[int, float, int, str]:
    manifest = _dict_value(sample.get("manifest"))
    updated = _parse_iso(_string_value(manifest.get("updated_at")))
    created = _parse_iso(_string_value(manifest.get("created_at")))
    effective = updated or created
    index = int(_number_value(sample.get("index")))
    effective_key = effective.timestamp() if effective is not None else 0.0
    run_id = _string_value(manifest.get("run_id"))
    return (
        1 if effective is not None else 0,
        effective_key,
        index,
        run_id,
    )


def _evidence_sort_key(item: Any) -> Tuple[float, float, str]:
    payload = _dict_value(item)
    duration = float(_number_value(payload.get("duration_seconds")))
    updated_at = _parse_iso(_string_value(payload.get("updated_at")))
    updated_ts = updated_at.timestamp() if updated_at is not None else 0.0
    run_id = _string_value(payload.get("run_id"))
    return duration, updated_ts, run_id


def _run_review_rework_evidence(
    *,
    manifest: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    usage_records: Sequence[Dict[str, Any]],
    timing: Dict[str, Any],
) -> Dict[str, Any]:
    review = _dict_value(manifest.get("review"))
    review_attempts = [_dict_value(item) for item in _list_value(manifest.get("review_attempts"))]
    revision_attempts = [
        attempt
        for attempt in review_attempts
        if _string_value(attempt.get("decision")) == "revision_requested"
        or _string_value(attempt.get("status")) == "REVISION_REQUESTED"
    ]
    review_retryable_attempts = [
        attempt
        for attempt in review_attempts
        if _string_value(attempt.get("status")) == "FAILED_RETRYABLE"
    ]

    revision_requested_count = len(revision_attempts)
    if revision_requested_count == 0 and _string_value(review.get("decision")) == "revision_requested":
        revision_requested_count = 1

    review_retryable_count = len(review_retryable_attempts)
    if review_retryable_count == 0:
        review_retryable_count = sum(
            1
            for event in events
            if _string_value(event.get("type")) in {"code_review_failed", "planner_review_failed"}
        )

    rework_count = _derive_run_rework_count(timing=timing, usage_records=usage_records)
    recovery_events = [
        _dict_value(event)
        for event in events
        if _string_value(_dict_value(event).get("type")) == "recovery_decision_recorded"
    ]
    recovery_decision_count = len(recovery_events)

    reason_summaries: List[str] = []
    for attempt in revision_attempts:
        summary = _first_non_empty(
            _string_or_none(attempt.get("summary")),
            _first_sentence(_string_or_none(attempt.get("reason"))),
            _first_sentence(_string_or_none(attempt.get("error"))),
        )
        if summary:
            _append_unique(reason_summaries, f"revision_requested: {summary}")
    if revision_requested_count and not revision_attempts:
        summary = _first_non_empty(
            _string_or_none(review.get("summary")),
            _first_sentence(_string_or_none(review.get("reason"))),
        )
        if summary:
            _append_unique(reason_summaries, f"revision_requested: {summary}")

    for attempt in review_retryable_attempts:
        summary = _first_non_empty(
            _string_or_none(attempt.get("summary")),
            _first_sentence(_string_or_none(attempt.get("error"))),
            _first_sentence(_string_or_none(attempt.get("reason"))),
        )
        if summary:
            _append_unique(reason_summaries, f"review_retryable: {summary}")

    for event in recovery_events:
        summary = _first_non_empty(
            _first_sentence(_string_or_none(event.get("summary"))),
            _first_sentence(_string_or_none(event.get("reason"))),
            _first_sentence(_string_or_none(event.get("message"))),
            _first_sentence(_string_or_none(event.get("recovery_action"))),
            _first_sentence(_string_or_none(event.get("category"))),
        )
        if summary:
            _append_unique(reason_summaries, f"recovery: {summary}")

    return {
        "revision_requested_count": revision_requested_count,
        "review_retryable_count": review_retryable_count,
        "rework_count": rework_count,
        "recovery_decision_count": recovery_decision_count,
        "reason_summaries": reason_summaries[:6],
    }


def _derive_run_rework_count(*, timing: Dict[str, Any], usage_records: Sequence[Dict[str, Any]]) -> int:
    run_rework_count = max(0, _phase_count(timing, PHASE_WORKER_EXECUTION) - 1)
    if run_rework_count == 0:
        run_rework_count = sum(1 for record in usage_records if _usage_match(record, role="worker", phase="rework"))
    return run_rework_count


def _coverage_status(coverage: Dict[str, Any], total_runs: int) -> str:
    exact = int(_number_value(coverage.get("exact_runs")))
    partial = int(_number_value(coverage.get("partial_runs")))
    if total_runs > 0 and exact == total_runs:
        return "complete"
    if exact > 0 or partial > 0:
        return "partial"
    return "unavailable"


def _load_manifests(runs_dir: Path) -> List[Dict[str, Any]]:
    manifests: List[Dict[str, Any]] = []
    if not runs_dir.exists():
        return manifests
    for path in sorted(runs_dir.glob("*/manifest.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        manifests.append(RunManifest.from_dict(data).to_dict())
    return manifests


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _number_value(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _string_or_none(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_sentence(value: Optional[str]) -> Optional[str]:
    text = _string_or_none(value)
    if not text:
        return None
    match = _SENTENCE_BOUNDARY_PATTERN.search(text)
    if match is None:
        return text
    sentence = text[: match.end()].strip()
    return sentence or text


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        text = _string_or_none(value)
        if text:
            return text
    return None


def _dict_value(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any) -> List[Any]:
    return value if isinstance(value, list) else []


def _append_unique(values: List[str], item: str) -> None:
    if item and item not in values:
        values.append(item)


def _extend_unique(values: List[str], items: Sequence[str]) -> None:
    for item in items:
        _append_unique(values, _string_value(item))
