# LLM: 只测试真实轮末钩子与宿主快照入口；不构造第二目录或授予额外Skill，不调用外部模型。
# 模块用途: 守住同run下一工具轮的Skill刷新及原授权裁剪接缝。
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin
from agent_py_agent.agent.agent_core.tool_loop import round_execution
from agent_py_agent.agent.backends import ModelResponse


# LLM: 只把宿主既有scope provider交给新钩子，provider的返回值代表已裁剪授权快照，不重复自行过滤。
# 函数用途: 核对刷新始终使用真实任务工作区及同一权限入口。
def test_refresh_uses_run_scope_provider(tmp_path):
    refresh = getattr(SimpleAgentRuntimeMixin, "refresh_current_skill_snapshot_if_needed", None)
    assert callable(refresh), "缺少受控快照刷新入口"
    calls, snapshot = [], object()
    agent = SimpleNamespace(_current_run_task_workspace=str(tmp_path / "task"),
                            effective_workspace_root=tmp_path / "effective", root=tmp_path)

    # LLM: 记录路径并模拟已执行限制的结果，绝不直接调用不受限snapshot_for。
    # 函数用途: 观察刷新所用的宿主权限入口。
    def scoped(workspace):
        calls.append(workspace)
        return snapshot

    agent.skill_snapshot_for_run_scope = scoped
    refresh(agent)
    assert calls == [Path(tmp_path / "task")]
    assert agent._current_skill_snapshot is snapshot


# LLM: 没有任务目录时才使用effective workspace，不能意外扫回启动cwd或其它Owner目录。
# 函数用途: 核对正常会话工作区回退与快照赋值。
def test_refresh_uses_effective_workspace_fallback(tmp_path):
    refresh = getattr(SimpleAgentRuntimeMixin, "refresh_current_skill_snapshot_if_needed", None)
    assert callable(refresh), "缺少受控快照刷新入口"
    calls = []
    agent = SimpleNamespace(_current_run_task_workspace="", effective_workspace_root=tmp_path / "custom",
                            root=tmp_path / "wrong", skill_snapshot_for_run_scope=lambda path: calls.append(path) or "restricted")
    refresh(agent)
    assert calls == [tmp_path / "custom"]
    assert agent._current_skill_snapshot == "restricted"


# LLM: 调度、审批、上下文缩减的独立既有测试不在此镜像；真实execute_tool_round返回前必须调用刷新。
# 函数用途: 验证普通轮末刷新发生在上下文预算处理之后。
def test_round_refreshes_after_context_budget(monkeypatch):
    events = []
    monkeypatch.setattr(round_execution, "_append_assistant_tool_round_context", lambda request: None)
    monkeypatch.setattr(round_execution, "_append_deferred_tool_call_notice", lambda *args, **kwargs: None)
    monkeypatch.setattr(round_execution, "_enforce_turn_context_budget", lambda *args: events.append("budget"))
    agent = SimpleNamespace(refresh_current_skill_snapshot_if_needed=lambda: events.append("refresh"))
    request = round_execution.ToolRoundExecutionRequest(
        agent, SimpleNamespace(tool_context=[]), 1, ModelResponse("", "test"), [],
        lambda request: None, lambda record: None,
    )
    assert round_execution.execute_tool_round(request) is False
    assert events == ["budget", "refresh"]
