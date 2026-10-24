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
        assert state["loaded_tool_order"] == (("q", "z", "a") if state_kind == "existing" else ("z", "a"))


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


# LLM: 真实 Compact 选择默认保持 9–32 项原顺序；显式小上限仍采用原新近性规则。
# 函数用途: 防止压缩偷偷收回已经展开的原生工具定义。
@pytest.mark.parametrize("count", [9, 12, 32])
def test_tool_discovery_compact_keeps_session_capacity(count):
    from agent_py_agent.agent.conversation.tool_discovery import active_tools_for_compact

    names = tuple(f"tool_{i}" for i in range(count))
    thread = SimpleNamespace(loaded_tool_names=names, metadata={"discovered_tool_history": list(names)})
    assert active_tools_for_compact(thread) == names
    assert tool_search_state.resolve_active_loaded_tool_names(names) == names
    assert tool_search_state.resolve_active_loaded_tool_names(names, max_active_discovered=2) == names[-2:]


# LLM: 复用真实注册表与不可变快照，仅替换受测工具的 schema；不用全局 handler 作为授权兜底。
# 函数用途: 构造含必填、枚举、数组、联合类型和长描述的微 schema 样本。
def _micro_schema_snapshot(tmp_path):
    from dataclasses import replace

    from agent_py_agent.tests.test_tool_default_deferral import _agent

    agent = _agent(tmp_path)
    snapshot = agent.tools.runtime_snapshot(allowed_tools=["read_file", "manage_models", "tool_search"])
    runtime = snapshot.runtime("read_file")
    schema = {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "description": "不应重复正文" * 1000},
        "mode": {"type": "string", "enum": ["read", "peek"]},
        "items": {"type": "array", "items": {"type": "integer"}},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    }}
    changed = replace(runtime, model_spec=replace(runtime.model_spec, input_schema=schema, schema_hash=""))
    return agent, replace(snapshot, runtimes=tuple(changed if item is runtime else item for item in snapshot.runtimes),
                          preloaded_tool_names=("tool_search",), snapshot_hash="")


# LLM: 只从当前快照读取授权且可见的正式定义；输出为独立容器，不能热改 native schema。
# 函数用途: 验证紧凑类型/枚举、顺序去重、限额与权限交集。
def test_tool_discovery_micro_schemas_are_compact_pure_and_authorized(tmp_path):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.conversation import tool_discovery as module

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    build = getattr(module, "build_discovered_tool_micro_schemas", lambda *_a, **_kw: [])
    before = json.dumps(snapshot.runtime("read_file").model_spec.input_schema, sort_keys=True)
    rows = build(snapshot, ["missing", "read_file", "read_file", "manage_models"])
    assert [row["name"] for row in rows] == ["read_file", "manage_models"]
    assert rows[0] == {"name": "read_file", "required": ["path"], "properties": {
        "path": "string", "mode": {"enum": ["read", "peek"]}, "items": "array<integer>", "limit": "integer|null",
    }}
    assert len(json.dumps(rows[0], ensure_ascii=False)) < 400
    rows[0]["required"].append("not-real")
    assert json.dumps(snapshot.runtime("read_file").model_spec.input_schema, sort_keys=True) == before
    restricted = replace(snapshot, allowed_tools=frozenset({"manage_models"}))
    assert [row["name"] for row in build(restricted, ["read_file", "manage_models"])] == ["manage_models"]
    assert build(snapshot, ["read_file"], max_tools=0) == []
    assert len(build(snapshot, ["read_file", "manage_models"], max_tools=1)) == 1
    assert build(object(), ["read_file"]) == []
    assert [row["name"] for row in build(agent.tools, ["read_file"])] == ["read_file"]


# LLM: 不可用、hidden 和不在当前 run 权限中的旧历史名都不能被参数速查带回。
# 函数用途: 防止已发现工具的历史成为越权读取 schema 的旁路。
@pytest.mark.parametrize("damage", ["hidden", "unavailable"])
def test_tool_discovery_micro_schemas_do_not_expose_unusable_tools(tmp_path, damage):
    from dataclasses import replace

    from agent_py_agent.agent.conversation import tool_discovery as module
    from agent_py_agent.agent.tooling.models import ToolAvailability, ToolExposure

    _agent, snapshot = _micro_schema_snapshot(tmp_path)
    runtime = snapshot.runtime("read_file")
    values = {"exposure": ToolExposure(False)} if damage == "hidden" else {"availability": ToolAvailability(False, "TOOL_UNAVAILABLE", "test")}
    changed = replace(runtime, **values)
    snapshot = replace(snapshot, runtimes=tuple(changed if row is runtime else row for row in snapshot.runtimes), snapshot_hash="")
    build = getattr(module, "build_discovered_tool_micro_schemas", lambda *_a, **_kw: None)
    assert build(snapshot, ["read_file"]) == []


# LLM: 用真实线程和 IR 观察 post-compact 接缝；旧历史不是权限，首部 native tools 不应变化。
# 函数用途: 即使没有在用 Skill 也带回工具参数，按原 IR 去重并遵守本次剩余预算。
def test_tool_discovery_post_compact_micro_schemas_append_only_and_budgeted(tmp_path):
    from dataclasses import replace

    from agent_py_agent.agent.backends.tool_ir import RuntimeFactsTurn, UserTurn
    from agent_py_agent.agent.capability.method_carry import prepare_conversation_method_carry
    from agent_py_agent.agent.conversation.store import ConversationStore

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    agent.conversation_store = ConversationStore(tmp_path / "conversations")
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local"})
    agent.conversation_store.threads.update_atomic(thread.thread_id, lambda current: replace(
        current, compact_generation=1, loaded_tool_names=("read_file",),
        metadata={"discovered_tool_history": ["read_file", "manage_models", "denied"]}))
    params = SimpleNamespace(tool_runtime_snapshot=snapshot, allowed_tools=["read_file", "tool_search"],
        loaded_tool_names={"read_file"}, live_archive_state={}, tool_ir_history=[UserTurn("原始请求")],
        task_attributes={"conversation_thread_id": thread.thread_id}, conversation_method_carry_attempts=set())
    agent._current_run_params = params
    before = agent.tools.model_visible_specs(runtime_snapshot=snapshot, loaded_tool_names=params.loaded_tool_names)
    prepare_conversation_method_carry(agent, params, budget_cap=1)
    assert len(params.tool_ir_history) == 1
    prepare_conversation_method_carry(agent, params, budget_cap=1000)
    assert len(params.tool_ir_history) == 2
    turn = params.tool_ir_history[-1]
    assert isinstance(turn, RuntimeFactsTurn) and turn.source == "discovered_tool_micro_schemas"
    assert "[本会话已解锁工具参数速查]" in turn.text and '"required":["path"]' in turn.text
    assert "manage_models" not in turn.text and "denied" not in turn.text
    prepare_conversation_method_carry(agent, params, budget_cap=1000)
    assert len(params.tool_ir_history) == 2
    assert before == agent.tools.model_visible_specs(runtime_snapshot=snapshot, loaded_tool_names=params.loaded_tool_names)
    assert params.loaded_tool_names == {"read_file"}


# LLM: 真实补表入口保留原失败码，只增加尾部参数速查和下一轮加载事实；持久线程沿原写口更新。
# 函数用途: 盲调失败之后直接重试，无需再调用一次 tool_search，跨 run 恢复也不丢失发现名。
def test_deferred_schema_reload_attaches_micro_schema_and_persists_discovery(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    agent.conversation_store = ConversationStore(tmp_path / "conversations")
    thread = agent.conversation_store.threads.get_or_create({"canonical_user_id": "local"})
    params = SimpleNamespace(tool_runtime_snapshot=snapshot, allowed_tools=None, loaded_tool_names=set(),
        live_archive_state={}, task_attributes={"conversation_thread_id": thread.thread_id})
    record = {"tool": "manage_models", "ok": False, "error_code": "TOOL_INVALID_ARGUMENTS",
        "tool_execution": {"failure_stage": "validation", "handler_executed": False}}
    hint = reload_schema_after_blind_call(agent, params, record)
    assert hint.startswith("\n[tool-schema-loaded] manage_models ")
    assert '"loaded_micro_schema"' in hint and '"schema_auto_activated_for_next_turn":true' in hint
    assert record["schema_reload"]["loaded_micro_schema"]["name"] == "manage_models"
    assert "action" in record["schema_reload"]["loaded_micro_schema"]["properties"]
    assert record["error_code"] == "TOOL_INVALID_ARGUMENTS" and record["ok"] is False
    assert params.loaded_tool_names == {"manage_models"}
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ()
    # 与真实 _record_tool_post_archive 同序：原生结果已进入IR之后才写同一份线程发现账。
    persist_discovered_tools(agent, params, record)
    assert agent.conversation_store.threads.require(thread.thread_id).loaded_tool_names == ("manage_models",)
    assert reload_schema_after_blind_call(agent, params, record) == ""


# LLM: 运行参数允许集合可以比快照更窄；拒绝的名字不加入状态、不读取参数、不写线程。
# 函数用途: 验证补表不是绕过当前 run 权限的旁路。
def test_deferred_schema_reload_does_not_activate_tool_outside_allowed_tools(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    params = SimpleNamespace(tool_runtime_snapshot=snapshot, allowed_tools=["read_file", "tool_search"],
        loaded_tool_names=set(), live_archive_state={})
    record = {"tool": "manage_models", "ok": False, "tool_execution": {"failure_stage": "validation"}}
    assert reload_schema_after_blind_call(agent, params, record) == ""
    assert params.loaded_tool_names == set() and "schema_reload" not in record


# LLM: 这是复核发现的故障隔离反例；普通增强错误不能改变原方法恢复调用或吞掉取消。
# 函数用途: 防止一个工具参数提示故障拖掉原有的 Skill/能力包沿用。
def test_method_carry_still_runs_when_micro_schema_preparation_fails(monkeypatch):
    from agent_py_agent.agent.capability import method_carry as module

    calls = []
    # LLM: 仅向附带速查入口注入普通错误，不替换原方法恢复入口。
    # 函数用途: 提供故障隔离的确定性失败输入。
    def broken(*_a):
        raise OSError("test")
    monkeypatch.setattr(module, "_prepare_discovered_tool_carry", broken)
    monkeypatch.setattr(module, "_prepare_method_carry", lambda *args: calls.append(args))
    agent, params = object(), object()
    module.prepare_conversation_method_carry(agent, params, budget_cap=100)
    assert calls == [(agent, params, 100)]


# LLM: 原工具结果尚未归档时不允许新增的辅助线程写入阻断该结果；持久化沿原post-archive入口。
# 函数用途: 注入线程写故障，验证盲调恢复本身仍交付失败说明及下轮加载事实。
def test_deferred_schema_reload_defers_thread_write_until_original_archive_boundary(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
        reload_schema_after_blind_call,
    )
    from agent_py_agent.agent.conversation.tool_discovery import persist_discovered_tools

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    writes = []
    # LLM: 记录实际写入尝试并抛出存储故障，防止测试仅凭返回提示推断归档安全。
    # 函数用途: 让任何提前的线程写入立即暴露。
    def broken(*args):
        writes.append(args)
        raise OSError("test")
    agent.conversation_store = SimpleNamespace(threads=SimpleNamespace(update_atomic=broken))
    params = SimpleNamespace(tool_runtime_snapshot=snapshot, allowed_tools=None, loaded_tool_names=set(),
        live_archive_state={}, task_attributes={"conversation_thread_id": "thread"})
    record = {"tool": "manage_models", "ok": False, "error_code": "TOOL_INVALID_ARGUMENTS",
        "tool_execution": {"failure_stage": "validation"}}
    assert reload_schema_after_blind_call(agent, params, record)
    assert writes == [] and params.loaded_tool_names == {"manage_models"}
    assert record["error_code"] == "TOOL_INVALID_ARGUMENTS"
    with pytest.raises(OSError):
        persist_discovered_tools(agent, params, record)
    assert len(writes) == 1


# LLM: 大枚举不能把完整Schema复制进尾部；摘要缺枚举时必须显式说明，以完整原生定义为准。
# 函数用途: 验证精简摘要不会因合法长枚举失去有界性。
def test_tool_discovery_micro_schema_bounds_large_enum(tmp_path):
    import json
    from dataclasses import replace

    from agent_py_agent.agent.conversation.tool_discovery import build_discovered_tool_micro_schemas

    _agent, snapshot = _micro_schema_snapshot(tmp_path)
    runtime = snapshot.runtime("read_file")
    schema = {"type": "object", "required": ["path"], "properties": {
        "path": {"type": "string", "enum": ["big" * 1000 + str(i) for i in range(40)]},
    }}
    changed = replace(runtime, model_spec=replace(runtime.model_spec, input_schema=schema, schema_hash=""))
    snapshot = replace(snapshot, runtimes=(changed,), available_tool_names=frozenset({"read_file"}), snapshot_hash="")
    row, = build_discovered_tool_micro_schemas(snapshot, ["read_file"])
    assert len(json.dumps(row)) < 512
    assert row["properties"]["path"]["enum_omitted"] is True
    assert row["properties"]["path"]["enum_count"] == 40
    assert runtime.model_spec.input_schema["properties"]["mode"]["enum"] == ["read", "peek"]


# LLM: 装箱仅模拟摘要大小，不替换授权逻辑；大条目位于最新位置时也不能挤掉放得下的小条目。
# 函数用途: 复现贪心删头把全部参考清空的问题。
def test_tool_discovery_micro_schema_budget_skips_oversized_newest_row(tmp_path, monkeypatch):
    from agent_py_agent.agent.capability.method_carry import prepare_conversation_method_carry
    from agent_py_agent.agent.conversation import tool_discovery as module

    agent, snapshot = _micro_schema_snapshot(tmp_path)
    thread = SimpleNamespace(compact_generation=1, loaded_tool_names=("read_file", "manage_models"), metadata={})
    agent.conversation_store = SimpleNamespace(threads=SimpleNamespace(load=lambda _tid: thread))
    params = SimpleNamespace(tool_runtime_snapshot=snapshot, allowed_tools=None, tool_ir_history=[],
        task_attributes={"conversation_thread_id": "thread"}, conversation_method_carry_attempts=set())
    agent._current_run_params = params
    tiny = {"name": "read_file", "required": ["path"], "properties": {"path": "string"}}
    huge = {"name": "manage_models", "required": ["field" * 10000], "properties": {}}
    monkeypatch.setattr(module, "build_discovered_tool_micro_schemas", lambda *_a, **_kw: [huge, tiny])
    prepare_conversation_method_carry(agent, params, budget_cap=200)
    assert len(params.tool_ir_history) == 1
    assert "read_file" in params.tool_ir_history[0].text and "manage_models" not in params.tool_ir_history[0].text
