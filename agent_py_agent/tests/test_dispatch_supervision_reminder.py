from __future__ import annotations

import json
from types import SimpleNamespace

# 防回归(T2·治"说了登记提醒却没真调"): 主代理派完子代理宣称"已登记非阻塞等待提醒",
# owner store 的 progress_policies/ 却是空目录——40 分钟窗口零定时唤醒,中途上报只能
# 等完成事件(命中延迟全为报告级 ≥267s;对照组模型真调了 wait,延迟 9.8~41.7s)。
# 机制层修法:create_subagents 成功出口自动登记低频监督 policy,不依赖模型自觉。
# 钉子:①无提醒时自动登记;②模型已登记的不覆盖;③配置 0=关;④登记的 policy 走
# 既有收口退休链(ok=True 自动停,无终态复活)。

from agent_py_agent.agent.agent_core.runtime.progress_policy_retirement import (
    retire_task_progress_policies_on_closeout,
)
from agent_py_agent.agent.agent_core.runtime.wait_tool import (
    register_dispatch_supervision_policy,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


def _agent(tmp_path, **config_kwargs) -> SimpleAgent:
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs", **config_kwargs), tmp_path
    )
    agent._current_run_params = SimpleNamespace(task_id="req_dispatch_task")
    return agent


def test_dispatch_supervision_registers_policy_when_none(tmp_path):
    agent = _agent(tmp_path)
    result = register_dispatch_supervision_policy(agent)
    assert result is not None and result["existing"] is False
    policy = agent.conversation_store.get_progress_policy(result["policy_id"])
    assert policy is not None and policy.enabled is True
    assert policy.task_id == "req_dispatch_task"
    assert policy.metadata.get("kind") == "subagent_progress_watch"
    assert policy.metadata.get("tool") == "dispatch_supervision_auto"
    # 默认间隔来自 config dispatch_supervision_reminder_seconds(180)
    assert policy.interval_seconds == 180


def test_dispatch_supervision_does_not_override_model_wait(tmp_path):
    agent = _agent(tmp_path)
    registered = json.loads(
        agent.tools.tools["wait"].execute(
            {"task_id": "req_dispatch_task", "seconds": 90, "reason": "模型自己登记的盯守"}
        ).output
    )
    result = register_dispatch_supervision_policy(agent)
    assert result is not None and result["existing"] is True
    assert result["policy_id"] == registered["policy_id"]
    kept = agent.conversation_store.get_progress_policy(registered["policy_id"])
    assert kept is not None and kept.enabled is True and kept.interval_seconds == 90
    # 没有第二条同任务 policy 被塞进来
    same_task = [
        p
        for p in agent.conversation_store.list_progress_policies(enabled_only=True)
        if p.task_id == "req_dispatch_task"
    ]
    assert len(same_task) == 1


def test_dispatch_supervision_disabled_by_config_zero(tmp_path):
    agent = _agent(tmp_path, dispatch_supervision_reminder_seconds=0)
    assert register_dispatch_supervision_policy(agent) is None
    assert agent.conversation_store.list_progress_policies(enabled_only=True) == []


def test_dispatch_supervision_policy_retires_on_ok_closeout(tmp_path):
    agent = _agent(tmp_path)
    result = register_dispatch_supervision_policy(agent)
    assert result is not None
    params = SimpleNamespace(task_id="req_dispatch_task", context_scope="default")
    retire_task_progress_policies_on_closeout(agent, params, {"ok": True})
    retired = agent.conversation_store.get_progress_policy(result["policy_id"])
    assert retired is not None and retired.enabled is False, "任务收口后监督提醒必须自动退休"


def test_dispatch_supervision_survives_missing_run_context(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
    # 无 _current_run_params/无 task_id → best-effort 返回 None,绝不抛
    assert register_dispatch_supervision_policy(agent) is None
