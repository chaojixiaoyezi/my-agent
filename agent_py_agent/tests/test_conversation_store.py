from __future__ import annotations

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic


def test_thread_messages_and_channel_bindings_survive_restart(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")

    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "feishu", 'channel_conversation_id': "feishu-chat-1", 'channel_user_id': "feishu-user-1", 'title': "长期研究任务", 'now': 1000.0})
    store.append_message({'thread_id': thread.thread_id, 'role': "user", 'content': "帮我持续跟进这个任务。", 'channel': "feishu", 'channel_message_id': "msg-1", 'now': 1001.0})
    store.bind_channel({'thread_id': thread.thread_id, 'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "wechat-chat-1", 'channel_user_id': "wechat-user-1", 'now': 1002.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期跟进 GitHub 项目", 'now': 1003.0})
    store.update_summary(thread.thread_id, "用户要求跨渠道延续同一任务。", now=1004.0)

    reopened = ConversationStore(tmp_path / "conversations")
    resolved = reopened.resolve_thread(
        channel="wechat",
        channel_conversation_id="wechat-chat-1",
        channel_user_id="wechat-user-1",
    )
    messages = reopened.recent_messages(thread.thread_id, limit=5)
    bundle = reopened.context_bundle(thread.thread_id)

    assert resolved is not None
    assert resolved.thread_id == thread.thread_id
    assert resolved.canonical_user_id == "user-1"
    assert messages[0].content == "帮我持续跟进这个任务。"
    assert bundle["thread"]["summary"] == "用户要求跨渠道延续同一任务。"
    assert bundle["tasks"][0]["task_id"] == "task-1"
    assert bundle["channel_bindings"][1]["channel"] == "wechat"


def test_progress_policy_due_and_mark_reported(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "local-thread", 'channel_user_id': "local-user", 'now': 10.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "每小时汇报一次", 'now': 11.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "local-thread", 'now': 12.0})

    assert store.due_progress_policies(now=71.0) == []
    assert store.due_progress_policies(now=72.0)[0].policy_id == policy.policy_id

    store.mark_progress_reported(policy.policy_id, now=72.0)
    updated = store.get_progress_policy(policy.policy_id)

    assert updated is not None
    assert updated.last_report_at == 72.0
    assert updated.next_due_at == 132.0


def test_update_json_file_atomic_updates_under_single_file_transaction(tmp_path) -> None:
    path = tmp_path / "state.json"

    first = update_json_file_atomic(path, lambda data: {"count": int(data.get("count") or 0) + 1})
    second = update_json_file_atomic(path, lambda data: {"count": int(data.get("count") or 0) + 1})

    assert first["count"] == 1
    assert second["count"] == 2


def test_new_unbound_channel_does_not_implicitly_mix_latest_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    first = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "feishu", 'channel_conversation_id': "chat-a", 'channel_user_id': "user-a", 'now': 10.0})
    second = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "chat-b", 'channel_user_id': "user-b", 'now': 20.0})

    assert second.thread_id != first.thread_id

    resumed = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "qq", 'channel_conversation_id': "chat-c", 'channel_user_id': "user-c", 'reuse_latest_for_user': True, 'now': 30.0})

    assert resumed.thread_id == second.thread_id
