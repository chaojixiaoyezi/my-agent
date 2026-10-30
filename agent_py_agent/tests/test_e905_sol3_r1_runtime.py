# LLM: 通过真实代理构造和恢复入口验证宿主冻结，全部文件只写测试临时家，不发送模型请求或启动服务。
# 模块用途: 防止部署根与环境根混用，以及同一运行片恢复额度随磁盘变化。
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.deployment_policy import DeploymentPolicyError
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home


# LLM: 临时宿主目录先经产品初始化；直接写合成策略仅为准备不同修订的故障夹具，不是正式写入口。
# 函数用途: 在测试家目录设置指定守卫额度。
def _policy(root, threshold, *, revision=1):
    paths = ensure_my_agent_home(root)
    path = paths.root / "config" / "deployment_policy.json"
    path.write_text(json.dumps({"schema_version": "deployment-policy.v1", "revision": revision, "runtime_guards": {
        "repeat_fail_threshold": threshold,
        "main_agent_auto_resume_attempt_limit": threshold,
        "provider_transient_redispatch_limit": threshold,
    }}), encoding="utf-8")
    return path


# LLM: 显式内存echo只用于构造测试桩，不依赖网络或用户实际目录模型。
# 函数用途: 建立实际代理并保留可信配置根。
def _agent(home, workspace):
    return SimpleAgent(AgentConfig(my_agent_home=str(home), model_backend="echo", prompt_files=[],
                                   enable_tools=False), workspace, defer_mcp_connect=True)


# LLM: 配置根和环境根都是真实临时目录，断言实际代理与配置投影共用同一冻结策略。
# 函数用途: 核对正式构造不读取环境中的另一份策略。
def test_simple_agent_guard_uses_configured_host_home(tmp_path, monkeypatch):
    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7)
    _policy(other, 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    agent = _agent(first, tmp_path / "workspace")
    assert agent.home_paths.root == first
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    assert getattr(agent.config, "runtime_guard_policy", None) is agent.runtime_guard_policy
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "changed"))
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    assert replace(agent.config).runtime_guard_policy is agent.runtime_guard_policy
    _policy(first, 11, revision=2)
    rebuilt = SimpleAgent(replace(agent.config), tmp_path / "reloaded-workspace", defer_mcp_connect=True)
    assert rebuilt.home_paths.root == first
    assert rebuilt.runtime_guard_policy.repeat_fail_threshold == 11
    assert rebuilt.config.runtime_guard_policy is rebuilt.runtime_guard_policy
    assert agent.runtime_guard_policy.repeat_fail_threshold == 7
    _policy(first, 17, revision=3).write_text("{broken", encoding="utf-8")
    with pytest.raises(DeploymentPolicyError):
        SimpleAgent(replace(rebuilt.config), tmp_path / "invalid-reload", defer_mcp_connect=True)


# LLM: 正式构造必须在使用错误环境策略之前拒绝损坏的配置根策略，不把读取失败变成默认。
# 函数用途: 验证真实Agent入口对损坏宿主策略失败关闭。
def test_simple_agent_bad_configured_policy_fails_closed(tmp_path, monkeypatch):
    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7).write_text("{broken", encoding="utf-8")
    _policy(other, 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    with pytest.raises(DeploymentPolicyError):
        _agent(first, tmp_path / "workspace")


# LLM: 恢复判断与落盘账本消费运行时原快照，环境和磁盘在两步之间变化也不能重选。
# 函数用途: 验证主代理恢复额度在判断与记账之间不漂移。
def test_main_resume_decision_and_ledger_share_frozen_policy(tmp_path, monkeypatch):
    from agent_py_agent.agent.contracts.main_agent_auto_resume import (
        auto_resume_decision,
        record_auto_resume_attempt,
    )
    from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy
    from agent_py_agent.agent.user_space.home_layout import home_paths

    home = tmp_path / "host"
    _policy(home, 7)
    frozen = runtime_guard_policy(trusted_host_paths=home_paths(home))
    request = SimpleNamespace(execute=True, recovery_packet_path=None, auto_recovery_active=False,
                              task_timeout_seconds=600)
    runtime = SimpleNamespace(paths={"root": tmp_path}, request=request,
                              case=SimpleNamespace(case_id="case"), runtime_guard_policy=frozen)
    bundle = SimpleNamespace(runtime=runtime, status="FAILED", recovery_packet_ref="packet.json", duration_seconds=1)
    _policy(home, 13, revision=2)
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    decision = auto_resume_decision(bundle)
    assert decision.allowed and decision.max_attempts == 7
    _policy(home, 17, revision=3)
    assert record_auto_resume_attempt(bundle)["max_attempts"] == decision.max_attempts


# LLM: 直接走正式runner候选闸验证快照传递，不模拟供应额度helper的返回值。
# 函数用途: 验证子代理重投不会被磁盘更新放宽额度。
def test_runner_candidate_consumes_frozen_guard(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.runner.dispatch import (
        RunnerCandidatePolicy,
        _is_dispatch_runner_candidate,
    )
    from agent_py_agent.agent.settings.deployment_policy import RuntimeGuardPolicy

    _policy(tmp_path / "home", 13)
    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    task = SimpleNamespace(id="test", attributes={}, status="FAILED", failure_type="transient_error",
                           runner_attempts=8, channel_status="", capability_requests=[], capability_gaps=[],
                           runner_active_attempt_id="", runner_session_pid=0)
    policy = RunnerCandidatePolicy()
    object.__setattr__(policy, "runtime_guard_policy", RuntimeGuardPolicy(provider_transient_redispatch_limit=7))
    assert not _is_dispatch_runner_candidate(task, policy=policy)


def test_planner_and_capability_sweep_prefilters_consume_agent_frozen_guard(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep import (
        _redispatch_stalled_subagents,
    )
    from agent_py_agent.agent.agent_core.orchestration.dispatch.service import (
        update_pending_work_state,
    )
    from agent_py_agent.agent.agent_core.planner_service import (
        PlannerStateParams,
        _collect_parent_planner_context,
    )
    from agent_py_agent.agent.capability.config import CapabilityConfig

    first, other = tmp_path / "configured", tmp_path / "environment"
    _policy(first, 7)
    _policy(other, 0)
    monkeypatch.setenv("MY_AGENT_HOME", str(other))
    agent = _agent(first, tmp_path / "workspace")
    # 冻结后把磁盘和环境都改成 0（收紧）或 20（放宽），验证两个真实预筛入口始终只认 agent 冻结额度 7。
    _policy(first, 0, revision=2)
    _policy(other, 0, revision=2)

    eligible_task = SimpleNamespace(
        id="run-eligible",
        attributes={},
        status="BLOCKED",
        failure_type="transient_error",
        runner_attempts=6,
        channel_status="",
        capability_requests=[],
        capability_gaps=[],
        runner_active_attempt_id="",
        runner_session_pid=0,
        child_ids=[],
    )
    exhausted_task = SimpleNamespace(
        id="run-exhausted",
        attributes={},
        status="BLOCKED",
        failure_type="transient_error",
        runner_attempts=8,
        channel_status="",
        capability_requests=[],
        capability_gaps=[],
        runner_active_attempt_id="",
        runner_session_pid=0,
        child_ids=[],
    )
    monkeypatch.setattr(agent.subagents, "list_runs", lambda: [eligible_task, exhausted_task])
    monkeypatch.setattr(
        agent.subagents,
        "board",
        SimpleNamespace(
            build_board=lambda **_kw: {},
            due_check=lambda **_kw: {},
            plan_actions=lambda *_a, **_kw: [],
        ),
    )
    _, ctx = _collect_parent_planner_context(
        agent,
        PlannerStateParams(cfg=CapabilityConfig(), limit=10, max_runners=10, reviewer="", note=""),
    )
    assert [t.id for t in ctx.runner_candidates] == ["run-eligible"]

    _policy(first, 20, revision=3)
    _policy(other, 20, revision=3)
    assert update_pending_work_state(agent) is True

    dispatched_ids: list[list[str]] = []

    def _fake_dispatch(router, cfg, *, params):
        dispatched_ids.append(list(params.include_run_ids or []))
        return SimpleNamespace(
            records=[
                SimpleNamespace(step="runner", action="started", run_id=rid)
                for rid in (params.include_run_ids or [])
            ]
        )

    monkeypatch.setattr(
        "agent_py_agent.agent.agent_core.orchestration.dispatch.capability_auto_sweep.conversation_lifecycle_decisions",
        lambda _agent, tasks: {str(t.id): SimpleNamespace(allowed=True) for t in tasks},
    )
    monkeypatch.setattr(agent, "dispatch_subagents", _fake_dispatch)
    started = _redispatch_stalled_subagents(agent)
    assert started == 1
    assert dispatched_ids == [["run-eligible"]]
