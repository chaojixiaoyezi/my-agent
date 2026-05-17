# LLM: Dispatch payload helpers keep orchestration tools thin and refs-only.
# 模块用途: 构建 dispatch_subagents 工具返回给 runner 的轻量记录，不读取正文或执行动作。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .orchestration_artifact_integrity_repair import artifact_integrity_repair_record_payload
from .orchestration_repair_contract import (
    RepairContractRequest,
    repair_contract_acceptance_checks,
    repair_contract_goal_suffix,
    repair_contract_tool_fields,
)


# LLM: _RepairRefs keeps top-level parent-repair helper signatures bundle-shaped.
# 类用途: 保存 rejected acceptance 修复所需的小报告引用，避免 helper 继续扩散散参。
@dataclass(frozen=True)
class _RepairRefs:
    test_ref: str
    followup_ref: str
    output_ref: str
    run_ref: str
    task_ref: str


# LLM: dispatch_record_payload exposes compact acceptance facts to coordinator runners.
# 函数用途: 给模型工具返回每条调度记录的核心字段和父级验收摘要，避免 coordinator 看不到 child 测试失败。
def dispatch_record_payload(item) -> dict[str, object]:
    payload = {
        "step": item.step,
        "action": item.action,
        "run_id": item.run_id,
        "ok": item.ok,
        "dry_run": item.dry_run,
        "applied": item.applied,
        "message": item.message,
        "before_status": item.before_status,
        "after_status": item.after_status,
    }
    payload.update(_dispatch_record_runner_payload(item))
    payload.update(_dispatch_record_acceptance_payload(item))
    payload.update(_dispatch_record_acceptance_repair_payload(item))
    payload.update(artifact_integrity_repair_record_payload(item))
    return payload


# LLM: dispatch_recovery_payload keeps blocking runner-selection fixes visible before bulky records.
# 函数用途: 当模型传错 run_id 时，把可执行恢复指令放到 dispatch_subagents 顶层返回。
def dispatch_recovery_payload(records: list[object]) -> dict[str, object]:
    for item in records:
        if item.step == "runner_selection" and item.action == "invalid_run_ids":
            return {
                "action": "retry_dispatch_with_valid_run_id",
                "message": item.message,
                "valid_run_ids": _valid_run_ids_from_refs(item.evidence_paths or []),
                "valid_task_refs": list(item.evidence_paths or [])[:20],
            }
    return {}


# LLM: _dispatch_record_runner_payload surfaces nested runner child creation as facts, not prose guesses.
# 函数用途: 顶层主代理读取 dispatch_subagents 结果时，直接看到 runner 内部创建的 child ids/roles。
def _dispatch_record_runner_payload(item) -> dict[str, object]:
    keys = {
        "runner_summary": "runner_summary",
        "runner_created_child_count": "runner_created_child_count",
        "runner_created_child_ids": "runner_created_child_ids",
        "runner_created_roles": "runner_created_roles",
        "runner_child_status_counts": "runner_child_status_counts",
        "runner_unfinished_child_ids": "runner_unfinished_child_ids",
        "runner_partial_success": "runner_partial_success",
    }
    return {
        name: value
        for name, attr in keys.items()
        if (value := getattr(item, attr, "")) not in ("", 0, [], None)
    }


# LLM: _valid_run_ids_from_refs converts task-dir refs into machine-readable retry ids.
# 函数用途: 从 dispatch 阻断证据路径中提取 run id，放到顶层 recovery payload，避免模型从自然语言里猜 id。
def _valid_run_ids_from_refs(paths: list[str]) -> list[str]:
    ids: list[str] = []
    for path in paths[:20]:
        run_id = Path(str(path)).name.strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _dispatch_record_acceptance_payload keeps parent test/follow-up refs compact and optional.
# 函数用途: 只在字段存在时附加测试数量、失败数、follow-up 动作和引用，避免普通记录输出噪声。
def _dispatch_record_acceptance_payload(item) -> dict[str, object]:
    keys = {
        "test_ref": "parent_acceptance_auto_execution_test_ref",
        "test_total": "parent_acceptance_auto_execution_test_total",
        "test_failed": "parent_acceptance_auto_execution_test_failed",
        "test_failure_summary": "parent_acceptance_test_failure_summary",
        "test_failure_details": "parent_acceptance_test_failure_details",
        "followup_ref": "parent_acceptance_followup_ref",
        "followup_status": "parent_acceptance_followup_status",
        "followup_action": "parent_acceptance_followup_action",
        "followup_command": "parent_acceptance_followup_command",
        "followup_reason": "parent_acceptance_followup_reason",
    }
    return {
        name: value
        for name, attr in keys.items()
        if (value := getattr(item, attr, "")) not in ("", 0, [], None)
    }


# LLM: _dispatch_record_acceptance_repair_payload turns rejected parent tests into a repair-child tool hint.
# 函数用途: 顶层 dispatch 看到父级验收失败时，直接给 root 一个 refs-first 修复小傻妞建议，避免 root 自己改文件。
def _dispatch_record_acceptance_repair_payload(item) -> dict[str, object]:
    if item.step != "acceptance" or item.action != "reject":
        return {}
    refs = _repair_refs(item)
    return {
        "next_action": "create_repair_child_from_parent_acceptance_refs",
        "parent_acceptance_repair_advice": {
            "phase": "parent_acceptance_repair_recommended",
            "failed_run_ids": [item.run_id] if item.run_id else [],
            "failure_refs": _compact_failure_refs(item, refs),
            "llm_next_step": (
                "父级真实验收已失败；请创建一名修复小傻妞读取 failure_refs，"
                "只修复验收报告点名的问题，然后重新 dispatch 并跑父级验收。"
            ),
            "suggested_tool_call": _top_level_repair_tool_call(item, refs),
        },
    }


# LLM: _repair_refs collects canonical report refs once for the top-level advice helpers.
# 函数用途: 从 dispatch acceptance record 推导 test/followup/output/run 引用，后续 helper 只传 bundle。
def _repair_refs(item) -> _RepairRefs:
    test_ref = str(getattr(item, "parent_acceptance_auto_execution_test_ref", "") or "")
    return _RepairRefs(
        test_ref=test_ref,
        followup_ref=str(getattr(item, "parent_acceptance_followup_ref", "") or ""),
        output_ref=_sibling_ref(test_ref, "output.json"),
        run_ref=_sibling_ref(test_ref, "run.json"),
        task_ref=_sibling_ref(test_ref, "task.json"),
    )


# LLM: _top_level_repair_tool_call uses create_subagents because top-level root cannot schedule children directly.
# 函数用途: 给主代理可复制的顶层修复派工参数，包含失败 refs 和继承的产物写入根。
def _top_level_repair_tool_call(
    item,
    refs: _RepairRefs,
) -> dict[str, object]:
    failure_refs = _compact_failure_refs(item, refs)
    contract_fields = repair_contract_tool_fields(
        RepairContractRequest(
            kind="parent_acceptance",
            failed_run_ids=[str(item.run_id)] if item.run_id else [],
            failure_refs=failure_refs,
            target_artifact_refs=_repair_target_refs(refs),
        )
    )
    return {
        "tool": "create_subagents",
        "count": 1,
        "role": "worker",
        "agent_name": "小傻妞-验收修复",
        "workflow_mode": "off",
        "goal": _repair_goal(item, refs),
        "extra_write_roots": _product_write_roots(refs.run_ref),
        "acceptance_checks": [
            "先修复父级验收 failure_refs 点名的问题，同时保持原任务完整目标",
            "修复后读取被修改文件并说明验证结果",
            "不要改写无关产物或健康分支",
            *repair_contract_acceptance_checks(failure_refs),
        ],
        "allowed_tools": [
            "list_files",
            "read_file",
            "search_text",
            "replace_in_file",
            "write_file",
            "append_file",
        ],
        **contract_fields,
    }


# LLM: _repair_goal keeps top-level repair work grounded in machine refs instead of natural summaries.
# 函数用途: 拼出修复 worker 的任务目标，包含 run id、测试失败摘要和可读取报告路径。
def _repair_goal(item, refs: _RepairRefs) -> str:
    details = "; ".join(str(value) for value in (getattr(item, "parent_acceptance_test_failure_details", []) or [])[:4])
    original = _original_contract_goal_text(refs.task_ref)
    return (
        f"修复父级验收失败的子代理 run：{item.run_id}。"
        f"先读取这些 refs：test_ref={refs.test_ref}; followup_ref={refs.followup_ref}; "
        f"output_ref={refs.output_ref}; run_ref={refs.run_ref}; task_ref={refs.task_ref}。"
        f"失败摘要：{getattr(item, 'parent_acceptance_test_failure_summary', '') or '查看 test_ref'}。"
        f"失败细节：{details or '查看 test_execution.json'}。"
        f"{original}"
        "先修复父级验收报告点名的问题，但最终必须重新满足原始完整验收要求；完成后写回文件并说明验证结果。"
        f"{repair_contract_goal_suffix()}"
    )


# LLM: _compact_failure_refs exposes report refs without inlining artifact bodies.
# 函数用途: 组装失败线索列表，供 root/repair worker 按需读取小报告。
def _compact_failure_refs(item, refs: _RepairRefs) -> list[dict[str, object]]:
    return [{
        "run_id": item.run_id,
        "test_ref": refs.test_ref,
        "followup_ref": refs.followup_ref,
        "output_ref": refs.output_ref,
        "run_ref": refs.run_ref,
        "task_ref": refs.task_ref,
        "original_goal": _task_goal(refs.task_ref),
        "original_acceptance_checks": _task_acceptance_checks(refs.task_ref),
        "test_failure_summary": getattr(item, "parent_acceptance_test_failure_summary", "") or "",
        "test_failure_details": list(getattr(item, "parent_acceptance_test_failure_details", []) or [])[:8],
    }]


# LLM: _sibling_ref derives run-local machine refs from the canonical test report path.
# 函数用途: 从 reports/test_execution.json 推导同一 run 的 output.json/run.json；不存在时返回空字符串。
def _sibling_ref(test_ref: str, filename: str) -> str:
    if not test_ref:
        return ""
    path = Path(test_ref).parent.parent / filename
    return str(path) if path.exists() else ""


# LLM: _product_write_roots forwards child-authorized product roots to the repair child.
# 函数用途: 读取 run.json 的 allowed_write_roots，过滤内部 task_dir，只把产物根交给修复小傻妞。
def _product_write_roots(run_ref: str) -> list[str]:
    payload = _read_json_object(run_ref)
    task_dir = str(payload.get("task_dir") or "")
    roots: list[str] = []
    for raw in payload.get("allowed_write_roots") or []:
        text = str(raw or "").strip()
        if text and text != task_dir and text not in roots:
            roots.append(text)
    return roots


# LLM: _output_artifact_refs reads only output.json artifact paths for repair targets.
# 函数用途: 从失败 child 的 output_ref 提取目标产物路径，供 repair_contract 明确验证对象。
def _output_artifact_refs(output_ref: str) -> list[str]:
    payload = _read_json_object(output_ref)
    refs: list[str] = []
    for item in payload.get("artifacts") or []:
        if isinstance(item, dict):
            refs.append(str(item.get("path") or item.get("file_path") or item.get("ref") or ""))
        else:
            refs.append(str(item or ""))
    return _unique_text(refs)


# LLM: _repair_target_refs merges declared artifacts with failed file/content test targets.
# 函数用途: 让修复合同包含缺失 xlsx/html 等目标路径，即使 child output 没把它写进 artifacts。
def _repair_target_refs(refs: _RepairRefs) -> list[str]:
    return _unique_text([*_output_artifact_refs(refs.output_ref), *_test_target_refs(refs.test_ref)])


# LLM: _test_target_refs extracts target paths from persisted failed test records.
# 函数用途: 从 test_execution.json 的 validation_result.path 中恢复需要修复/验证的产物路径。
def _test_target_refs(test_ref: str) -> list[str]:
    payload = _read_json_object(test_ref)
    refs: list[str] = []
    for record in payload.get("records") or []:
        if not isinstance(record, dict):
            continue
        result = record.get("validation_result")
        if isinstance(result, dict):
            refs.append(str(result.get("path") or ""))
    return _unique_text(refs)


# LLM: _task_goal reads only the persisted task contract, never product bodies.
# 函数用途: 从失败 child 的 task.json 取原始 goal，供修复建议保持完整任务目标。
def _task_goal(task_ref: str) -> str:
    payload = _read_json_object(task_ref)
    return str(payload.get("goal") or "").strip()


# LLM: _task_acceptance_checks reads inherited full-success gates from task.json.
# 函数用途: 提取失败 child 的原始 acceptance_checks，避免 repair worker 只修最近一个症状。
def _task_acceptance_checks(task_ref: str) -> list[str]:
    payload = _read_json_object(task_ref)
    raw = payload.get("acceptance_checks")
    if not isinstance(raw, list):
        return []
    return _unique_text([str(item) for item in raw])


# LLM: _original_contract_goal_text keeps the repair goal compact while preserving full success criteria.
# 函数用途: 把原始 goal/验收条件摘要拼入修复目标；没有 task_ref 时返回空字符串。
def _original_contract_goal_text(task_ref: str) -> str:
    goal = _task_goal(task_ref)
    checks = _task_acceptance_checks(task_ref)
    parts = []
    if goal:
        parts.append(f"原始任务目标：{goal}")
    if checks:
        parts.append("原始完整验收要求：" + "；".join(checks[:8]))
    return ("。".join(parts) + "。") if parts else ""


# LLM: _read_json_object tolerates missing or malformed refs in dispatch summaries.
# 函数用途: 安全读取小型机器 JSON；失败返回空对象，不影响 dispatch 主流程。
def _read_json_object(path: str) -> dict[str, object]:
    if not path:
        return {}
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# LLM: _unique_text mirrors the local refs de-duplication used by repair payload helpers.
# 函数用途: 过滤空字符串并保持 artifact refs 的首次出现顺序。
def _unique_text(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = " ".join(str(value or "").split())
        if text and text not in unique:
            unique.append(text)
    return unique
