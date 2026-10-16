from dataclasses import replace

import pytest

from agent_py_agent.agent.agent_core.cli_run_conversation import (
    CliRunConversationPersistenceError,
    bind_cli_run_conversation,
)
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation.learning_source import (
    background_projection,
    schedule_projection,
    subagent_projection,
    user_dialogue_projection,
)
from agent_py_agent.tests.test_cli_run_conversation import _agent, _StaticBackend


# LLM: 复用实际 CLI 运行参数，不从 prompt、origin 或模拟工具结果授予学习。
# 函数用途: 为来源合同回归构造可信 CLI 宿主输入，所有 Agent 仍使用测试临时 home。
def _params(attributes=None):
    return RunParams(source="cli_run", request_id="cli-source-request",
                     run_id="cli-source-run", task_id="cli-source-task",
                     task_attributes=attributes or {})


def test_cli_host_propagates_one_original_source_to_thread_message_and_run(tmp_path):
    agent = _agent(tmp_path)
    assert agent.home_paths.root.resolve().is_relative_to(tmp_path.resolve())
    bound = bind_cli_run_conversation(agent, _params(), "真实用户输入")
    thread = agent.conversation_store.threads.require(bound.task_attributes["conversation_thread_id"])
    message = agent.conversation_store.messages.recent(thread.thread_id, limit=0)[0]
    assert thread.learning_source == user_dialogue_projection()
    assert message.metadata["learning_source"] == thread.learning_source
    assert bound.task_attributes["learning_source"] == thread.learning_source


@pytest.mark.parametrize("source", [schedule_projection(), background_projection(),
    {**user_dialogue_projection(), "can_learn": False},
    {**background_projection(), "source_kind": "dataset"},
    None, {"can_learn": True}])
def test_cli_host_preserves_explicit_forbidden_or_unverifiable_source(tmp_path, source):
    agent = _agent(tmp_path)
    bound = bind_cli_run_conversation(agent, _params({"learning_source": source}), "用户正文不能覆盖宿主来源")
    thread = agent.conversation_store.threads.require(bound.task_attributes["conversation_thread_id"])
    message = agent.conversation_store.messages.recent(thread.thread_id, limit=0)[0]
    assert bound.task_attributes["learning_source"]["can_learn"] is False
    assert message.metadata["learning_source"] == bound.task_attributes["learning_source"]
    assert thread.learning_source == bound.task_attributes["learning_source"]


@pytest.mark.parametrize("trace", ["scheduler_run_id", "conversation_session_task_id",
                                 "background_wake_signal_id", "background_delivery_reason"])
def test_cli_auto_trace_cannot_be_laundered_by_user_projection(tmp_path, trace):
    agent = _agent(tmp_path)
    bound = bind_cli_run_conversation(agent, _params({trace: "original-automation",
                      "learning_source": user_dialogue_projection()}), "普通用户样式正文")
    assert bound.task_attributes["learning_source"]["can_learn"] is False


def test_cli_existing_forbidden_thread_wins_over_new_cli_host_projection(tmp_path):
    agent = _agent(tmp_path)
    thread = agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "cli",
        "channel_conversation_id": "cli-source-request", "channel_user_id": "local-agent",
        "owner_id": agent.home_paths.owner_id, "owner_home": str(agent.home_paths.owner_home_dir),
        "learning_source": schedule_projection(),
    })
    bound = bind_cli_run_conversation(agent, _params(), "输入不提升旧线程来源")
    assert bound.task_attributes["conversation_thread_id"] == thread.thread_id
    assert bound.task_attributes["learning_source"] == schedule_projection()
    assert agent.conversation_store.threads.require(thread.thread_id).learning_source == schedule_projection()


@pytest.mark.parametrize("changed_source", [schedule_projection(),
    subagent_projection(trigger_kind="user_dialogue", run_ids=["other-run"]),
    {**user_dialogue_projection(), "policy_version": 2}])
def test_cli_replay_source_or_lineage_drift_stops_before_model(tmp_path, changed_source):
    agent = _agent(tmp_path)
    first = bind_cli_run_conversation(agent, _params(), "同一用户输入")
    backend = _StaticBackend()
    agent.backend = backend
    with pytest.raises(CliRunConversationPersistenceError):
        agent.run("同一用户输入", params=replace(first,
                     task_attributes={**first.task_attributes, "learning_source": changed_source}))
    assert backend.calls == 0


def test_cli_continuation_inherits_original_root_source_without_new_user_evidence(tmp_path):
    agent = _agent(tmp_path)
    first = bind_cli_run_conversation(agent, _params(), "首轮真实用户输入")
    continuation = replace(first, request_id="cli-source-request#cont-1", continuation_seq=1,
        continuation_root_request_id="cli-source-request",
        continuation_root_thread_id=first.task_attributes["conversation_thread_id"],
        continuation_parent_attempt_id="attempt-original")
    bound = bind_cli_run_conversation(agent, continuation, "内部续跑事件")
    assert bound.task_attributes["learning_source"] == first.task_attributes["learning_source"]
    thread_id = bound.task_attributes["conversation_thread_id"]
    rows = agent.conversation_store.messages.recent(thread_id, limit=0)
    assert [row.role for row in rows] == ["user", "system"]
    assert rows[0].metadata["learning_source"] == user_dialogue_projection()
    assert rows[1].metadata["learning_source"] == rows[0].metadata["learning_source"]
    assert agent.conversation_store.threads.require(thread_id).learning_source == user_dialogue_projection()


def test_cli_orphan_continuation_never_synthesizes_user_source(tmp_path):
    agent = _agent(tmp_path)
    continuation = replace(_params({"learning_source": user_dialogue_projection()}),
        continuation_seq=1, continuation_root_request_id="missing-root",
        continuation_root_thread_id="")
    bound = bind_cli_run_conversation(agent, continuation, "没有原用户来源的续跑")
    assert bound.task_attributes["learning_source"]["can_learn"] is False
    rows = agent.conversation_store.messages.recent(bound.task_attributes["conversation_thread_id"], limit=0)
    assert [row.role for row in rows] == ["system"]


def test_cli_continuation_root_thread_drift_stops_before_model(tmp_path):
    agent = _agent(tmp_path)
    first = bind_cli_run_conversation(agent, _params(), "首轮真实用户输入")
    backend = _StaticBackend()
    agent.backend = backend
    continuation = replace(first, request_id="cli-source-request#cont-1", continuation_seq=1,
        continuation_root_request_id="cli-source-request", continuation_root_thread_id="other-thread")
    with pytest.raises(CliRunConversationPersistenceError):
        agent.run("内部续跑事件", params=continuation)
    assert backend.calls == 0


def test_cli_learning_exclusion_limits_original_ingress_and_child(tmp_path):
    from agent_py_agent.agent.conversation.learning_source import child_projection_from_parent
    from agent_py_agent.agent.user_space.owner_policy import (
        LearningExclusionEntry,
        append_learning_exclusions,
    )

    agent = _agent(tmp_path)
    append_learning_exclusions(agent.home_paths, (LearningExclusionEntry("cli-source-request"),))
    bound = bind_cli_run_conversation(agent, _params(), "用户正文不取消政策排除")
    source = bound.task_attributes["learning_source"]
    assert source["can_learn"] is False
    child = child_projection_from_parent(source, run_id="original-child", task_id="original-child-task")
    assert child["can_learn"] is False
    rows = agent.conversation_store.messages.recent(bound.task_attributes["conversation_thread_id"], limit=0)
    assert rows[0].metadata["learning_source"] == source


def test_cli_unreadable_learning_policy_never_grants_child_learning(tmp_path):
    agent = _agent(tmp_path)
    policy = agent.home_paths.owner_memory_policy_json
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("{broken-policy", encoding="utf-8")
    bound = bind_cli_run_conversation(agent, _params(), "未知政策只收紧学习来源")
    assert bound.task_attributes["learning_source"]["can_learn"] is False


def test_cli_excluded_actual_remember_preserves_original_promotion_denial(tmp_path):
    from agent_py_agent.agent.user_space.owner_policy import (
        LearningExclusionEntry,
        append_learning_exclusions,
    )
    from agent_py_agent.tests.test_cli_run_conversation import _RememberOnceBackend

    agent = _agent(tmp_path)
    append_learning_exclusions(agent.home_paths, (LearningExclusionEntry("cli-source-request"),))
    agent.backend = _RememberOnceBackend()
    agent.run("请长期记住：我的阅读清单代号是雪松。", params=_params())
    candidate = agent.memory_candidates.list()[0]
    assert agent.memory.all() == []
    assert candidate.status == "pending_review"
    assert candidate.learning_source["can_learn"] is False
