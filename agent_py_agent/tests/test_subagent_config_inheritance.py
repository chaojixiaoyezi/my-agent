# LLM: 正式包节点只核运行时参数/常量；全源码字符串和重复定义守卫独立成精确节点，禁止无源码空扫描冒称通过。
# 模块用途: 分开子代理真实继承合同与源码单一权威约束，包内继续验证原常量和运行行为。
"""子代理参数继承主代理的合同钉子。

钉死：子代理 runner 复用主代理同一个 agent 对象与 AgentConfig（模型、后端、超时、
上下文窗口、compact 触发点全部单一权威）；子代理不允许另设 compact 阈值，
默认 prompt 模板也只有一个权威位置。
"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.runner.prompts import (
    SUBAGENT_DEFAULT_PLAN,
    SUBAGENT_DEFAULT_THOUGHT,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy


class _FakeConfig:
    memory_compact_trigger_percent = 70
    model_context_window_tokens = 200_000


class _FakeAgent:
    config = _FakeConfig()
    root = "."


def test_subagent_compact_trigger_inherits_main_by_default():
    """task_local 回合的触发点与主代理完全一致。"""
    agent = _FakeAgent()
    main_policy = runtime_compact_policy(agent)
    sub_policy = runtime_compact_policy(agent, context_scope="task_local")
    assert sub_policy.trigger_percent == main_policy.trigger_percent == 70
    assert sub_policy.context_window_tokens == main_policy.context_window_tokens


# LLM: 这两个断言读取正式runtime常量，必须继续在真实pyc包内执行，不能随源码扫描一起排除。
# 函数用途: 确认原默认thought非空且plan保留四步，不读取业务源码。
def test_default_templates_have_single_authority():
    """默认模板的正式运行时常量保持可用。"""
    assert SUBAGENT_DEFAULT_THOUGHT
    assert len(SUBAGENT_DEFAULT_PLAN) == 4


def test_capability_config_warns_on_unknown_fields(tmp_path):
    """能力配置中的未知字段只告警并忽略（2026-09-27 起与主配置一致）。"""
    from agent_py_agent.agent.capability.config import load_capability_config

    config_file = tmp_path / "capability_config.yaml"
    config_file.write_text("unknown_runtime_override: 60\n", encoding="utf-8")
    config = load_capability_config(config_file)
    assert any("unknown_runtime_override" in w for w in config.config_warnings)


def test_finalize_context_carries_context_scope_for_compact_policy():
    """R0 真实任务回归钉子：finalization compact 链路要求 FinalizeContext 携带
    context_scope；这条属性在本机环境失败集掩盖下曾漏检，单独钉死。"""
    from dataclasses import fields

    from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext

    names = {f.name for f in fields(FinalizeContext)}
    assert "context_scope" in names
    assert "do_save" in names


def test_compact_auto_cycle_fields_reads_finalize_context_scope(tmp_path):
    """compact_auto_cycle_fields 必须能在最小 FinalizeContext 上运行（属性级回归）。"""
    from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
    from agent_py_agent.agent.agent_core.finalization_compact_auto import compact_auto_cycle_fields

    class _Resp:
        text = "done"
        runtime_status = ""
        runtime_reason = ""
        runtime_source = ""

    class _Cfg:
        memory_compact_trigger_percent = 70
        model_context_window_tokens = 200_000
        agent_name = "test"
        auto_save_memory = False

    class _Agent:
        config = _Cfg()
        root = None
        session_id = "s"

    agent = _Agent()
    agent.root = tmp_path
    run_home = tmp_path / "tasks" / "root-1" / "work" / "agents" / "run-1"
    run_home.mkdir(parents=True)
    ctx = FinalizeContext(
        user_prompt="p", final_prompt="p", final_response=_Resp(), memories=[],
        executed_tools=[], archive_tool_calls=[], routed_context=None,
        resume_context_result=None, runtime_injections=[],
        compression_snapshot_id="", compression_snapshot_path="",
        compression_applied=False, request_id="r", run_id="", task_id="",
        source="run", do_save=False,
        task_attributes={"agent_run_workspace_dir": str(run_home)},
        recovery_task_refs=None,
        recovery_content_paths=None, recovery_next_actions=None,
        context_scope="task_local",
    )
    payload = compact_auto_cycle_fields(agent, ctx, {"turn": 10, "active": 10})
    assert "memory_compact_auto_status" in payload
