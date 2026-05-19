# LLM: Parent acceptance repair payloads turn failed parent tests into scoped repair work.
# 模块用途: 从 REJECT 的父级验收 refs 中提取失败测试和建议，给父 runner 一个可派修复小傻妞的结构化工具调用。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dispatch_test_failure_summary import acceptance_test_failure_payload
from .orchestration_repair_contract import (
    RepairContractRequest,
    repair_contract_acceptance_checks,
    repair_contract_goal_suffix,
    repair_contract_tool_fields,
)

_MAX_FAILED_LABELS = 6


# LLM: parent_acceptance_repair_advice_payload is advisory and never creates child tasks itself.
# 函数用途: 扫描直接 child 的 acceptance/test/followup refs；有父级验收拒绝时返回 scoped repair 建议。
def parent_acceptance_repair_advice_payload(children: list[Any]) -> dict[str, object]:
    signals = parent_acceptance_repair_signals_from_tasks(children)
    if not signals:
        return {}
    failed_ids = [str(item["run_id"]) for item in signals if item.get("run_id")]
    return {
        "parent_acceptance_repair_advice": {
            "phase": "parent_acceptance_repair_recommended",
            "failed_run_ids": failed_ids,
            "failure_refs": signals,
            "llm_next_step": (
                "父级真实验收已拒绝这些 child；先按 failure_refs 读取小报告，"
                "再创建 scoped repair worker 修复被测试点名的问题。修复后重新 dispatch 并执行父级验收 tests。"
            ),
            "suggested_tool_call": _repair_child_tool_call(signals),
        },
        "needs_parent_acceptance_repair_wave": True,
    }


# LLM: parent_acceptance_rejected checks only persisted parent acceptance decisions.
# 函数用途: 给 progress payload 判断 child 是否已被父级验收拒绝；读取失败时返回 False。
def parent_acceptance_rejected(item: Any) -> bool:
    payload = _read_json(_acceptance_ref(item))
    return str(payload.get("decision") or "").upper() == "REJECT"


# LLM: parent_acceptance_repair_signals_from_tasks exposes the same refs-first facts to action apply.
# 函数用途: 让 due-check/action handler 复用父级验收失败信号，避免一套给模型、一套给系统。
def parent_acceptance_repair_signals_from_tasks(children: list[Any]) -> list[dict[str, object]]:
    signals = [_repair_signal(item) for item in children]
    return [item for item in signals if item]


# LLM: _repair_signal extracts bounded facts from one rejected child.
# 函数用途: 把 acceptance_review、test_execution 和 followup 文件转成模型可读但 refs-first 的失败信号。
def _repair_signal(item: Any) -> dict[str, object]:
    if not parent_acceptance_rejected(item):
        return {}
    acceptance_ref = _acceptance_ref(item)
    test_ref = _test_ref(item)
    followup_ref = _followup_ref(item)
    task_ref = _task_ref(item)
    followup = _followup_payload(followup_ref)
    failure_payload = acceptance_test_failure_payload(test_ref) if test_ref.exists() else {}
    return {
        "run_id": str(getattr(item, "id", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "verification_status": str(getattr(item, "verification_status", "") or ""),
        "acceptance_ref": str(acceptance_ref) if acceptance_ref.exists() else "",
        "test_ref": str(test_ref) if test_ref.exists() else "",
        "followup_ref": str(followup_ref) if followup_ref.exists() else "",
        "task_ref": str(task_ref) if task_ref.exists() else "",
        "original_goal": _task_goal(task_ref),
        "original_acceptance_checks": _task_acceptance_checks(task_ref),
        "test_failure_summary": str(failure_payload.get("parent_acceptance_test_failure_summary") or ""),
        "test_failure_details": list(failure_payload.get("parent_acceptance_test_failure_details") or []),
        "failed_tests": _failed_test_labels(followup),
        "artifact_refs": _test_target_refs(test_ref),
        "allowed_write_roots": _allowed_write_roots(item),
    }


# LLM: _repair_child_tool_call gives the parent a copyable schedule_child_subagents payload.
# 函数用途: 生成一名修复 worker 的建议，不自动执行，也不固定最终角色流程。
def _repair_child_tool_call(signals: list[dict[str, object]]) -> dict[str, object]:
    roots = _merged_roots(signals)
    contract_fields = repair_contract_tool_fields(
        RepairContractRequest(
            kind="parent_acceptance",
            failed_run_ids=[str(item.get("run_id") or "") for item in signals],
            failure_refs=signals,
            target_artifact_refs=_signal_artifact_refs(signals),
        )
    )
    return {
        "tool": "schedule_child_subagents",
        "apply": True,
        "children": [
            {
                "role": "worker",
                "agent_name": "小傻妞-验收修复",
                "goal": _repair_goal(signals),
                "extra_write_roots": roots,
                "acceptance_checks": [
                    "先修复父级验收 failure_refs 点名的问题，同时保持原任务完整目标",
                    "修复后必须让父级重新 dispatch 并执行验收 tests",
                    "不要改写健康分支或无关产物",
                    *repair_contract_acceptance_checks(signals),
                ],
                "allowed_tools": [
                    "subagent_board",
                    "list_files",
                    "read_file",
                    "search_text",
                    "replace_in_file",
                    "write_file",
                    "append_file",
                ],
                **contract_fields,
            }
        ],
    }


# LLM: _repair_goal stays short but includes exact refs and failed-test names.
# 函数用途: 给修复小傻妞一段稳定目标，避免父级从自然语言摘要里重新猜失败原因。
def _repair_goal(signals: list[dict[str, object]]) -> str:
    ids = ", ".join(str(item.get("run_id") or "") for item in signals if item.get("run_id"))
    refs = _signal_refs(signals)
    failures = _signal_failure_labels(signals)
    original = _original_contract_goal_text(signals)
    return (
        f"修复父级验收失败的 child runs：{ids}。"
        f"先读取这些 refs：{refs}。"
        f"失败测试/线索：{failures}。"
        f"{original}"
        "先修复被父级验收报告点名的问题，最终必须重新满足原始完整验收要求；"
        "修复完成后写回证据 refs，等待父级重新执行验收 tests。"
        f"{repair_contract_goal_suffix()}"
    )


# LLM: _signal_refs returns only small report refs, never artifact bodies.
# 函数用途: 拼接 acceptance/test/followup 引用，供 repair worker 自己按需读取。
def _signal_refs(signals: list[dict[str, object]]) -> str:
    refs = _unique_text([
        str(signal.get(key) or "")
        for signal in signals
        for key in ("acceptance_ref", "test_ref", "followup_ref", "task_ref")
    ])
    return "; ".join(refs[:9]) or "无可用报告 refs"


# LLM: _signal_failure_labels keeps repair goals actionable when validation details are sparse.
# 函数用途: 合并 summary、details 和 failed_tests，避免 content_check 失败只剩“验收失败”四个字。
def _signal_failure_labels(signals: list[dict[str, object]]) -> str:
    labels: list[str] = []
    for signal in signals:
        labels.extend(str(item) for item in signal.get("failed_tests") or [])
        summary = str(signal.get("test_failure_summary") or "")
        if summary:
            labels.append(summary)
        labels.extend(str(item) for item in signal.get("test_failure_details") or [])
    unique = _unique_text(labels)
    return "; ".join(unique[:_MAX_FAILED_LABELS]) or "查看 test_execution.json 和 followup 中的 failed_tests"


# LLM: _signal_artifact_refs extracts target product refs when failure signals already know them.
# 函数用途: 给修复合同补目标产物路径；父级验收信号没有产物时保守返回空列表。
def _signal_artifact_refs(signals: list[dict[str, object]]) -> list[str]:
    refs: list[str] = []
    for signal in signals:
        refs.extend(str(ref) for ref in signal.get("artifact_refs") or [])
    return _unique_text(refs)


# LLM: _original_contract_goal_text summarizes inherited success checks for runner-context repair goals.
# 函数用途: 把失败 child 的原始 goal/acceptance_checks 放进修复目标，避免只修最新错误后误报完成。
def _original_contract_goal_text(signals: list[dict[str, object]]) -> str:
    goals = _unique_text([str(item.get("original_goal") or "") for item in signals])
    checks = _unique_text([
        str(check)
        for signal in signals
        for check in list(signal.get("original_acceptance_checks") or [])
    ])
    parts: list[str] = []
    if goals:
        parts.append("原始任务目标：" + "；".join(goals[:3]))
    if checks:
        parts.append("原始完整验收要求：" + "；".join(checks[:8]))
    return ("。".join(parts) + "。") if parts else ""


# LLM: _task_goal reads the failed child task contract beside reports_dir.
# 函数用途: 从 task.json 恢复原始 goal；不可读时返回空，不影响调度主流程。
def _task_goal(path: Path) -> str:
    payload = _read_json(path)
    return str(payload.get("goal") or "").strip()


# LLM: _task_acceptance_checks reads persisted full-success gates from task.json.
# 函数用途: 从失败 child 的原始 acceptance_checks 生成 repair_contract.full_success_checks。
def _task_acceptance_checks(path: Path) -> list[str]:
    payload = _read_json(path)
    raw = payload.get("acceptance_checks")
    if not isinstance(raw, list):
        return []
    return _unique_text([str(item) for item in raw])


# LLM: _test_target_refs extracts file targets from parent test execution records.
# 函数用途: 从 file/content 检查失败记录里提取目标路径，让 repair contract 知道要验证哪个产物。
def _test_target_refs(path: Path) -> list[str]:
    payload = _read_json(path)
    refs: list[str] = []
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        result = record.get("validation_result")
        if isinstance(result, dict):
            refs.append(str(result.get("path") or ""))
            root = str(result.get("checked_root") or "").strip()
            for file_name in result.get("checked_files") or []:
                if root and file_name:
                    refs.append(str(Path(root) / str(file_name)))
    return _unique_text(refs)


# LLM: _failed_test_labels normalizes followup failed_tests for model-facing repair goals.
# 函数用途: 提取失败测试名、错误和 validation_method，作为短文本线索。
def _failed_test_labels(followup: dict[str, object]) -> list[str]:
    labels: list[str] = []
    failed = followup.get("failed_tests") if isinstance(followup, dict) else []
    for item in failed if isinstance(failed, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("test_name") or "test").strip()
        error = str(item.get("error") or item.get("validation_method") or "").strip()
        labels.append(f"{name}: {error}" if error else name)
    return _unique_text(labels)


# LLM: _followup_payload tolerates absent or old followup files.
# 函数用途: 读取 parent_acceptance_auto_followup.json 的 followup 字段；不可读时返回空。
def _followup_payload(path: Path) -> dict[str, object]:
    payload = _read_json(path)
    followup = payload.get("followup") if isinstance(payload, dict) else {}
    return followup if isinstance(followup, dict) else {}


# LLM: _read_json keeps malformed report refs from breaking dispatch_subagents.
# 函数用途: 读取小型 JSON 报告；不存在、损坏或非 object 时返回空 dict。
def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _acceptance_ref returns the canonical per-run acceptance review path.
# 函数用途: 从 task.reports_dir 推导 acceptance_review.json，缺 reports_dir 时返回空路径。
def _acceptance_ref(item: Any) -> Path:
    return _reports_dir(item) / "acceptance_review.json"


# LLM: _test_ref returns the canonical parent test execution path.
# 函数用途: 从 task.reports_dir 推导 test_execution.json，供失败摘要提取。
def _test_ref(item: Any) -> Path:
    return _reports_dir(item) / "test_execution.json"


# LLM: _followup_ref returns the canonical parent follow-up path.
# 函数用途: 从 task.reports_dir 推导 parent_acceptance_auto_followup.json。
def _followup_ref(item: Any) -> Path:
    return _reports_dir(item) / "parent_acceptance_auto_followup.json"


# LLM: _task_ref returns the canonical failed child task contract path.
# 函数用途: 优先使用 task_dir/task.json；否则从 reports_dir 的父目录兜底，兼容旧测试和旧 runner。
def _task_ref(item: Any) -> Path:
    raw = str(getattr(item, "task_dir", "") or "")
    if raw:
        return Path(raw) / "task.json"
    return _reports_dir(item).parent / "task.json"


# LLM: _reports_dir isolates missing reports_dir handling.
# 函数用途: reports_dir 为空时返回一个不存在的相对占位路径，不触发任意目录扫描。
def _reports_dir(item: Any) -> Path:
    raw = str(getattr(item, "reports_dir", "") or "")
    return Path(raw) if raw else Path("__missing_reports_dir__")


# LLM: _allowed_write_roots forwards only existing child-authorized product roots.
# 函数用途: repair worker 继承失败 child 的产物写入边界；无字段时返回空，由父级继承兜底。
def _allowed_write_roots(item: Any) -> list[str]:
    return _unique_text([str(root) for root in getattr(item, "allowed_write_roots", []) or [] if str(root).strip()])


# LLM: _merged_roots dedupes roots from all failed children.
# 函数用途: 给一个 repair worker 授权多个失败 child 的共同产物根，避免漏修多文件失败。
def _merged_roots(signals: list[dict[str, object]]) -> list[str]:
    roots: list[str] = []
    for signal in signals:
        roots.extend(str(root) for root in signal.get("allowed_write_roots") or [])
    return _unique_text(roots)


# LLM: _unique_text keeps model-facing lists stable and compact.
# 函数用途: 去掉空字符串和重复项，保留首次出现顺序。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique


__all__ = [
    "parent_acceptance_rejected",
    "parent_acceptance_repair_advice_payload",
    "parent_acceptance_repair_signals_from_tasks",
]
