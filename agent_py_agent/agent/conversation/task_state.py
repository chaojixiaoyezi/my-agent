from __future__ import annotations


# LLM: Completion is a structured task-lane transition; model wording never has authority here.
# 函数用途: 判断当前运行属性是否已经把普通会话任务从 task 切回 chat。
def conversation_task_completed(task_attributes: object) -> bool:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    return bool(
        str(attrs.get("conversation_thread_id") or "").strip()
        and str(attrs.get("conversation_task_id") or "").strip()
        and str(attrs.get("conversation_lane") or "").strip().lower() == "chat"
    )


__all__ = ["conversation_task_completed"]
