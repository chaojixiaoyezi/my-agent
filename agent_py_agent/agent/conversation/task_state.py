from __future__ import annotations

from .models import THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES


# LLM: Completion is a structured runtime transition; model wording never has authority here.
# 函数用途: 判断当前运行是否已经把结构化工作记录收口。
def conversation_task_completed(task_attributes: object) -> bool:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    return attrs.get("conversation_task_completed") is True


# LLM: TaskRun closeout may consume only canonical link states that end the current
# execution authority. ``interrupted`` is terminal for this run even though an explicit
# later resume may create/reopen another attempt; arbitrary aliases stay fail-closed.
# 函数用途: 判断持久会话任务链接是否已经结束当前执行，不读取模型回复或临时属性。
def conversation_task_link_is_terminal(status: object) -> bool:
    return str(status or "").strip().lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES


__all__ = ["conversation_task_completed", "conversation_task_link_is_terminal"]
