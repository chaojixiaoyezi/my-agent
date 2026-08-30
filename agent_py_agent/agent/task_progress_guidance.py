from __future__ import annotations

"""Shared model guidance for closing exact task-progress items.

This module only builds bounded advisory facts.  It never mutates the progress
ledger, interprets task prose, or decides that work is complete.
"""

from typing import Any

# LLM: Keep the exact-id closeout reminder shared by tool results and background
# task state. Callers decide which current-generation ids are open; this module
# must never recover ids from titles, goals, final text, or artifact names.
# 模块用途: 统一生成 Todo 收尾软提醒，避免工具回执和后台续轮各写一套不同规则。

_OPEN_ID_LIMIT = 24


# LLM: This switch controls extra model-context guidance only. It must not alter
# ledger writes, task lifecycle, final-response delivery, or completion status.
# 函数用途: 读取 Todo 最终回复前核对提示开关；旧配置缺字段时沿用开启默认值。
def task_progress_closeout_guidance_enabled(agent: object) -> bool:
    config = getattr(agent, "config", None)
    return bool(getattr(config, "task_progress_closeout_guidance_enabled", True))


# LLM: This always-visible sentence mirrors 会话运行时/终端交互 plan discipline but remains
# advisory. It must never inspect prose, mutate a ledger, schedule a continuation, or gate final.
# 函数用途: 给每轮模型上下文加入 Todo 实时更新与最终回复前核对纪律，避免长任务后清单停在旧状态。
def task_progress_model_discipline() -> str:
    return "task_progress 是软清单；完成即按 exact id 更新，回复前核对；未完/阻塞如实保留，宿主不自动打勾或续跑。"


# LLM: The returned contract is advisory and exact-id-only. A model may close an
# item only from current evidence; the host neither validates nor auto-applies it.
# 函数用途: 把当前仍开放的 Todo 编号整理成最终回复前的一次软核对说明。
def task_progress_closeout_contract(open_item_ids: object) -> dict[str, Any]:
    values = open_item_ids if isinstance(open_item_ids, list | tuple) else ()
    exact_ids = list(
        dict.fromkeys(
            str(value or "").strip()[:128]
            for value in values
            if str(value or "").strip()
        )
    )
    if not exact_ids:
        return {}
    return {
        "schema_version": "task-progress-closeout-guidance.v1",
        "severity": "soft",
        "blocking": False,
        "open_item_ids": exact_ids[:_OPEN_ID_LIMIT],
        "open_count": len(exact_ids),
        "ids_truncated": len(exact_ids) > _OPEN_ID_LIMIT,
        "before_final": {
            "tool": "task_progress",
            "action": "update",
            "identity_field": "items[].id",
            "matching": "exact_id_only",
            "completed_rule": "update_only_when_current_evidence_proves_complete",
            "unfinished_rule": "continue_when_possible_or_leave_open_and_report_truthfully",
        },
        "host_behavior": "never_auto_close_never_completion_gate",
        "instruction": (
            "最终回复前核对这些 exact id：本轮已有证据完成的项先用 task_progress 更新原 id；"
            "仍未完成、阻塞、过时或无法确认的项保持原状态并如实处理。不要按标题、最终文案或产物名猜完成。"
        ),
    }


__all__ = [
    "task_progress_closeout_contract",
    "task_progress_closeout_guidance_enabled",
    "task_progress_model_discipline",
]
