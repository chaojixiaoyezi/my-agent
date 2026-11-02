"""R10 返工 R1：子代理创建的模型建议与谱系权威（P1-R10-01/02），全程真实 SubAgentManager、RuntimeDB 与 ConversationStore。"""
from __future__ import annotations

import os
from dataclasses import replace

from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
from agent_py_agent.agent.conversation.host_message_context import media_for_run
from agent_py_agent.agent.conversation.im_delegation import prepare_delegated_execution
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    _child_create_params,
    scheduled_child_create_params,
)
from agent_py_agent.tests.test_decision_subagent import created, provider
from agent_py_agent.tests.test_decision_subagent import prepared as prepared
from agent_py_agent.tests.test_host_message_context import select_child
from agent_py_agent.tests.test_im_input_media_delegation import active_media_child, media_case


# 函数用途: 读出运行库为该孩子记录的父 run_id（无父边时为空串），用来与 canonical parent_id 对账。
def _runtime_parent_run_id(agent, task) -> str:
    repo = agent.subagents.runtime_db
    parent_agent_run_id = repo.agent_run_for_run_id(task.id)["parent_agent_run_id"]
    return repo.get_agent_run(parent_agent_run_id)["run_id"] if parent_agent_run_id else ""


def test_non_business_child_under_other_execution_keeps_runtime_parent_equal_to_canonical_parent(tmp_path, monkeypatch):
    agent, params, _, _, _, _ = media_case(tmp_path, monkeypatch)
    actor = agent._current_run_params.host_channel_actor
    prepare_delegated_execution(agent, params)
    child = agent.subagents.list_runs()[0]
    with active_media_child(agent, child) as active:
        raw = {"goal": "在另一执行轮下创建的普通孩子"}
        run_params = create_run_params(agent, raw, raw["goal"], ["read_file"])
        # 前提：非业务创建，且 canonical 谱系父不是当前执行轮；当前执行轮不能替谱系父作证。
        assert run_params.business_context is None
        assert run_params.parent_id and run_params.parent_id != active.id
        assert run_params.host_parent_execution is None
        manual = agent.subagents.create_run(params=run_params)
    assert manual.parent_id == run_params.parent_id
    for task in agent.subagents.list_runs():
        assert _runtime_parent_run_id(agent, task) == task.parent_id
    select_child(agent, manual)
    assert media_for_run(agent, actor) == ()


def test_nested_decision_advice_survives_commit(prepared, monkeypatch):
    agent, (a, _), _ = prepared
    parent = agent.subagents.create_run(goal="父任务", allowed_tools=["create_subagents"])
    agent._current_subagent_run_id = parent.id
    agent._current_run_params.run_id = parent.id
    seen = provider(monkeypatch, prepared, {"0": a})
    task = created(agent, CreateSubagentsTool(agent).execute({"items": [{"goal": "检查材料"}]}))[0]
    assert len(seen) == 1 and task.parent_id == parent.id
    thread = agent.conversation_store.threads.require(task.agent_thread_id)
    assert thread.metadata["host_subagent_model_advice.v1"]["profile_id"] == a
    assert _runtime_parent_run_id(agent, task) == parent.id


def test_scheduled_child_params_carry_canonical_parent_execution(prepared):
    agent, _, _ = prepared
    manager = agent.subagents
    parent = manager.create_run(goal="父任务", allowed_tools=["create_subagents"])
    spec = HierarchyChildSpec(goal="检查材料")
    params = scheduled_child_create_params(manager, parent, spec, sibling_index=1)
    assert params.host_parent_execution.run_id == parent.id
    assert params.host_parent_execution.agent_run_id == manager.runtime_db.agent_run_for_run_id(parent.id)["agent_run_id"]
    # 准备与提交各构造一次：同一事实必须得到相等参数，refreeze 才不会丢弃建议；读范围预检的参数只差父执行来源。
    assert scheduled_child_create_params(manager, parent, spec, sibling_index=1) == params
    assert replace(params, host_parent_execution=None) == _child_create_params(parent, spec, sibling_index=1)
    before = len(os.listdir("/dev/fd"))
    for _ in range(5):
        scheduled_child_create_params(manager, parent, spec, sibling_index=1)
    assert len(os.listdir("/dev/fd")) == before
