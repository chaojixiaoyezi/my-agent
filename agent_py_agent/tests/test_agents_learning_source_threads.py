"""B 路学习来源：线程级落点的真实存储集成测试（真实 ConversationStore）。"""

import json
from pathlib import Path

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls


def _store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


def _create(store: ConversationStore, chat_id: str, **extra):
    request = {
        "canonical_user_id": "user-1",
        "channel": "internal",
        "channel_conversation_id": chat_id,
        "channel_user_id": "user-1",
        "now": 10.0,
        **extra,
    }
    return store.threads.get_or_create(request)


def test_thread_with_user_source_round_trips_to_disk(tmp_path):
    store = _store(tmp_path)
    thread = _create(store, "chat-1", learning_source=ls.user_dialogue_projection())
    loaded = store.threads.load(thread.thread_id)
    assert loaded.learning_source == ls.user_dialogue_projection()
    payload = json.loads(store.storage.thread_path(thread.thread_id).read_text(encoding="utf-8"))
    assert payload["learning_source"]["can_learn"] is True
    assert payload["learning_source"]["source_kind"] == "user_dialogue"


def test_thread_without_source_omits_key_and_stays_unknown(tmp_path):
    store = _store(tmp_path)
    thread = _create(store, "chat-2")
    assert thread.learning_source == {}
    payload = json.loads(store.storage.thread_path(thread.thread_id).read_text(encoding="utf-8"))
    assert "learning_source" not in payload


def test_bad_source_value_is_normalized_to_unknown(tmp_path):
    store = _store(tmp_path)
    thread = _create(store, "chat-3", learning_source={"can_learn": True})
    assert thread.learning_source == {}


def test_existing_thread_source_is_not_overwritten(tmp_path):
    store = _store(tmp_path)
    first = _create(store, "chat-4", learning_source=ls.dispatch_projection())
    again = _create(store, "chat-4", learning_source=ls.user_dialogue_projection())
    assert again.thread_id == first.thread_id
    assert again.learning_source == ls.dispatch_projection()


def test_dispatch_thread_messages_are_unlearnable_while_user_thread_is_learnable(tmp_path):
    store = _store(tmp_path)
    user_thread = _create(store, "chat-5", learning_source=ls.user_dialogue_projection())
    dispatch_thread = _create(store, "chat-6", learning_source=ls.dispatch_projection())
    user_message = store.messages.append(
        {"thread_id": user_thread.thread_id, "role": "user", "content": "hello", "now": 11.0}
    )
    dispatch_message = store.messages.append(
        {"thread_id": dispatch_thread.thread_id, "role": "user", "content": "task book", "now": 11.0}
    )
    owner = "user-1"
    user_facts = ls.facts_for_message(
        user_message.metadata,
        owner=owner,
        thread_id=user_thread.thread_id,
        thread_source=user_thread.learning_source,
    )
    dispatch_facts = ls.facts_for_message(
        dispatch_message.metadata,
        owner=owner,
        thread_id=dispatch_thread.thread_id,
        thread_source=dispatch_thread.learning_source,
    )
    assert user_facts.can_learn is True
    assert dispatch_facts.can_learn is False
    assert dispatch_facts.source_kind == "dispatch"


def test_background_turn_in_user_thread_is_unlearnable(tmp_path):
    store = _store(tmp_path)
    thread = _create(store, "chat-7", learning_source=ls.user_dialogue_projection())
    background_message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "done",
            "now": 11.0,
            "metadata": {"background_delivery_reason": "owner_delivery", "task_id": "task-1"},
        }
    )
    facts = ls.facts_for_message(
        background_message.metadata,
        owner="user-1",
        thread_id=thread.thread_id,
        thread_source=thread.learning_source,
    )
    assert facts.can_learn is False
    assert facts.source_kind == "background"
