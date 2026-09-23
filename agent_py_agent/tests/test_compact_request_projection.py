"""原Compact候选和CAS携带对应完整请求材料，错误投影不回退粗估。"""
import pytest

from agent_py_agent.agent.conversation import compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.conversation.compact_projection import ConversationCompactProjection
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.tests.test_gateway_compact_deferred_source import _pressure_thread


def test_original_fallback_commits_its_own_projection_not_last_candidate(tmp_path, monkeypatch):
    agent, _, _, context = _pressure_thread(tmp_path)
    agent.config.memory_compact_auto_trigger_percent = 90
    agent.config.memory_compact_recovery_target_percent = 60
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, context, request_id="short-" + role,
                                                   role=role, content="应保留的短尾部")
    thread = agent.conversation_store.threads.require(context.thread_id)
    source = compact.load_conversation_compact_source(agent, agent.conversation_store, thread)
    views, materials = [], []

    def project(view):
        views.append(view)
        material = object()
        materials.append(material)
        tokens = source.policy.trigger_tokens + 1 if len(views) != 2 else source.policy.trigger_tokens - 1
        return ConversationCompactProjection(tokens, material)

    monkeypatch.setattr(compact, "_summarize", lambda *_a, **_k: "完整来源中的阶段摘要")
    monkeypatch.setattr(compact, "_projected_context_tokens", lambda *_a, **_k: pytest.fail("完整投影不能调用粗估"))
    result = compact.prepare_conversation_context(
        agent, agent.conversation_store, thread,
        options=compact.ConversationCompactOptions(current_prompt="继续", source=source, request_projector=project),
    )
    assert len(views) == 3 and views[0].is_candidate is False
    assert views[1].is_candidate and views[2].is_candidate
    assert result.compacted and result.thread.compact_generation == 1
    assert result.messages == views[1].messages
    assert result.request_projection.material is materials[1]
    assert result.request_projection.material is not materials[-1]
    assert result.projected_tokens == source.policy.trigger_tokens - 1


@pytest.mark.parametrize("bad", [None, "unknown", -1, True, 1.5])
def test_unknown_projection_never_becomes_legacy_estimate_or_zero_tokens(tmp_path, monkeypatch, bad):
    agent, _, _, context = _pressure_thread(tmp_path)
    thread = agent.conversation_store.threads.require(context.thread_id)
    monkeypatch.setattr(compact, "_projected_context_tokens", lambda *_a, **_k: pytest.fail("未知投影不能改走粗估"))
    value = bad if bad is None or isinstance(bad, str) else ConversationCompactProjection(bad, object())
    with pytest.raises(ConversationCompactError) as error:
        compact.prepare_conversation_context(
            agent, agent.conversation_store, thread,
            options=compact.ConversationCompactOptions(force=True, request_projector=lambda _: value),
        )
    assert error.value.code == "COMPACT_REQUEST_PROJECTION_UNKNOWN"
    assert agent.conversation_store.threads.require(context.thread_id).compact_generation == 0
