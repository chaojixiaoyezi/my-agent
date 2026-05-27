# LLM: Real run fact scan extracts machine facts from bounded JSON reports and logs.
# 模块用途: 扫描真实运行目录里的报告和日志，只提取结构化 code、ok 和 marker，不解析聊天正文。

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .real_run_review_rules import TAG_RULES

_MARKER_RE = re.compile(r"\[([A-Z][A-Z0-9_]+)\]")
_REPORT_NAME_PARTS = ("report", "acceptance", "validation", "execution")
_LOG_NAMES = {"stdout.txt", "stderr.txt", "events.jsonl"}


# LLM: CollectedFacts stores structured runtime facts for real-run review.
# 类用途: 保存扫描得到的错误码、证据引用和 ok 布尔值，不包含自然语言推断。
@dataclass(frozen=True)
class CollectedFacts:
    codes: tuple[str, ...]
    refs: tuple[str, ...]
    ok_values: tuple[bool, ...]


# LLM: ReviewScanLimits carries bounded file-read budgets for one real-run scan.
# 类用途: 保存 JSON 报告和文本日志的最大读取字节数，0 表示对应类型不读取大文件。
@dataclass(frozen=True)
class ReviewScanLimits:
    max_report_bytes: int
    max_log_bytes: int


# LLM: review_scan_limits resolves real-run review read budgets from AgentConfig.
# 函数用途: 从配置对象读取复盘扫描预算；未传配置时只使用 schema 默认。
def review_scan_limits(config: object | None) -> ReviewScanLimits:
    if config is None:
        from ..settings.config import AgentConfig

        config = AgentConfig()
    return ReviewScanLimits(
        max_report_bytes=_config_int(config, "real_run_review_max_report_bytes"),
        max_log_bytes=_config_int(config, "real_run_review_max_log_bytes"),
    )


# LLM: json_facts extracts codes and ok flags from bounded JSON reports.
# 函数用途: 扫描 run 目录中的报告 JSON，只读取机器字段并记录证据路径。
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


# LLM: marker_facts extracts bracketed runtime markers from bounded text logs.
# 函数用途: 从 stdout/stderr/events 中读取 `[CODE]` 机器标记，避免把普通日志正文当事实。
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


# LLM: _candidate_json_files chooses bounded report-like JSON files from a run directory.
# 函数用途: 找出可复盘 JSON 报告，避免把全部临时数据当合同事实。
def _candidate_json_files(root: Path, *, max_report_bytes: int) -> tuple[Path, ...]:
    paths = [
        path
        for path in root.rglob("*.json")
        if any(part in path.name for part in _REPORT_NAME_PARTS) and _size_allowed(path, max_report_bytes)
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


# LLM: _size_allowed prevents huge artifacts from being parsed as control reports.
# 函数用途: 限制复盘读取文件大小，保护本地验收速度和内存。
def _size_allowed(path: Path, limit: int) -> bool:
    try:
        return path.stat().st_size <= limit
    except OSError:
        return False


# LLM: _ordered_unique preserves first-seen order while deduplicating hashable values.
# 函数用途: 去重但保留顺序，确保扫描结果稳定。
def _ordered_unique(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


# LLM: _config_int normalizes one real-run review scan budget.
# 函数用途: 读取单个配置字段并归一为非负整数，非法值按 0 处理。
def _config_int(config: object, key: str) -> int:
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        return 0


# LLM: _rel emits stable relative refs when possible.
# 函数用途: 把证据路径写成相对引用；跨根路径时回退绝对路径。
def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


__all__ = ["CollectedFacts", "ReviewScanLimits", "json_facts", "marker_facts", "review_scan_limits"]
