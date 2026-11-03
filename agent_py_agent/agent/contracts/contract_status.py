
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..common.bounded_walk import bounded_walk
from ..common.value_parsing import text_value as _text

# 参数减量第 3 批 E 组：合同状态扫描预算不再是配置项（原 contract_status_* 三项，值不变）；
# 请求里显式给的 limit/max_files/max_file_bytes（命令行 --limit、--max-files）仍优先。
CONTRACT_STATUS_RECENT_FINDINGS_LIMIT = 20
CONTRACT_STATUS_MAX_SCAN_FILES = 1000
CONTRACT_STATUS_MAX_REPORT_BYTES = 2_000_000


@dataclass(frozen=True)
class ContractStatusReport:
    root: str
    scanned_files: int
    skipped_files: int
    files_with_findings: int
    finding_count: int
    by_code: dict[str, int]
    by_severity: dict[str, int]
    recent_findings: tuple[dict[str, object], ...]
    incomplete: bool = field(default=False, kw_only=True)
    coverage: dict[str, object] = field(default_factory=dict, kw_only=True)

    # LLM: 零 finding 只在覆盖完整时才能表示通过，保留截断的不确定性。
    # 函数用途: 判断本次合同扫描是否完整且没有发现问题。
    @property
    def ok(self) -> bool:
        return self.finding_count == 0 and not self.incomplete

    # LLM: 保留原有统计字段，新增覆盖事实供 CLI 和上层验收使用。
    # 函数用途: 把扫描统计和不完整标记转成可序列化报告。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "root": self.root,
            "scanned_files": self.scanned_files,
            "skipped_files": self.skipped_files,
            "files_with_findings": self.files_with_findings,
            "finding_count": self.finding_count,
            "by_code": dict(self.by_code),
            "by_severity": dict(self.by_severity),
            "recent_findings": [dict(item) for item in self.recent_findings],
            "incomplete": self.incomplete,
            "coverage": dict(self.coverage),
        }


@dataclass
class _StatusAccumulator:
    by_code: Counter[str]
    by_severity: Counter[str]
    recent: list[dict[str, object]]
    limit: int


# LLM: 请求只带显式覆盖值，None 表示用本模块的扫描预算常量；原 config 字段随参数减量第 3 批 E 组删除，不再读配置。
# 类用途: 一次 `contracts status` 扫描可选的条数、文件数与单文件字节上限。
@dataclass(frozen=True)
class ContractStatusScanRequest:
    limit: int | None = None
    max_files: int | None = None
    max_file_bytes: int | None = None


# LLM: 扫描只读且有界；无 finding 不代表截断后仍可声称完整通过。
# 函数用途: 汇总合同问题并同时公开本次扫描的覆盖缺口。
def summarize_contract_status(
    root: Path,
    request: ContractStatusScanRequest | None = None,
) -> ContractStatusReport:
    base = root.expanduser().resolve(strict=False)
    scan = request or ContractStatusScanRequest()
    resolved = _status_scan_limits(scan)
    accumulator = _StatusAccumulator(Counter(), Counter(), [], resolved.limit)
    scanned = 0
    skipped = 0
    files_with_findings = 0
    coverage: dict[str, object] = {}
    for path in _json_paths(base, max_files=resolved.max_files, coverage=coverage):
        findings, was_skipped = _findings_from_file(path, resolved.max_file_bytes)
        if was_skipped:
            skipped += 1
            continue
        scanned += 1
        if not findings:
            continue
        files_with_findings += 1
        _tally_findings(path, findings, accumulator)
    return ContractStatusReport(
        root=str(base),
        scanned_files=scanned,
        skipped_files=skipped,
        files_with_findings=files_with_findings,
        finding_count=sum(accumulator.by_code.values()),
        by_code=dict(sorted(accumulator.by_code.items())),
        by_severity=dict(sorted(accumulator.by_severity.items())),
        recent_findings=tuple(accumulator.recent),
        incomplete=bool(coverage.get("truncated")),
        coverage=coverage,
    )


@dataclass(frozen=True)
class _StatusScanLimits:
    limit: int
    max_files: int
    max_file_bytes: int


# LLM: 请求里显式给的值优先，没给就用本模块的扫描预算常量；非数字按 0 处理（与原口径一致）。只读。
# 函数用途: 算出一次合同状态扫描实际用的最近 finding 条数、扫描文件数和单文件字节上限。
def _status_scan_limits(request: ContractStatusScanRequest) -> _StatusScanLimits:
    return _StatusScanLimits(
        limit=_provided_or_default_int(request.limit, CONTRACT_STATUS_RECENT_FINDINGS_LIMIT),
        max_files=_provided_or_default_int(request.max_files, CONTRACT_STATUS_MAX_SCAN_FILES),
        max_file_bytes=_provided_or_default_int(request.max_file_bytes, CONTRACT_STATUS_MAX_REPORT_BYTES),
    )


# LLM: None 表示请求没给，用常量；负数夹到 0，非数字按 0。纯函数。
# 函数用途: 取请求给的整数，没给就用默认常量。
def _provided_or_default_int(value: int | None, default: object) -> int:
    source = default if value is None else value
    try:
        return max(0, int(source))
    except (TypeError, ValueError):
        return 0


# LLM: 最新排序只针对有界候选；coverage 接收截断/选择上限，不改变旧 list 返回形状。
# 函数用途: 在进入目录前剪枝，再选出本次预算内最近的 JSON。
def _json_paths(root: Path, *, max_files: int, coverage: dict[str, object] | None = None) -> list[Path]:
    if max_files <= 0:
        if coverage is not None:
            coverage.update(truncated=True, stop_reason="max_files", visited_entries=0, visited_dirs=0)
        return []
    if root.is_file():
        return [root] if root.suffix.lower() == ".json" else []
    if not root.exists():
        return []
    result = bounded_walk(root, include=lambda entry: entry.name.endswith(".json") and entry.is_file(follow_symlinks=False))
    paths = list(result.paths)
    paths.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    if coverage is not None:
        coverage.update(result.coverage, truncated=result.truncated or len(paths) > max_files,
                        selection_limited=len(paths) > max_files)
    return paths[:max(0, max_files)]


def _file_too_large(path: Path, max_file_bytes: int) -> bool:
    try:
        return path.stat().st_size > max_file_bytes
    except OSError:
        return True


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _findings_from_file(path: Path, max_file_bytes: int) -> tuple[list[dict[str, Any]], bool]:
    if _file_too_large(path, max_file_bytes):
        return [], True
    payload = _read_json(path)
    return (_findings(payload), False) if payload is not None else ([], True)


def _tally_findings(
    path: Path,
    findings: list[dict[str, Any]],
    accumulator: _StatusAccumulator,
) -> None:
    for finding in findings:
        code = _text(finding.get("code")) or "CONTRACT_FINDING"
        severity = _text(finding.get("severity")) or "unknown"
        accumulator.by_code[code] += 1
        accumulator.by_severity[severity] += 1
        if len(accumulator.recent) < accumulator.limit:
            accumulator.recent.append(_recent_finding(path, finding, code, severity))


def _findings(payload: object) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    stack: list[tuple[object, int]] = [(payload, 0)]
    while stack:
        value, depth = stack.pop()
        if depth > 8:
            continue
        findings.extend(_direct_findings(value))
        stack.extend((child, depth + 1) for child in _child_values(value))
    return findings


def _direct_findings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        return []
    return [dict(item) for item in value["findings"] if isinstance(item, dict) and _text(item.get("code"))]


def _child_values(value: object) -> list[object]:
    if isinstance(value, dict):
        return [item for key, item in value.items() if key != "findings" and isinstance(item, (dict, list))]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, (dict, list))]
    return []


def _recent_finding(path: Path, finding: dict[str, Any], code: str, severity: str) -> dict[str, object]:
    return {
        "file": str(path),
        "code": code,
        "severity": severity,
        **_optional_field(finding, "stage_ref"),
        **_optional_field(finding, "location"),
        **_optional_field(finding, "trace"),
    }


def _optional_field(payload: dict[str, Any], key: str) -> dict[str, object]:
    return {key: payload[key]} if key in payload else {}

__all__ = ["ContractStatusReport", "ContractStatusScanRequest", "summarize_contract_status"]
