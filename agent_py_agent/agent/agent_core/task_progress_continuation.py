from __future__ import annotations

"""Standalone 任务 task_progress 账本驱动自动续跑。

真机铁证(2026-08-07, scrapy/celery→Go 复刻):两个普通 gateway 请求都开了
task_progress 账本、跑了一轮工具调用(2-10 个)、模型回复「接下来会…继续推进」
的中间汇报 → runtime_status=ok + reason=tool_result → 被判定 terminal →
workspace 标 DONE(progress 0.0、artifact 空、账本 1 pending + coverage 9 项
incomplete 全被无视)。根因:_mark_open_goal_progress_unfinished 的账本驱动被
thread_goal_id(/goal)限制,普通 standalone 任务(无 conversation thread)的
task_progress 账本不参与生命周期判定。

本模块在 _run_with_params 的 compact 续跑决策之后追加一道账本驱动决策:
账本有未 closed 项 → 请求内自动续跑(注入账本结构化事实 + nudge,模型继续推进
未完成工作),直到账本 closed 或达上限(默认 3 轮,可配)停止。达上限后账本仍
open → 如实收口 unfinished(TASK_PROGRESS_LIMIT_REACHED)→ workspace 标
BLOCKED 等用户,绝不标 DONE 撒谎。thread_goal_id(/goal)任务走既有对话机制
不在此通道;conversation_thread_id/conversation_task_id 不排除(gateway 每个
请求都带,详见 decision 注释,2026-08-07 真机铁证)。

结构化信号铁律:判定只看 task_progress 账本的状态字段(items/coverage.counts),
不匹配任何模型自然语言文案。
"""

from dataclasses import dataclass
from typing import Any

_DEFAULT_CONTINUE_LIMIT = 3


@dataclass(frozen=True)
class TaskProgressContinuationDecision:
    should_continue: bool
    reason: str
    injection: str = ""
    user_prompt: str = ""


def configured_task_progress_continue_limit(agent: object, params: object) -> int:
    """续跑上限:task_attributes 显式覆盖 → config 兜底 → 默认 3。"""
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and attrs.get("task_progress_continue_limit") is not None:
        # 注意:只能用显式键判空,不能 `or 0`——task_attributes 常被运行时注入
        # run_workspace 等结构,缺键时 `or 0` 会把默认 3 覆盖成 1(2026-08-07 实锤)。
        try:
            return max(1, int(attrs.get("task_progress_continue_limit")))
        except (TypeError, ValueError):
            pass
    try:
        configured = int(
            getattr(getattr(agent, "config", None), "task_progress_auto_continue_limit", 0) or 0
        )
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else _DEFAULT_CONTINUE_LIMIT


def task_progress_continuation_decision(
    agent: object,
    params: object,
    result: object,
    *,
    depth: int = 0,
) -> TaskProgressContinuationDecision:
    """判定本轮结束后是否按 task_progress 账本自动续跑。

    全部输入都是结构化信号:
      - 作用域:thread_goal_id(/goal)任务走既有对话机制,不在请求内续跑。
        conversation_thread_id/conversation_task_id **不能**排除——gateway 每个
        /ask 请求都带这对属性(2026-08-07 真机铁证),而 conversation 侧的账本
        续跑唤醒(_schedule_typed_unfinished_continuation)只认 thread_goal_id,
        无 goal 的 conversation 任务若被排除就进无人区,假 DONE 依旧。
      - 本轮 outcome:只有正常结束(ok)才可能续跑;等待、失败、已收口不续跑。
      - 账本:ledger_open_progress_item_count > 0 才算未完成(空账本/全 done 不续跑)。
      - 轮数:depth 达到上限即停止,防止模型永远不 closed 账本的死循环。
    """
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and str(attrs.get("thread_goal_id") or "").strip():
        return TaskProgressContinuationDecision(False, "conversation_scoped")
    if str(getattr(result, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return TaskProgressContinuationDecision(False, "not_ok")
    task_id = _ledger_key(agent, params)
    if not task_id:
        return TaskProgressContinuationDecision(False, "no_task_id")
    from ..conversation.runtime import ledger_open_progress_item_count

    open_count = ledger_open_progress_item_count(agent, task_id)
    if open_count <= 0:
        return TaskProgressContinuationDecision(False, "ledger_closed")
    limit = configured_task_progress_continue_limit(agent, params)
    if int(depth or 0) >= limit:
        return TaskProgressContinuationDecision(False, "limit_reached")
    progress = _read_progress_ledger(agent, task_id)
    return TaskProgressContinuationDecision(
        True,
        "ledger_open",
        build_task_progress_continue_injection(
            progress,
            depth=int(depth or 0) + 1,
            limit=limit,
            task_id=task_id,
            open_count=open_count,
        ),
        task_progress_continue_user_prompt(),
    )


def build_task_progress_continue_injection(
    progress: dict[str, Any],
    *,
    depth: int,
    limit: int,
    task_id: str,
    open_count: int,
) -> str:
    """构造续跑轮注入:账本结构化事实 + nudge,不含模型正文推断。"""
    next_action = str(progress.get("next_action") or "").strip()
    counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
    summary = str(progress.get("summary") or "").strip()
    sections = [
        "# Task Progress Continuation",
        f"continuation_round: {depth}/{limit}",
        f"task_id: {task_id}",
        f"open_ledger_items: {open_count}",
    ]
    if isinstance(counts, dict):
        detail = "、".join(
            f"{status}:{count}"
            for status, count in counts.items()
            if str(count or "").strip() and str(count) != "0"
        )
        if detail:
            sections.append(f"ledger_counts: {detail}")
    if next_action:
        sections.append(f"next_action: {next_action}")
    if summary:
        sections.append(f"ledger_summary: {summary}")
    sections.extend(
        [
            "",
            "任务账本仍有未完成项。继续推进账本中的未完成工作：",
            "- 优先执行 next_action 或账本里 pending/in_progress 的项；",
            "- 每完成一项就调用 task_progress 更新账本状态并记录证据；",
            "- 全部 closed 之后才写最终交付；本轮不要总结、不要宣告完成。",
        ]
    )
    return "\n".join(sections)


def task_progress_continue_user_prompt() -> str:
    return (
        "继续推进任务账本中未完成的工作（见 # Task Progress Continuation 注入）。"
        "每完成一项就更新账本并记录证据；全部账本项 closed 之后再写最终交付与总结。"
    )


def mark_task_progress_continued(result: Any, source_result: Any, *, depth: int) -> Any:
    result.task_progress_auto_continued = True
    result.task_progress_auto_continue_depth = depth
    return result


def mark_task_progress_limit_reached(result: Any) -> Any:
    """续跑达上限后账本仍 open:如实收口为 unfinished,绝不标 DONE。

    只覆盖正常结束(ok)的结果;等待、失败、已收口的语义保持不变。
    """
    if str(getattr(result, "runtime_status", "ok") or "ok").strip().lower() != "ok":
        return result
    result.runtime_status = "unfinished"
    result.runtime_reason = "TASK_PROGRESS_LIMIT_REACHED"
    result.runtime_source = "task_progress"
    return result


def _ledger_key(agent: object, params: object) -> str:
    """账本 key 必须与 task_progress 工具的读写 key 完全一致。

    会话任务(带 conversation_task_id)用 progress_ledger_id:gateway 每轮请求派生
    新任务身份(req_2→req_3→req_0),按 conversation_task_id 读=读错位账本=0 pending
    =不续跑(真机实证 2026-08-07);progress_ledger_id 按 task_path 指纹寻址,跨请求
    稳定。非会话场景保持 durable_task_id 原语义(任务身份),零变化。
    """
    attrs = getattr(params, "task_attributes", None)
    if isinstance(attrs, dict) and str(attrs.get("conversation_task_id") or "").strip():
        from .runtime.task_identity import progress_ledger_id

        return str(progress_ledger_id(agent, params) or "").strip()
    from .runtime.task_identity import durable_task_id

    return str(durable_task_id(params) or "").strip()


def _read_progress_ledger(agent: object, task_id: str) -> dict[str, Any]:
    try:
        from .runtime.owner_roots import runtime_owner_root
        from ..task_progress import read_task_progress

        progress = read_task_progress(runtime_owner_root(agent), task_id)
        return progress if isinstance(progress, dict) else {}
    except Exception:
        return {}


__all__ = [
    "TaskProgressContinuationDecision",
    "build_task_progress_continue_injection",
    "configured_task_progress_continue_limit",
    "mark_task_progress_continued",
    "mark_task_progress_limit_reached",
    "task_progress_continuation_decision",
    "task_progress_continue_user_prompt",
]
