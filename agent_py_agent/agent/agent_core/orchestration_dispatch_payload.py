# LLM: Dispatch payload helpers keep orchestration tools thin and refs-only.
# 模块用途: 构建 dispatch_subagents 工具返回给 runner 的轻量记录，不读取正文或执行动作。

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .orchestration_artifact_integrity_repair import artifact_integrity_repair_record_payload


# LLM: _RepairRefs keeps top-level parent-repair helper signatures bundle-shaped.
# 类用途: 保存 rejected acceptance 修复所需的小报告引用，避免 helper 继续扩散散参。
@dataclass(frozen=True)
class _RepairRefs:
    test_ref: str
    followup_ref: str
    output_ref: str
    run_ref: str


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
    )


# LLM: _top_level_repair_tool_call uses create_subagents because top-level root cannot schedule children directly.
# 函数用途: 给主代理可复制的顶层修复派工参数，包含失败 refs 和继承的产物写入根。
def _top_level_repair_tool_call(
    item,
    refs: _RepairRefs,
) -> dict[str, object]:
    return {
        "tool": "create_subagents",
        "count": 1,
        "role": "worker",
        "workflow_mode": "off",
        "goal": _repair_goal(item, refs),
        "extra_write_roots": _product_write_roots(refs.run_ref),
        "acceptance_checks": [
            "只修复父级验收 failure_refs 点名的问题",
            "修复后读取被修改文件并说明验证结果",
            "不要改写无关产物或健康分支",
        ],
        "allowed_tools": [
            "list_files",
            "read_file",
            "search_text",
            "replace_in_file",
            "write_file",
            "append_file",
        ],
    }


# LLM: _repair_goal keeps top-level repair work grounded in machine refs instead of natural summaries.
# 函数用途: 拼出修复 worker 的任务目标，包含 run id、测试失败摘要和可读取报告路径。
def _repair_goal(item, refs: _RepairRefs) -> str:
    details = "; ".join(str(value) for value in (getattr(item, "parent_acceptance_test_failure_details", []) or [])[:4])
    return (
        f"修复父级验收失败的子代理 run：{item.run_id}。"
        f"先读取这些 refs：test_ref={refs.test_ref}; followup_ref={refs.followup_ref}; "
        f"output_ref={refs.output_ref}; run_ref={refs.run_ref}。"
        f"失败摘要：{getattr(item, 'parent_acceptance_test_failure_summary', '') or '查看 test_ref'}。"
        f"失败细节：{details or '查看 test_execution.json'}。"
        "只修复父级验收报告点名的问题；完成后写回文件并说明验证结果。"
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
