from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.models import ConversationCompactCommit
from agent_py_agent.agent.gateway_parts.io import update_json_file_atomic
from agent_py_agent.agent.runtime_errors import DataCorruptionError


def test_passive_store_open_and_missing_reads_do_not_create_directories(tmp_path) -> None:
    root = tmp_path / "missing" / "conversations"
    store = ConversationStore(root, initialize=False)

    assert store.threads.load("missing-thread") is None
    with pytest.raises(KeyError, match="unknown conversation thread"):
        store.claims.load("missing-thread")
    assert not root.parent.exists()


def test_thread_messages_and_channel_bindings_survive_restart(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")

    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "feishu", 'channel_conversation_id': "feishu-chat-1", 'channel_user_id': "feishu-user-1", 'title': "长期研究任务", 'now': 1000.0})
    store.messages.append({'thread_id': thread.thread_id, 'role': "user", 'content': "帮我持续跟进这个任务。", 'channel': "feishu", 'channel_message_id': "msg-1", 'now': 1001.0})
    store.threads.bind_channel({'thread_id': thread.thread_id, 'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "wechat-chat-1", 'channel_user_id': "wechat-user-1", 'now': 1002.0})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期跟进 GitHub 项目", 'now': 1003.0})
    store.threads.update_summary(thread.thread_id, "用户要求跨渠道延续同一任务。", now=1004.0)

    reopened = ConversationStore(tmp_path / "conversations")
    resolved = reopened.threads.resolve(
        channel="wechat",
        channel_conversation_id="wechat-chat-1",
        channel_user_id="wechat-user-1",
    )
    messages = reopened.messages.recent(thread.thread_id, limit=5)
    bundle = reopened.context_bundle(thread.thread_id)

    assert resolved is not None
    assert resolved.thread_id == thread.thread_id
    assert resolved.canonical_user_id == "user-1"
    assert messages[0].content == "帮我持续跟进这个任务。"
    assert bundle["thread"]["summary"] == "用户要求跨渠道延续同一任务。"
    assert bundle["tasks"][0]["task_id"] == "task-1"
    assert bundle["channel_bindings"][1]["channel"] == "wechat"


def test_thread_model_usage_is_idempotent_partitioned_and_survives_restart(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "user-usage", "now": 10.0}
    )
    request = {
        "event_id": "usage-event-1",
        "thread_id": thread.thread_id,
        "request_id": "request-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "source": "background_main_agent",
        "now": 11.0,
        "model_calls": {
            "schema": "model_call_summary.v1",
            "usage_breakdown": {
                "schema": "model_usage_breakdown.v1",
                "provider": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 70,
                    "cache_write_input_tokens": 5,
                    "call_count": 1,
                },
                "estimated": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "call_count": 0,
                },
            },
        },
    }

    first = store.model_usage.append_once(request)
    replay = store.model_usage.append_once({**request, "now": 99.0})
    reopened = ConversationStore(tmp_path / "conversations")

    assert first == replay
    assert reopened.model_usage.summary(thread.thread_id) == {
        "schema": "thread_model_usage_summary.v1",
        "thread_id": thread.thread_id,
        "event_count": 1,
        "provider": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 70,
            "cache_write_input_tokens": 5,
            "call_count": 1,
        },
        "estimated": {
            "input_tokens": 0,
            "output_tokens": 0,
            "call_count": 0,
            "unfinished_input_tokens": 0,
            "unfinished_call_count": 0,
        },
    }


def test_thread_model_usage_cumulative_snapshots_persist_only_new_deltas(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "user-usage-snapshot", "now": 10.0}
    )
    first_calls = _cumulative_model_calls(
        physical=1,
        input_tokens=90_000,
        output_tokens=10,
        cache_read_tokens=80_000,
    )
    second_calls = _cumulative_model_calls(
        physical=2,
        input_tokens=90_500,
        output_tokens=110,
        cache_read_tokens=80_400,
    )
    common = {
        "thread_id": thread.thread_id,
        "request_id": "request-compact-retry",
        "run_id": "run-child-1",
        "task_id": "task-child-1",
        "source": "subagent_run",
    }

    first = store.model_usage.append_snapshot_once(
        {**common, "event_id": "usage-snapshot-1", "model_calls": first_calls}
    )
    second = store.model_usage.append_snapshot_once(
        {**common, "event_id": "usage-snapshot-2", "model_calls": second_calls}
    )
    replay = store.model_usage.append_snapshot_once(
        {
            **common,
            "event_id": "usage-snapshot-2",
            "model_calls": second_calls,
            "now": 99.0,
        }
    )

    assert replay == second
    with pytest.raises(
        DataCorruptionError,
        match="snapshot event id reused with different input",
    ):
        store.model_usage.append_snapshot_once(
            {
                **common,
                "event_id": "usage-snapshot-2",
                "model_calls": {
                    **second_calls,
                    "output_tokens": 111,
                    "total_tokens": 90_611,
                },
            }
        )
    assert first.model_calls["physical_model_attempt_count"] == 1
    assert second.model_calls["physical_model_attempt_count"] == 1
    assert second.model_calls["usage_breakdown"]["provider"] == {
        "cache_read_input_tokens": 400,
        "cache_write_input_tokens": 0,
        "call_count": 1,
        "input_tokens": 500,
        "output_tokens": 100,
    }
    assert second.model_calls["ledger_projection"][
        "snapshot_physical_model_attempt_count"
    ] == 2
    assert store.model_usage.summary(thread.thread_id)["provider"] == {
        "input_tokens": 90_500,
        "output_tokens": 110,
        "cache_read_input_tokens": 80_400,
        "cache_write_input_tokens": 0,
        "call_count": 2,
    }


def test_snapshot_rows_name_the_backends_and_models_their_own_calls_used(tmp_path) -> None:
    # 前台回合与随后的唤醒回合共用 request_id，是同一本累计账分几次落盘；每行只记本行调用用到的名字。
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user-usage-identity", "now": 10.0})
    ledger = ModelCallLedger()
    scope = {"thread_id": thread.thread_id, "request_id": "gwreq-identity", "run_id": "", "task_id": ""}

    def call(call_id: str, backend: str, model: str) -> None:
        ledger.started(ModelCallStartedParams(call_id=call_id, backend=backend, model=model,
                                              input_tokens=100, request_id="gwreq-identity"))
        ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=10))

    def settle(source: str) -> dict:
        snapshot = {"schema": "model_call_summary.v1", **ledger.cumulative_summary(request_id="gwreq-identity")}
        return store.model_usage.append_snapshot_once({**scope, "source": source, "model_calls": snapshot}).model_calls

    call("call-1", "anthropic", "model-a")
    call("call-2", "anthropic", "model-a")
    foreground = settle("gateway")
    call("call-3", "openai_chat", "model-b")
    switched = settle("background_main_agent")
    call("call-4", "anthropic", "model-a")
    resumed = settle("background_main_agent")

    assert (foreground["models"], foreground["backends"]) == (["model-a"], ["anthropic"])
    assert foreground["model_attempt_counts"] == {"model-a": 2}
    assert (switched["models"], switched["backends"]) == (["model-b"], ["openai_chat"])
    assert switched["backend_attempt_counts"] == {"openai_chat": 1}
    assert (resumed["models"], resumed["backends"]) == (["model-a"], ["anthropic"])
    assert resumed["model_attempt_counts"] == {"model-a": 1}
    assert resumed["physical_model_attempt_count"] == 1
    assert resumed["purpose_breakdown"]["main"]["models"] == ["model-a"]
    assert resumed["purpose_breakdown"]["main"]["backend_attempt_counts"] == {"anthropic": 1}


def test_snapshot_without_attempt_counts_keeps_the_newly_seen_names_rule(tmp_path) -> None:
    # 没有按名尝试次数的旧快照：只能报本范围新出现的名字，同名续用是空（旧账已知局限），也不补写次数键。
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "user-usage-legacy-identity", "now": 10.0})
    scope = {"thread_id": thread.thread_id, "request_id": "request-legacy", "run_id": "", "task_id": "",
             "source": "gateway"}
    first = _cumulative_model_calls(physical=1, input_tokens=100, output_tokens=10, cache_read_tokens=0)
    second = _cumulative_model_calls(physical=2, input_tokens=200, output_tokens=20, cache_read_tokens=0)

    rows = [
        store.model_usage.append_snapshot_once(
            {**scope, "model_calls": {**calls, "backends": ["anthropic"], "models": names}}
        ).model_calls
        for calls, names in ((first, ["model-a"]), (second, ["model-a", "model-b"]))
    ]

    assert [row["models"] for row in rows] == [["model-a"], ["model-b"]]
    assert [row["backends"] for row in rows] == [["anthropic"], []]
    assert not any("model_attempt_counts" in row or "backend_attempt_counts" in row for row in rows)


def test_snapshot_auto_identity_is_scope_and_content_bound(tmp_path):
    store = ConversationStore(tmp_path)
    thread = store.threads.get_or_create({"canonical_user_id": "usage-owner"})
    calls = {**_cumulative_model_calls(physical=1, input_tokens=100, output_tokens=2, cache_read_tokens=0),
             "usage_scope_id": "scope-one"}
    request = {"thread_id": thread.thread_id, "request_id": "request", "run_id": "run",
               "task_id": "task", "source": "foreground", "model_calls": calls}
    first = store.model_usage.append_snapshot_once(request)
    replay = store.model_usage.append_snapshot_once({**request, "source": "background", "now": 200.0})
    assert first == replay
    late = {**calls, "provider_http_attempt_count": 2, "provider_http_retry_count": 1}
    second = store.model_usage.append_snapshot_once({**request, "model_calls": late})
    assert second.event_id != first.event_id
    assert second.model_calls["physical_model_attempt_count"] == 0
    assert second.model_calls["accounted_input_tokens"] == 0
    assert second.model_calls["provider_http_retry_count"] == 1
    assert store.model_usage.append_snapshot_once(request) == first
    rows, errors = store.model_usage.events_report(thread.thread_id)
    assert not errors and len(rows) == 2


@pytest.mark.parametrize("snapshot", [False, True])
def test_composed_model_usage_replays_share_the_canonical_append_lock(tmp_path, snapshot):
    stores = [ConversationStore(tmp_path / "conversations") for _ in range(2)]
    thread = stores[0].threads.get_or_create({"canonical_user_id": "usage-owner"})
    calls = _cumulative_model_calls(
        physical=1, input_tokens=100, output_tokens=20, cache_read_tokens=70,
    )
    request = {
        "event_id": "usage-race", "thread_id": thread.thread_id,
        "request_id": "request-race", "model_calls": calls,
    }
    barrier = Barrier(8)

    # LLM: 两个独立 Store 模拟同源前后台同时收口，必须通过原追加锁取得同一个已提交事件。
    # 函数用途: 同步并发提交相同用量，核对首次时间戳和账本行均不会重复。
    def append(index):
        usage = stores[index % 2].model_usage
        writer = usage.append_snapshot_once if snapshot else usage.append_once
        barrier.wait(timeout=10)
        return writer({**request, "now": 10.0 + index})

    with ThreadPoolExecutor(max_workers=8) as workers:
        events = list(workers.map(append, range(8)))

    assert all(event == events[0] for event in events)
    reopened = ConversationStore(tmp_path / "conversations")
    rows, errors = reopened.model_usage.events_report(thread.thread_id)
    assert rows == [events[0]] and errors == []
    total = reopened.model_usage.summary(thread.thread_id)
    assert total["event_count"] == 1
    assert total["provider"]["input_tokens"] == 100
    assert total["provider"]["call_count"] == 1


def _cumulative_model_calls(
    *,
    physical: int,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
) -> dict[str, object]:
    return {
        "schema": "model_call_summary.v1",
        "logical_model_turn_count": physical,
        "physical_model_attempt_count": physical,
        "model_retry_count": 0,
        "provider_http_attempt_count": physical,
        "provider_http_retry_count": 0,
        "status_counts": {"finished": physical},
        "backends": ["test"],
        "models": ["test-model"],
        "accounted_input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cached_input_tokens": cache_read_tokens,
        "cache_creation_input_tokens": 0,
        "provider_usage_call_count": physical,
        "estimated_usage_call_count": 0,
        "usage_breakdown": {
            "schema": "model_usage_breakdown.v1",
            "provider": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_write_input_tokens": 0,
                "call_count": physical,
            },
            "estimated": {
                "input_tokens": 0,
                "output_tokens": 0,
                "call_count": 0,
            },
        },
    }


def test_thread_model_usage_conflict_and_corruption_fail_closed(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "user-usage", "now": 10.0}
    )
    request = {
        "event_id": "usage-event-1",
        "thread_id": thread.thread_id,
        "request_id": "request-1",
        "model_calls": {
            "schema": "model_call_summary.v1",
            "usage_breakdown": {
                "schema": "model_usage_breakdown.v1",
                "provider": {"input_tokens": 10, "call_count": 1},
                "estimated": {},
            },
        },
    }
    store.model_usage.append_once(request)

    with pytest.raises(DataCorruptionError, match="reused with different input"):
        store.model_usage.append_once(
            {
                **request,
                "model_calls": {
                    **request["model_calls"],
                    "usage_breakdown": {
                        "schema": "model_usage_breakdown.v1",
                        "provider": {"input_tokens": 11, "call_count": 1},
                        "estimated": {},
                    },
                },
            }
        )

    store.model_usage._path(thread.thread_id).write_text("not-json\n", encoding="utf-8")
    with pytest.raises(DataCorruptionError, match="unreadable"):
        store.model_usage.summary(thread.thread_id)


def test_exact_agent_thread_is_idempotent_unbound_and_rejects_run_collision(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    request = {
        "thread_id": "thread-run-child-1",
        "agent_run_id": "run-child-1",
        "parent_agent_thread_id": "thread-main",
        "root_agent_thread_id": "thread-main",
        "agent_depth": 1,
        "canonical_user_id": "local/main",
        "owner_id": "local/main",
        "owner_home": str(tmp_path / "owner"),
        "cwd": str(tmp_path),
        "runtime_workspace_roots": [str(tmp_path)],
        "now": 10.0,
    }

    first = store.threads.ensure_agent(request)
    reopened = ConversationStore(tmp_path / "conversations")
    second = reopened.threads.ensure_agent({**request, "now": 20.0})

    assert first.thread_id == second.thread_id == "thread-run-child-1"
    assert second.channel_bindings == ()
    assert second.metadata["thread_kind"] == "agent"
    assert second.metadata["agent_run_id"] == "run-child-1"
    assert reopened.threads._read_bindings_report() == ({}, None)
    with pytest.raises(DataCorruptionError, match="run identity conflicts"):
        reopened.threads.ensure_agent(
            {**request, "agent_run_id": "run-child-2", "now": 30.0}
        )


def test_existing_agent_thread_ensure_keeps_newer_model_and_compact_state(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from agent_py_agent.agent.conversation import agent_thread_store

    store = ConversationStore(tmp_path / "conversations")
    request = {
        "thread_id": "thread-run-child-race", "agent_run_id": "run-child-race",
        "canonical_user_id": "local/main", "owner_id": "local/main",
        "owner_home": str(tmp_path / "owner"), "now": 10.0,
    }
    store.threads.ensure_agent(request)
    original_read = agent_thread_store.read_json_object_report
    changed = False

    def read_then_change_model(*args, **kwargs):
        nonlocal changed
        payload, error = original_read(*args, **kwargs)
        if not changed:
            changed = True
            store.threads.update_atomic(
                request["thread_id"],
                lambda latest: replace(
                    latest, model_profile_id="selected-by-user", compact_generation=3,
                    model_selection_revision=latest.model_selection_revision + 1,
                    model_selection_source="explicit",
                    model_selection_last_explicit_revision=latest.model_selection_revision + 1,
                    metadata={**latest.metadata, "model_decision": {"status": "retained"}},
                ),
            )
        return payload, error

    monkeypatch.setattr(agent_thread_store, "read_json_object_report", read_then_change_model)
    ensured = store.threads.ensure_agent({**request, "now": 20.0})

    assert changed
    assert ensured.model_profile_id == "selected-by-user"
    assert ensured.compact_generation == 3
    assert ensured.metadata["model_decision"] == {"status": "retained"}
    assert store.threads.load(request["thread_id"]) == ensured


def test_detached_named_task_binds_exact_existing_message_anchor(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    anchor = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "先确认来源字段。",
            "now": 11.0,
        }
    )
    link = store.tasks.bind(
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
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "后来无关聊天",
            "now": 13.0,
        }
    )
    rebound = store.tasks.bind(
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
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "audit-child",
            "channel_user_id": "user-1",
            "owner_home": str(owner_home),
        }
    )

    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "child-1",
            "goal": "处理一个审计来源",
            "status": "completed",
            "task_path": str(task_root),
        }
    )

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "DONE"


def test_v4_thread_record_loads_with_safe_current_compact_defaults(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "legacy-user",
            "channel": "feishu",
            "channel_conversation_id": "legacy-chat",
            "channel_user_id": "legacy-user",
            "now": 100.0,
        }
    )
    path = store.storage.thread_path(thread.thread_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = "conversation_thread.v4"
    for field_name in (
        "compact_checkpoint_id",
        "compact_source_tool_pairs",
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
    loaded = reopened.threads.load(thread.thread_id)

    assert loaded is not None
    assert loaded.compact_checkpoint_id == ""
    assert loaded.compact_source_tool_pairs == 0
    assert loaded.compact_consecutive_failures == 0
    assert loaded.compact_failure_updated_at == 0.0
    assert loaded.compact_failure_code == ""


def test_compact_generation_cas_is_atomic_across_store_instances(tmp_path) -> None:
    root = tmp_path / "conversations"
    store = ConversationStore(root)
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "cas-user",
            "channel": "feishu",
            "channel_conversation_id": "cas-chat",
            "channel_user_id": "cas-user",
        }
    )

    def commit(label: str):
        independent_store = ConversationStore(root)
        return independent_store.threads.update_compact_state(
            thread.thread_id,
            commit=ConversationCompactCommit(
                summary=f"summary-{label}",
                operation_evidence={},
                checkpoint_id=f"checkpoint-{label}",
                compacted_through_message_id=f"message-{label}",
                compacted_through_byte_offset=100,
                source_messages=2,
                source_tool_pairs=3,
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

    stored = store.threads.load(thread.thread_id)
    assert len(successes) == 1
    assert len(failures) == 1
    assert "generation changed" in str(failures[0])
    assert stored is not None
    assert stored.compact_generation == 1
    assert stored.compact_source_tool_pairs == 3
    assert stored.compact_checkpoint_id in {"checkpoint-a", "checkpoint-b"}


def test_provider_context_observation_is_generation_fenced_and_compact_clears_it(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {"canonical_user_id": "provider-context-user", "now": 10.0}
    )
    observation = {
        "schema": "provider_context_observation.v2",
        "raw_estimated_tokens": 100_000,
        "provider_input_tokens": 40_000,
        "context_surface_fingerprint": "a" * 64,
        "compact_generation": 0,
    }

    updated = store.threads.update_provider_context_observation(
        thread.thread_id,
        observation,
        expected_compact_generation=0,
    )
    assert updated.provider_context_observation == observation
    assert updated.updated_at == thread.updated_at

    compacted = store.threads.update_compact_state(
        thread.thread_id,
        commit=ConversationCompactCommit(
            summary="summary",
            operation_evidence={},
            checkpoint_id="checkpoint-1",
            compacted_through_message_id="message-1",
            compacted_through_byte_offset=100,
            source_messages=2,
            source_tool_pairs=3,
        ),
        expected_generation=0,
    )
    assert compacted.compact_generation == 1
    assert compacted.provider_context_observation == {}

    stale = store.threads.update_provider_context_observation(
        thread.thread_id,
        observation,
        expected_compact_generation=0,
    )
    assert stale.compact_generation == 1
    assert stale.provider_context_observation == {}


def test_delayed_message_does_not_move_thread_activity_backwards(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 100.0,
        }
    )
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "user", "content": "较新的消息", "now": 200.0}
    )
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "assistant", "content": "延迟补写", "now": 150.0}
    )

    loaded = store.threads.load(thread.thread_id)
    messages = store.messages.recent(thread.thread_id)

    assert loaded is not None
    assert loaded.updated_at == 200.0
    assert [(item.content, item.created_at) for item in messages] == [
        ("较新的消息", 200.0),
        ("延迟补写", 150.0),
    ]


def test_thread_records_owner_identity_when_provided(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")

    thread = store.threads.get_or_create({
        'canonical_user_id': "user-1",
        'owner_id': "providers/feishu/users/u001",
        'owner_home': "/tmp/home/owners/providers/feishu/users/u001",
        'channel': "feishu",
        'channel_conversation_id': "chat-1",
        'channel_user_id': "user-1",
        'now': 100.0,
    })

    loaded = store.threads.load(thread.thread_id)
    assert loaded is not None
    assert loaded.owner_id == "providers/feishu/users/u001"
    assert loaded.owner_home.endswith("/owners/providers/feishu/users/u001")


def test_progress_policy_due_and_mark_reported(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "local-thread", 'channel_user_id': "local-user", 'now': 10.0})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "每小时汇报一次", 'now': 11.0})
    policy = store.progress.create({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "local-thread", 'now': 12.0})

    assert store.progress.due(now=71.0) == []
    assert store.progress.due(now=72.0)[0].policy_id == policy.policy_id

    store.progress.mark_reported(policy.policy_id, now=72.0)
    updated = store.progress.load(policy.policy_id)

    assert updated is not None
    assert updated.last_report_at == 72.0
    assert updated.next_due_at == 132.0


def test_progress_policy_bad_file_is_reported_without_hiding_good_due_policy(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    policy = store.progress.create({'thread_id': thread.thread_id, 'task_id': "task-1", 'interval_seconds': 60, 'route_channel': "internal", 'route_target': "thread-1", 'now': 12.0})
    bad_path = store.storage.policies_dir / "bad-policy.json"
    bad_path.write_text("{not-json", encoding="utf-8")

    due, errors = store.progress.due_report(now=72.0)

    assert [item.policy_id for item in due] == [policy.policy_id]
    assert errors
    assert errors[0]["context"] == "conversation.progress_policy.read"
    assert errors[0]["policy_id"] == "bad-policy"
    assert errors[0]["path"] == str(bad_path)


def test_update_task_status_keeps_thread_binding(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "开发网站", 'now': 2.0})

    link = store.tasks.update_status({'task_id': "task-1", 'status': "DONE", 'now': 3.0})
    links = store.tasks.list(thread.thread_id)
    stored_thread = store.threads.load(thread.thread_id)

    assert link is not None
    assert link.status == "DONE"
    assert links[0].task_id == "task-1"
    assert links[0].status == "DONE"
    assert stored_thread is not None
    assert stored_thread.task_ids == ("task-1",)
    assert stored_thread.active_task_ids == ()


@pytest.mark.parametrize("terminal_status", ["completed", "interrupted"])
def test_update_task_status_immediately_retires_exact_task_policies(
    tmp_path,
    terminal_status,
) -> None:
    """终态提交点立即退休本任务 policy，不等待下一次 scheduler tick。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "terminal-policy-cleanup",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "任务一", "now": 2.0}
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-2", "goal": "任务二", "now": 2.0}
    )
    first = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "interval_seconds": 60,
            "now": 3.0,
        }
    )
    second = store.progress.create(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-2",
            "interval_seconds": 60,
            "now": 3.0,
        }
    )

    store.tasks.update_status(
        {"task_id": "task-1", "status": terminal_status, "now": 4.0}
    )

    assert store.progress.load(first.policy_id).enabled is False
    assert store.progress.load(second.policy_id).enabled is True
    assert store.progress.due(now=100.0) == [
        store.progress.load(second.policy_id)
    ]


def test_selected_workspace_task_survives_completion_and_restart(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
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
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成项目一",
            "status": "active",
            "task_path": str(workspace),
            "now": 2.0,
        }
    )

    selected = store.tasks.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-1", "now": 3.0}
    )
    store.tasks.update_status(
        {"task_id": "task-1", "status": "completed", "now": 4.0}
    )
    reopened_store = ConversationStore(tmp_path / "conversations")
    reopened = reopened_store.threads.load(thread.thread_id)

    assert selected.workspace_task_id == "task-1"
    assert reopened is not None
    assert reopened.workspace_task_id == "task-1"
    assert reopened.active_task_ids == ()
    payload = json.loads(reopened_store.storage.thread_path(thread.thread_id).read_text(encoding="utf-8"))
    assert payload["schema_version"] == "conversation_thread.v10"


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

    first = store.threads.get_or_create(request)
    second = store.threads.get_or_create(
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
    thread = store.threads.get_or_create(
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
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-first",
            "goal": "第一个项目",
            "task_path": str(first_workspace),
            "now": 2.0,
        }
    )
    store.tasks.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-first", "now": 3.0}
    )
    stale = store.threads.load(thread.thread_id)
    assert stale is not None and stale.workspace_task_id == "task-first"

    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-second",
            "goal": "第二个项目",
            "task_path": str(second_workspace),
            "now": 4.0,
        }
    )
    store.tasks.select_workspace_task(
        {"thread_id": thread.thread_id, "task_id": "task-second", "now": 5.0}
    )

    # Simulate a request that loaded the thread before the workspace switch.
    monkeypatch.setattr(store.threads, "require", lambda _thread_id: stale)
    monkeypatch.setattr(store.messages, "_require_thread", lambda _thread_id: stale)
    monkeypatch.setattr(store.observations, "_require_thread", lambda _thread_id: stale)
    store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": "切换发生前已经在处理的消息",
            "now": 6.0,
        }
    )
    store.observations.append(
        {
            "thread_id": thread.thread_id,
            "event_type": "progress",
            "summary": "迟到的进度",
            "now": 7.0,
        }
    )
    store.threads.bind_channel(
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
    store.threads.update_summary(thread.thread_id, "最新摘要", now=9.0)
    store.threads.update_verbose_level(thread.thread_id, "full", now=10.0)

    loaded = store.threads.load(thread.thread_id)
    assert loaded is not None
    assert loaded.workspace_task_id == "task-second"
    assert loaded.task_ids == ("task-first", "task-second")
    assert loaded.summary == "最新摘要"
    assert loaded.verbose_level == "full"
    assert loaded.updated_at == 10.0


def test_selected_workspace_task_rejects_task_from_another_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    first = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-one",
            "channel_user_id": "user-1",
        }
    )
    second = store.threads.get_or_create(
        {
            "canonical_user_id": "user-2",
            "channel": "feishu",
            "channel_conversation_id": "thread-two",
            "channel_user_id": "user-2",
        }
    )
    workspace = tmp_path / "owner-two" / "tasks" / "private-project"
    workspace.mkdir(parents=True)
    store.tasks.bind(
        {
            "thread_id": second.thread_id,
            "task_id": "task-two",
            "goal": "第二条会话的项目",
            "task_path": str(workspace),
        }
    )

    with pytest.raises(ValueError, match="not bound to conversation thread"):
        store.tasks.select_workspace_task(
            {"thread_id": first.thread_id, "task_id": "task-two"}
        )

    unchanged = store.threads.load(first.thread_id)
    assert unchanged is not None
    assert unchanged.workspace_task_id == ""


def test_task_link_lifecycle_projects_to_owner_workspace_state(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
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
    summary_path = task_root / "work" / "summaries" / "current_summary.md"
    summary_path.parent.mkdir(parents=True)
    summary_path.write_text(
        "# Current Summary\n\n"
        "- task_id: task-1\n"
        "- primary_run_id: task-1\n"
        "- status: RUNNING\n"
        "- current_step: waiting_for_child_runs\n\n"
        "## Latest\n\n"
        "子代理已登记，等待父任务汇总。\n",
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps({"version": 1, "task_id": "task-1", "status": "RUNNING"}),
        encoding="utf-8",
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "完成一个任务",
            "status": "active",
            "task_path": str(task_root),
            "now": 2.0,
        }
    )

    store.tasks.update_status(
        {"task_id": "task-1", "status": "completed", "expected_status": "active", "now": 3.0}
    )
    completed = json.loads(state_path.read_text(encoding="utf-8"))
    completed_summary = summary_path.read_text(encoding="utf-8")
    store.tasks.update_status(
        {"task_id": "task-1", "status": "active", "expected_status": "completed", "now": 4.0}
    )
    store.tasks.update_status(
        {"task_id": "task-1", "status": "interrupted", "expected_status": "active", "now": 5.0}
    )
    interrupted = json.loads(state_path.read_text(encoding="utf-8"))
    interrupted_summary = summary_path.read_text(encoding="utf-8")

    assert completed["status"] == "DONE"
    assert completed["current_step"] == "DONE"
    assert completed["updated_at"] == "1970-01-01T00:00:03+00:00"
    assert "- status: DONE" in completed_summary
    assert "- current_step: DONE" in completed_summary
    assert "子代理已登记，等待父任务汇总。" in completed_summary
    assert interrupted["status"] == "PAUSED"
    assert interrupted["current_step"] == "PAUSED"
    assert interrupted["updated_at"] == "1970-01-01T00:00:05+00:00"
    assert "- status: PAUSED" in interrupted_summary
    assert "- current_step: PAUSED" in interrupted_summary


def test_task_link_lifecycle_does_not_overwrite_another_workspace_identity(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
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

    linked = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得覆盖别的任务状态",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    completed = store.tasks.update_status({"task_id": "task-1", "status": "completed"})

    assert linked.task_id == "task-1"
    assert completed is not None and completed.status == "completed"
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "task_id": "task-other",
        "status": "RUNNING",
    }


def test_task_link_lifecycle_does_not_write_outside_owner_tasks(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
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

    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得写出 owner task 根目录",
            "status": "active",
            "task_path": str(outside),
        }
    )
    store.tasks.update_status({"task_id": "task-1", "status": "completed"})

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "RUNNING"


def test_task_link_lifecycle_does_not_follow_state_symlink_outside_task(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    owner_home = tmp_path / "owner"
    thread = store.threads.get_or_create(
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

    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "不得通过状态链接写出任务目录",
            "status": "active",
            "task_path": str(task_root),
        }
    )
    store.tasks.update_status({"task_id": "task-1", "status": "completed"})

    assert json.loads(outside_state.read_text(encoding="utf-8"))["status"] == "RUNNING"


def test_update_task_status_expected_status_does_not_overwrite_terminal_race(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "开发网站", "now": 2.0}
    )
    store.tasks.update_status({"task_id": "task-1", "status": "completed", "now": 3.0})

    rejected = store.tasks.update_status(
        {
            "task_id": "task-1",
            "status": "cancelled",
            "expected_status": "active",
            "now": 4.0,
        }
    )
    link = store.tasks.list(thread.thread_id)[0]

    assert rejected is None
    assert link.status == "completed"


def test_bind_task_preserves_existing_identity_and_only_fills_missing_path(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    first = store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "原始用户目标", "now": 2.0}
    )
    filled = store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-1",
            "goal": "定时唤醒：等待子任务完成",
            "task_path": str(tmp_path / "task-root"),
            "now": 3.0,
        }
    )
    repeated = store.tasks.bind(
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
    first = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    second = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-2",
            "channel_user_id": "user-1",
            "now": 2.0,
        }
    )
    store.tasks.bind(
        {"thread_id": first.thread_id, "task_id": "task-1", "goal": "原始目标", "now": 3.0}
    )
    completed = store.tasks.bind(
        {
            "thread_id": first.thread_id,
            "task_id": "task-1",
            "goal": "不应覆盖",
            "status": "completed",
            "now": 4.0,
        }
    )

    with pytest.raises(ValueError, match="another conversation thread"):
        store.tasks.bind(
            {"thread_id": second.thread_id, "task_id": "task-1", "goal": "跨线程覆盖", "now": 5.0}
        )

    loaded = store.tasks.list(first.thread_id)[0]
    stored_thread = store.threads.load(first.thread_id)
    assert completed.goal == loaded.goal == "原始目标"
    assert completed.status == "completed"
    assert stored_thread is not None
    assert stored_thread.task_ids == ("task-1",)
    assert stored_thread.active_task_ids == ()


def test_bind_task_cannot_resurrect_terminal_task_but_explicit_update_can(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "user-1",
            "now": 1.0,
        }
    )
    store.tasks.bind(
        {"thread_id": thread.thread_id, "task_id": "task-1", "goal": "长任务", "now": 2.0}
    )
    cancelled = store.tasks.update_status(
        {"task_id": "task-1", "status": "cancelled", "expected_status": "active", "now": 3.0}
    )
    rebound = store.tasks.bind(
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
    assert store.threads.load(thread.thread_id).active_task_ids == ()

    reopened = store.tasks.update_status(
        {"task_id": "task-1", "status": "active", "expected_status": "cancelled", "now": 5.0}
    )
    assert reopened is not None and reopened.status == "active"
    assert store.threads.load(thread.thread_id).active_task_ids == ("task-1",)


def test_concurrent_task_bindings_merge_thread_indexes_without_lost_ids(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
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
        store.tasks.bind(
            {
                "thread_id": thread.thread_id,
                "task_id": task_id,
                "goal": f"并发任务 {task_id}",
            }
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(bind, task_ids))

    loaded = store.threads.load(thread.thread_id)
    assert loaded is not None
    assert set(loaded.task_ids) == set(task_ids)
    assert set(loaded.active_task_ids) == set(task_ids)
    assert {item.task_id for item in store.tasks.list(thread.thread_id)} == set(task_ids)


def test_concurrent_same_named_audit_reservation_allows_exactly_one(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
        }
    )

    def reserve(task_id: str) -> str:
        try:
            store.tasks.bind(
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
    links = store.tasks.list(thread.thread_id)
    assert len(links) == 1
    assert links[0].work_kind == "audit"
    assert links[0].work_name == "同名检查"


def test_thread_for_task_reports_corrupt_task_link(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-bad", 'goal': "长期任务", 'now': 2.0})
    store.storage.task_path("task-bad").write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.tasks.thread_for("task-bad")

    resolved, error = store.tasks.thread_for_report("task-bad")
    assert resolved is None
    assert error is not None
    assert error["context"] == "conversation.thread_for_task"
    assert error["task_id"] == "task-bad"
    assert error["path"] == str(store.storage.task_path("task-bad"))


def test_thread_for_task_reports_corrupt_linked_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': "task-1", 'goal': "长期任务", 'now': 2.0})
    store.storage.thread_path(thread.thread_id).write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.tasks.thread_for("task-1")

    resolved, error = store.tasks.thread_for_report("task-1")
    assert resolved is None
    assert error is not None
    assert error["context"] == "conversation.thread.read"
    assert error["thread_id"] == thread.thread_id
    assert error["path"] == str(store.storage.thread_path(thread.thread_id))


def test_update_json_file_atomic_updates_under_single_file_transaction(tmp_path) -> None:
    path = tmp_path / "state.json"

    first = update_json_file_atomic(path, lambda data: {"count": int(data.get("count") or 0) + 1})
    second = update_json_file_atomic(path, lambda data: {"count": int(data.get("count") or 0) + 1})

    assert first["count"] == 1
    assert second["count"] == 2


def test_new_unbound_channel_does_not_implicitly_mix_latest_thread(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    first = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "feishu", 'channel_conversation_id': "chat-a", 'channel_user_id': "user-a", 'now': 10.0})
    second = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "chat-b", 'channel_user_id': "user-b", 'now': 20.0})

    assert second.thread_id != first.thread_id

    resumed = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "qq", 'channel_conversation_id': "chat-c", 'channel_user_id': "user-c", 'reuse_latest_for_user': True, 'now': 30.0})

    assert resumed.thread_id == second.thread_id


def test_list_threads_report_keeps_good_threads_when_one_thread_file_is_bad(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    good = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    bad_path = store.storage.threads_dir / "bad-thread.json"
    bad_path.write_text("{bad-json", encoding="utf-8")

    threads, errors = store.threads.list_report()

    assert [item.thread_id for item in threads] == [good.thread_id]
    assert errors
    assert errors[0]["context"] == "conversation.thread.read"
    assert errors[0]["thread_id"] == "bad-thread"
    assert errors[0]["path"] == str(bad_path)


def test_resolve_thread_report_reports_corrupt_bindings_index(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.storage.bindings_path.write_text("{bad-json", encoding="utf-8")

    thread, error = store.threads.resolve_report(
        channel="internal",
        channel_conversation_id="thread-1",
        channel_user_id="user-1",
    )

    assert thread is None
    assert error is not None
    assert error["context"] == "conversation.bindings.read"
    assert error["path"] == str(store.storage.bindings_path)


def test_latest_thread_for_user_report_reports_corrupt_latest_index(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.storage.user_latest_path.write_text("{bad-json", encoding="utf-8")

    thread, error = store.threads.latest_for_user_report("user-1")

    assert thread is None
    assert error is not None
    assert error["context"] == "conversation.user_latest.read"
    assert error["path"] == str(store.storage.user_latest_path)


def test_get_or_create_thread_does_not_duplicate_when_bindings_index_is_corrupt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.storage.bindings_path.write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 2.0})

    threads, errors = store.threads.list_report()
    assert len(threads) == 1
    assert errors == []


def test_get_or_create_thread_does_not_duplicate_when_latest_index_is_corrupt(tmp_path) -> None:
    store = ConversationStore(tmp_path / "conversations")
    store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 1.0})
    store.storage.user_latest_path.write_text("{bad-json", encoding="utf-8")

    with pytest.raises(DataCorruptionError):
        store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "wechat", 'channel_conversation_id': "thread-2", 'channel_user_id': "user-1", 'reuse_latest_for_user': True, 'now': 2.0})

    threads, errors = store.threads.list_report()
    assert len(threads) == 1
    assert errors == []


def test_mark_progress_failed_records_backoff_and_retires(tmp_path) -> None:
    """失败续跑记账(问题6):写失败账+退避顺延;连续失败达阈值退休;未知 policy 安全。"""
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-fail-store",
            "channel_user_id": "user-1",
            "now": 10.0,
        }
    )
    policy = store.progress.create(
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
    assert store.progress.mark_failed("policy-nope", now=21.0, backoff_seconds=300, failure_count=1) is None

    # 第 1 次失败:记账 + 退避顺延,enabled 保持。
    failed = store.progress.mark_failed(
        policy.policy_id, now=30.0, backoff_seconds=300.0, failure_count=1
    )
    assert failed is not None and failed.enabled is True
    assert failed.metadata["failure_count"] == 1
    assert failed.metadata["last_failure_at"] == 30.0
    assert failed.metadata["last_backoff_seconds"] == 300.0
    assert failed.next_due_at == 30.0 + 300.0

    # 阈值内再失败:账累加,仍 enabled。
    failed2 = store.progress.mark_failed(
        policy.policy_id, now=40.0, backoff_seconds=600.0, failure_count=2
    )
    assert failed2.enabled is True
    assert failed2.metadata["failure_count"] == 2

    # 达退休阈值:enabled=False,离开 due 扫描,账目保留供复盘。
    retired = store.progress.mark_failed(
        policy.policy_id, now=50.0, backoff_seconds=1200.0, failure_count=3
    )
    assert retired.enabled is False
    assert retired.metadata["failure_count"] == 3
    assert retired.metadata["retired_at"] == 50.0
    assert policy.policy_id not in {p.policy_id for p in store.progress.due(now=100.0)}


# 读取侧增量索引只允许改变耗时。这里把唤醒查询的四个语义(urgent 优先、created_at 升序、
# limit 截断、status 过滤)和 load_error 门钉死在真实 store 上:调用方
# (runtime/guidance)以"load_error 非空即不消费"作硬门,所以坏文件必须每轮都被报出来。
def test_wake_pending_report_keeps_order_limit_status_filter_and_load_errors(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    normal = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': "agent_event", 'urgency': "normal", 'now': 20.0})
    urgent_late = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': "agent_event", 'urgency': "urgent", 'now': 22.0})
    urgent_early = store.wakes.raise_signal({'thread_id': thread.thread_id, 'reason': "agent_event", 'urgency': "urgent", 'now': 21.0})
    # 崩溃窗口残留:队列目录里状态已不是 pending 的文件不得被返回。
    leftover = store.storage.wake_queue_dir / "urgent" / "wake-handled-left.json"
    leftover.write_text(
        json.dumps(
            {
                "wake_signal_id": "wake-handled-left",
                "thread_id": thread.thread_id,
                "urgency": "urgent",
                "created_at": 1.0,
                "status": "handled",
            }
        ),
        encoding="utf-8",
    )
    (store.storage.wake_queue_dir / "normal" / "wake-broken.json").write_text("{not json", encoding="utf-8")

    expected_order = [urgent_early.wake_signal_id, urgent_late.wake_signal_id, normal.wake_signal_id]
    signals, errors = store.wakes.pending_report(limit=0)
    assert [item.wake_signal_id for item in signals] == expected_order
    assert errors and errors[0]["context"] == "conversation.wake_signal.read"

    capped, capped_errors = store.wakes.pending_report(limit=2)
    assert [item.wake_signal_id for item in capped] == expected_order[:2]
    urgent_only, _ = store.wakes.pending_report(limit=0, include_normal=False)
    assert [item.wake_signal_id for item in urgent_only] == expected_order[:2]

    # 重复调用必须逐字稳定:信号、顺序、limit 截断、以及 load_error 全集。
    for _ in range(3):
        repeated, repeated_errors = store.wakes.pending_report(limit=0)
        repeated_capped, repeated_capped_errors = store.wakes.pending_report(limit=2)
        assert [item.to_dict() for item in repeated] == [item.to_dict() for item in signals]
        assert repeated_errors == errors
        assert [item.to_dict() for item in repeated_capped] == [item.to_dict() for item in capped]
        assert repeated_capped_errors == capped_errors


# 观察查询的语义同样钉死:已处理(handled 映射或行内 handled_at)不返回、二者其一的
# requires 标记才返回、按 observed_at 升序、limit 取最早 N 条,重复调用结果一致。
def test_unhandled_observations_requiring_main_keeps_order_limit_and_handled_filter(
    tmp_path,
) -> None:
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-1", 'channel_user_id': "user-1", 'now': 10.0})
    other = store.threads.get_or_create({'canonical_user_id': "user-1", 'channel': "internal", 'channel_conversation_id': "thread-2", 'channel_user_id': "user-1", 'now': 11.0})
    first = store.observations.append({'thread_id': thread.thread_id, 'event_type': "child_agent_event", 'summary': "最早", 'requires_main_agent': True, 'now': 30.0})
    second = store.observations.append({'thread_id': other.thread_id, 'event_type': "child_agent_event", 'summary': "需要模型报告", 'requires_llm_report': True, 'now': 31.0})
    plain = store.observations.append({'thread_id': thread.thread_id, 'event_type': "child_agent_event", 'summary': "不需要主代理", 'now': 32.0})
    handled = store.observations.append({'thread_id': other.thread_id, 'event_type': "child_agent_event", 'summary': "已处理", 'requires_main_agent': True, 'now': 33.0})
    store.observations.mark_handled([handled.observation_id], now=34.0)

    events = store.observations.unhandled_requiring_main(limit=0)
    assert [item.observation_id for item in events] == [first.observation_id, second.observation_id]
    assert plain.observation_id not in {item.observation_id for item in events}
    assert handled.observation_id not in {item.observation_id for item in events}
    assert [item.observed_at for item in events] == sorted(item.observed_at for item in events)
    assert [
        item.observation_id for item in store.observations.unhandled_requiring_main(limit=1)
    ] == [first.observation_id]

    for _ in range(3):
        assert [item.to_dict() for item in store.observations.unhandled_requiring_main(limit=0)] == [
            item.to_dict() for item in events
        ]


# LLM: JSONL 记录边界只能是物理 LF。真实事故：子代理 transcript 的 JSON 字符串里带 U+0085(NEL)，
# splitlines() 把一条完整记录切成两条 → 按 LF 读 0 错误、按 splitlines 读 19 错误，child 被判
# conversation transcript is unreadable 而整体 FAILED。这里锁住"合法字符不被当边界"和
# "真正的坏行仍然报错"两侧，防止用过滤字符/吞坏行的方式掩盖。
# 函数用途: 验证 jsonl_lines 只按 LF 切记录，并保留字符串内的 Unicode 行分隔字符。
def test_jsonl_lines_only_split_on_physical_lf() -> None:
    from agent_py_agent.agent.common.json_io import jsonl_lines

    nel = "\u0085"
    paragraph = "\u2028"
    line_sep = "\u2029"
    payload = json.dumps(
        {"content": f"第一段{nel}第二段{paragraph}第三段{line_sep}第四段"},
        ensure_ascii=False,
    )
    text = payload + "\n" + '{"content": "普通记录"}' + "\n"

    lines = jsonl_lines(text)
    assert len(lines) == 2
    assert json.loads(lines[0])["content"] == f"第一段{nel}第二段{paragraph}第三段{line_sep}第四段"
    assert json.loads(lines[1])["content"] == "普通记录"
    # 对照：str.splitlines() 会把这一条记录切成 4 条，正是事故成因。
    assert len(text.splitlines()) > len(lines)
    assert jsonl_lines("") == ()
    assert jsonl_lines('{"a": 1}') == ('{"a": 1}',)
    assert jsonl_lines('{"a": 1}\r\n') == ('{"a": 1}',)


# LLM: 会话账本是子代理 transcript 的权威来源；带 Unicode 行分隔字符的正文必须能读回，
# 且读取报告不能出现 load_errors（load_errors 会让 append_message_once 抛 DataCorruptionError）。
# 函数用途: 验证带 NEL/U+2028/U+2029 的消息可写可读且不产生损坏报告。
def test_conversation_jsonl_reader_keeps_unicode_line_separators(tmp_path) -> None:
    from agent_py_agent.agent.conversation.store import (
        ConversationStore,
    )
    from agent_py_agent.agent.conversation.store_io import read_jsonl_report

    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "owner-a"})
    tricky = "首行\u0085次行\u2028三行\u2029四行"
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "assistant", "content": tricky}
    )
    store.messages.append(
        {"thread_id": thread.thread_id, "role": "assistant", "content": "普通正文"}
    )

    report = store.messages.recent_report(thread.thread_id, limit=0)
    rows, load_errors = report
    assert load_errors == []
    assert [row.content for row in rows] == [tricky, "普通正文"]

    path = store.storage.messages_dir / f"{thread.thread_id}.jsonl"
    raw = read_jsonl_report(path, context="guard")
    assert raw.load_errors == []
    assert len(raw.rows) == 2


# LLM: 真正的截断/非法 JSON 仍然必须报结构化错误：修复只改"记录边界"，不允许变成吞坏行。
# 函数用途: 验证半行、非法 JSON 与非对象行仍产生 load_errors。
def test_conversation_jsonl_reader_still_reports_real_corruption(tmp_path) -> None:
    from agent_py_agent.agent.conversation.store_io import read_jsonl_report

    path = tmp_path / "broken.jsonl"
    path.write_text(
        '{"ok": 1}\n'
        '{"truncated": \n'
        'not json at all\n'
        '[1, 2, 3]\n',
        encoding="utf-8",
    )
    report = read_jsonl_report(path, context="guard-broken")
    assert [row["ok"] for row in report.rows] == [1]
    assert len(report.load_errors) == 3
    assert all(item.get("path") == str(path) for item in report.load_errors)


# LLM: 迁移到 LF 边界时最容易犯的是"把包装套错位置"：曾把 jsonl_lines(reversed(text)) 与
# jsonl_lines(enumerate(text), start=1) 写出来，前者让 jsonl_lines 收到 reversed 对象
# （AttributeError: 'reversed' object has no attribute 'split'），在真机上表现为子代理
# runner 直接 FAILED。这两条路径（控制面 JSONL 读、归档写后倒序回读校验）必须有守卫。
# 函数用途: 验证 memory_archive 的两处倒序/带序号读取在 NEL 正文下仍可用。
def test_memory_archive_jsonl_readers_handle_unicode_separators(tmp_path) -> None:
    import json as _json

    from agent_py_agent.agent.memory_archive import control_plane, storage

    tricky = "记录\u0085正文\u2028续\u2029尾"
    path = tmp_path / "ledger.jsonl"
    path.write_text(
        "\n".join(
            [
                _json.dumps({"event_id": "e1", "content": tricky}, ensure_ascii=False),
                _json.dumps({"event_id": "e2", "content": "普通"}, ensure_ascii=False),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    rows = control_plane._read_jsonl(path)
    assert [row.get("event_id") for row in rows] == ["e1", "e2"]
    assert rows[0]["content"] == tricky

    storage._verify_record_exists(
        path,
        key="event_id",
        value="e1",
        expected={"event_id": "e1", "content": tricky},
    )


# LLM: 真实ThreadStore/JSON文件，spy只记录线程reader调用并透传；不初始化额外缓存或修改产品权限。
# 函数用途: 为逐文件缓存回归准备冷记录与读盘计数，不依赖其它测试留下的全局状态。
@pytest.fixture
def thread_record_cache_case(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import store_threads
    from agent_py_agent.agent.conversation.models import ConversationThread

    store = ConversationStore(tmp_path / 'conversations')
    thread = ConversationThread(thread_id='cache-thread', canonical_user_id='owner', title='initial', updated_at=2.0)
    path = store.storage.thread_path(thread.thread_id)
    path.write_text(json.dumps(thread.to_dict()), encoding='utf-8')
    reads = []
    original = store_threads.read_json_object_report

    # LLM: 所有返回与异常仍来自原JSON reader，调用次数是缓存是否避开重解析的实际观察点。
    # 函数用途: 计数目标线程文件的读取，不模拟缓存命中或解析结果。
    def read(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(store_threads, 'read_json_object_report', read)
    return SimpleNamespace(store=store, thread=thread, path=path, module=store_threads, reads=reads, reader=read)


# LLM: 同路径的独立Store须复用真实文件快照，返回值仍独立，不能只验证私有缓存存在。
# 函数用途: 核对第二次读取不再走JSON reader。
def test_thread_record_cache_reuses_reads_across_store_instances(thread_record_cache_case):
    case = thread_record_cache_case
    first, error = case.store.threads.load_report(case.thread.thread_id)
    reopened = ConversationStore(case.store.storage.root, initialize=False)
    second, reopened_error = reopened.threads.load_report(case.thread.thread_id)
    assert error is reopened_error is None
    assert first == second and first is not second
    assert case.reads == [case.path]


# LLM: 文件缓存不得冻结目录成员或改变排序/limit；新增和删除必须在下一次原glob扫描中体现。
# 函数用途: 计数冷热列表读取并核对增删线程后的返回顺序。
def test_thread_record_cache_list_hits_keep_order_limit_and_new_files(thread_record_cache_case):
    case = thread_record_cache_case
    for name, stamp in [('alpha', 1), ('beta', 3)]:
        path = case.store.storage.thread_path(name)
        path.write_text(json.dumps(replace(case.thread, thread_id=name, updated_at=stamp).to_dict()), encoding='utf-8')
    first, errors = case.store.threads.list_report(limit=0)
    assert errors == [] and [item.thread_id for item in first] == ['alpha', 'cache-thread', 'beta']
    assert len(case.reads) == 3
    limited, errors = case.store.threads.list_report(limit=1)
    assert errors == [] and [item.thread_id for item in limited] == ['beta']
    assert len(case.reads) == 3
    case.store.storage.thread_path('alpha').unlink()
    case.store.storage.thread_path('gamma').write_text(
        json.dumps(replace(case.thread, thread_id='gamma', updated_at=4).to_dict()), encoding='utf-8',
    )
    refreshed, errors = case.store.threads.list_report(limit=0)
    assert errors == [] and [item.thread_id for item in refreshed] == ['cache-thread', 'beta', 'gamma']
    assert len(case.reads) == 4


# LLM: mtime与size相同不能保证内容相同；原地写依赖ctime，原子替换依赖inode。
# 函数用途: 覆盖两种保留旧mtime的外部改写，防止复用旧线程。
@pytest.mark.parametrize('atomic', [False, True])
def test_thread_record_cache_invalidates_same_size_same_mtime_changes(thread_record_cache_case, atomic):
    case = thread_record_cache_case
    assert case.store.threads.require(case.thread.thread_id).title == 'initial'
    before = case.path.stat()
    payload = case.thread.to_dict()
    payload['title'] = 'changed'
    destination = case.path.with_suffix('.replacement') if atomic else case.path
    destination.write_text(json.dumps(payload), encoding='utf-8')
    os.utime(destination, ns=(before.st_atime_ns, before.st_mtime_ns))
    if atomic:
        destination.replace(case.path)
    after = case.path.stat()
    assert before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
    assert (before.st_ino, before.st_ctime_ns) != (after.st_ino, after.st_ctime_ns)
    assert case.store.threads.require(case.thread.thread_id).title == 'changed'
    assert case.store.threads.require(case.thread.thread_id).title == 'changed'
    assert len(case.reads) == 2


# LLM: 错误和空对象必须逐次回读并保留原context/path/thread_id，修复文件后可再次命中。
# 函数用途: 验证语法、对象类型、模型转换失败和空记录都不进入正缓存。
@pytest.mark.parametrize('content', ['{broken', '[]', '{}', '{"compact_generation": "bad"}'])
def test_thread_record_cache_never_caches_corruption_or_empty_records(thread_record_cache_case, content):
    case = thread_record_cache_case
    assert case.store.threads.require(case.thread.thread_id).title == 'initial'
    case.path.write_text(content, encoding='utf-8')
    for _ in range(2):
        value, error = case.store.threads.load_report(case.thread.thread_id)
        assert value is None
        if content == '{}':
            assert error is None
        else:
            assert error['context'] == 'conversation.thread.read'
            assert error['thread_id'] == case.thread.thread_id and error['path'] == str(case.path)
        assert str(case.path) not in getattr(case.module, '_THREAD_RECORD_CACHE', {})
    case.path.write_text(json.dumps(case.thread.to_dict()), encoding='utf-8')
    assert case.store.threads.require(case.thread.thread_id).title == 'initial'
    assert case.store.threads.require(case.thread.thread_id).title == 'initial'
    assert len(case.reads) == 4


# LLM: 删除不允许被旧投影掩盖，返回原(None,None)并清缓存；同路径重新出现须恢复新内容。
# 函数用途: 核对线程删除与重建的读取语义。
def test_thread_record_cache_drops_deleted_records(thread_record_cache_case):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    case.path.unlink()
    assert case.store.threads.load_report(case.thread.thread_id) == (None, None)
    assert str(case.path) not in getattr(case.module, '_THREAD_RECORD_CACHE', {})
    case.path.write_text(json.dumps(replace(case.thread, title='restored').to_dict()), encoding='utf-8')
    assert case.store.threads.require(case.thread.thread_id).title == 'restored'


# LLM: 冷读取返回到后置stat之间发生真实替换时，本轮数据可返回但不可缓存为新版本。
# 函数用途: 验证不稳定读窗不会污染后续稳定命中。
def test_thread_record_cache_requires_a_stable_read_fingerprint(thread_record_cache_case, monkeypatch):
    case = thread_record_cache_case

    # LLM: 在原reader返回后用真实外部改写制造读窗换代；首轮结果可返回，但不得写入缓存。
    # 函数用途: 验证读前读后指纹不同会放弃缓存，下一轮从新文件恢复。
    def changing_reader(path, **kwargs):
        value = case.reader(path, **kwargs)
        path.write_text(json.dumps(replace(case.thread, title='changed').to_dict()), encoding='utf-8')
        monkeypatch.setattr(case.module, 'read_json_object_report', case.reader)
        return value

    monkeypatch.setattr(case.module, 'read_json_object_report', changing_reader)
    assert case.store.threads.require(case.thread.thread_id).title == 'initial'
    assert case.store.threads.require(case.thread.thread_id).title == 'changed'
    assert case.store.threads.require(case.thread.thread_id).title == 'changed'
    assert len(case.reads) == 2


# LLM: stat不可用不授权复用投影，仍每次调用原reader且不发布新缓存。
# 函数用途: 核对无指纹时退回真实读取。
def test_thread_record_cache_cannot_hit_when_stat_is_unavailable(thread_record_cache_case, monkeypatch):
    case = thread_record_cache_case
    monkeypatch.setattr(case.module, '_file_fingerprint', lambda path: None, raising=False)
    assert case.store.threads.load_report(case.thread.thread_id)[1] is None
    assert case.store.threads.load_report(case.thread.thread_id)[1] is None
    assert len(case.reads) == 2
    assert str(case.path) not in getattr(case.module, '_THREAD_RECORD_CACHE', {})


# LLM: 缩小容量仍验证公开load行为和真实reader计数，命中必须刷新LRU而不靠文件时间排序。
# 函数用途: 核对超限淘汰最久未使用的记录。
def test_thread_record_cache_is_bounded_and_uses_lru(thread_record_cache_case, monkeypatch):
    case = thread_record_cache_case
    monkeypatch.setattr(case.module, '_THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT', 2, raising=False)
    for name in ['a', 'b', 'c']:
        case.store.storage.thread_path(name).write_text(json.dumps(replace(case.thread, thread_id=name).to_dict()), encoding='utf-8')
    for name in ['a', 'b', 'a', 'c', 'b']:
        assert case.store.threads.require(name).thread_id == name
    assert len(case.reads) == 4
    cache = getattr(case.module, '_THREAD_RECORD_CACHE', {})
    assert list(cache) == [str(case.store.storage.thread_path(name)) for name in ['c', 'b']]


# LLM: 当前模型全部七个字典都需隔离，尤其空字典不能通过replace浅拷贝共享。
# 函数用途: 修改首次返回对象，验证后续命中的嵌套数据与原文件一致。
@pytest.mark.parametrize('nested', [False, True])
def test_thread_record_cache_clones_empty_and_nested_mutable_fields(thread_record_cache_case, nested):
    case = thread_record_cache_case
    fields = ['compact_operation_evidence', 'provider_context_observation', 'model_context_usage',
        'learning_source', 'metadata', 'decision_settings', 'model_metrics']
    if nested:
        payload = case.thread.to_dict()
        for name in fields:
            if name != 'decision_settings':
                payload[name] = {'nested': [{'value': 'disk'}]}
        case.path.write_text(json.dumps(payload), encoding='utf-8')
    first = case.store.threads.require(case.thread.thread_id)
    expected = copy.deepcopy(first.to_dict())
    for name in fields:
        container = getattr(first, name)
        if 'nested' in container:
            container['nested'][0]['value'] = 'caller-only'
        container['caller-only'] = True
    second = case.store.threads.require(case.thread.thread_id)
    assert second.to_dict() == expected
    for name in fields:
        assert getattr(first, name) is not getattr(second, name)
    assert len(case.reads) == 1


# LLM: 写后更新或失效都允许，但输入对象和返回对象的嵌套修改都不能影响缓存。
# 函数用途: 覆盖两个写入口的可见性和调用者对象隔离。
@pytest.mark.parametrize('method', ['write', 'update_atomic'])
def test_thread_record_cache_writer_refreshes_without_sharing_caller_data(thread_record_cache_case, method):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    snapshot = replace(case.thread, title='updated', metadata={'nested': [{'value': 'saved'}]})
    if method == 'write':
        case.store.threads.write(snapshot)
    else:
        result = case.store.threads.update_atomic(case.thread.thread_id, lambda _: snapshot)
        result.metadata['nested'][0]['value'] = 'returned-only'
    snapshot.metadata['nested'][0]['value'] = 'caller-only'
    assert case.store.threads.require(case.thread.thread_id).metadata == {'nested': [{'value': 'saved'}]}
    assert case.store.threads.require(case.thread.thread_id).title == 'updated'
    assert len(case.reads) <= 2


# LLM: writer已释放文件锁后，后置stat不能证明返回的旧对象仍等于磁盘的新内容。
# 函数用途: 两个写入口均不得把另一写者的指纹绑定给旧快照。
@pytest.mark.parametrize('method', ['write', 'update_atomic'])
def test_thread_record_cache_writer_does_not_publish_another_writers_fingerprint(thread_record_cache_case, monkeypatch, method):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    primitive_name = 'write_json_file_atomic' if method == 'write' else 'update_json_file_atomic'
    original = getattr(case.module, primitive_name)
    external_write = case.module.write_json_file_atomic

    # LLM: 原提交完成到ThreadStore收到返回值之间，插入另一写者的真实提交；不改原更新返回值。
    # 函数用途: 防止给本轮旧对象贴上另一写者的新文件指纹，污染后续读取。
    def change_after_commit(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        external_write(path, replace(case.thread, title='external').to_dict())
        return result

    monkeypatch.setattr(case.module, primitive_name, change_after_commit)
    if method == 'write':
        case.store.threads.write(replace(case.thread, title='own-write'))
    else:
        assert case.store.threads.update_summary(case.thread.thread_id, 'own-summary').summary == 'own-summary'
    assert case.store.threads.require(case.thread.thread_id).title == 'external'


# LLM: 即使线程已缓存，权限变化后的真实reader失败仍应保留原错误；错误本身不能缓存。
# 函数用途: 用受控文件读取拒绝验证缓存失效及权限错误的重复报告。
def test_thread_record_cache_drops_permission_failures(thread_record_cache_case, monkeypatch):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    before = case.path.stat()
    os.utime(case.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
    original = Path.read_text

    # LLM: 只拒绝目标文件的真实读取，存在性检查和其它文件仍沿原路径；不模拟JSON错误信封。
    # 函数用途: 精确注入可重复的PermissionError，不依赖测试执行者的系统属主权限。
    def denied(path, *args, **kwargs):
        if path == case.path:
            raise PermissionError('cache read denied')
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', denied)
    for _ in range(2):
        thread, error = case.store.threads.load_report(case.thread.thread_id)
        assert thread is None and error['context'] == 'conversation.thread.read'
        assert error['path'] == str(case.path) and error['thread_id'] == case.thread.thread_id
        assert str(case.path) not in case.module._THREAD_RECORD_CACHE
    assert len(case.reads) == 3


# LLM: 同路径并发命中只允许共享私有快照，不共享返回字典；使用真实线程池和原reader计数。
# 函数用途: 验证缓存锁和克隆在八个并发调用者间不丢记录、不串数据。
def test_thread_record_cache_parallel_reads_are_isolated(thread_record_cache_case):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    barrier = Barrier(8)

    # LLM: 每个worker多次读同一物理文件并原地改自己的返回字典，不写权威线程。
    # 函数用途: 同时触发LRU命中与对象克隆，核对每次读到的仍是原空metadata。
    def read_and_mutate(index):
        barrier.wait(timeout=10)
        for _ in range(25):
            value = case.store.threads.require(case.thread.thread_id)
            assert value.metadata == {}
            value.metadata['caller'] = index
        return index

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert list(workers.map(read_and_mutate, range(8))) == list(range(8))
    assert len(case.reads) == 1


# LLM: 私有克隆原语还需隔离两个字典元组，构造合法dataclass后直接验证入缓存/出缓存两边界。
# 函数用途: 核对通知和方法账本的元组内部字典不会被来源对象或返回对象修改。
def test_thread_record_cache_clones_dictionary_tuple_fields(thread_record_cache_case):
    case = thread_record_cache_case
    snapshot = replace(case.thread, pending_host_notices=({'nested': [1]},), conversation_methods=({'nested': [2]},))
    case.module._store_cached_thread(case.path, case.module._file_fingerprint(case.path), snapshot)
    snapshot.pending_host_notices[0]['nested'].append('input')
    snapshot.conversation_methods[0]['nested'].append('input')
    first = case.store.threads.require(case.thread.thread_id)
    assert first.pending_host_notices == ({'nested': [1]},)
    assert first.conversation_methods == ({'nested': [2]},)
    first.pending_host_notices[0]['nested'].append('output')
    first.conversation_methods[0]['nested'].append('output')
    second = case.store.threads.require(case.thread.thread_id)
    assert second.pending_host_notices == ({'nested': [1]},)
    assert second.conversation_methods == ({'nested': [2]},)


# LLM: 线程文件内thread_id与文件名身份不一致时视为损坏，不得写入正缓存或返回错误身份对象。
# 函数用途: 验证身份不匹配的线程记录在读取与枚举时均报错且不入缓存。
def test_thread_record_cache_rejects_mismatched_identity(thread_record_cache_case):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    assert str(case.path) in case.module._THREAD_RECORD_CACHE
    mismatched = replace(case.thread, thread_id='different-thread').to_dict()
    case.path.write_text(json.dumps(mismatched), encoding='utf-8')
    thread, error = case.store.threads.load_report(case.thread.thread_id)
    assert thread is None and error is not None
    assert error['thread_id'] == case.thread.thread_id
    assert str(case.path) not in case.module._THREAD_RECORD_CACHE
    threads, errors = case.store.threads.list_report(limit=0)
    assert threads == [] and len(errors) == 1
    assert str(case.path) not in case.module._THREAD_RECORD_CACHE


# LLM: 大线程仍完整解析且返回独立对象，但不得驻留缓存或额外克隆；此前的小记录必须被逐出。
# 函数用途: 用超过64KiB的真实metadata验证缓存体积上限和大记录直返。
def test_scaling_d_large_thread_bypasses_cache_and_cloning(thread_record_cache_case, monkeypatch):
    case = thread_record_cache_case
    case.store.threads.require(case.thread.thread_id)
    payload = replace(case.thread, metadata={"large": "x" * (70 * 1024), "nested": [1]}).to_dict()
    case.path.write_text(json.dumps(payload), encoding="utf-8")
    clones = []
    original = case.module._clone_cached_thread

    # LLM: 克隆仍执行原实现，计数不替代数据隔离或读取结果。
    # 函数用途: 核对大记录读取没有额外deepcopy。
    def clone(thread):
        clones.append(thread.thread_id)
        return original(thread)

    monkeypatch.setattr(case.module, "_clone_cached_thread", clone)
    first = case.store.threads.require(case.thread.thread_id)
    assert first.metadata == payload["metadata"]
    assert str(case.path) not in case.module._THREAD_RECORD_CACHE
    first.metadata["nested"].append(2)
    second = case.store.threads.require(case.thread.thread_id)
    assert second.metadata["nested"] == [1]
    assert clones == []
    assert case.module._THREAD_RECORD_CACHE_MAX_ENTRIES_COUNT == 16384
    assert case.module._THREAD_RECORD_CACHE_MAX_RECORD_BYTES == 64 * 1024


# LLM: 空文件走真实消息尾读并跨线程保留独立游标，只有不可变零位图应共享。
# 函数用途: 验证空seen过滤器不按线程重复分配8KiB，且游标容量保持32768。
def test_scaling_d_empty_message_cursors_share_seen_filter(tmp_path):
    from agent_py_agent.agent.conversation import message_cursor
    from agent_py_agent.agent.io.cursor_cache import cursor_key

    seen = []
    for name in ("a", "b"):
        path = tmp_path / f"{name}.jsonl"
        path.touch()
        os.utime(path, (1, 1))
        assert message_cursor.read_after_rows(path, "", 10) == ([], [])
        seen.append(message_cursor._MESSAGE_CURSORS.get(cursor_key(path, "")).seen)
    assert seen[0] == seen[1] == bytes(8192)
    assert seen[0] is seen[1]
    assert seen[0] is message_cursor._EMPTY_SEEN_FILTER
    assert message_cursor._MESSAGE_CURSOR_CACHE_MAX_COUNT == 32768


# LLM: 同次缓存提交的锚点和新增位置必须共用一次冻结，计数真实bytes构造而非mock缓存结果。
# 函数用途: 验证非空seen在_save_positions与空尾更新之间不会重复复制。
def test_scaling_d_nonempty_seen_is_frozen_once(tmp_path, monkeypatch):
    import builtins

    from agent_py_agent.agent.conversation import message_cursor
    from agent_py_agent.agent.io.cursor_cache import cursor_key, file_stamp

    path = tmp_path / "messages.jsonl"
    path.write_text("{}\n")
    os.utime(path, (1, 1))
    stamp = file_stamp(path.stat())
    seen = bytearray(8192)
    seen[0] = 1
    cursor = message_cursor.MessageCursor(stamp, 0, 1, bytes(seen))
    calls = []

    # LLM: 只观察当前模块把bytearray转bytes的次数，所有字节内容仍由内建构造器生成。
    # 函数用途: 捕获过滤器复制，避免只检查对象存在。
    def freeze(value):
        if isinstance(value, bytearray):
            calls.append(1)
        return builtins.bytes(value)

    monkeypatch.setattr(message_cursor, "bytes", freeze, raising=False)
    message_cursor._cache_read(path, stamp, ("anchor", cursor), ([], [], [("next", 1, 2)], seen), limit=10)
    assert calls == [1]
    a = message_cursor._MESSAGE_CURSORS.get(cursor_key(path, "anchor"))
    b = message_cursor._MESSAGE_CURSORS.get(cursor_key(path, "next"))
    assert a.seen is b.seen and a.seen[0] == 1


# LLM: 真实回执/排队文件/终态文件验证整条对账入口；热队列仍存在时不能错误结算。
# 函数用途: 验证queued请求已归档且不在热队列时落盘终态未知，不再永久queued。
@pytest.mark.parametrize("hot", [None, "inbox", "processing"])
# LLM: 每个场景只操作临时Gateway，不运行模型、业务命令或网络请求。
# 函数用途: 对比终态唯一与终态/热队列共存的回执状态。
def test_scaling_d_terminal_queued_receipt_settles(tmp_path, hot):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.tests.test_steer_delivery_recovery import _background_input

    agent, paths, request_id = _background_input(tmp_path, "scaling-task")
    agent.conversation_store.tasks.update_status({"task_id": "scaling-task", "status": "completed"})
    delivery.reconcile_gateway_input_receipts(paths, agent)
    source = paths.inbox / f"{request_id}.json"
    payload = json.loads(source.read_text())
    if hot is None:
        source.unlink()
    elif hot == "processing":
        source.replace(paths.processing / source.name)
    write_json_file_atomic(paths.terminal / source.name, {**payload, "status": "completed", "turn_phase": "closed"})
    summary = delivery.reconcile_gateway_input_receipts(paths, agent)
    expected = "terminal_unknown" if hot is None else "queued"
    assert delivery.read_gateway_input_receipt(paths, request_id).state == expected
    assert summary[expected] == 1 and summary["errors"] == 0


# LLM: 真实目录和游标文件，glob spy透传；state子目录写入不应失效父目录投影。
# 函数用途: 核对回执扫描按名字二分轮转，缓存复用与新增文件下一轮可见。
def test_scaling_d_receipt_directory_cache_rotates_without_reglob(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.tests.test_steer_delivery_recovery import _gateway_agent

    _agent, paths = _gateway_agent(tmp_path)
    root = paths.root / "input_receipts"
    (root / "state").mkdir(parents=True, exist_ok=True)
    for name in ("a", "b", "c"):
        (root / f"{name}.json").write_text("{}")
    os.utime(root, (1, 1))
    calls = []
    original = Path.glob

    # LLM: 只记录本次临时回执目录枚举，其他文件访问保持原样。
    # 函数用途: 证明游标更新后没有重复全目录排序。
    def glob(path, pattern, *args, **kwargs):
        if path == root:
            calls.append(pattern)
        return original(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", glob)
    assert [p.name for p in delivery._reconcile_receipt_paths(paths, limit=2)] == ["a.json", "b.json"]
    before = root.stat().st_mtime_ns
    delivery._commit_reconcile_cursor(paths, root / "b.json")
    assert root.stat().st_mtime_ns == before
    assert [p.name for p in delivery._reconcile_receipt_paths(paths, limit=2)] == ["c.json", "a.json"]
    assert calls == ["*.json"]
    (root / "d.json").write_text("{}")
    assert [p.name for p in delivery._reconcile_receipt_paths(paths, limit=2)] == ["c.json", "d.json"]


# LLM: 回执和canonical文件真实落盘，仅在新helper读取guidance的边界注入消费证明或typed未知；不伪装真实端到端鉴权。
# 函数用途: 覆盖终态queued的consumed分支，以及原typed异常同对象传播且游标/回执不推进。
@pytest.mark.parametrize("outcome", ["consumed", "uncertain"])
def test_scaling_d_terminal_queued_consumption_and_uncertain_cursor(tmp_path, monkeypatch, outcome):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts import input_delivery_service as delivery
    from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic
    from agent_py_agent.tests.test_steer_delivery_recovery import _background_input

    agent, paths, request_id = _background_input(tmp_path, "scaling-task")
    agent.conversation_store.tasks.update_status({"task_id": "scaling-task", "status": "completed"})
    delivery.reconcile_gateway_input_receipts(paths, agent)
    source = paths.inbox / f"{request_id}.json"
    payload = json.loads(source.read_text())
    source.unlink()
    write_json_file_atomic(paths.terminal / source.name, {**payload, "status": "completed", "turn_phase": "closed"})
    cursor = paths.root / "input_receipts/state/reconcile_cursor.json"
    before_cursor = cursor.read_bytes()
    before_receipt = delivery.gateway_input_receipt_path(paths, request_id).read_bytes()
    error = RequestExecutionOutcomeUncertainError({
        "id": request_id, "execution_attempt_id": "test-attempt",
        "lease_epoch": 1, "lease_owner": "test-attempt",
    }, "test-original-guidance-read")
    original = delivery.gateway_input_guidance_binding
    calls = []

    # LLM: 外层目标检查沿真实guidance，只有第二次（新helper）读取注入分支输入，保持异常身份不包装。
    # 函数用途: 将消费确认或原运输未结放在终态queued收口的精确边界。
    def binding(*args):
        calls.append(1)
        if len(calls) == 1:
            return original(*args)
        if outcome == "uncertain":
            raise error
        return SimpleNamespace(status="consumed"), ""

    monkeypatch.setattr(delivery, "gateway_input_guidance_binding", binding)
    if outcome == "uncertain":
        with pytest.raises(RequestExecutionOutcomeUncertainError) as caught:
            delivery.reconcile_gateway_input_receipts(paths, agent)
        assert caught.value is error
        assert cursor.read_bytes() == before_cursor
        assert delivery.gateway_input_receipt_path(paths, request_id).read_bytes() == before_receipt
    else:
        summary = delivery.reconcile_gateway_input_receipts(paths, agent)
        assert summary["consumed"] == 1 and summary["errors"] == 0
        assert delivery.read_gateway_input_receipt(paths, request_id).state == "consumed"
    assert len(calls) == 2
    assert not source.exists()
