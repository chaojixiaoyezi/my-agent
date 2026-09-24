"""Gateway 与 child 在原准备边界冻结同一 Compact 摘要及原文范围。"""

from __future__ import annotations

import time

from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.conversation.agent_thread import (
    AgentThreadTurnInput,
    prepare_subagent_thread_turn,
)
from agent_py_agent.agent.conversation.compact_checkpoint import (
    CompactCheckpointRequest,
    write_compact_checkpoint,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE, CompactScope
from agent_py_agent.agent.conversation.history_seed import (
    history_source_text_messages,
    seed_text_messages,
)
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.gateway_parts.request_binding import gateway_message_work_scope
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_prompt import gateway_conversation_history_seed
from agent_py_agent.tests.test_gateway_conversation_compact import _agent, _request
from agent_py_agent.tests.test_subagent_compact_recovery import _child

# LLM: 测试账本经真实 writer 与线程 CAS 提交，允许交错 scope，但不替换 resolver 或宿主读取。
# 函数用途: 写入一条指定摘要范围的检查点，验证准备边界不会误用提交链最新局部摘要。

# LLM: 测试只经生产文本规则从上下文只读来源解析历史，替代已移除的具体副本字段。
# 函数用途: 取得 Gateway 上下文的 (role, content) 历史供断言。
def _context_history(conversation):
    source = conversation.history_source
    return () if source is None else history_source_text_messages(source)

def _commit_row(agent, thread_id, *, row, summary, scope, publish):
    store = agent.conversation_store
    thread = store.threads.require(thread_id)
    checkpoint_id = write_compact_checkpoint(agent, CompactCheckpointRequest(
        thread=thread, summary=summary, operation_evidence={}, compact_rows=(row,), retained_tail=(),
        source_end_byte_offset=(thread.compact_generation + 1) * 100,
        projected_tokens_before=900, projected_tokens_after=300,
        policy=runtime_compact_policy(agent), forced=False, scope=scope,
    ))
    return store.threads.update_compact_state(
        thread_id,
        commit=ConversationCompactCommit(
            summary=summary, operation_evidence={}, checkpoint_id=checkpoint_id,
            compacted_through_message_id=row.message_id,
            compacted_through_byte_offset=(thread.compact_generation + 1) * 100,
            source_messages=thread.compact_source_messages + 1,
            source_tool_pairs=thread.compact_source_tool_pairs,
            publish_thread_view=publish,
        ),
        expected_generation=thread.compact_generation,
        now=time.time(),
    )


def test_gateway_thread_seed_keeps_interleaved_local_row_after_global_cursor(tmp_path):
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request("ou_scope_gateway")
    initial = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, request, "create", "开始", defer_compact=True,
    ))
    store = agent.conversation_store
    first = store.messages.append({"thread_id": initial.thread_id, "role": "user", "content": "全线程第一段"})
    _commit_row(agent, initial.thread_id, row=first, summary="全线程摘要一", scope=THREAD_COMPACT_SCOPE, publish=True)
    local = store.messages.append({"thread_id": initial.thread_id, "role": "assistant", "content": "局部任务原文"})
    _commit_row(agent, initial.thread_id, row=local, summary="局部摘要不得进入普通轮",
                scope=CompactScope(kind="turn", turn_id="audit-only"), publish=False)
    before_next_global = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, request, "between", "继续", defer_compact=True,
    ))
    between_seed = gateway_conversation_history_seed(before_next_global)
    assert before_next_global.compact_generation == 2
    assert between_seed is not None and between_seed.compact_generation == 1
    assert between_seed.compact_summary == "全线程摘要一"
    normal = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, request, "normal-between", "继续",
    ))
    normal_seed = gateway_conversation_history_seed(normal)
    assert normal_seed is not None and normal_seed == gateway_conversation_history_seed(before_next_global)
    assert normal.compact_context == before_next_global.compact_context
    last = store.messages.append({"thread_id": initial.thread_id, "role": "user", "content": "全线程第二段"})
    _commit_row(agent, initial.thread_id, row=last, summary="全线程摘要二", scope=THREAD_COMPACT_SCOPE, publish=True)

    loaded = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, request, "follow", "继续", defer_compact=True,
    ))
    assert loaded.compact_context is not None
    assert loaded.compact_context.scope == THREAD_COMPACT_SCOPE
    assert loaded.compact_context.view.summary == loaded.compact_summary == "全线程摘要二"
    assert loaded.compact_source is not None
    assert loaded.compact_source.compact_context == loaded.compact_context
    assert [row.message_id for row in loaded.compact_source.messages] == [local.message_id]
    assert [text for _role, text in _context_history(loaded)] == ["局部任务原文"]
    seed = gateway_conversation_history_seed(loaded)
    assert seed is not None and seed.compact_summary == "全线程摘要二"
    assert seed_text_messages(seed) == (("assistant", "局部任务原文"),)


def test_child_thread_seed_ignores_latest_local_checkpoint(tmp_path):
    agent, task = _child(tmp_path, backend="anthropic_compatible", tools=False)
    store = agent.conversation_store
    thread_id = task.agent_thread_id
    old = store.messages.recent(thread_id, limit=0)[0]
    _commit_row(agent, thread_id, row=old, summary="局部活动轮摘要",
                scope=CompactScope(kind="turn", turn_id="other-turn"), publish=False)

    current = prepare_subagent_thread_turn(
        agent, task, turn=AgentThreadTurnInput("继续核对", "attempt-current", "turn-current"),
        defer_compact=True,
    )
    assert current.compact_context is not None
    assert current.compact_context.scope == THREAD_COMPACT_SCOPE
    assert current.compact_context.view.checkpoint_id == ""
    assert current.history_seed is not None and current.history_seed.compact_summary == ""
    assert current.compact_generation == 1 and current.history_seed.compact_generation == 0
    assert current.compact_source is not None
    assert old.message_id in {row.message_id for row in current.compact_source.messages}


def test_gateway_audit_prepare_does_not_inherit_thread_or_sibling_summary(tmp_path):
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request("ou_scope_audit")
    initial = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, request, "create", "开始", defer_compact=True,
    ))
    store = agent.conversation_store
    ordinary = store.messages.append({"thread_id": initial.thread_id, "role": "user", "content": "普通对话事实"})
    _commit_row(agent, initial.thread_id, row=ordinary, summary="不属于本Audit的全线程摘要",
                scope=THREAD_COMPACT_SCOPE, publish=True)
    sibling = store.messages.append({
        "thread_id": initial.thread_id, "role": "user", "content": "兄弟Audit隐私",
        "metadata": {"conversation_work_kind": "audit", "conversation_work_name": "兄弟Audit",
                     "conversation_task_id": "sibling-audit"},
    })
    audit_request = {
        **request,
        "system_task": {"kind": "audit_prepare", "attributes": {
            "conversation_audit_prepare": True,
            "conversation_work_kind": "audit",
            "conversation_work_name": "当前Audit",
        }},
        "conversation_audit_scope": {"audit_id": "current-audit", "name": "当前Audit", "status": "preparing"},
    }
    loaded = gateway_conversation_context(GatewayConversationLoadRequest(
        agent, audit_request, "audit-request", "准备", defer_compact=True,
    ))
    assert loaded.compact_context is not None
    assert loaded.compact_context.scope == CompactScope(
        kind="turn", task_id="current-audit", turn_id="audit-request",
    )
    assert loaded.compact_summary == ""
    assert loaded.compact_source is not None
    source_ids = {row.message_id for row in loaded.compact_source.messages}
    assert ordinary.message_id in source_ids and sibling.message_id not in source_ids
    seed = gateway_conversation_history_seed(loaded, work_scope=gateway_message_work_scope(audit_request))
    assert seed is not None and seed.compact_summary == ""
    assert "兄弟Audit隐私" not in str(seed_text_messages(seed))
