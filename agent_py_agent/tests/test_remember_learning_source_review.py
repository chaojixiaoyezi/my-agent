"""Remember 真实入口不得把派活、后台或未知来源当作用户学习资格。"""
import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability.memory_tool import RememberTool
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.tests.test_remember_observation_identity import _tool_evidence


# LLM: 仅创建隔离宿主事实，不替换候选/晋升/证据验证；role=user本身不代表可学。
# 函数用途: 构造真实消息、线程、当前运行及其明确来源。
def _agent(tmp_path, source, metadata, attributes):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), prompt_files=[]), tmp_path / "workspace")
    spec = dict(canonical_user_id="user-1", channel="internal", channel_conversation_id="chat-1", channel_user_id="user-1")
    if source is not None:
        spec["learning_source"] = source
    thread = agent.conversation_store.threads.get_or_create(spec)
    agent.conversation_store.messages.append(dict(thread_id=thread.thread_id, role="user", content="项目使用 UTC 保存时间", metadata={"gateway_request_id": "req-source", **metadata}))
    agent._current_run_params = SimpleNamespace(request_id="req-source", run_id="run-source", task_id="task-source", task_attributes={"conversation_thread_id": thread.thread_id, **attributes})
    return agent


@pytest.mark.parametrize("case", [
    (None, {}, {}, False),
    (ls.dispatch_projection(), {}, {}, False),
    (ls.schedule_projection(), {}, {}, False),
    (ls.background_projection(), {}, {}, False),
    (ls.user_dialogue_projection(), {"session_task_id": "dispatch"}, {}, False),
    (ls.user_dialogue_projection(), {"learning_source": ls.user_dialogue_projection(), "session_task_id": "dispatch"}, {}, False),
    (ls.user_dialogue_projection(), {}, {"scheduler_job_id": "schedule"}, False),
    (ls.user_dialogue_projection(), {}, {}, True),
    (None, {"learning_source": ls.user_dialogue_projection()}, {}, True),
    (ls.subagent_projection(trigger_kind="user_dialogue", run_ids=["child"], task_ids=["parent-task"]), {}, {}, True),
], ids=["legacy-unknown", "dispatch-thread", "schedule-thread", "background-thread", "dispatch-message", "stale-explicit-user", "runtime-schedule", "user", "new-user-old-thread", "user-child"])
@pytest.mark.parametrize("origin", ["user_explicit", "tool_verified"])
# LLM: 保留全部来源及origin正反矩阵；可学来源经真实证据审核自动归档，成功工具不能洗白不可学来源。
# 函数用途: 验证来源资格决定是否自动写正式记忆，而不是把所有候选一律挂起。
def test_remember_keeps_host_learning_qualification(tmp_path, case, origin):
    source, metadata, attributes, allowed = case
    agent = _agent(tmp_path, source, metadata, attributes)
    request = dict(content="项目使用 UTC 保存时间", kind="project", origin=origin, subject_key="source.review")
    if origin == "tool_verified":
        evidence = _tool_evidence(agent, "source-review")
        request["evidence_refs"] = [evidence["call_id"]]
    result = RememberTool(agent).execute(request)
    assert result.ok, result.output
    assert json.loads(result.output)["active_memory_changed"] is allowed, result.output
    assert len(agent.memory.all()) == (1 if allowed else 0)
    candidate = agent.memory_candidates.list()[0]
    assert candidate.status == ('promoted' if allowed else 'pending_review')
    assert candidate.learning_source, "真实入口不能用空来源投影跳过来源闸"
    assert ls.projection_is_learnable(candidate.learning_source) is allowed
    if allowed and source and source.get("source_kind") == "subagent":
        assert candidate.learning_source["source_run_ids"] == ["child"]
