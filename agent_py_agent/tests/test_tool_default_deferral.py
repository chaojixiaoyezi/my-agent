from __future__ import annotations

"""按工具声明的默认收起（tool_default_deferral_enabled）合同单测。

又大又少用的工具在自己的 ToolModelHints 里声明 default_deferred + deferred_summary。开关打开时，前台回合不再发它们的
原生 Schema，目录末尾留“名字：一句用途”的索引，tool_search 一步找回；开关关闭时提示词与可见工具逐字不变。
显式 allowed_tools 的回合、tool_search 不可用的快照不收起；决策点的展示投影与它取并集。
"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.background_tool_policy import (
    BACKGROUND_CONTINUATION_REQUIRED_TOOLS,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.models import TOOL_DISCOVERY_ENTRY_NAMES, ToolModelHints
from agent_py_agent.agent.tooling.registry import _declared_deferred_names

# 第一阶段按近 30 天生产调用频率和风险定的名单；新增声明时同步这里和 DESIGN_LEDGER 的理由。
# audit_records 不收起：IM 用户自查“发消息报错/没回复”靠它，真实验收里收起后模型没去搜索（I6），改前对照直接调用成功。
# 第二阶段（toolfold）：按生产近 3 天 + 评测 90 次运行的“双零使用”再收 6 个管理/通道类工具；
# 既有合同“递归代理和持续目标控制属于主链、orchestration/goal 默认直出”不动（create_goal、update_goal、
# cancel_subagents 保持直出），生产在用的 terminal_session、read_artifact、session_search、process_session 等同样保持直出。
DECLARED = {
    "admin_controls", "cancel_session_task", "gateway_status",
    "manage_models", "memory_search", "package_build", "package_install", "publish_audit_update", "restart_gateway", "schedule",
    "send_message", "send_session_message", "skill_summarize", "stop_named_work", "update_persona",
    "user_config", "watch_stream",
}

# 实际生效的声明收起 = 声明集合 - 后台续跑必需（豁免）：后台回合没有用户在场，靠 tool_search
# 找回工具的窗口极窄，这些工具始终直出（3a 2026-10-05 裁定，清单来自 background_tool_policy）。
DEFERRED_EFFECTIVE = DECLARED - BACKGROUND_CONTINUATION_REQUIRED_TOOLS


def _agent(tmp_path, **overrides) -> SimpleAgent:
    cfg = AgentConfig(enable_tools=True, enable_subagents=True, memory_path=str(tmp_path / "m.jsonl"), **overrides)
    return SimpleAgent(cfg, str(tmp_path))


def _visible(agent, **kwargs) -> set[str]:
    return {spec.name for spec in agent.tools.model_visible_specs(**kwargs)}


def test_switch_defaults_off_and_keeps_surface_and_catalog_unchanged(tmp_path):
    # 出厂默认已改为开启（toolfold，2026-10-05）：关回 false 时行为必须与历史全量直出逐字一致。
    assert AgentConfig().tool_default_deferral_enabled is True
    off = _agent(tmp_path / "off", tool_default_deferral_enabled=False)
    # 部分声明工具按运行条件注册/可用（如 publish_audit_update 只在 Audit 回合、会话工具看管理员开关）；
    # 断言只看“当前快照里真实可用”的子集，开关关闭时它们必须照旧直出（watch_stream 本来就按 web 类别收起）。
    available = set(off.tools.runtime_snapshot().available_tool_names)
    assert (DECLARED - {"watch_stream"}) & available <= _visible(off)
    section = off.tools.render_catalog_section()
    assert "默认收起的工具" not in section


def test_declared_tools_leave_the_native_surface_and_get_a_compact_index(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    visible = _visible(agent)
    assert visible.isdisjoint(DEFERRED_EFFECTIVE)
    # 续跑必需工具里由声明豁免的都应直出；watch_stream 例外——它由 web 类别折叠（与声明收起无关），
    # 不可用/未注册的工具（如测试环境的 send_message）按快照可用性排除。
    available = set(agent.tools.runtime_snapshot().available_tool_names)
    assert (DECLARED & BACKGROUND_CONTINUATION_REQUIRED_TOOLS - {"watch_stream"}) & available <= visible, "后台续跑必需工具始终直出"
    assert TOOL_DISCOVERY_ENTRY_NAMES & set(agent.tools.tools) <= visible
    assert {"read_file", "write_file", "run_command", "create_subagents", "remember"} <= visible
    section = agent.tools.render_catalog_section()
    index = section.split("默认收起的工具", 1)[1]
    available = set(agent.tools.runtime_snapshot().available_tool_names)
    for name in sorted(DEFERRED_EFFECTIVE & available):
        summary = agent.tools.tools[name].model_spec.hints.deferred_summary
        assert f"\n  - {name}：{summary}" in index
    names_line = section.split("默认收起的工具", 1)[0].rsplit("⊞", 1)[1]
    assert "user_config" not in names_line, "声明收起的工具只在索引里出现一次，不重复进名字串"


def test_declared_set_is_pinned_and_every_summary_is_short(tmp_path):
    agent = _agent(tmp_path)
    declared = {name for name, tool in agent.tools.tools.items() if tool.model_spec.hints.default_deferred}
    # memory_search 默认不注册（enable_memory_search_tool 默认关）、部分会话工具按可见性条件注册；
    # 固定集合只钉“测试环境实际注册”的子集，未注册工具的标记由产品代码各自用例覆盖。
    assert declared == DECLARED & set(agent.tools.tools)
    assert not declared & TOOL_DISCOVERY_ENTRY_NAMES
    for name in declared:
        assert 0 < len(agent.tools.tools[name].model_spec.hints.deferred_summary) <= 60, name


def test_tool_search_loads_a_declared_tool_for_the_next_call(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    result = agent.tools.tools["tool_search"].execute({"query": "manage_models", "limit": 3})
    loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
    assert loaded[0] == "manage_models"
    payload = json.loads(result.output)
    assert payload["tools"][0]["input_schema"]["properties"], "搜索结果带完整参数 Schema"
    assert "manage_models" in _visible(agent, loaded_tool_names=set(loaded))


@pytest.mark.parametrize(("query", "tool"), [
    ("看一下这个直播流", "watch_stream"),
    ("帮我换个模型", "manage_models"),
    ("帮我配置一个向量模型", "manage_models"),
    ("改一下配置项", "user_config"),
    ("明天早上提醒我", "schedule"),
    ("重启网关", "restart_gateway"),
    ("gateway 状态", "gateway_status"),
    ("关闭某个用户的决策模型", "admin_controls"),
    ("把学来的方法打成能力包", "package_build"),
    ("把打好的能力包装上", "package_install"),
    ("把这次的做法总结成内部技能", "skill_summarize"),
])
def test_natural_requests_find_each_declared_tool(tmp_path, query, tool):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    hits = [spec.name for spec in agent.tools.search_deferred_specs(query, limit=5)]
    assert tool in hits


def test_explicit_allowed_tools_stay_fully_visible(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    assert _visible(agent, allowed_tools=["user_config", "read_file"]) == {"user_config", "read_file"}


def test_declared_deferral_needs_an_available_visible_tool_search(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    snapshot = agent.tools.runtime_snapshot()
    assert _declared_deferred_names(snapshot, [], enabled=True) == frozenset(
        DEFERRED_EFFECTIVE & snapshot.available_tool_names
    )
    assert _declared_deferred_names(snapshot, [], enabled=False) == frozenset()
    assert _declared_deferred_names(snapshot, ["system"], enabled=True) == frozenset(), "tool_search 被类别收起时不再额外收起"
    runtimes = tuple(runtime for runtime in snapshot.runtimes if runtime.model_spec.name != "tool_search")
    without_search = replace(snapshot, runtimes=runtimes, snapshot_hash="",
                             available_tool_names=frozenset(runtime.model_spec.name for runtime in runtimes))
    assert _declared_deferred_names(without_search, [], enabled=True) == frozenset()


def test_decision_projection_and_declared_deferral_merge(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    base = agent.tools.runtime_snapshot()
    projected = replace(base, presentation_deferred_names=frozenset({"session_search"}),
                        presentation_shortlist_names=frozenset({"read_file"}))
    visible = _visible(agent, runtime_snapshot=projected)
    assert "session_search" not in visible and visible.isdisjoint(DEFERRED_EFFECTIVE)
    searchable = {spec.name for spec in agent.tools.search_deferred_specs("session_search", runtime_snapshot=projected)}
    assert "session_search" in searchable
    section = agent.tools.render_catalog_section(runtime_snapshot=projected)
    assert "本轮未列短名单" in section or "本轮提示" in section
    assert "\n  - user_config：" in section, "短名单不裁剪声明收起的索引"


def test_hint_requires_a_summary_when_declared():
    with pytest.raises(ValueError):
        ToolModelHints(default_deferred=True)
    with pytest.raises(ValueError):
        ToolModelHints(default_deferred="yes", deferred_summary="x")
    assert ToolModelHints(default_deferred=True, deferred_summary="  查东西  ").deferred_summary == "查东西"
    assert ToolModelHints().deferred_summary == ""


def test_discovery_entry_stays_direct_even_if_it_declares_deferral(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    search_tool = agent.tools.tools["tool_search"]
    spec = search_tool.model_spec
    search_tool.model_spec = replace(spec, hints=replace(spec.hints, default_deferred=True, deferred_summary="搜索工具"))
    assert "tool_search" in _visible(agent)
    assert "tool_search" not in _declared_deferred_names(agent.tools.runtime_snapshot(), [], enabled=True)


def test_index_without_other_folded_names_has_no_dangling_name_list(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True, tool_catalog_deferred_categories=[])
    section = agent.tools.render_catalog_section()
    assert "也可用 list_tools 查看完整清单。\n  默认收起的工具" in section
    assert "完整清单：" not in section


def test_semantic_document_only_grows_for_tools_with_a_summary(tmp_path):
    from agent_py_agent.agent.tooling.models import _tool_semantic_document

    agent = _agent(tmp_path)
    assert "summary:" not in _tool_semantic_document(agent.tools.tools["read_file"].model_spec)
    schedule = agent.tools.tools["schedule"].model_spec
    assert _tool_semantic_document(schedule).endswith("summary: " + schedule.hints.deferred_summary)


def test_switch_is_normalized_from_yaml_strings():
    from agent_py_agent.agent.settings.config import normalize_agent_config

    normalized, warnings = normalize_agent_config({"tool_default_deferral_enabled": "false"})
    assert normalized["tool_default_deferral_enabled"] is False
    normalized, _ = normalize_agent_config({"tool_default_deferral_enabled": "true"})
    assert normalized["tool_default_deferral_enabled"] is True


@pytest.mark.parametrize("broken", ["unavailable", "hidden"])
def test_declared_deferral_backs_off_when_tool_search_is_unusable(tmp_path, broken):
    from agent_py_agent.agent.tooling.models import ToolAvailability, ToolExposure

    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    snapshot = agent.tools.runtime_snapshot()
    damaged = {
        "unavailable": {"availability": ToolAvailability(False, "TOOL_UNAVAILABLE", "test")},
        "hidden": {"exposure": ToolExposure(model_visible=False)},
    }[broken]
    runtimes = tuple(replace(runtime, **damaged) if runtime.model_spec.name == "tool_search" else runtime
                     for runtime in snapshot.runtimes)
    assert _declared_deferred_names(replace(snapshot, runtimes=runtimes, snapshot_hash=""), [], enabled=True) == frozenset()


def test_explicit_allowed_tools_catalog_has_no_declared_index(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    section = agent.tools.render_catalog_section(allowed_tools=["user_config", "read_file", "tool_search"])
    assert "默认收起的工具" not in section
    assert agent.tools.search_deferred_specs("user_config", allowed_tools=["user_config", "tool_search"]) == []


# 函数用途: 造一条工具归档记录（只含兜底会读的结构化字段）。
def _failed_record(tool="manage_models", stage="validation", error_code="", ok=False):
    return {"tool": tool, "ok": ok, "error_code": error_code,
            "tool_execution": {"failure_stage": stage, "handler_executed": False}}


# 函数用途: 用真实注册表和本快照跑一次盲调兜底，返回（提示, 下一次请求的临时可见名单）。
def _blind_call(agent, record=None, loaded=()):
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )

    params = SimpleNamespace(tool_runtime_snapshot=agent.tools.runtime_snapshot(), allowed_tools=None,
                             loaded_tool_names=set(loaded))
    return reload_schema_after_blind_call(agent, params, record or _failed_record()), params.loaded_tool_names


def test_blind_call_validation_failure_loads_the_schema_for_the_next_request(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    hint, loaded = _blind_call(agent)
    assert loaded == {"manage_models"}
    assert hint.startswith("\n[tool-schema-loaded] manage_models ")
    assert "manage_models" in _visible(agent, loaded_tool_names=loaded)


@pytest.mark.parametrize("case", [
    (False, "manage_models", False, "validation"),
    (False, "web_fetch", False, "validation"),
    (True, "manage_models", True, "validation"),
    (True, "manage_models", False, "execution"),
    (True, "audit_records", False, "validation"),
    (True, "read_file", False, "validation"),
    (True, "no_such_tool", False, "validation"),
])
def test_blind_call_reload_only_for_declared_deferral_validation_failures(tmp_path, case):
    deferral, tool, ok, stage = case
    agent = _agent(tmp_path, tool_default_deferral_enabled=deferral)
    assert _blind_call(agent, _failed_record(tool, stage, ok=ok)) == ("", set())


def test_blind_call_already_loaded_is_left_alone(tmp_path):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    assert _blind_call(agent, loaded={"manage_models"}) == ("", {"manage_models"})


@pytest.mark.parametrize(("stage", "error_code", "reloads"), [
    ("execution", "TOOL_INVALID_ARGUMENTS", True),
    ("execution", "TOOL_PARAMETER_REQUIRED", True),
    ("execution", "TOOL_TIMEOUT", False),
    ("execution", "NO_SUCH_ERROR_CODE", False),
])
def test_handler_side_argument_errors_also_reload_the_schema(tmp_path, stage, error_code, reloads):
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    hint, loaded = _blind_call(agent, _failed_record("user_config", stage, error_code))
    assert (loaded == {"user_config"}) is reloads
    assert bool(hint) is reloads


def test_continuation_required_tools_stay_direct_and_other_declared_still_fold(tmp_path):
    """后台续跑必需工具始终直出；不在续跑目录里的声明工具照常收起。"""
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    visible = _visible(agent)
    assert {"update_persona", "remember", "skill_search", "run_command"} <= visible, "续跑必需工具直出"
    assert {"user_config", "schedule", "manage_models"}.isdisjoint(visible), "非续跑声明工具仍收起"


# 本批（toolfold）新收起的工具：每个都要能被 tool_search 一步找回（各自一条断言）。
# 未注册或当前运行条件下不可用的（memory_search 默认不注册、publish_audit_update 只在 Audit 准备回合、
# 会话类工具看管理员开关）按快照可用性跳过；至少要求命中一部分，防止整个用例被条件静默清空。
def test_tool_search_finds_each_newly_declared_tool(tmp_path):
    new_names = {
        "cancel_session_task", "memory_search", "publish_audit_update",
        "send_message", "send_session_message", "stop_named_work",
    }
    agent = _agent(tmp_path, tool_default_deferral_enabled=True)
    available = set(agent.tools.runtime_snapshot().available_tool_names)
    # cancel_session_task、send_message、send_session_message 进入后台续跑目录（豁免直出），不参与找回。
    effective = new_names - BACKGROUND_CONTINUATION_REQUIRED_TOOLS
    registered = sorted(effective & available)
    assert registered, f"本批新工具在测试环境应至少可用一个：{sorted(new_names & set(agent.tools.tools))}"
    for name in registered:
        result = agent.tools.tools["tool_search"].execute({"query": name, "limit": 3})
        loaded = result.result_envelope["tool_search"]["loaded_tool_names"]
        assert name in loaded, f"{name} 应能被 tool_search 一步找回"
        assert name in _visible(agent, loaded_tool_names=set(loaded))
