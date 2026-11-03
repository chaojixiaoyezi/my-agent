"""插话落盘和旧线程回填均从宿主字段取来源，不授权内部 guidance。"""
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.learning_source import user_dialogue_projection
from agent_py_agent.agent.conversation.models import GuidanceEntry
from agent_py_agent.agent.conversation.store_guidance_acknowledgements import (
    GuidanceAcknowledgements,
)
from agent_py_agent.agent.gateway_parts.control_guidance_payload import control_guidance_payload
from agent_py_agent.agent.memory_store.curator_inputs import CuratorMessageSource, _collect_messages
from agent_py_agent.agent.memory_store.learning_source_authority import ConversationLearningSources


def test_steer_message_has_user_dialogue_source(tmp_path):
    scope = SimpleNamespace(user_id="SYNTHETIC_ONLY_USER", channel="tui", conversation_id="SYNTHETIC_ONLY_CHAT",
                            metadata={}, resolved_owner=None, input_media=())
    command = SimpleNamespace(value="SYNTHETIC_ONLY_RULE")
    payload = control_guidance_payload("request", "SYNTHETIC_ONLY_REQUEST", command, scope, "thread", "turn")
    assert payload["metadata"]["learning_source"] == user_dialogue_projection()
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store, "tui")
    payload["metadata"]["thread_id"] = thread.thread_id
    ack = object.__new__(GuidanceAcknowledgements)
    ack._append_message_once = store.messages.append_once
    entry = GuidanceEntry(guidance_id="SYNTHETIC_ONLY_GUIDANCE", target_type="request", target_id="req",
                          message=command.value, metadata=payload["metadata"])
    assert ack.project_transcript(entry)
    messages, errors = store.messages.recent_report(thread.thread_id, limit=0)
    assert not errors and len(messages) == 1
    assert messages[0].metadata["learning_source"] == user_dialogue_projection()


def test_subagent_guidance_not_user_dialogue():
    captured = []
    ack = object.__new__(GuidanceAcknowledgements)
    ack._append_message_once = lambda message, **kwargs: captured.append(message)
    entry = GuidanceEntry(guidance_id="SYNTHETIC_ONLY_GUIDANCE", target_type="agent_run", target_id="run",
        message="SYNTHETIC_ONLY_RULE", metadata={"record_in_transcript": True, "thread_id": "thread",
        "kind": "user_agent_control", "learning_source": user_dialogue_projection()})
    assert ack.project_transcript(entry)
    assert "learning_source" not in captured[0]["metadata"]


def _thread(store, channel):
    return store.threads.get_or_create({"canonical_user_id": "SYNTHETIC_ONLY_USER", "channel": channel,
        "channel_conversation_id": "SYNTHETIC_ONLY_CHAT", "channel_user_id": "SYNTHETIC_ONLY_USER", "now": 10.0})


@pytest.mark.parametrize("channel,learnable", [("tui", True), ("web", True), ("feishu", True),
    ("scheduler", False), ("automation", False), ("subagent", False), ("internal", False)])
def test_legacy_thread_backfill(tmp_path, channel, learnable):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store, channel)
    messages = [store.messages.append({"thread_id": thread.thread_id, "role": role, "channel": channel,
        "content": "SYNTHETIC_ONLY_RULE", "now": 11.0, "metadata": {"request_id": "req-" + role}})
        for role in ("user", "assistant", "tool")]
    sources = ConversationLearningSources(store, "SYNTHETIC_ONLY_USER")
    assert sources.message(thread.thread_id, messages[0].message_id)["can_learn"] is learnable
    assert sources.request(thread.thread_id, "req-user")["can_learn"] is learnable
    for message in messages[1:]:
        assert sources.message(thread.thread_id, message.message_id)["can_learn"] is False
    collected, errors = _collect_messages(source=CuratorMessageSource(store), cursors={}, limit=20, max_chars=20000)
    assert not errors
    user = next(item for item in collected if item.role == "user")
    assert bool(user.thread_source.get("can_learn")) is learnable
    assert all(not item.thread_source.get("can_learn") for item in collected if item.role != "user")


def test_legacy_automation_message_not_laundered(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _thread(store, "tui")
    message = store.messages.append({"thread_id": thread.thread_id, "role": "user", "channel": "tui",
        "content": "SYNTHETIC_ONLY_RULE", "metadata": {"scheduler_job_id": "job"}})
    assert ConversationLearningSources(store, "SYNTHETIC_ONLY_USER").message(thread.thread_id, message.message_id)["can_learn"] is False


def test_legacy_user_preference_passes_real_promotion_source_gate(tmp_path):
    import hashlib

    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.tests.test_memory_promotion_v2 import _runtime

    service, store, _ = _runtime(tmp_path)
    thread = store.threads.get_or_create({'canonical_user_id': 'user-1', 'channel': 'tui',
        'channel_conversation_id': 'SYNTHETIC_ONLY_LEGACY_CHAT', 'channel_user_id': 'user-1', 'now': 12})
    assert not thread.learning_source  # 旧字段缺失由ConversationStore规范化为空对象，仍属unknown。
    text = 'SYNTHETIC_ONLY_PREFERENCE：回复请用简体中文。'
    message = store.messages.append({'thread_id': thread.thread_id, 'role': 'user', 'channel': 'tui',
        'content': text, 'now': 13})
    ref = {'thread_id': thread.thread_id, 'message_id': message.message_id, 'role': 'user',
        'quote': text, 'content_hash': 'sha256:' + hashlib.sha256(text.encode()).hexdigest()}
    source = ConversationLearningSources(store, 'user-1').message(thread.thread_id, message.message_id)
    candidate = service.candidates.observe(CandidateObservation('user_preference', text, 'SYNTHETIC_ONLY_LANGUAGE',
        MemoryScope('personal', 'personal'), 'user_explicit', source_message_refs=(ref,), learning_source=source,
        promotion_target='user', promotion_mode='auto_eligible', confidence=0.99))
    result = service.promote(candidate.candidate_id, automatic=True)
    reviewed = service.candidates.get(candidate.candidate_id)
    assert reviewed.status == ('promoted' if result.promoted else 'approved')
    assert reviewed.learning_source['can_learn'] is True
    # 成功晋升必须持久化promoted；仅精确IO阻塞允许保留approved，不能把其它失败伪装成来源通过。
    assert result.promoted or result.reason_code == 'PROMOTION_IO_BLOCKED'
