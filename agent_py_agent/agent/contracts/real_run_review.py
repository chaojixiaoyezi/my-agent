# LLM: Real run review turns live-task folders into structured failure samples before more live retries.
# 模块用途: 只读取报告、日志标记和文件事实，聚类真实运行失败模式，给离线回归队列和最终复盘报告使用。

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .real_run_review_models import FailurePattern, RealRunRecord, RealRunReview
from .real_run_review_render import render_real_run_review_markdown
from .real_run_review_rules import P0_TAGS, P1_TAGS, STAGE_BY_TAG, TAG_RULES

_MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_]+)\]")
_REPORT_NAME_PARTS = ("report", "acceptance", "validation", "execution")
_LOG_NAMES = {"stdout.txt", "stderr.txt", "events.jsonl"}
_MAX_REPORT_BYTES = 5_000_000
_MAX_LOG_BYTES = 1_000_000

# LLM: review_real_run_tree scans one root for run directories and returns structured review facts.
# 函数用途: 复盘某个真实运行根目录下的 run；只读文件，不启动模型、不调用真实工具。
def review_real_run_tree(root: Path, *, run_glob: str = "*") -> RealRunReview:
    run_dirs = tuple(path for path in sorted(Path(root).expanduser().glob(run_glob)) if path.is_dir())
    records = tuple(review_real_run_directory(path, review_root=Path(root).expanduser()) for path in run_dirs)
    return RealRunReview(
        summary=_summary(records),
        records=records,
        clusters=cluster_real_run_failures(records),
    )


# LLM: review_real_run_directory reads reports and logs from one run directory.
# 函数用途: 把单个真实运行目录规整成一行复盘记录，所有结论来自结构化报告和机器标记。
def review_real_run_directory(run_dir: Path, *, review_root: Path | None = None) -> RealRunRecord:
    root = Path(run_dir).expanduser()
    review_base = review_root or root.parent
    json_facts = _json_facts(root)
    marker_facts = _marker_facts(root)
    codes = _ordered_unique(json_facts.codes + marker_facts.codes)
    tags = _tags_for_codes(codes)
    status = _final_status(json_facts)
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
        evidence_refs=_ordered_unique(json_facts.refs + marker_facts.refs),
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


# LLM: cluster_real_run_failures groups records by machine tags and sorts by severity/frequency.
# 函数用途: 生成失败模式聚类统计；不按目录名写专项逻辑。
def cluster_real_run_failures(records: tuple[RealRunRecord, ...]) -> tuple[FailurePattern, ...]:
    grouped: dict[str, list[RealRunRecord]] = {}
    for record in records:
        for tag in record.root_cause_tags:
            grouped.setdefault(tag, []).append(record)
    patterns = tuple(_cluster_payload(tag, rows) for tag, rows in grouped.items())
    return tuple(sorted(patterns, key=lambda item: (_priority_rank(item.priority), -item.count, item.tag)))


# LLM: _CollectedFacts stores structured runtime facts for the surrounding contract logic.
# 类用途: 保存当前模块使用的结构化字段，避免后续流程从普通自然语言推断机器事实。
@dataclass(frozen=True)
class _CollectedFacts:
    codes: tuple[str, ...]
    refs: tuple[str, ...]
    ok_values: tuple[bool, ...]


# LLM: _json_facts extracts codes and ok flags from bounded JSON reports.
# 函数用途: 扫描 run 目录中的报告 JSON，只读取机器字段并记录证据路径。
def _json_facts(root: Path) -> _CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    ok_values: list[bool] = []
    for path in _candidate_json_files(root):
        payload = _read_json(path)
        if payload is None:
            continue
        path_codes = _codes_from_payload(payload)
        path_ok_values = _ok_values(payload)
        if path_codes or path_ok_values:
            refs.append(_rel(path, root))
        codes.extend(path_codes)
        ok_values.extend(path_ok_values)
    return _CollectedFacts(
        codes=_ordered_unique(codes),
        refs=tuple(refs),
        ok_values=tuple(ok_values),
    )


# LLM: _marker_facts extracts bracketed runtime markers from bounded text logs.
# 函数用途: 从 stdout/stderr/events 中读取 `[CODE]` 机器标记，避免把普通日志正文当事实。
def _marker_facts(root: Path) -> _CollectedFacts:
    codes: list[str] = []
    refs: list[str] = []
    for path in _candidate_log_files(root):
        if not _size_allowed(path, _MAX_LOG_BYTES):
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        path_codes = [code for code in _MARKER_RE.findall(content) if _is_failure_marker(code)]
        if path_codes:
            refs.append(_rel(path, root))
            codes.extend(path_codes)
    return _CollectedFacts(codes=_ordered_unique(codes), refs=tuple(refs), ok_values=())


# LLM: _candidate_json_files chooses bounded report-like JSON files from a run directory.
# 函数用途: 找出可复盘 JSON 报告，避免把全部临时数据当合同事实。
def _candidate_json_files(root: Path) -> tuple[Path, ...]:
    paths = [
        path
        for path in root.rglob("*.json")
        if any(part in path.name for part in _REPORT_NAME_PARTS) and _size_allowed(path, _MAX_REPORT_BYTES)
    ]
    return tuple(sorted(paths))


# LLM: _candidate_log_files chooses stdout/stderr/events ledgers that may contain machine markers.
# 函数用途: 找出可复盘文本日志，只用于 bracketed marker 提取。
def _candidate_log_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in root.rglob("*") if path.is_file() and path.name in _LOG_NAMES))


# LLM: _read_json returns None for malformed reports instead of crashing the whole review.
# 函数用途: 读取单个 JSON 报告；损坏报告由缺失 evidence 体现，不让复盘中断。
def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


# LLM: _codes_from_payload recursively reads known machine-code fields only.
# 函数用途: 从报告结构中提取 code/error_code/issues 等机器字段，不解析 message/detail 文案。
def _codes_from_payload(payload: Any) -> tuple[str, ...]:
    codes: list[str] = []
    _collect_codes(payload, codes)
    return tuple(codes)


# LLM: _collect_codes walks JSON values looking for stable error-code fields.
# 函数用途: 递归收集结构化错误码，保留出现顺序用于 first_failure_code。
def _collect_codes(value: Any, codes: list[str]) -> None:
    if isinstance(value, list):
        _collect_codes_from_list(value, codes)
        return
    if not isinstance(value, dict):
        return
    _collect_code_fields(value, codes)
    _collect_codes_from_list(list(value.values()), codes)


# LLM: _collect_code_fields extracts code carriers from one JSON object.
# 函数用途: 只读明确 code 字段，避免递归函数自身过深。
def _collect_code_fields(value: dict[str, Any], codes: list[str]) -> None:
    for key in ("code", "error_code"):
        _append_code_value(value.get(key), codes)
    for key in ("error_codes", "issues", "warning_codes", "blocker_codes"):
        _append_code_value(value.get(key), codes)


# LLM: _collect_codes_from_list applies code extraction to a flat sequence.
# 函数用途: 把 list/object 递归拆成浅层 helper，降低合同检查的嵌套深度。
def _collect_codes_from_list(values: list[Any], codes: list[str]) -> None:
    for child in values:
        _collect_codes(child, codes)


# LLM: _append_code_value accepts scalar and list code carriers.
# 函数用途: 把结构化错误码字段规整为大写 code 字符串。
def _append_code_value(value: Any, codes: list[str]) -> None:
    if isinstance(value, str) and _looks_like_code(value):
        codes.append(value)
    if isinstance(value, list):
        for item in value:
            _append_code_value(item, codes)


# LLM: _looks_like_code filters out ordinary prose before a value becomes a machine fact.
# 函数用途: 只接受稳定 code 形态，防止误把展示文案当运行事实。
def _looks_like_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", value.strip()))


# LLM: _is_failure_marker accepts only runtime markers that participate in failure clustering.
# 函数用途: 过滤普通工具事件标记，防止 `[TOOL_CALL]` 这类进度事件变成失败事实。
def _is_failure_marker(code: str) -> bool:
    return any(code.startswith(prefix) for _, prefixes in TAG_RULES for prefix in prefixes)


# LLM: _ok_values reads boolean ok fields from nested reports.
# 函数用途: 提取验收/报告中的 ok=true/false，供 final_status 计算。
def _ok_values(payload: Any) -> tuple[bool, ...]:
    values: list[bool] = []
    _collect_ok_values(payload, values)
    return tuple(values)


# LLM: _collect_ok_values recursively extracts boolean ok values.
# 函数用途: 遍历报告树，记录所有布尔 ok 字段。
def _collect_ok_values(value: Any, values: list[bool]) -> None:
    if isinstance(value, list):
        _collect_ok_values_from_list(value, values)
        return
    if not isinstance(value, dict):
        return
    if isinstance(value.get("ok"), bool):
        values.append(value["ok"])
    _collect_ok_values_from_list(list(value.values()), values)


# LLM: _collect_ok_values_from_list applies ok extraction to a flat sequence.
# 函数用途: 拆分递归遍历，避免状态提取 helper 自身出现深嵌套。
def _collect_ok_values_from_list(items: list[Any], values: list[bool]) -> None:
    for child in items:
        _collect_ok_values(child, values)


# LLM: _final_status derives run status from report ok booleans only.
# 函数用途: 把报告中的 ok 字段转成 PASSED/FAILED/UNKNOWN，避免读最终聊天总结。
def _final_status(facts: _CollectedFacts) -> str:
    if any(value is False for value in facts.ok_values):
        return "FAILED"
    if facts.ok_values and all(value is True for value in facts.ok_values):
        return "PASSED"
    return "UNKNOWN"


# LLM: _tags_for_codes maps machine codes to generic failure tags.
# 函数用途: 根据稳定 code 前缀归类失败模式，所有规则都是通用合同层标签。
def _tags_for_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    tags: list[str] = []
    for tag, prefixes in TAG_RULES:
        if any(code.startswith(prefix) for code in codes for prefix in prefixes):
            tags.append(tag)
    return tuple(tags)


# LLM: _task_types extracts structured case ids from task directories when present.
# 函数用途: 从 `tasks/<case_id>` 路径提取任务类型；没有则回退到 run 目录名。
def _task_types(root: Path) -> tuple[str, ...]:
    values: list[str] = []
    for path in root.rglob("tasks/*"):
        if path.is_dir() and path.parent.name == "tasks":
            values.append(path.name)
    return _ordered_unique(values) or (root.name,)


# LLM: _failure_stage chooses one broad failing layer from generic root-cause tags.
# 函数用途: 给复盘表提供 plan/tool/artifact/acceptance/state 等稳定层级。
def _failure_stage(tags: tuple[str, ...]) -> str:
    for tag in tags:
        stage = STAGE_BY_TAG.get(tag)
        if stage:
            return stage
    return "none"


# LLM: _priority converts root-cause tags into the P0/P1/P2 repair queue.
# 函数用途: 标记修复优先级；假成功/状态严重错优先 P0，主链路阻断为 P1。
def _priority(tags: tuple[str, ...], status: str) -> str:
    if any(tag in P0_TAGS for tag in tags):
        return "P0"
    if any(tag in P1_TAGS for tag in tags):
        return "P1"
    return "P2" if status == "FAILED" else "NONE"


# LLM: _priority_rank gives deterministic ordering to cluster severities.
# 函数用途: 聚类排序时让 P0 在前、P1 次之、低风险最后。
def _priority_rank(priority: str) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "NONE": 3}.get(priority, 4)


# LLM: _recommended_test names the offline regression surface without task-specific branching.
# 函数用途: 根据失败标签建议新增哪类离线测试，用于报告，不直接改变运行逻辑。
def _recommended_test(tags: tuple[str, ...], first_code: str) -> str:
    if not tags:
        return ""
    tag = tags[0]
    return f"pytest://agent_py_agent/tests/replay/{tag}#{first_code or tag}"


# LLM: _cluster_payload builds one FailurePattern from records sharing a tag.
# 函数用途: 生成单个失败模式的 count、runs 和首个错误码集合。
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


# LLM: _summary counts record statuses for the top-level report.
# 函数用途: 汇总 total/passed/failed/unknown，供脚本和最终说明使用。
def _summary(records: tuple[RealRunRecord, ...]) -> dict[str, int]:
    failed = sum(1 for record in records if record.final_status == "FAILED")
    passed = sum(1 for record in records if record.final_status == "PASSED")
    unknown = sum(1 for record in records if record.final_status == "UNKNOWN")
    return {"failed": failed, "passed": passed, "total": len(records), "unknown": unknown}


# LLM: _ordered_unique preserves first-seen order while deduplicating hashable values.
# 函数用途: 去重但保留顺序，确保 first_failure_code 和报告展示稳定。
def _ordered_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


# LLM: _size_allowed prevents huge artifacts from being parsed as control reports.
# 函数用途: 限制复盘读取文件大小，保护本地验收速度和内存。
def _size_allowed(path: Path, limit: int) -> bool:
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


# LLM: _rel emits stable relative refs when possible.
# 函数用途: 把证据路径写成相对引用；跨根路径时回退绝对路径。
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
