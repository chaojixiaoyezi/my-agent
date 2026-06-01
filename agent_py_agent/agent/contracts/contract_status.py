# LLM: Contract status summarizes recent machine findings without invoking models or tools.
# 模块用途: 扫描合同报告 JSON，汇总失败码、严重级别和最近 finding，给 CLI/看板做只读可观测入口。

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..settings.defaults import default_agent_config


# LLM: ContractStatusReport keeps this contract helper structure-first and stable.
# 类用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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

    # LLM: ok keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
    @property
    def ok(self) -> bool:
        return self.finding_count == 0

    # LLM: to_dict keeps this contract helper structure-first and stable.
    # 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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
        }


# LLM: _StatusAccumulator keeps this contract helper structure-first and stable.
# 类用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
@dataclass
class _StatusAccumulator:
    by_code: Counter[str]
    by_severity: Counter[str]
    recent: list[dict[str, object]]
    limit: int


# LLM: ContractStatusScanRequest keeps optional scan budgets bundled for callers and CLI.
# 类用途: 保存合同状态扫描的覆盖数量、单文件大小、recent finding 数量和配置来源。
@dataclass(frozen=True)
class ContractStatusScanRequest:
    limit: int | None = None
    max_files: int | None = None
    max_file_bytes: int | None = None
    config: object | None = None


# LLM: summarize_contract_status reads bounded JSON reports and counts explicit findings fields.
# 函数用途: 从 report.findings 等结构化字段统计合同失败，不解析自然语言消息。
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
    for path in _json_paths(base, max_files=resolved.max_files):
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
    )


# LLM: _StatusScanLimits is the resolved immutable budget used by one status scan.
# 类用途: 保存已解析的 recent 数量、扫描文件数和单文件字节上限。
@dataclass(frozen=True)
class _StatusScanLimits:
    limit: int
    max_files: int
    max_file_bytes: int


# LLM: _status_scan_limits resolves optional scan overrides against AgentConfig defaults.
# 函数用途: 将 request 中的显式值和主配置合成最终扫描预算。
def _status_scan_limits(request: ContractStatusScanRequest) -> _StatusScanLimits:
    defaults = _contract_status_config_defaults(request.config)
    return _StatusScanLimits(
        limit=_provided_or_config_int(request.limit, defaults.contract_status_recent_findings_limit),
        max_files=_provided_or_config_int(request.max_files, defaults.contract_status_max_scan_files),
        max_file_bytes=_provided_or_config_int(request.max_file_bytes, defaults.contract_status_max_report_bytes),
    )


# LLM: _contract_status_config_defaults keeps status scans tied to AgentConfig when no explicit config is passed.
# 函数用途: 返回合同状态扫描使用的配置对象；没有调用方配置时只回退到 schema 默认。
def _contract_status_config_defaults(config: object | None) -> object:
    if config is not None:
        return config
    return default_agent_config()


# LLM: _provided_or_config_int applies an explicit override before falling back to config.
# 函数用途: 归一化单个合同状态扫描预算；非法值回退为 0，避免异常中断看板扫描。
def _provided_or_config_int(value: int | None, fallback: object) -> int:
    source = fallback if value is None else value
    try:
        return max(0, int(source))
    except (TypeError, ValueError):
        return 0


# LLM: _json_paths keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _json_paths(root: Path, *, max_files: int) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() == ".json" else []
    if not root.exists():
        return []
    paths = [
        path
        for path in root.rglob("*.json")
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
    ]
    paths.sort(key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    return paths[:max(0, max_files)]


# LLM: _file_too_large keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _file_too_large(path: Path, max_file_bytes: int) -> bool:
    try:
        return path.stat().st_size > max_file_bytes
    except OSError:
        return True


# LLM: _read_json keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# LLM: _findings_from_file keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _findings_from_file(path: Path, max_file_bytes: int) -> tuple[list[dict[str, Any]], bool]:
    if _file_too_large(path, max_file_bytes):
        return [], True
    payload = _read_json(path)
    return (_findings(payload), False) if payload is not None else ([], True)


# LLM: _tally_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
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


# LLM: _direct_findings keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _direct_findings(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or not isinstance(value.get("findings"), list):
        return []
    return [dict(item) for item in value["findings"] if isinstance(item, dict) and _text(item.get("code"))]


# LLM: _child_values keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _child_values(value: object) -> list[object]:
    if isinstance(value, dict):
        return [item for key, item in value.items() if key != "findings" and isinstance(item, (dict, list))]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, (dict, list))]
    return []


# LLM: _recent_finding keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _recent_finding(path: Path, finding: dict[str, Any], code: str, severity: str) -> dict[str, object]:
    return {
        "file": str(path),
        "code": code,
        "severity": severity,
        **_optional_field(finding, "stage_ref"),
        **_optional_field(finding, "location"),
        **_optional_field(finding, "trace"),
    }


# LLM: _optional_field keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _optional_field(payload: dict[str, Any], key: str) -> dict[str, object]:
    return {key: payload[key]} if key in payload else {}


# LLM: _text keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["ContractStatusReport", "ContractStatusScanRequest", "summarize_contract_status"]
