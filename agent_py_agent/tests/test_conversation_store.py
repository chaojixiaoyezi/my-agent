from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
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


def test_detached_named_task_binds_exact_existing_message_anchor(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    anchor = store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "先确认来源字段。",
            "now": 11.0,
        }
    )
    link = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全监测",
            "cancellation_scope": "detached",
            "now": 12.0,
        }
    )
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "后来无关聊天",
            "now": 13.0,
        }
    )
    rebound = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "audit-1",
            "goal": "持续研判",
            "work_kind": "audit",
            "work_name": "安全监测",
            "cancellation_scope": "detached",
            "task_path": str(tmp_path / "task"),
            "now": 14.0,
        }
    )

    assert link.context_anchor_message_id == anchor.message_id
    assert rebound.context_anchor_message_id == anchor.message_id


def test_untyped_audit_child_workspace_status_sync_stays_under_audit_root(tmp_path) -> None:
    owner_home = tmp_path / "owner"
    task_root = owner_home / "audits" / "audit-1" / "work" / "agents" / "child-1"
    work_dir = task_root / "work"
    work_dir.mkdir(parents=True)
    state_path = work_dir / "state.json"
    state_path.write_text(
        json.dumps({"task_id": "child-1", "status": "RUNNING"}),
        encoding="utf-8",
    )
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "audit-child",
            "channel_user_id": "user-1",
            "owner_home": str(owner_home),
        }
    )

    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "child-1",
            "goal": "处理一个审计来源",
            "status": "completed",
            "task_path": str(task_root),
        }
    )

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "DONE"


def test_v4_thread_record_loads_with_safe_v5_compact_defaults(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "legacy-user",
            "channel": "feishu",
            "channel_conversation_id": "legacy-chat",
            "channel_user_id": "legacy-user",
            "now": 100.0,
        }
    )
    path = store._thread_path(thread.thread_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "conversation_thread.v4"
    for field_name in (
        "compact_checkpoint_id",
        "compact_consecutive_failures",
        "compact_failure_updated_at",
        "compact_failure_code",
    ):
        payload.pop(field_name, None)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    reopened = ConversationStore(tmp_path / "conversations")
    loaded = reopened.load_thread(thread.thread_id)

    assert loaded is not None
    assert loaded.compact_checkpoint_id == ""
    assert loaded.compact_consecutive_failures == 0
    assert loaded.compact_failure_updated_at == 0.0
    assert loaded.compact_failure_code == ""


def test_compact_generation_cas_is_atomic_across_store_instances(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "cas-user",
            "channel": "feishu",
            "channel_conversation_id": "cas-chat",
            "channel_user_id": "cas-user",
        }
    )

    def commit(label: str):
        independent_store = ConversationStore(root)
        return independent_store.update_compact_state(
            thread.thread_id,
            commit=ConversationCompactCommit(
                summary=f"summary-{label}",
                operation_evidence={},
                checkpoint_id=f"checkpoint-{label}",
                compacted_through_message_id=f"message-{label}",
                compacted_through_byte_offset=100,
                source_messages=2,
            ),
            expected_generation=0,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(commit, label) for label in ("a", "b")]
    successes = []
    failures = []
    for future in futures:
        try:
            successes.append(future.result())
        except RuntimeError as exc:
            failures.append(exc)

    stored = store.load_thread(thread.thread_id)
    assert len(successes) == 1
    assert len(failures) == 1
    assert "generation changed" in str(failures[0])
    assert stored is not None
    assert stored.compact_generation == 1
    assert stored.compact_checkpoint_id in {"checkpoint-a", "checkpoint-b"}


def test_delayed_message_does_not_move_thread_activity_backwards(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 100.0,
        }
    )
    store.append_message(
        {"thread_id": thread.thread_id, "role": "user", "content": "较新的消息", "now": 200.0}
    )
    store.append_message(
        {"thread_id": thread.thread_id, "role": "assistant", "content": "延迟补写", "now": 150.0}
    )

    loaded = store.load_thread(thread.thread_id)
    messages = store.recent_messages(thread.thread_id)

    assert loaded is not None
    assert loaded.updated_at == 200.0
    assert [(item.content, item.created_at) for item in messages] == [
        ("较新的消息", 200.0),
        ("延迟补写", 150.0),
    ]


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


def test_selected_workspace_task_survives_completion_and_restart(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-sticky-workspace",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    workspace = tmp_path / "owner" / "tasks" / "project-one"
    workspace.mkdir(parents=True)
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成项目一",
            "status": "active",
            "task_path": str(workspace),
            "now": 2.0,
        }
    )

    selected = store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "now": 3.0}
    )
    store.update_task_status(
        {"task_id": "task-1", "status": "completed", "now": 4.0}
    )
    reopened_store = ConversationStore(tmp_path / "conversations")
    reopened = reopened_store.load_thread(thread.thread_id)

    assert selected.workspace_task_id == "task-1"
    assert reopened is not None
    assert reopened.workspace_task_id == "task-1"
    assert reopened.active_task_ids == ()
    payload = json.loads(reopened_store._thread_path(thread.thread_id).read_text(encoding="utf-8"))
    assert payload["schema_version"] == "conversation_thread.v6"


def test_thread_persists_client_cwd_across_requests_without_override(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    project = tmp_path / "project"
    project.mkdir()
    request = {
        "canonical_user_id": "local-agent",
        "channel": "chat",
        "channel_conversation_id": "cwd-session",
        "channel_user_id": "local-agent",
        "cwd": str(project),
        "runtime_workspace_roots": [str(project)],
        "now": 1.0,
    }

    first = store.get_or_create_thread(request)
    second = store.get_or_create_thread(
        {
            "canonical_user_id": "local-agent",
            "channel": "chat",
            "channel_conversation_id": "cwd-session",
            "channel_user_id": "local-agent",
            "now": 2.0,
        }
    )

    assert first.cwd == str(project)
    assert first.runtime_workspace_roots == (str(project),)
    assert second.thread_id == first.thread_id
    assert second.cwd == str(project)
    assert second.runtime_workspace_roots == (str(project),)


def test_stale_message_snapshot_cannot_revert_selected_workspace(tmp_path, monkeypatch) -> None:
    store = ConversationStore(tmp_path / "conversations")
    owner_home = tmp_path / "owner"
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "owner_home": str(owner_home),
            "channel": "feishu",
            "channel_conversation_id": "thread-stale-message",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first_workspace = owner_home / "tasks" / "first"
    second_workspace = owner_home / "tasks" / "second"
    first_workspace.mkdir(parents=True)
    second_workspace.mkdir(parents=True)
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-first",
            "goal": "第一个项目",
            "task_path": str(first_workspace),
            "now": 2.0,
        }
    )
    store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-first", "now": 3.0}
    )
    stale = store.load_thread(thread.thread_id)
    assert stale is not None and stale.workspace_task_id == "task-first"

    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-second",
            "goal": "第二个项目",
            "task_path": str(second_workspace),
            "now": 4.0,
        }
    )
    store.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-second", "now": 5.0}
    )

    # Simulate a request that loaded the thread before the workspace switch.
    monkeypatch.setattr(store, "_require_thread", lambda _thread_id: stale)
    store.append_message(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "切换发生前已经在处理的消息",
            "now": 6.0,
        }
    )
    store.append_observation(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "迟到的进度",
            "now": 7.0,
        }
    )
    store.bind_channel(
        {
            "thread_id": thread.thread_id,
            "canonical_user_id": "user-1",
            "owner_home": str(owner_home),
            "channel": "chat",
            "channel_conversation_id": "same-user-local",
            "channel_user_id": "user-1",
            "now": 8.0,
        }
    )
    store.update_summary(thread.thread_id, "最新摘要", now=9.0)
    store.update_verbose_level(thread.thread_id, "full", now=10.0)

    loaded = store.load_thread(thread.thread_id)
    assert loaded is not None
    assert loaded.workspace_task_id == "task-second"
    assert loaded.task_ids == ("task-first", "task-second")
    assert loaded.summary == "最新摘要"
    assert loaded.verbose_level == "full"
    assert loaded.updated_at == 10.0


def test_selected_workspace_task_rejects_task_from_another_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    first = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-one",
            "channel_user_id": "user-1",
        }
    )
    second = store.get_or_create_thread(
        {
            "canonical_user_id": "user-2",
            "channel": "feishu",
            "channel_conversation_id": "thread-two",
            "channel_user_id": "user-2",
        }
    )
    workspace = tmp_path / "owner-two" / "tasks" / "private-project"
    workspace.mkdir(parents=True)
    store.bind_task(
        {
            "thread_id": second.thread_id,
            "task_id": "task-two",
            "goal": "第二条会话的项目",
            "task_path": str(workspace),
        }
    )

    with pytest.raises(ValueError, match="not bound to conversation thread"):
        store.select_workspace_task(
            {"thread_id": first.thread_id, "task_id": "task-two"}
        )

    unchanged = store.load_thread(first.thread_id)
    assert unchanged is not None
    assert unchanged.workspace_task_id == ""


def test_task_link_lifecycle_projects_to_owner_workspace_state(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-workspace-state",
            "channel_user_id": "user-1",
            "owner_home": str(tmp_path / "owner"),
            "now": 1.0,
        }
    )
    task_root = tmp_path / "owner" / "tasks" / "2026-07-16" / "task-one"
    state_path = task_root / "work" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"version": 1, "task_id": "task-1", "status": "RUNNING"}),
        encoding="utf-8",
    )
    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成一个任务",
            "status": "active",
            "task_path": str(task_root),
            "now": 2.0,
        }
    )

    store.update_task_status(
        {"task_id": "task-1", "status": "completed", "expected_status": "active", "now": 3.0}
    )
    completed = json.loads(state_path.read_text(encoding="utf-8"))
    store.update_task_status(
        {"task_id": "task-1", "status": "active", "expected_status": "completed", "now": 4.0}
    )
    store.update_task_status(
        {"task_id": "task-1", "status": "interrupted", "expected_status": "active", "now": 5.0}
    )
    interrupted = json.loads(state_path.read_text(encoding="utf-8"))

    assert completed["status"] == "DONE"
    assert completed["updated_at"] == "1970-01-01T00:00:03+00:00"
    assert interrupted["status"] == "PAUSED"
    assert interrupted["updated_at"] == "1970-01-01T00:00:05+00:00"


def test_task_link_lifecycle_does_not_overwrite_another_workspace_identity(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-workspace-identity",
            "channel_user_id": "user-1",
            "owner_home": str(tmp_path / "owner"),
        }
    )
    task_root = tmp_path / "owner" / "tasks" / "wrong-link"
    state_path = task_root / "work" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"version": 1, "task_id": "task-other", "status": "RUNNING"}),
        encoding="utf-8",
    )

    linked = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得覆盖别的任务状态",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    completed = store.update_task_status({"task_id": "task-1", "status": "completed"})

    assert linked.task_id == "task-1"
    assert completed is not None and completed.status == "completed"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "task_id": "task-other",
        "status": "RUNNING",
    }


def test_task_link_lifecycle_does_not_write_outside_owner_tasks(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-workspace-path",
            "channel_user_id": "user-1",
            "owner_home": str(tmp_path / "owner"),
        }
    )
    outside = tmp_path / "outside"
    state_path = outside / "work" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({"version": 1, "task_id": "task-1", "status": "RUNNING"}),
        encoding="utf-8",
    )

    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得写出 owner task 根目录",
            "status": "active",
            "task_path": str(outside),
        }
    )
    store.update_task_status({"task_id": "task-1", "status": "completed"})

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "RUNNING"


def test_task_link_lifecycle_does_not_follow_state_symlink_outside_task(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    owner_home = tmp_path / "owner"
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-workspace-symlink",
            "channel_user_id": "user-1",
            "owner_home": str(owner_home),
        }
    )
    outside_state = tmp_path / "outside-state.json"
    outside_state.write_text(
        json.dumps({"version": 1, "task_id": "task-1", "status": "RUNNING"}),
        encoding="utf-8",
    )
    task_root = owner_home / "tasks" / "task-with-symlink"
    work_root = task_root / "work"
    work_root.mkdir(parents=True)
    (work_root / "state.json").symlink_to(outside_state)

    store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得通过状态链接写出任务目录",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    store.update_task_status({"task_id": "task-1", "status": "completed"})

    assert json.loads(outside_state.read_text(encoding="utf-8"))["status"] == "RUNNING"


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


def test_bind_task_cannot_resurrect_terminal_task_but_explicit_update_can(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.bind_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "长任务", "now": 2.0}
    )
    cancelled = store.update_task_status(
        {"task_id": "task-1", "status": "cancelled", "expected_status": "active", "now": 3.0}
    )
    rebound = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "重启恢复中的旧快照",
            "status": "active",
            "task_path": str(tmp_path / "task-root"),
            "now": 4.0,
        }
    )

    assert cancelled is not None and cancelled.status == "cancelled"
    assert rebound.status == "cancelled"
    assert rebound.task_path == str(tmp_path / "task-root")
    assert store.load_thread(thread.thread_id).active_task_ids == ()

    reopened = store.update_task_status(
        {"task_id": "task-1", "status": "active", "expected_status": "cancelled", "now": 5.0}
    )
    assert reopened is not None and reopened.status == "active"
    assert store.load_thread(thread.thread_id).active_task_ids == ("task-1",)


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


def test_concurrent_same_named_audit_reservation_allows_exactly_one(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
        }
    )

    def reserve(task_id: str) -> str:
        try:
            store.bind_task(
                {
                    "thread_id": thread.thread_id,
                    "task_id": task_id,
                    "goal": "持续检查",
                    "status": "active",
                    "work_kind": "audit",
                    "work_name": "同名检查",
                    "cancellation_scope": "detached",
                }
            )
            return "created"
        except ValueError:
            return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("audit-a", "audit-b")))

    assert sorted(results) == ["created", "duplicate"]
    links = store.task_links(thread.thread_id)
    assert len(links) == 1
    assert links[0].work_kind == "audit"
    assert links[0].work_name == "同名检查"


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


def test_mark_progress_failed_records_backoff_and_retires(tmp_path) -> None:
    """失败续跑记账(问题6):写失败账+退避顺延;连续失败达阈值退休;未知 policy 安全。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-fail-store",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    policy = store.set_progress_policy(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "route_channel": "internal",
            "route_target": thread.thread_id,
            "now": 20.0,
        }
    )

    # 未知 policy:安全返回 None,不抛。
    assert store.mark_progress_failed("policy-nope", now=21.0, backoff_seconds=300, failure_count=1) is None

    # 第 1 次失败:记账 + 退避顺延,enabled 保持。
    failed = store.mark_progress_failed(
        policy.policy_id, now=30.0, backoff_seconds=300.0, failure_count=1
    )
    assert failed is not None and failed.enabled is True
    assert failed.metadata["failure_count"] == 1
    assert failed.metadata["last_failure_at"] == 30.0
    assert failed.metadata["last_backoff_seconds"] == 300.0
    assert failed.next_due_at == 30.0 + 300.0

    # 阈值内再失败:账累加,仍 enabled。
    failed2 = store.mark_progress_failed(
        policy.policy_id, now=40.0, backoff_seconds=600.0, failure_count=2
    )
    assert failed2.enabled is True
    assert failed2.metadata["failure_count"] == 2

    # 达退休阈值:enabled=False,离开 due 扫描,账目保留供复盘。
    retired = store.mark_progress_failed(
        policy.policy_id, now=50.0, backoff_seconds=1200.0, failure_count=3
    )
    assert retired.enabled is False
    assert retired.metadata["failure_count"] == 3
    assert retired.metadata["retired_at"] == 50.0
    assert policy.policy_id not in {p.policy_id for p in store.due_progress_policies(now=100.0)}
