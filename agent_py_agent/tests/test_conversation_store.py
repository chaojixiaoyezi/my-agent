from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.runtime_errors import DataCorruptionError


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


def test_thread_records_owner_identity_when_provided(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")

    thread = store.get_or_create_thread({
        'canonical_user_id': "user-1",
        'owner_id': "providers/feishu/users/u001",
        'owner_home': "/tmp/home/owners/providers/feishu/users/u001",
        'channel': "feishu",
        'channel_conversation_id': "chat-1",
        'channel_user_id': "user-1",
        'now': 100.0,
    })

    loaded = store.load_thread(thread.thread_id)
    assert loaded is not None
    assert loaded.owner_id == "providers/feishu/users/u001"
    assert loaded.owner_home.endswith("/owners/providers/feishu/users/u001")


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


def test_progress_policy_bad_file_is_reported_without_hiding_good_due_policy(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    policy = store.set_progress_policy({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    bad_path = store.policies_dir / "bad-policy.json"
    bad_path.write_text("{not-json", encoding="utf-8")

    due, errors = store.due_progress_policies_report(now=72.0)

    assert [item.policy_id for item in due] == [policy.policy_id]
    assert errors
    assert errors[0]["context"] == "conversation.progress_policy.read"
    assert errors[0]["policy_id"] == "bad-policy"
    assert errors[0]["path"] == str(bad_path)


def test_update_task_status_keeps_thread_binding(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "开发网站", 'now': 2.0})

    link = store.update_task_status({'task_id': "task-1", 'status': "DONE", 'now': 3.0})
    links = store.task_links(thread.thread_id)
    stored_thread = store.load_thread(thread.thread_id)

    assert link is not None
    assert link.status == "DONE"
    assert links[0].task_id == "task-1"
    assert links[0].status == "DONE"
    assert stored_thread is not None
    assert stored_thread.task_ids == ("task-1",)
    assert stored_thread.active_task_ids == ()


def test_update_task_status_expected_status_does_not_overwrite_terminal_race(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "开发网站", "now": 2.0}
    )
    store.update_task_status({"task_id": "task-1", "status": "completed", "now": 3.0})

    rejected = store.update_task_status(
        {
            "task_id": "task-1",
            "status": "cancelled",
            "expected_status": "active",
            "now": 4.0,
        }
    )
    link = store.task_links(thread.thread_id)[0]

    assert rejected is None
    assert link.status == "completed"


def test_bind_task_preserves_existing_identity_and_only_fills_missing_path(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "原始用户目标", "now": 2.0}
    )
    filled = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "定时唤醒：等待子任务完成",
            "task_path": str(tmp_path / "task-root"),
            "now": 3.0,
        }
    )
    repeated = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "子代理内部提示",
            "task_path": str(tmp_path / "wrong-root"),
            "now": 4.0,
        }
    )

    assert filled.goal == "原始用户目标"
    assert repeated.goal == "原始用户目标"
    assert repeated.task_path == str(tmp_path / "task-root")
    assert repeated.created_at == first.created_at == 2.0


def test_bind_task_rejects_cross_thread_rebind_and_preserves_terminal_index(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    first = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    second = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-2",
            "channel_user_id": "user-1",
            "now": 2.0,
        }
    )
    store.bind_task(
        {"thread_id": first.thread_id, "task_id": "task-1", "goal": "原始目标", "now": 3.0}
    )
    completed = store.bind_task(
        {
            "thread_id": first.thread_id,
            "task_id": "task-1",
            "goal": "不应覆盖",
            "status": "completed",
            "now": 4.0,
        }
    )

    with pytest.raises(ValueError, match="another conversation thread"):
        store.bind_task(
            {"thread_id": second.thread_id, "task_id": "task-1", "goal": "跨线程覆盖", "now": 5.0}
        )

    loaded = store.task_links(first.thread_id)[0]
    stored_thread = store.load_thread(first.thread_id)
    assert completed.goal == loaded.goal == "原始目标"
    assert completed.status == "completed"
    assert stored_thread is not None
    assert stored_thread.task_ids == ("task-1",)
    assert stored_thread.active_task_ids == ()


def test_concurrent_task_bindings_merge_thread_indexes_without_lost_ids(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    task_ids = [f"task-{index}" for index in range(16)]

    def bind(task_id: str) -> None:
        store.bind_task(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": f"并发任务 {task_id}",
            }
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bind, task_ids))

    loaded = store.load_thread(thread.thread_id)
    assert loaded is not None
    assert set(loaded.task_ids) == set(task_ids)
    assert set(loaded.active_task_ids) == set(task_ids)
    assert {item.task_id for item in store.task_links(thread.thread_id)} == set(task_ids)


def test_thread_for_task_reports_corrupt_task_link(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-bad", 'goal': "长期任务", 'now': 2.0})
    store._task_path("task-bad").write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.thread_for_task("task-bad")

    resolved, error = store.thread_for_task_report("task-bad")
    assert resolved is None
    assert error is not None
    assert error["context"] == "conversation.thread_for_task"
    assert error["task_id"] == "task-bad"
    assert error["path"] == str(store._task_path("task-bad"))


def test_thread_for_task_reports_corrupt_linked_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bind_task({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期任务", 'now': 2.0})
    store._thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.thread_for_task("task-1")

    resolved, error = store.thread_for_task_report("task-1")
    assert resolved is None
    assert error is not None
    assert error["context"] == "conversation.thread.read"
    assert error["thread_id"] == thread.thread_id
    assert error["path"] == str(store._thread_path(thread.thread_id))


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


def test_list_threads_report_keeps_good_threads_when_one_thread_file_is_bad(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    good = store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    bad_path = store.threads_dir / "bad-thread.json"
    bad_path.write_text("{bad-json", encoding="utf-8")

    threads, errors = store.list_threads_report()

    assert [item.thread_id for item in threads] == [good.thread_id]
    assert errors
    assert errors[0]["context"] == "conversation.thread.read"
    assert errors[0]["thread_id"] == "bad-thread"
    assert errors[0]["path"] == str(bad_path)


def test_resolve_thread_report_reports_corrupt_bindings_index(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bindings_path.write_text("{bad-json", encoding="utf-8")

    thread, error = store.resolve_thread_report(
        channel="internal",
        channel_conversation_id="thread-1",
        channel_user_id="user-1",
    )

    assert thread is None
    assert error is not None
    assert error["context"] == "conversation.bindings.read"
    assert error["path"] == str(store.bindings_path)


def test_latest_thread_for_user_report_reports_corrupt_latest_index(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.user_latest_path.write_text("{bad-json", encoding="utf-8")

    thread, error = store.latest_thread_for_user_report("user-1")

    assert thread is None
    assert error is not None
    assert error["context"] == "conversation.user_latest.read"
    assert error["path"] == str(store.user_latest_path)


def test_get_or_create_thread_does_not_duplicate_when_bindings_index_is_corrupt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.bindings_path.write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 2.0})

    threads, errors = store.list_threads_report()
    assert len(threads) == 1
    assert errors == []


def test_get_or_create_thread_does_not_duplicate_when_latest_index_is_corrupt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.user_latest_path.write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.get_or_create_thread({'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "thread-2", 'channel_user_id': "user-1", 'reuse_latest_for_user': True, 'now': 2.0})

    threads, errors = store.list_threads_report()
    assert len(threads) == 1
    assert errors == []
