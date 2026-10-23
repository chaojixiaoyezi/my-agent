# LLM: 只测同一归档信封的保序投影与真实调用边界，不调用模型、不扩大授权、不从正文恢复工具。
# 模块用途: 覆盖C1b的盲调补表、Compact、Gateway及子代理工具顺序接线。
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import tool_search_state


# LLM: 仅构造原有typed搜索信封，轮号按调用者显式提供，不猜历史或权限。
# 函数用途: 为跨轮与旧尾项规则生成最小归档输入。
def _search(names, **extra):
    return {"tool_result_envelope": {"tool_search": {"loaded_tool_names": names}}, **extra}


# LLM: 新有序接口必须保留原信任和旧set接口；数字、正文、错误信封不能变成工具名。
# 函数用途: 验证首见去重、跨轮累计、旧记录只取尾项以及坏数据拒绝。
@pytest.mark.parametrize(("records", "expected"), [
    ([_search([" z ", "a", "z", "", None, 7, False])], ("z", "a")),
    ([_search(["z"], tool_round=3), _search(["a"], tool_round=1), _search(["z", "b"], tool_round=3)], ("z", "a", "b")),
    ([_search(["old"]), _search(["z", "a"], tool_round=2), _search(["ignored"])], ("z", "a")),
    ([_search(["old"]), _search(["z", "a"])], ("z", "a")),
    ([_search(["old"]), {"output": "loaded_tool_names z a"}], ()),
    ([_search(("z", "a"))], ()),
    ([{"tool_result_envelope": "tool_search z a"}], ()),
    ([{"tool_result_envelope": {"credential": {}, "tool_search": {"loaded_tool_names": ["z", "web_fetch", "a"]}}}], ("web_fetch", "z", "a")),
    ([{"tool_result_envelope": {"credential_discovery": {}}}], ("web_fetch",)),
    ([{"tool_result_envelope": {"credential": "web_fetch"}}], ()),
    ([], ()),
])
def test_carried_tool_names_keep_first_discovery_order_and_original_membership(records, expected):
    ordered = getattr(tool_search_state, "ordered_carried_loaded_tool_names", tool_search_state.pending_carried_loaded_tool_names)
    assert ordered(records) == expected
    legacy = tool_search_state.pending_carried_loaded_tool_names(records)
    assert type(legacy) is set and legacy == set(expected)


# LLM: 原资格判断全部保留；list与set只改变容器写法，重试已加载工具不得重载或重复记录顺序。
# 函数用途: 复现list盲调崩溃，并验证有效加载时按首见顺序追加独立状态。
@pytest.mark.parametrize("container", [list, set])
@pytest.mark.parametrize("state_kind", ["empty", "existing", "absent"])
def test_blind_call_reload_accepts_list_and_set_and_tracks_order(container, state_kind):
    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )
    from agent_py_agent.agent.tooling.models import ToolFailureStage

    state = None if state_kind == "absent" else ({"loaded_tool_order": ["q", "z"]} if state_kind == "existing" else {})
    params = SimpleNamespace(loaded_tool_names=container(), live_archive_state=state, allowed_tools=None,
                             tool_runtime_snapshot=SimpleNamespace(runtime=lambda _name: object()))
    agent = SimpleNamespace(tools=SimpleNamespace(catalog_default_deferral_enabled=True, model_visible_specs=lambda **_kw: []))
    notices = []
    for name in ("z", "a", "z"):
        record = {"tool": name, "ok": False, "tool_execution": {"failure_stage": ToolFailureStage.VALIDATION.value}}
        try:
            notices.append(reload_schema_after_blind_call(agent, params, record))
        except AttributeError as error:
            pytest.fail(f"盲调加载应兼容list而不是调用list.add: {error}")
    assert notices[0] and notices[1] and notices[2] == ""
    assert params.loaded_tool_names == (["z", "a"] if container is list else {"z", "a"})
    assert params.allowed_tools is None
    if state is not None:
        assert state["loaded_tool_order"] == (["q", "z", "a"] if state_kind == "existing" else ["z", "a"])


# LLM: 线程身份判定沿原规则，线程已有顺序优先；同线程新发现后追加，跨线程面不能带入。
# 函数用途: 验证Compact绑定只做保序合并，不排序或借用其它线程工具。
@pytest.mark.parametrize(("surface_thread", "expected"), [
    ("thread", ("z", "a", "b", "c")), ("", ("z", "a", "b", "c")), ("foreign", ("z", "a")),
])
def test_compact_bound_tools_prioritize_thread_order(surface_thread, expected):
    from agent_py_agent.agent.conversation.compact import _bound_loaded_tools

    surface = SimpleNamespace(thread_id=surface_thread, loaded_tool_names=("b", "a", "c"))
    thread = SimpleNamespace(thread_id="thread", loaded_tool_names=("z", "a"))
    assert _bound_loaded_tools(surface, thread) == expected


# LLM: 在resolver接收参数的真实边界取证；其余准备依赖被固定，不冒称完整供应商工具表或缓存命中验证。
# 函数用途: 验证Compact provider没有在交给唯一工具resolver前先转set丢序。
def test_compact_provider_passes_ordered_names_to_native_resolver(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import loop_support
    from agent_py_agent.agent.conversation import compact_provider_surface as module
    from agent_py_agent.agent.prompting_parts.cache_layout import CacheStructuredPrompt

    snapshot = object()
    presentation = SimpleNamespace(tool_snapshot=snapshot, selected_skill_ids=(), required_skill_ids=(), selection=None)
    monkeypatch.setattr(loop_support, "_tool_snapshots_for_run", lambda *_a: (snapshot, object()))
    monkeypatch.setattr(loop_support, "_resolve_tool_sections", lambda *_a: ("", ""))
    monkeypatch.setattr(module, "_compact_capability_presentation", lambda *_a: presentation)
    monkeypatch.setattr(module, "_compact_parent_prompt", lambda *_a: CacheStructuredPrompt("fixed", "volatile"))
    monkeypatch.setattr(module, "self_map_available", lambda *_a: False)
    received = []
    # LLM: 捕获原调用参数但不替换受测prepare入口，不调用任何外部供应商。
    # 函数用途: 记录真正交给native resolver的工具顺序。
    def capture(_agent, params):
        received.append(params.loaded_tool_names)
        return []
    monkeypatch.setattr(module, "resolve_native_tools", capture)
    agent = SimpleNamespace(config=SimpleNamespace(enable_tools=False), tools=object(), prompts=object())
    surface = module.ConversationCompactModelSurface(loaded_tool_names=("z", " a ", "z", "", "b"))
    module.prepare_conversation_compact_provider_surface(agent, surface, run_id="compact-test")
    assert received == [("z", "a", "b")]


# LLM: 旧set入口调用即失败，避免偶然哈希迭代顺序使丢序测试假绿；身份解析和system prompt为固定测试边界。
# 函数用途: 验证Gateway与子代理从归档直达有序surface，不在上游先走set。
def test_gateway_and_pending_subagent_compact_preserve_order_without_set_hop(monkeypatch):
    from agent_py_agent.agent import gateway_compact_context as gateway
    from agent_py_agent.agent.agent_core.runner import prompts
    from agent_py_agent.agent.agent_core.runtime.run_params import RunParams
    from agent_py_agent.agent.agent_core.subagent import run_flow

    # LLM: 受测保序路径禁止调用旧集合API，但不删除旧API或改变它对其它调用者的合同。
    # 函数用途: 把隐式丢序的上游set转换变成可复现失败。
    def forbidden(*_args):
        pytest.fail("保序恢复不能先调用旧set还原器")
    monkeypatch.setattr(tool_search_state, "pending_carried_loaded_tool_names", forbidden)
    monkeypatch.setattr(gateway, "pending_carried_loaded_tool_names", forbidden, raising=False)
    monkeypatch.setattr(gateway.request_binding, "gateway_runtime_authority", lambda *_a: {"run_id": "run"})
    monkeypatch.setattr(prompts, "subagent_runner_system_prompt", lambda _c: "system")
    records = [_search(["z"], tool_round=0), _search(["a", "z", "b"], tool_round=1)]
    context = SimpleNamespace(request=object(), request_id="request", agent=object(), on_chunk=None, allowed_tools=["z"])
    request = gateway.build_gateway_compact_load_request(context, "hello", RunParams(), records)
    assert request.model_surface.loaded_tool_names == ("z", "a", "b")
    assert run_flow._pending_subagent_compact_model_surface(context, records).loaded_tool_names == ("z", "a", "b")
    assert context.allowed_tools == ["z"]


# LLM: 直接surface输入的清洗只去首尾空白和重复，不按字母重排，不改变调用者allowed_tools。
# 函数用途: 验证子代理顺序入口本身也保序，覆盖非归档来源。
def test_subagent_compact_surface_keeps_input_order(monkeypatch):
    from agent_py_agent.agent.agent_core.runner import prompts
    from agent_py_agent.agent.agent_core.subagent.run_flow import _subagent_compact_model_surface

    monkeypatch.setattr(prompts, "subagent_runner_system_prompt", lambda _c: "system")
    context = SimpleNamespace(allowed_tools=["z"])
    surface = _subagent_compact_model_surface(context, loaded_tool_names=("z", " a ", "z", "", "b"))
    assert surface.loaded_tool_names == ("z", "a", "b") and surface.allowed_tools == ("z",)
