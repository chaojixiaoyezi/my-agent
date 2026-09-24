"""三宿主种子准备只保存只读来源：约 4.2M 字符历史在准备阶段不再同时驻留两份完整正文，发送边界解析仍完整。"""
from __future__ import annotations

import gc
import hashlib
import json
import tracemalloc

import pytest

from agent_py_agent.agent.conversation.agent_thread import _agent_thread_history_seed
from agent_py_agent.agent.conversation.background_context import TaskScopeDecision
from agent_py_agent.agent.conversation.background_history_seed import (
    BackgroundHistoryProjection,
    project_background_history_seed,
)
from agent_py_agent.agent.conversation.compact import load_conversation_compact_source
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactView
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.history_seed import (
    seed_provider_history_messages,
    seed_text_messages,
)
from agent_py_agent.agent.gateway_parts import request_context, request_prompt
from agent_py_agent.tests.test_compact_source_lifetime import _case


# LLM: 与 12.4 旧基线脚本同一 fixture（128 行、约 4.2M 字符）与同一 tracemalloc 口径；只量种子准备，不量摘要或发送。
# 函数用途: 按宿主调用原准备入口生成种子，并返回准备后驻留与峰值。
def _prepare_seed(agent, thread, host):
    source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
    if host == "gateway":
        context = request_context.GatewayConversationContext(
            thread_id=thread.thread_id,
            history_source=request_context._gateway_history_source(source.messages, "current", None),
        )
        return request_prompt.gateway_conversation_history_seed(context)
    if host == "child":
        view = ConversationCompactView(thread.thread_id, 0, "", source.messages, {}, {}, 1000, False)
        return _agent_thread_history_seed(agent, view)
    projection = BackgroundHistoryProjection(thread.thread_id, TaskScopeDecision("", frozenset(), None, False), {}, 1000)
    return project_background_history_seed(agent, projection, source.messages, scope_applied=True)


@pytest.mark.parametrize("host", ["gateway", "child", "background"])
def test_host_seed_preparation_keeps_only_addresses_and_resolves_complete_history(tmp_path, host):
    agent, thread, ids, expected, chars = _case(tmp_path)
    gc.collect()
    tracemalloc.start()
    try:
        seed = _prepare_seed(agent, thread, host)
        prepared_current, prepared_peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        canonical = seed_provider_history_messages(seed)
        resolved_current, resolved_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"{host} chars={chars} seed_current={prepared_current} seed_peak={prepared_peak} "
          f"native_resolved_current={resolved_current} native_resolved_peak={resolved_peak}")
    # 旧实现同一 fixture 准备后驻留约 8.5MB、峰值约 8.6–9.0MB；只读来源只保存地址，远低于正文大小。
    assert seed.source is not None and not seed.messages and not seed.canonical_messages
    assert prepared_current < chars // 4
    assert prepared_peak < chars // 2
    # 发送边界解析仍逐字完整：同一完整 JSON hash 与行数。
    digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False).encode()).digest()
    assert digest == expected
    assert len(seed_text_messages(seed)) == len(ids)
