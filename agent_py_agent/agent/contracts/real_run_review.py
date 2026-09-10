# Real-run review data model
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RealRunRecord:
    run_id: str
    run_ref: str
    task_types: tuple[str, ...]
    final_status: str
    first_failure_code: str
    last_surface_code: str
    failure_stage: str
    root_cause_tags: tuple[str, ...]
    priority: str
    evidence_refs: tuple[str, ...]
    has_offline_regression_test: bool
    recommended_offline_test: str
    missing_artifact: bool = False
    empty_artifact: bool = False
    artifact_path_mismatch: bool = False
    tool_failed: bool = False
    tool_param_error: bool = False
    tool_result_format_error: bool = False
    repeated_tool_call: bool = False
    contract_acceptance_failed: bool = False
    dry_run_real_conflict: bool = False
    approval_anomaly: bool = False
    invalid_state_transition: bool = False
    context_contract_lost: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "run_ref": self.run_ref,
            "task_types": list(self.task_types),
            "final_status": self.final_status,
            "first_failure_code": self.first_failure_code,
            "last_surface_code": self.last_surface_code,
            "failure_stage": self.failure_stage,
            "root_cause_tags": list(self.root_cause_tags),
            "priority": self.priority,
            "evidence_refs": list(self.evidence_refs),
            "has_offline_regression_test": self.has_offline_regression_test,
            "recommended_offline_test": self.recommended_offline_test,
            "missing_artifact": self.missing_artifact,
            "empty_artifact": self.empty_artifact,
            "artifact_path_mismatch": self.artifact_path_mismatch,
            "tool_failed": self.tool_failed,
            "tool_param_error": self.tool_param_error,
            "tool_result_format_error": self.tool_result_format_error,
            "repeated_tool_call": self.repeated_tool_call,
            "contract_acceptance_failed": self.contract_acceptance_failed,
            "dry_run_real_conflict": self.dry_run_real_conflict,
            "approval_anomaly": self.approval_anomaly,
            "invalid_state_transition": self.invalid_state_transition,
            "context_contract_lost": self.context_contract_lost,
        }


@dataclass(frozen=True)
class FailurePattern:
    tag: str
    count: int
    priority: str
    run_ids: tuple[str, ...]
    first_failure_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "tag": self.tag,
            "count": self.count,
            "priority": self.priority,
            "run_ids": list(self.run_ids),
            "first_failure_codes": list(self.first_failure_codes),
        }


@dataclass(frozen=True)
class RealRunReview:
    summary: dict[str, int]
    records: tuple[RealRunRecord, ...]
    clusters: tuple[FailurePattern, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": dict(self.summary),
            "records": [record.to_dict() for record in self.records],
            "clusters": [cluster.to_dict() for cluster in self.clusters],
        }


__all__ = ["FailurePattern", "RealRunRecord", "RealRunReview"]

# Real-run review classification rules
TAG_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("artifact_missing", ("ARTIFACT_MISSING", "STAGED_ARTIFACT_MISSING")),
    ("artifact_empty", ("ARTIFACT_EMPTY",)),
    (
        "artifact_path_mismatch",
        ("ARTIFACT_PATH_MISMATCH", "ARTIFACT_OUT_OF_BOUNDS", "ARTIFACT_PATH_OUTSIDE_WORKSPACE"),
    ),
    ("staged_checkpoint_empty", ("STAGED_JSON_NO_ROWS",)),
    (
        "structured_columns_missing",
        (
            "XLSX_MISSING_REQUIRED_COLUMNS",
            "XLSX_REQUIRED_COLUMN_EMPTY_VALUES",
            "STAGED_JSON_REQUIRED_COLUMNS_MISSING",
            "STAGED_JSON_REQUIRED_COLUMN_EMPTY_VALUES",
        ),
    ),
    (
        "evidence_claims_missing",
        ("EVIDENCE_REQUIRED_FIELD_MISSING", "EVIDENCE_SOURCE_MISSING", "EVIDENCE_CLAIM_UNSOURCED"),
    ),
    ("static_site_contract_failed", ("STATIC_SITE_",)),
    ("tool_failed", ("TOOL_FAILED", "TOOL_RESULT_FAILED", "TOOL_TRACE_ERROR_CODE_MISSING")),
    ("tool_schema_failed", ("TOOL_SCHEMA", "TOOL_ARGUMENT", "UNKNOWN_TOOL")),
    ("tool_result_format_failed", ("TOOL_RESULT_VALIDATION", "TOOL_RETURN_FORMAT")),
    ("repeated_tool_blocked", ("REPEATED_TOOL", "NO_PROGRESS_REPEATED_TOOL")),
    ("dry_run_real_conflict", ("DRY_RUN_CLAIMED_REAL", "REAL_RUN_IN_DRY_RUN")),
    ("approval_anomaly", ("APPROVAL_",)),
    ("invalid_state_transition", ("STATE_TRANSITION_INVALID",)),
    ("context_contract_lost", ("CONTEXT_BUNDLE", "COMPACT_REF_MISSING", "CONTRACT_HASH_MISMATCH")),
    ("local_progress_blocked", ("LOCAL_PROGRESS_GUARD_BLOCKED",)),
)

STAGE_BY_TAG = {
    "artifact_missing": "artifact",
    "artifact_empty": "artifact",
    "artifact_path_mismatch": "artifact",
    "staged_checkpoint_empty": "artifact",
    "structured_columns_missing": "acceptance",
    "evidence_claims_missing": "acceptance",
    "static_site_contract_failed": "acceptance",
    "tool_failed": "tool",
    "tool_schema_failed": "tool",
    "tool_result_format_failed": "tool",
    "repeated_tool_blocked": "loop",
    "dry_run_real_conflict": "approval",
    "approval_anomaly": "approval",
    "invalid_state_transition": "state",
    "context_contract_lost": "context",
    "local_progress_blocked": "loop",
}

P0_TAGS = {"dry_run_real_conflict", "invalid_state_transition"}
P1_TAGS = {
    "artifact_missing",
    "artifact_empty",
    "artifact_path_mismatch",
    "staged_checkpoint_empty",
    "structured_columns_missing",
    "evidence_claims_missing",
    "static_site_contract_failed",
    "tool_failed",
    "tool_schema_failed",
    "tool_result_format_failed",
    "repeated_tool_blocked",
    "approval_anomaly",
    "context_contract_lost",
    "local_progress_blocked",
}

__all__ = ["P0_TAGS", "P1_TAGS", "STAGE_BY_TAG", "TAG_RULES"]

# Real-run review fact scan
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings.defaults import default_agent_config

_MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_]+)\]")
_REPORT_NAME_PARTS = ("report", "acceptance", "validation", "execution")
_LOG_NAMES = {"stdout.txt", "stderr.txt", "events.jsonl"}


@dataclass(frozen=True)
class CollectedFacts:
    codes: tuple[str, ...]
    refs: tuple[str, ...]
    ok_values: tuple[bool, ...]


@dataclass(frozen=True)
class ReviewScanLimits:
    max_report_bytes: int
    max_log_bytes: int


def review_scan_limits(config: object | None) -> ReviewScanLimits:
    if config is None:
        config = default_agent_config()
    return ReviewScanLimits(
        max_report_bytes=_config_int(config, "real_run_review_max_report_bytes"),
        max_log_bytes=_config_int(config, "real_run_review_max_log_bytes"),
    )


def json_facts(root: Path, *, max_report_bytes: int) -> CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    ok_values: list[bool] = []
    for path in _candidate_json_files(root, max_report_bytes=max_report_bytes):
        payload = _read_json(path)
        if payload is None:
            continue
        path_codes = _codes_from_payload(payload)
        path_ok_values = _ok_values(payload)
        if path_codes or path_ok_values:
            refs.append(_rel(path, root))
        codes.extend(path_codes)
        ok_values.extend(path_ok_values)
    return CollectedFacts(
        codes=_ordered_unique(codes),
        refs=tuple(refs),
        ok_values=tuple(ok_values),
    )


def marker_facts(root: Path, *, max_log_bytes: int) -> CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    for path in _candidate_log_files(root):
        if not _size_allowed(path, max_log_bytes):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        path_codes = [code for code in _MARKER_RE.findall(content) if _is_failure_marker(code)]
        if path_codes:
            refs.append(_rel(path, root))
            codes.extend(path_codes)
    return CollectedFacts(codes=_ordered_unique(codes), refs=tuple(refs), ok_values=())


def _candidate_json_files(root: Path, *, max_report_bytes: int) -> tuple[Path, ...]:
    paths = [
        path
        for path in root.rglob("*.json")
        if any(part in path.name for part in _REPORT_NAME_PARTS) and _size_allowed(path, max_report_bytes)
    ]
    return tuple(sorted(paths))


def _candidate_log_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file() and path.name in _LOG_NAMES))


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def _codes_from_payload(payload: Any) -> tuple[str, ...]:
    codes: list[str] = []
    _collect_codes(payload, codes)
    return tuple(codes)


def _collect_codes(value: Any, codes: list[str]) -> None:
    if isinstance(value, list):
        _collect_codes_from_list(value, codes)
        return
    if not isinstance(value, dict):
        return
    _collect_code_fields(value, codes)
    _collect_codes_from_list(list(value.values()), codes)


def _collect_code_fields(value: dict[str, Any], codes: list[str]) -> None:
    for key in ("code", "error_code"):
        _append_code_value(value.get(key), codes)
    for key in ("error_codes", "issues", "warning_codes", "blocker_codes"):
        _append_code_value(value.get(key), codes)


def _collect_codes_from_list(values: list[Any], codes: list[str]) -> None:
    for child in values:
        _collect_codes(child, codes)


def _append_code_value(value: Any, codes: list[str]) -> None:
    if isinstance(value, str) and _looks_like_code(value):
        codes.append(value)
    if isinstance(value, list):
        for item in value:
            _append_code_value(item, codes)


def _looks_like_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", value.strip()))


def _is_failure_marker(code: str) -> bool:
    return any(code.startswith(prefix) for _, prefixes in TAG_RULES for prefix in prefixes)


def _ok_values(payload: Any) -> tuple[bool, ...]:
    values: list[bool] = []
    _collect_ok_values(payload, values)
    return tuple(values)


def _collect_ok_values(value: Any, values: list[bool]) -> None:
    if isinstance(value, list):
        _collect_ok_values_from_list(value, values)
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("ok"), bool):
        values.append(value["ok"])
    _collect_ok_values_from_list(list(value.values()), values)


def _collect_ok_values_from_list(items: list[Any], values: list[bool]) -> None:
    for child in items:
        _collect_ok_values(child, values)


def _size_allowed(path: Path, limit: int) -> bool:
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


def _ordered_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _config_int(config: object, key: str) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return 0


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


__all__ = ["CollectedFacts", "ReviewScanLimits", "json_facts", "marker_facts", "review_scan_limits"]

# Real-run review rendering
def render_real_run_review_markdown(review: RealRunReview) -> str:
    lines = [
        "# 真实运行复盘报告",
        "",
        "## 摘要",
        "",
        f"- total: {review.summary['total']}",
        f"- passed: {review.summary['passed']}",
        f"- failed: {review.summary['failed']}",
        f"- unknown: {review.summary['unknown']}",
        "",
        "## 失败模式聚类",
        "",
        "| tag | priority | count | runs | first_failure_codes |",
        "|---|---:|---:|---|---|",
    ]
    for cluster in review.clusters:
        lines.append(_cluster_row(cluster))
    lines.extend(_record_table_header())
    for record in review.records:
        lines.append(
            f"| {record.run_id} | {record.final_status} | {record.failure_stage} | "
            f"{record.first_failure_code} | {', '.join(record.root_cause_tags)} | "
            f"{', '.join(record.evidence_refs[:3])} |"
        )
    lines.append("")
    return "\n".join(lines)


def _cluster_row(cluster) -> str:
    return (
        f"| {cluster.tag} | {cluster.priority} | {cluster.count} | "
        f"{', '.join(cluster.run_ids)} | {', '.join(cluster.first_failure_codes)} |"
    )


def _record_table_header() -> list[str]:
    return [
        "",
        "## 每轮摘要",
        "",
        "| run_id | status | stage | first_failure | tags | evidence_refs |",
        "|---|---|---|---|---|---|",
    ]


__all__ = ["render_real_run_review_markdown"]

# Real-run review entrypoints


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