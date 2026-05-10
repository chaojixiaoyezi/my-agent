# LLM: Dispatch payload helpers keep orchestration tools thin and refs-only.
# 模块用途: 构建 dispatch_subagents 工具返回给 runner 的轻量记录，不读取正文或执行动作。

from __future__ import annotations

from pathlib import Path


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
