# LLM: Dispatch payload helpers keep orchestration tools thin and refs-only.
# 模块用途: 构建 dispatch_subagents 工具返回给 runner 的轻量记录，不读取正文或执行动作。

from __future__ import annotations


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
    payload.update(_dispatch_record_acceptance_payload(item))
    return payload


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
