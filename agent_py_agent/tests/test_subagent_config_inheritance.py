"""子代理参数继承主代理的合同钉子。

钉死：子代理 runner 复用主代理同一个 agent 对象与 AgentConfig（模型、后端、超时、
上下文窗口、compact 触发点全部单一权威）；只有显式声明的覆盖项（capability_config 的
subagent_compact_trigger_percent > 0）才允许分叉；默认 prompt 模板只有一个权威位置。
"""

from __future__ import annotations

import re
from pathlib import Path

from agent_py_agent.agent.agent_core.runner.prompts import (
    SUBAGENT_DEFAULT_PLAN,
    SUBAGENT_DEFAULT_THOUGHT,
)
from agent_py_agent.agent.agent_core.runtime.context_compactor import runtime_compact_policy
from agent_py_agent.agent.capability.config import CapabilityConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent_py_agent" / "agent"


class _FakeConfig:
    memory_compact_auto_trigger_percent = 70
    model_context_window_tokens = 200_000


class _FakeAgent:
    config = _FakeConfig()
    root = "."


def test_subagent_compact_trigger_inherits_main_by_default():
    """capability 配置为 0（默认）时，task_local 回合的触发点与主代理完全一致。"""

    class _Snapshot:
        config = CapabilityConfig()  # subagent_compact_trigger_percent 默认 0

    agent = _FakeAgent()
    agent._capability_config_runtime_snapshot = _Snapshot()
    main_policy = runtime_compact_policy(agent)
    sub_policy = runtime_compact_policy(agent, context_scope="task_local")
    assert sub_policy.trigger_percent == main_policy.trigger_percent == 70
    assert sub_policy.context_window_tokens == main_policy.context_window_tokens


def test_subagent_compact_trigger_explicit_override():
    """capability 配置显式 >0 时只影响 task_local 回合，主代理回合不受影响。"""

    class _Snapshot:
        config = CapabilityConfig(subagent_compact_trigger_percent=60)

    agent = _FakeAgent()
    agent._capability_config_runtime_snapshot = _Snapshot()
    assert runtime_compact_policy(agent).trigger_percent == 70
    assert runtime_compact_policy(agent, context_scope="task_local").trigger_percent == 60


def test_subagent_runner_reuses_parent_agent_object():
    """run_flow 的模型回合必须走 lifecycle.agent.run（同一 agent 对象），
    不允许为子代理另建 backend/model/config。"""
    src = (AGENT_ROOT / "agent_core" / "subagent" / "run_flow.py").read_text(encoding="utf-8")
    assert "lifecycle.agent.run(" in src
    forbidden = ["SimpleAgent(", "load_config(", "make_backend(", "AgentConfig("]
    offenders = [marker for marker in forbidden if marker in src]
    assert offenders == [], f"子代理 run_flow 不应自建 agent/config/backend: {offenders}"


def test_default_templates_have_single_authority():
    """thought/plan 默认模板只允许定义在 runner/prompts.py；派工链路只引用。"""
    assert SUBAGENT_DEFAULT_THOUGHT
    assert len(SUBAGENT_DEFAULT_PLAN) == 4
    pattern = re.compile(re.escape(SUBAGENT_DEFAULT_THOUGHT))
    offenders = []
    for path in AGENT_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name == "prompts.py" and path.parent.name == "runner":
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], f"默认 thought 模板出现重复定义: {offenders}"


def test_capability_config_rejects_unknown_fields(tmp_path):
    """capability 配置 fail-closed：未知字段直接报错，不静默忽略。"""
    from agent_py_agent.agent.capability.config import load_capability_config

    config_file = tmp_path / "capability_config.yaml"
    config_file.write_text("subagent_compact_trigger_percentt: 60\n", encoding="utf-8")
    try:
        load_capability_config(config_file)
    except ValueError as exc:
        assert "未知字段" in str(exc)
    else:
        raise AssertionError("未知字段应当报错")


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
        memory_compact_auto_trigger_percent = 70
        model_context_window_tokens = 200_000
        agent_name = "test"
        auto_save_memory = False

    class _Agent:
        config = _Cfg()
        root = None
        session_id = "s"

    agent = _Agent()
    agent.root = tmp_path
    ctx = FinalizeContext(
        user_prompt="p", final_prompt="p", final_response=_Resp(), memories=[],
        executed_tools=[], archive_tool_calls=[], routed_context=None,
        resume_context_result=None, runtime_injections=[],
        compression_snapshot_id="", compression_snapshot_path="",
        compression_applied=False, request_id="r", run_id="", task_id="",
        source="run", do_save=False, task_attributes=None, recovery_task_refs=None,
        recovery_content_paths=None, recovery_next_actions=None,
        context_scope="task_local",
    )
    payload = compact_auto_cycle_fields(agent, ctx, {"turn": 10, "active": 10})
    assert "memory_compact_auto_status" in payload
