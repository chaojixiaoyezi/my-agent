# LLM: Artifact integrity repair payloads route broken deliverables to scoped repair children.
# 模块用途: 把产物结构失败信号转换成可派修复小傻妞的结构化工具建议。

from __future__ import annotations

from typing import Any

from .orchestration_artifact_integrity_signals import (
    artifact_integrity_blocked,
    artifact_integrity_signals_from_records,
    artifact_integrity_signals_from_tasks,
)
from .orchestration_repair_contract import (
    RepairContractRequest,
    repair_contract_acceptance_checks,
    repair_contract_goal_suffix,
    repair_contract_tool_fields,
)

_REPAIR_TOOLS = [
    "subagent_board",
    "list_files",
    "read_file",
    "search_text",
    "replace_in_file",
    "write_file",
    "append_file",
]


# LLM: artifact_integrity_repair_advice_payload returns runner-context schedule_child_subagents advice.
# 函数用途: 父 runner 看到直接 child 因产物结构阻塞时，优先创建修复 child，而不是自己读正文修。
def artifact_integrity_repair_advice_payload(children: list[Any]) -> dict[str, object]:
    signals = artifact_integrity_signals_from_tasks(children)
    if not signals:
        return {}
    return {
        "artifact_integrity_repair_advice": _advice(signals, top_level=False),
        "needs_artifact_integrity_repair_wave": True,
    }


# LLM: artifact_integrity_repair_advice_from_records promotes top-level classify blockers into repair advice.
# 函数用途: 顶层 root 只能看到 dispatch records 时，也能拿到 create_subagents 修复建议。
def artifact_integrity_repair_advice_from_records(records: list[Any]) -> dict[str, object]:
    signals = artifact_integrity_signals_from_records(records)
    if not signals:
        return {}
    return {
        "next_action": "create_repair_child_from_artifact_integrity_refs",
        "artifact_integrity_repair_advice": _advice(signals, top_level=True),
        "needs_artifact_integrity_repair_wave": True,
    }


# LLM: artifact_integrity_repair_record_payload annotates one dispatch record with repair advice.
# 函数用途: 单条 classify_blocker 记录如果指向产物结构失败，就附加机器可读修复通道。
def artifact_integrity_repair_record_payload(item: Any) -> dict[str, object]:
    signals = artifact_integrity_signals_from_records([item])
    if not signals:
        return {}
    return {
        "next_action": "create_repair_child_from_artifact_integrity_refs",
        "artifact_integrity_repair_advice": _advice(signals, top_level=True),
    }


# LLM: _advice builds the common model-facing repair payload for top-level and runner-context callers.
# 函数用途: 统一 failed_run_ids、failure_refs、下一步说明和建议工具调用，避免两套提示漂移。
def _advice(signals: list[dict[str, object]], *, top_level: bool) -> dict[str, object]:
    return {
        "phase": "artifact_integrity_repair_recommended",
        "failed_run_ids": [str(item["run_id"]) for item in signals if item.get("run_id")],
        "failure_refs": signals,
        "llm_next_step": (
            "child 产物结构检查失败；不要由父级直接改文件。"
            "请创建修复小傻妞读取 failure_refs，只修复列出的产物文件，"
            "修复后重新 dispatch 并执行父级验收。"
        ),
        "suggested_tool_call": _top_level_tool_call(signals) if top_level else _runner_context_tool_call(signals),
    }


# LLM: _top_level_tool_call uses create_subagents because root is outside a runner context.
# 函数用途: 给顶层 root 的可复制派工参数，继承失败 child 的产物写入根。
def _top_level_tool_call(signals: list[dict[str, object]]) -> dict[str, object]:
    contract_fields = _contract_tool_fields(signals)
    return {
        "tool": "create_subagents",
        "count": 1,
        "role": "worker",
        "agent_name": "小傻妞-产物修复",
        "workflow_mode": "off",
        "goal": _repair_goal(signals),
        "extra_write_roots": _merged_roots(signals),
        "acceptance_checks": _acceptance_checks(),
        "allowed_tools": _REPAIR_TOOLS,
        **contract_fields,
    }


# LLM: _runner_context_tool_call uses schedule_child_subagents for coordinator/worker parents.
# 函数用途: 给 runner 内父节点的修复 child 建议，不自动创建，不写死工作流。
def _runner_context_tool_call(signals: list[dict[str, object]]) -> dict[str, object]:
    contract_fields = _contract_tool_fields(signals)
    return {
        "tool": "schedule_child_subagents",
        "apply": True,
        "children": [{
            "role": "worker",
            "agent_name": "小傻妞-产物修复",
            "goal": _repair_goal(signals),
            "extra_write_roots": _merged_roots(signals),
            "acceptance_checks": _acceptance_checks(),
            "allowed_tools": _REPAIR_TOOLS,
            **contract_fields,
        }],
    }


# LLM: _repair_goal stays refs-first while naming the concrete broken artifact paths.
# 函数用途: 给修复小傻妞明确 run/output/run refs、产物路径和失败码，减少自然语言误传。
def _repair_goal(signals: list[dict[str, object]]) -> str:
    ids = ", ".join(str(item.get("run_id") or "") for item in signals if item.get("run_id")) or "unknown"
    refs = "; ".join(_signal_ref_text(item) for item in signals)
    blockers = "; ".join(str(blocker) for item in signals for blocker in item.get("blockers", []) or [])[:900]
    artifacts = "; ".join(str(path) for item in signals for path in item.get("artifact_refs", []) or [])[:900]
    return (
        f"修复产物结构检查失败的 child runs：{ids}。"
        f"先读取这些 refs：{refs}。"
        f"失败产物：{artifacts or '查看 output_ref artifacts/tests'}。"
        f"失败码：{blockers or '查看 output_ref blockers/tests'}。"
        "只修复列出的产物文件，补全缺失结构或明显截断内容；不要改写健康分支。"
        f"{repair_contract_goal_suffix()}"
    )


# LLM: _merged_roots dedupes product roots across multiple failed children.
# 函数用途: 一个修复 child 可处理多个同域失败文件，但仍只拿到必要写入边界。
def _merged_roots(signals: list[dict[str, object]]) -> list[str]:
    roots: list[str] = []
    for item in signals:
        roots.extend(str(root) for root in item.get("allowed_write_roots", []) or [])
    return _unique_text(roots)


# LLM: _signal_ref_text keeps repair goals compact while preserving exact refs.
# 函数用途: 拼接 task/output/run 引用，不展开 JSON 内容。
def _signal_ref_text(signal: dict[str, object]) -> str:
    return (
        f"run_id={signal.get('run_id', '')}; "
        f"task_ref={signal.get('task_ref', '')}; "
        f"output_ref={signal.get('output_ref', '')}; "
        f"run_ref={signal.get('run_ref', '')}"
    )


# LLM: _acceptance_checks keeps artifact repair child scope narrow and verifiable.
# 函数用途: 给修复子代理的验收条件，强调只修失败产物、读回检查、重新验收。
def _acceptance_checks() -> list[str]:
    return [
        "只修复 artifact_integrity failure_refs 列出的产物文件",
        "修复后读取被修改文件，确认缺失结构或截断问题已消失",
        "不要改写无关产物或健康分支",
        *repair_contract_acceptance_checks(),
    ]


# LLM: _contract_tool_fields adds the common same-run repair/execute/verify contract.
# 函数用途: 把产物修复信号转换成 create/schedule 都能携带的上下文合同字段。
def _contract_tool_fields(signals: list[dict[str, object]]) -> dict[str, object]:
    return repair_contract_tool_fields(
        RepairContractRequest(
            kind="artifact_integrity",
            failed_run_ids=[str(item.get("run_id") or "") for item in signals],
            failure_refs=signals,
            target_artifact_refs=[
                str(path)
                for item in signals
                for path in item.get("artifact_refs", []) or []
            ],
        )
    )


# LLM: _unique_text filters empty strings and preserves first-seen order.
# 函数用途: 稳定模型可见列表，避免重复路径放大 prompt。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique


__all__ = [
    "artifact_integrity_blocked",
    "artifact_integrity_repair_advice_from_records",
    "artifact_integrity_repair_advice_payload",
    "artifact_integrity_repair_record_payload",
]
