# LLM: Quality payload helpers expose QA disagreement as advice, not as an automatic workflow.
# 模块用途: 从子代理状态和 output refs 里提取 QA 失败线索，给父级 LLM 返回可选 repair 建议。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_QA_ROLES = {"tester", "bug_finder", "acceptor"}
_NEGATIVE_TERMS = (
    "失败",
    "缺陷",
    "缺失",
    "断裂",
    "不通过",
    "failed",
    "failure",
    "missing",
    "broken",
    "error",
)
_QUALITY_SCAN_MAX_NODES = 96


# LLM: quality_repair_advice_payload scans persisted QA descendants and returns refs-first guidance.
# 函数用途: 当 QA 子孙报告失败、缺陷或冲突时，给父级模型一个创建修复子代理的建议，不直接改任务树。
def quality_repair_advice_payload(agent: Any, parent_run_id: str) -> dict[str, object]:
    descendants = _descendant_tasks(agent, parent_run_id)
    failing = [_qa_failure_signal(item) for item in descendants]
    failing = [item for item in failing if item]
    if not failing:
        return {}
    failed_ids = [str(item["run_id"]) for item in failing if item.get("run_id")]
    return {
        "qa_repair_advice": {
            "phase": "repair_wave_recommended",
            "failed_or_conflicting_qa_run_ids": failed_ids,
            "qa_signal_refs": list(failing),
            "llm_next_step": (
                "先读取失败 QA 的 output/report refs，再由 LLM 创建 scoped repair worker；"
                "修复后重新 dispatch tester/acceptor，不要让单个 acceptor 的通过覆盖 tester 失败证据。"
            ),
            "suggested_tool_call": _repair_child_tool_call(failed_ids),
        },
        "needs_repair_wave": True,
    }


# LLM: _descendant_tasks keeps QA scanning bounded and based on task refs only.
# 函数用途: 广度优先扫描当前父节点的后代 task 记录，不读取业务产物正文。
def _descendant_tasks(agent: Any, parent_run_id: str) -> list[Any]:
    try:
        runs = list(agent.subagents.list_runs())
    except Exception:
        return []
    by_parent: dict[str, list[Any]] = {}
    for item in runs:
        by_parent.setdefault(str(getattr(item, "parent_id", "") or ""), []).append(item)
    queue = list(by_parent.get(parent_run_id, []))
    seen: set[str] = set()
    descendants: list[Any] = []
    while queue and len(descendants) < _QUALITY_SCAN_MAX_NODES:
        item = queue.pop(0)
        run_id = str(getattr(item, "id", "") or "")
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        descendants.append(item)
        queue.extend(by_parent.get(run_id, []))
    return descendants


# LLM: _qa_failure_signal reads small output metadata to spot QA failures without opening artifacts.
# 函数用途: 从 QA 子代理的 output.json/状态摘要里找失败线索，返回可追溯 refs。
def _qa_failure_signal(item: Any) -> dict[str, object]:
    role_text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower()
    if not any(role in role_text for role in _QA_ROLES):
        return {}
    output_path = _output_json_path(item)
    payload = _read_output_payload(output_path)
    if not _payload_has_negative_signal(item, payload):
        return {}
    return {
        "run_id": str(getattr(item, "id", "") or ""),
        "role": str(getattr(item, "role", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "verification_status": str(getattr(item, "verification_status", "") or ""),
        "output_ref": str(output_path) if output_path else "",
        "summary": _payload_summary(payload),
    }


# LLM: _output_json_path centralizes the compatible places a task may expose output.json.
# 函数用途: 找到子任务的 output.json 路径；找不到时仍允许状态失败触发修复建议。
def _output_json_path(item: Any) -> Path | None:
    explicit = str(getattr(item, "output_json", "") or "")
    if explicit:
        return Path(explicit)
    task_dir = str(getattr(item, "task_dir", "") or "")
    if task_dir:
        return Path(task_dir) / "output.json"
    return None


# LLM: _read_output_payload keeps corrupt or missing output refs from breaking dispatch payloads.
# 函数用途: 安全读取 output.json；失败时返回空 dict，让状态本身继续可用。
def _read_output_payload(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _payload_has_negative_signal is advisory; it never marks a task failed by itself.
# 函数用途: 判断 QA 输出里是否有失败/缺陷/断裂等线索，供 LLM 决定 repair。
def _payload_has_negative_signal(item: Any, payload: dict[str, Any]) -> bool:
    status = str(getattr(item, "status", "") or "").upper()
    if status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
        return True
    for test in payload.get("tests") or []:
        if isinstance(test, dict) and (test.get("ok") is False or test.get("passed") is False):
            return True
    text = json.dumps(_textual_signal_payload(payload), ensure_ascii=False).lower()
    return any(term in text for term in _NEGATIVE_TERMS)


# LLM: _textual_signal_payload narrows negative-word scanning to model summaries, not artifact bodies.
# 函数用途: 只扫描 output 摘要、验收说明和 blockers，避免大正文误触发。
def _textual_signal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    structured = payload.get("structured_output") if isinstance(payload.get("structured_output"), dict) else {}
    return {
        "summary": payload.get("summary") or structured.get("summary") or "",
        "acceptance": payload.get("acceptance") or [],
        "blockers": payload.get("blockers") or [],
        "next_actions": payload.get("next_actions") or [],
    }


# LLM: _payload_summary gives the parent enough context without copying large reports.
# 函数用途: 返回短摘要，方便模型知道为什么建议 repair。
def _payload_summary(payload: dict[str, Any]) -> str:
    summary = _textual_signal_payload(payload).get("summary")
    return str(summary or "")[:400]


# LLM: _repair_child_tool_call is a suggestion the LLM may edit, not an automatic action.
# 函数用途: 生成 refs-first 的 repair child 建议，强调按失败 QA refs 修复最小范围。
def _repair_child_tool_call(failed_ids: list[str]) -> dict[str, object]:
    joined = ", ".join(failed_ids)
    return {
        "tool": "schedule_child_subagents",
        "apply": True,
        "children": [
            {
                "role": "worker",
                "agent_name": "qa-repair-worker",
                "goal": (
                    "根据失败或冲突 QA refs 修复最小业务范围："
                    f"{joined}。先读取这些 QA run 的 output/report refs，"
                    "只改被证据点名的文件；修复后让 tester/acceptor 复测。"
                ),
                "allowed_tools": [
                    "subagent_board",
                    "list_files",
                    "read_file",
                    "search_text",
                    "replace_in_file",
                    "write_file",
                    "append_file",
                ],
            }
        ],
    }
