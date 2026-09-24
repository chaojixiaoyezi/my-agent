"""Gateway 会话上下文历史的测试辅助：按生产原规则从只读来源解析，替代已移除的具体副本字段。"""
from __future__ import annotations

from agent_py_agent.agent.conversation.history_projection import project_history_row
from agent_py_agent.agent.conversation.history_seed import freeze_history_source
from agent_py_agent.agent.conversation.models import MessageLogEntry


# LLM: 与旧 GatewayConversationContext.history 同口径：已选中行按生产单行规则投影后的全部 (role, content)，不过滤空正文。
# 函数用途: 取得 Gateway 上下文的历史供断言。
def context_history(conversation) -> tuple[tuple[str, str], ...]:
    source = conversation.history_source
    return () if source is None else tuple((row.role, row.content) for row in source.projected_rows())


# LLM: 只构造已裁决的内存会话行，投影规则沿生产 project_history_row。
# 函数用途: 用给定 (role, content) 构造 Gateway 上下文的只读历史来源。
def history_source_of(*turns):
    rows = tuple(
        MessageLogEntry(message_id=f"history-{index}", thread_id="thread", role=role, content=content)
        for index, (role, content) in enumerate(turns)
    )
    return freeze_history_source(rows, project_row=project_history_row)
