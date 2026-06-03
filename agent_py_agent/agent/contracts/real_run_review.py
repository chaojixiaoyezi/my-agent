
from __future__ import annotations

from pathlib import Path

from .real_run_review_fact_scan import CollectedFacts, json_facts, marker_facts, review_scan_limits
from .real_run_review_models import FailurePattern, RealRunRecord, RealRunReview
from .real_run_review_render import render_real_run_review_markdown
from .real_run_review_rules import P0_TAGS, P1_TAGS, STAGE_BY_TAG, TAG_RULES


def review_real_run_tree(
    root: Path,
    *,
    run_glob: str = "*",
    config: object | None = None,
) -> RealRunReview:
    run_dirs = tuple(path for path in sorted(Path(root).expanduser().glob(run_glob)) if path.is_dir())
    records = tuple(
        review_real_run_directory(path, review_root=Path(root).expanduser(), config=config)
        for path in run_dirs
    )
    return RealRunReview(
        summary=_summary(records),
        records=records,
        clusters=cluster_real_run_failures(records),
    )


def review_real_run_directory(
    run_dir: Path,
    *,
    review_root: Path | None = None,
    config: object | None = None,
) -> RealRunRecord:
    root = Path(run_dir).expanduser()
    review_base = review_root or root.parent
    limits = review_scan_limits(config)
    json_report_facts = json_facts(root, max_report_bytes=limits.max_report_bytes)
    marker_report_facts = marker_facts(root, max_log_bytes=limits.max_log_bytes)
    codes = _ordered_unique(json_report_facts.codes + marker_report_facts.codes)
    tags = _tags_for_codes(codes)
    status = _final_status(json_report_facts)
    task_types = _task_types(root)
    first_code = codes[0] if codes else ""
    return RealRunRecord(
        run_id=root.name,
        run_ref=_rel(root, review_base),
        task_types=task_types,
        final_status=status,
        first_failure_code=first_code,
        last_surface_code=codes[-1] if codes else "",
        failure_stage=_failure_stage(tags),
        root_cause_tags=tags,
        priority=_priority(tags, status),
        evidence_refs=_ordered_unique(json_report_facts.refs + marker_report_facts.refs),
        has_offline_regression_test=False,
        recommended_offline_test=_recommended_test(tags, first_code),
        missing_artifact="artifact_missing" in tags,
        empty_artifact="artifact_empty" in tags,
        artifact_path_mismatch="artifact_path_mismatch" in tags,
        tool_failed="tool_failed" in tags,
        tool_param_error="tool_schema_failed" in tags,
        tool_result_format_error="tool_result_format_failed" in tags,
        repeated_tool_call="repeated_tool_blocked" in tags,
        contract_acceptance_failed=status == "FAILED" and bool(codes),
        dry_run_real_conflict="dry_run_real_conflict" in tags,
        approval_anomaly="approval_anomaly" in tags,
        invalid_state_transition="invalid_state_transition" in tags,
        context_contract_lost="context_contract_lost" in tags,
    )


def cluster_real_run_failures(records: tuple[RealRunRecord, ...]) -> tuple[FailurePattern, ...]:
    grouped: dict[str, list[RealRunRecord]] = {}
    for record in records:
        for tag in record.root_cause_tags:
            grouped.setdefault(tag, []).append(record)
    patterns = tuple(_cluster_payload(tag, rows) for tag, rows in grouped.items())
    return tuple(sorted(patterns, key=lambda item: (_priority_rank(item.priority), -item.count, item.tag)))


def _final_status(facts: CollectedFacts) -> str:
    if any(value is False for value in facts.ok_values):
        return "FAILED"
    if facts.ok_values and all(value is True for value in facts.ok_values):
        return "PASSED"
    return "UNKNOWN"


def _tags_for_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    tags: list[str] = []
    for tag, prefixes in TAG_RULES:
        if any(code.startswith(prefix) for code in codes for prefix in prefixes):
            tags.append(tag)
    return tuple(tags)


def _task_types(root: Path) -> tuple[str, ...]:
    values: list[str] = []
    for path in root.rglob("tasks/*"):
        if path.is_dir() and path.parent.name == "tasks":
            values.append(path.name)
    return _ordered_unique(values) or (root.name,)


def _failure_stage(tags: tuple[str, ...]) -> str:
    for tag in tags:
        stage = STAGE_BY_TAG.get(tag)
        if stage:
            return stage
    return "none"


def _priority(tags: tuple[str, ...], status: str) -> str:
    if any(tag in P0_TAGS for tag in tags):
        return "P0"
    if any(tag in P1_TAGS for tag in tags):
        return "P1"
    return "P2" if status == "FAILED" else "NONE"


def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "NONE": 3}.get(priority, 4)


def _recommended_test(tags: tuple[str, ...], first_code: str) -> str:
    if not tags:
        return ""
    tag = tags[0]
    return f"pytest://agent_py_agent/tests/replay/{tag}#{first_code or tag}"


def _cluster_payload(tag: str, records: list[RealRunRecord]) -> FailurePattern:
    priority = _priority((tag,), "FAILED")
    return FailurePattern(
        tag=tag,
        count=len(records),
        priority=priority,
        run_ids=tuple(record.run_id for record in records),
        first_failure_codes=_ordered_unique(
            record.first_failure_code for record in records if record.first_failure_code
        ),
    )


def _summary(records: tuple[RealRunRecord, ...]) -> dict[str, int]:
    failed = sum(1 for record in records if record.final_status == "FAILED")
    passed = sum(1 for record in records if record.final_status == "PASSED")
    unknown = sum(1 for record in records if record.final_status == "UNKNOWN")
    return {"failed": failed, "passed": passed, "total": len(records), "unknown": unknown}


def _ordered_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


__all__ = [
    "FailurePattern",
    "RealRunRecord",
    "RealRunReview",
    "cluster_real_run_failures",
    "render_real_run_review_markdown",
    "review_real_run_directory",
    "review_real_run_tree",
]
