# LLM: 能力包 v2 块 3：回合正常返回后，用 AgentRunResult.pack_verifications（核验账本生成的结构化事实）撰写一条宿主提示
#   （source=pack_verification），排入同会话原提示队列并返回给当轮一起发布与提交；同来源同 code 的旧提示被替换。
#   文字只来自结构化事实，不读模型回答；模型看不到这条提示。没有核验事实、没有会话线程时什么都不做。
#   块 4/5 起，只有输入原件被就地改、或只有必需交付物缺失（没有任何检查结果）的回合也要发：三类事实任一非空就发。
#   改动同步 test_pack_verification_service.py 与 request_execution 的收尾提示批次。
# 模块用途: 让用户在回合结束时看到宿主亲自跑的能力包检查结论，而不是模型的自述。
from __future__ import annotations

from ..capability.pack_verification_report import (
    NOTICE_CODE,
    NOTICE_SOURCE,
    pack_verification_notice_text,
)
from ..conversation.host_notices import HostNotice, host_notice, queue_host_notice

# 有任一项就要给用户发宿主提示的事实：检查结果、被就地改的输入原件、缺的必需交付物。
_NOTICE_FACT_KEYS = ("results", "inputs_modified", "deliverables_missing")


# LLM: 只接收本轮 AgentRunResult；队列写失败不改变业务结果，也不另开恢复通道。
# 函数用途: 把本轮宿主核验结论排入提示队列，并返回当轮需要一起发布与提交的提示。
def queue_pack_verification_notice(context: object, conversation: object, result: object) -> tuple[HostNotice, ...]:
    facts = getattr(result, "pack_verifications", None)
    thread_id = str(getattr(conversation, "thread_id", "") or "")
    if not isinstance(facts, dict) or not thread_id or not any(facts.get(key) for key in _NOTICE_FACT_KEYS):
        return ()
    notice = host_notice(NOTICE_SOURCE, NOTICE_CODE, pack_verification_notice_text(facts), details={
        "result_count": len(facts.get("results") or []), "rework_count": facts.get("rework_count", 0),
        "closeout_checked": facts.get("closeout_checked", False),
        "inputs_modified_count": len(facts.get("inputs_modified") or []),
        "deliverables_missing_count": len(facts.get("deliverables_missing") or [])})
    store = getattr(context.agent, "conversation_store", None)
    return (notice,) if queue_host_notice(store, thread_id, notice, replace_same_code=True) else ()


__all__ = ["queue_pack_verification_notice"]
