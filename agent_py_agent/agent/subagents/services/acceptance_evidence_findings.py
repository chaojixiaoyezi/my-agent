# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Evidence-related acceptance finding builders."""

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..reports import AcceptanceReviewFinding
from .acceptance_machine_evidence import passed_test_execution_report

if TYPE_CHECKING:
    from ..models import SubAgentTask

_DESCENDANT_EVIDENCE_MAX_NODES = 96
_DESCENDANT_EVIDENCE_MAX_BYTES = 65536
_CURRENT_ATTEMPT_TIME_EPSILON = 0.001


# LLM: build_evidence_findings 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 构建证据findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def build_evidence_findings(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    findings = _base_evidence_findings(task, created_at)
    required_tools = _required_tool_evidence(task)
    if "read_file" in required_tools:
        findings.append(_required_read_file_finding(task, created_at))
    if "write_file" in required_tools:
        findings.append(_required_write_file_finding(task, created_at))
    return findings


# LLM: _base_evidence_findings 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理基础证据findings相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _base_evidence_findings(task: SubAgentTask, created_at: float) -> list[AcceptanceReviewFinding]:
    evidence = _current_attempt_items(task, task.evidence)
    packets = _current_attempt_items(task, task.evidence_packets)
    ok_evidence = [item for item in evidence if item.ok]
    bad_evidence = [item for item in evidence if not item.ok]
    packets_with_refs = [
        item for item in packets if item.evidence_refs or item.artifact_refs
    ]
    machine_report = None if packets else passed_test_execution_report(task)
    evidence_chain_ok = bool(packets_with_refs) or machine_report is not None
    evidence_chain_message = _evidence_chain_message(packets_with_refs, machine_report)
    evidence_chain_path = str(machine_report.json_path) if machine_report is not None else task.output_json
    return [
        AcceptanceReviewFinding(
            name="evidence_present",
            ok=bool(ok_evidence),
            severity="P0",
            message=f"已有 {len(ok_evidence)} 条可用验收证据。" if ok_evidence else "缺少可用验收证据。",
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="evidence_chain_present",
            ok=evidence_chain_ok,
            severity="P0",
            message=evidence_chain_message,
            evidence_path=evidence_chain_path,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="evidence_not_failed",
            ok=not bad_evidence,
            severity="P1",
            message="没有失败验收证据。" if not bad_evidence else f"存在 {len(bad_evidence)} 条失败证据。",
            evidence_path=task.acceptance_file,
            created_at=created_at,
        ),
    ]


# LLM: _current_attempt_items prevents stale failed runner evidence from blocking a successful retry.
# 函数用途: 优先返回最近一次 runner 尝试产生的证据/证据包；没有可识别当前尝试时才回退到完整历史。
def _current_attempt_items(task: SubAgentTask, items: list) -> list:
    last_attempt_at = float(getattr(task, "runner_last_attempt_at", 0.0) or 0.0)
    if last_attempt_at <= 0:
        return items
    threshold = last_attempt_at - _CURRENT_ATTEMPT_TIME_EPSILON
    current = [
        item
        for item in items
        if float(getattr(item, "created_at", 0.0) or 0.0) >= threshold
    ]
    return current or items


# LLM: _evidence_chain_message explains whether traceability came from worker packets or machine tests.
# 函数用途: 生成证据链 finding 文案；父级测试报告只在没有 worker evidence packet 时作为兜底证据链。
def _evidence_chain_message(packets_with_refs, machine_report) -> str:
    if packets_with_refs:
        return f"已有 {len(packets_with_refs)} 条 evidence packet 可追溯。"
    if machine_report is not None:
        return f"父级真实测试报告可追溯: total={machine_report.total_tests} failed=0。"
    return "缺少带 evidence/artifact refs 的 evidence packet。"


# LLM: _required_read_file_finding 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验requiredread文件finding需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _required_read_file_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    scope = _tool_evidence_scope(task, "read_file", {"read_file", "file_read", "file_content"})
    has_read = bool(scope)
    return AcceptanceReviewFinding(
        name="acceptance_requires_read_file",
        ok=has_read,
        severity="P0",
        message=(
            f"结构化工具证据合同要求 read_file，且已有{_scope_label(scope)}对应工具和证据。"
            if has_read
            else "结构化工具证据合同要求 read_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )


# LLM: _required_write_file_finding 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 校验requiredwrite文件finding需要的输入和状态，不满足时把错误明确反馈给调用方；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def _required_write_file_finding(task: SubAgentTask, created_at: float) -> AcceptanceReviewFinding:
    scope = _tool_evidence_scope(task, "write_file", {"write_file", "file_write", "file_written"})
    has_write = bool(scope)
    return AcceptanceReviewFinding(
        name="acceptance_requires_write_file",
        ok=has_write,
        severity="P0",
        message=(
            f"结构化工具证据合同要求 write_file，且已有{_scope_label(scope)}对应工具和证据。"
            if has_write
            else "结构化工具证据合同要求 write_file，但缺少对应工具执行或证据。"
        ),
        evidence_path=task.acceptance_file,
        created_at=created_at,
    )


# LLM: _tool_evidence_scope lets coordinator acceptance trust completed descendant work evidence.
# 函数用途: 先看当前任务证据，再按 child_ids 有界扫描后代 task.json；协调者不用亲自 write_file 也能验收叶子产物。
def _tool_evidence_scope(task: SubAgentTask, tool_name: str, aliases: set[str]) -> str:
    if _has_tool_evidence(task.used_tools, task.evidence, tool_name, aliases):
        return "self"
    for record in _descendant_task_records(task):
        if _has_tool_evidence(_string_list(record.get("used_tools")), _dict_list(record.get("evidence")), tool_name, aliases):
            return "descendant"
    return ""


# LLM: _has_tool_evidence accepts live task evidence and persisted child task.json evidence.
# 函数用途: 判断某个工具是否有真实证据；要求 used_tools 和 evidence 同时命中，避免只凭模型口头声明。
def _has_tool_evidence(used_tools: list[str], evidence: list, tool_name: str, aliases: set[str]) -> bool:
    if tool_name not in used_tools:
        return False
    return any(
        _evidence_value(item, "ok")
        and (
            str(_evidence_value(item, "kind") or "") in aliases
            or tool_name in str(_evidence_value(item, "command") or "").lower()
        )
        for item in evidence
    )


# LLM: _required_tool_evidence reads tool gates from machine fields only.
# 函数用途: 从 task.attributes.required_tool_evidence 等结构化字段读取工具证据要求；不解析 acceptance_checks 文本。
def _required_tool_evidence(task: SubAgentTask) -> set[str]:
    attrs = getattr(task, "attributes", {})
    attrs = attrs if isinstance(attrs, dict) else {}
    return {
        _canonical_tool_name(item)
        for value in (
            attrs.get("required_tool_evidence"),
            attrs.get("acceptance_required_tools"),
            attrs.get("required_tools"),
        )
        for item in _string_list(value)
        if _canonical_tool_name(item)
    }


# LLM: _canonical_tool_name keeps aliases explicit and closed.
# 函数用途: 只接受工具 id 或工具 id 别名，不把自然语言句子切词。
def _canonical_tool_name(value: object) -> str:
    text = str(value or "").strip().casefold().replace("-", "_")
    aliases = {
        "read_file": "read_file",
        "file_read": "read_file",
        "file_content": "read_file",
        "write_file": "write_file",
        "file_write": "write_file",
        "file_written": "write_file",
    }
    return aliases.get(text, "")


# LLM: _descendant_task_records scans exact persisted descendants, never globbing arbitrary ids.
# 函数用途: 从当前 task.child_ids 开始读取后代 task.json，只取小 JSON 证据字段，避免验收读取业务大文件。
def _descendant_task_records(task: SubAgentTask) -> list[dict[str, object]]:
    workspace = _child_workspace(task)
    if workspace is None:
        return []
    records: list[dict[str, object]] = []
    queue = _string_list(getattr(task, "child_ids", []))
    seen: set[str] = set()
    while queue and len(records) < _DESCENDANT_EVIDENCE_MAX_NODES:
        run_id = queue.pop(0)
        if run_id in seen:
            continue
        seen.add(run_id)
        record = _read_child_task_record(workspace, run_id)
        if not record:
            continue
        records.append(record)
        queue.extend(child_id for child_id in _string_list(record.get("child_ids")) if child_id not in seen)
    return records


# LLM: _child_workspace derives sibling run dirs from a persisted task directory.
# 函数用途: 定位同一 subagents workspace 下的 child run 目录；失败时返回 None 保守不信任。
def _child_workspace(task: SubAgentTask) -> Path | None:
    try:
        task_dir = Path(str(getattr(task, "task_dir", "") or "")).expanduser()
    except OSError:
        return None
    if not str(task_dir):
        return None
    return task_dir.parent


# LLM: _read_child_task_record is bounded so evidence scans cannot load large artifacts.
# 函数用途: 读取 child_id/task.json 的小状态文件；过大、损坏或缺失都返回空字典。
def _read_child_task_record(workspace: Path, run_id: str) -> dict[str, object]:
    path = workspace / run_id / "task.json"
    try:
        if path.stat().st_size > _DESCENDANT_EVIDENCE_MAX_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _evidence_value accepts both dataclass evidence objects and persisted JSON dictionaries.
# 函数用途: 统一读取 evidence 的 ok/kind/command 字段，兼容内存对象和 task.json。
def _evidence_value(item: object, key: str) -> object:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


# LLM: _string_list normalizes persisted arrays without trusting unexpected scalar types.
# 函数用途: 将 used_tools、child_ids 等字段收敛成字符串列表。
def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]


# LLM: _dict_list keeps descendant evidence scanning tolerant of malformed JSON fields.
# 函数用途: 只保留列表里的字典项，避免坏 evidence 结构打断验收流程。
def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


# LLM: _scope_label makes acceptance messages explain whether proof is local or descendant-sourced.
# 函数用途: 生成中文提示片段，帮助父代理理解证据来自当前节点还是后代节点。
def _scope_label(scope: str) -> str:
    return "后代" if scope == "descendant" else ""
