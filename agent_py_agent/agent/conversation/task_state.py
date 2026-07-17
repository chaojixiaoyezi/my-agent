from __future__ import annotations


# LLM: Completion is a structured runtime transition; model wording never has authority here.
# 函数用途: 判断当前运行是否已经把结构化工作记录收口。
def conversation_task_completed(task_attributes: object) -> bool:
    attrs = task_attributes if isinstance(task_attributes, dict) else {}
    return attrs.get("conversation_task_completed") is True


__all__ = ["conversation_task_completed"]
