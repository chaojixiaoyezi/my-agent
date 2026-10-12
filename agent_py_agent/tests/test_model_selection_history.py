"""换模型后沿真实 Compact 存储恢复历史，全部正文为测试生成。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.model.selection_history import (
    commit_model_selection_history,
    prepare_model_selection_history,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.tool_ir import AssistantTurn, CompactionSummary, UserTurn
from agent_py_agent.agent.conversation import auxiliary_model_call
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    load_conversation_compact_source,
    prepare_conversation_context,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_provider_surface import (
    ConversationCompactProviderSurface,
)
from agent_py_agent.agent.conversation.compact_scope import THREAD_COMPACT_SCOPE
from agent_py_agent.agent.conversation.history_seed import freeze_history_source
from agent_py_agent.agent.conversation.models import ConversationHistorySeed
from agent_py_agent.tests.test_compact_remote_provider import BLOCK, _thread


# LLM: 参数使用产品 frozen dataclass，暴露错误直接赋值；身份和工具账不由模型切换重新构造。
# 函数用途: 构造具有稳定列表引用的合成请求，供历史恢复与提交测试共用。
def _params(**changes):
    return ToolLoopExecuteParams(
        user_prompt="核对合成资料", memories=[], runtime_injections=[], prompt_files=[],
        tool_catalog_section="", tool_recommendations_section="", tool_context=[],
        effective_on_chunk=None, allowed_tools=None, write_boundary=None, task_attributes={},
        request_id="current-request", run_id="run", task_id="task", one_shot_tool_calls=set(),
        executed_tools=["read_file"], archive_tool_calls=[], **changes,
    )


def test_reasoning_cleanup_preserves_tools_unknown_blocks_and_original_containers():
    unknown = {"type": "future_media", "payload": {"value": 3}}
    original = _params(tool_ir_history=[AssistantTurn("保留正文", content_blocks=[
        {"type": "thinking", "thinking": "old", "signature": "old"}, unknown,
    ])], provider_history_messages=[{"role": "assistant", "content": [
        {"type": "responses_reasoning", "encrypted_content": "old"}, {"type": "text", "text": "保留历史"},
    ]}])
    history, prior, executed = original.tool_ir_history, original.provider_history_messages, original.executed_tools
    prepared = prepare_model_selection_history(SimpleNamespace(), original, None)
    assert len(history[0].content_blocks) == 2
    assert prepared.tool_ir_history[0].content_blocks == [unknown]
    commit_model_selection_history(original, prepared)
    assert original.tool_ir_history is history and original.provider_history_messages is prior
    assert original.executed_tools is executed and executed == ["read_file"]
    assert prior[0]["content"] == [{"type": "text", "text": "保留历史"}]


@pytest.mark.parametrize("has_seed", [True, False])
def test_incompatible_compact_restores_original_thread_rows_and_steering(tmp_path, monkeypatch, has_seed):
    backend = SimpleNamespace(name="openai_responses", model_name="fake", max_tokens=2000,
        supports_remote_compaction=lambda: True,
        provider_compaction_scope=lambda: {"protocol": "openai_responses", "endpoint": "https://chatgpt.com/backend-api/codex"})
    agent, thread = _thread(tmp_path, backend)
    monkeypatch.setattr(auxiliary_model_call, "generate_auxiliary_model_response",
                        lambda _: ModelResponse(text="", backend="fake", assistant_content_blocks=[BLOCK]))
    source = load_conversation_compact_source(agent, agent.conversation_store, thread, scope=THREAD_COMPACT_SCOPE)
    result = prepare_conversation_context(agent, agent.conversation_store, thread, options=ConversationCompactOptions(
        source=source, force=True, exclude_request_id="current-request",
        provider_surface=ConversationCompactProviderSurface("稳定前缀", None, "摘要系统")))
    assert result.compacted
    old = load_conversation_compact_source(agent, agent.conversation_store, result.thread, scope=THREAD_COMPACT_SCOPE)
    seed = ConversationHistorySeed(source=freeze_history_source(old.messages, project_row=lambda row, **kw: row)) if has_seed else None
    params = _params(compact_context=old.compact_context, conversation_history_seed=seed,
        tool_ir_history=[CompactionSummary("旧密文占位", source="applied_compact"), UserTurn("原请求")],
        active_turn_user_inputs=[{"schema_version": "active-turn-user-input.v1", "input_ids": ["steer-1"], "text": "保留新增条目"}])
    agent.backend = SimpleNamespace(name="anthropic_compatible", model_name="MiniMax-M2.7", max_tokens=2000)
    if not has_seed:
        with pytest.raises(ConversationCompactError, match="原历史来源"):
            prepare_model_selection_history(agent, params, result.thread)
        return
    prepared = prepare_model_selection_history(agent, params, result.thread)
    assert prepared.compact_context.view.provider_compaction == {}
    assert len(prepared.conversation_history_seed.source.rows) == 8
    assert len(prepared.provider_history_messages) == 8
    assert any(item.input_ids == ("steer-1",) for item in prepared.tool_ir_history if isinstance(item, UserTurn))
    assert not any(isinstance(item, CompactionSummary) for item in prepared.tool_ir_history)
    assert params.compact_context is old.compact_context
    # 已携带的输入不会因重复投影再加入第二份。
    again = prepare_model_selection_history(agent, replace(prepared, compact_context=old.compact_context), result.thread)
    assert sum("steer-1" in item.input_ids for item in again.tool_ir_history if isinstance(item, UserTurn)) == 1
