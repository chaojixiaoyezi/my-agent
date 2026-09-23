"""原工具快照的宿主展示投影：真实 schema 减量、搜索可达与原权限/插件绑定保持。"""
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from agent_py_agent.agent.backends.tool_schema import tool_model_specs_to_anthropic_tools
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    ToolAvailability,
    ToolExposure,
    ToolHandlerOutcome,
)
from agent_py_agent.agent.tooling.registry import (
    ToolRegistry,
    ToolRegistryParams,
    _render_deferred_notice,
)
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)


# LLM: 测试工具提供完整原生Schema和可观测调用次数，不实现第二条工具执行或发现链。
# 类用途: 验证展示缩小后原参数、可用性检查与handler绑定仍来自同一快照。
class OptionalTool(BaseTool):
    runtime_policy = make_test_runtime_policy("read_only")

    # LLM: 所有名称和schema只用于临时测试；生产投影不按这些名称或正文作选择。
    # 函数用途: 构造能在原Registry检索的明确类别工具。
    def __init__(self, name, *, category="plugins", ready=True):
        self.ready, self.probes, self.calls = ready, 0, 0
        self.model_spec = make_test_model_spec(name, category=category, description="按结构化参数读取测试内容。" * 50,
            keywords=(name,), input_schema={"type": "object", "properties": {
                "text": {"type": "string", "description": "原完整参数说明，不得用短名单替换。" * 50}},
                "required": ["text"], "additionalProperties": False})

    # LLM: 就绪检查只报告测试状态；展示投影不能额外调用此方法或热改状态。
    # 函数用途: 记录原快照冻结发生的检查次数。
    def availability(self):
        self.probes += 1
        return ToolAvailability.ready() if self.ready else ToolAvailability.unavailable("尚未就绪")

    # LLM: 调用只计数并回显测试参数，仍须通过原ToolExecutor校验与权限门。
    # 函数用途: 证明隐藏/再次发现没有替换原handler。
    def execute(self, params):
        self.calls += 1
        return ToolHandlerOutcome(self.model_spec.name, True, params["text"])


@pytest.fixture
def prepared(tmp_path):
    registry = ToolRegistry(ToolRegistryParams(workspace_root=tmp_path, max_chars=10000, max_entries=100,
        max_matches=100, web_max_chars=10000, http_timeout=1, catalog_limit=100, retrieval_limit=10,
        vector_search_enabled=False, catalog_deferred_categories=["presentation_legacy"], operation_store_required=False))
    a, b = OptionalTool("presentation_optional_a"), OptionalTool("presentation_optional_b")
    legacy = OptionalTool("presentation_legacy", category="presentation_legacy")
    unavailable = OptionalTool("presentation_unavailable", ready=False)
    for tool in (a, b, legacy, unavailable):
        registry.register(tool)
    snapshot = registry.runtime_snapshot(run_id="presentation-run")
    try:
        yield registry, snapshot, a, b, legacy
    finally:
        registry.close_mcp_clients()


# LLM: 使用真实Provider工具转换器，比较实际发送schema而非工具名数量或模型口头结论。
# 函数用途: 生成可做逐字对照和体积比较的原生工具JSON。
def native_schema(registry, snapshot, **kwargs):
    return json.dumps(tool_model_specs_to_anthropic_tools(registry.model_visible_specs(runtime_snapshot=snapshot, **kwargs)),
                      ensure_ascii=False, sort_keys=True)


# LLM: 沿原Registry→ToolExecutor→execute_scoped执行搜索，不调用旁路检索器或手工补loaded事实。
# 函数用途: 取得真实搜索工具的报告与typed loaded名称。
def search(registry, snapshot, query):
    call = canonical_test_call(snapshot, "tool_search", {"query": query, "limit": 5})
    result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result
    assert result.ok, result
    return json.loads(result.output), result.metadata["handler_details"]["tool_search"]["loaded_tool_names"]


def test_none_preserves_native_schema_catalog_and_original_deferred_notice_bytes(prepared):
    registry, snapshot, _a, _b, legacy = prepared
    original = native_schema(registry, snapshot), registry.render_catalog_section(runtime_snapshot=snapshot)
    explicit_none = replace(snapshot, presentation_deferred_names=None, presentation_shortlist_names=None)
    assert (native_schema(registry, explicit_none), registry.render_catalog_section(runtime_snapshot=explicit_none)) == original
    assert _render_deferred_notice([legacy.model_spec]) == (
        "- ⊞ 另有 1 个工具已注册但未直接展开；需要时先用 tool_search 搜索并加载，"
        "也可用 list_tools 查看完整清单：presentation_legacy")


def test_progressive_projection_removes_actual_native_schema_and_search_restores_exact_contract(prepared):
    registry, snapshot, a, b, legacy = prepared
    projected = replace(snapshot, presentation_deferred_names=frozenset({b.model_spec.name}),
                        presentation_shortlist_names=frozenset({a.model_spec.name}))
    full, reduced = native_schema(registry, snapshot), native_schema(registry, projected)
    assert len(full) - len(reduced) > 1000
    assert b.model_spec.name not in reduced and a.model_spec.name in reduced and legacy.model_spec.name not in reduced
    assert all(name in reduced for name in ("tool_search", "list_tools"))
    payload, loaded = search(registry, projected, b.model_spec.name)
    result = next(row for row in payload["tools"] if row["name"] == b.model_spec.name)
    assert result["input_schema"] == b.model_spec.input_schema and result["schema_hash"] == b.model_spec.schema_hash
    restored = registry.model_visible_specs(runtime_snapshot=projected, loaded_tool_names=set(loaded))
    assert next(spec for spec in restored if spec.name == b.model_spec.name) is b.model_spec
    assert native_schema(registry, projected, loaded_tool_names=set(loaded)) == full
    assert projected.runtimes is snapshot.runtimes and projected.available_tool_names is snapshot.available_tool_names
    assert projected.snapshot_hash == snapshot.snapshot_hash and projected.runtime(b.model_spec.name).handler is b
    assert a.probes == b.probes == legacy.probes == 1


def test_metadata_only_shortens_name_cards_without_removing_direct_schema_or_loading_deferred(prepared):
    registry, snapshot, a, b, legacy = prepared
    projected = replace(snapshot, presentation_shortlist_names=frozenset({a.model_spec.name, legacy.model_spec.name}))
    assert native_schema(registry, projected) == native_schema(registry, snapshot)
    assert legacy.model_spec.name not in native_schema(registry, projected)
    query = f"{a.model_spec.name} {b.model_spec.name} {legacy.model_spec.name}"
    original = registry.render_recommended_tools_section(query, runtime_snapshot=snapshot)
    selected = registry.render_recommended_tools_section(query, runtime_snapshot=projected)
    assert b.model_spec.name in original and b.model_spec.name not in selected
    assert a.model_spec.name in selected and legacy.model_spec.name not in selected
    payload, loaded = search(registry, projected, legacy.model_spec.name)
    assert legacy.model_spec.name in loaded and payload["tools"]
    assert not projected.presentation_deferred_names


def test_shortlist_omission_keeps_complete_deferred_search_and_manifest(prepared):
    registry, snapshot, _a, b, legacy = prepared
    projected = replace(snapshot, presentation_deferred_names=frozenset({b.model_spec.name}),
                        presentation_shortlist_names=frozenset())
    notice = registry.render_catalog_section(runtime_snapshot=projected)
    assert b.model_spec.name not in notice and legacy.model_spec.name not in notice
    assert "另有 2 个工具" in notice and "tool_search" in notice and "list_tools" in notice
    assert "没有授权" not in registry.render_recommended_tools_section("测试", runtime_snapshot=projected)
    for tool in (b, legacy):
        _payload, loaded = search(registry, projected, tool.model_spec.name)
        assert tool.model_spec.name in loaded
    call = canonical_test_call(projected, "list_tools", {})
    result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=projected).result
    assert result.ok
    manifest = json.loads(result.output)
    assert {b.model_spec.name, legacy.model_spec.name}.issubset({row["name"] for row in manifest["tools"]})


def test_real_loaded_schema_and_handler_survive_adaptive_hiding(prepared):
    registry, snapshot, _a, b, _legacy = prepared
    projected = replace(snapshot, presentation_deferred_names=frozenset({b.model_spec.name}),
                        presentation_shortlist_names=frozenset())
    _payload, loaded = search(registry, projected, b.model_spec.name)
    assert b.model_spec.name in native_schema(registry, projected, loaded_tool_names=set(loaded))
    call = canonical_test_call(projected, b.model_spec.name, {"text": "使用原handler"})
    result = registry.execute_tool(call, write_boundary=None, runtime_snapshot=projected).result
    assert result.ok and result.output == "使用原handler" and b.calls == 1
    assert projected.allowed_tools == snapshot.allowed_tools


@pytest.mark.parametrize("scope_source", ["argument", "snapshot"])
def test_explicit_allowed_tools_remain_fully_visible_and_search_cannot_expand_grant(prepared, scope_source):
    registry, snapshot, a, b, legacy = prepared
    names = ["tool_search", a.model_spec.name, legacy.model_spec.name]
    base = registry.runtime_snapshot(allowed_tools=names) if scope_source == "snapshot" else snapshot
    projected = replace(base, presentation_deferred_names=frozenset({a.model_spec.name}), presentation_shortlist_names=frozenset())
    args = {"allowed_tools": names} if scope_source == "argument" else {}
    assert {spec.name for spec in registry.model_visible_specs(runtime_snapshot=projected, **args)} == set(names)
    assert not registry.search_deferred_specs(b.model_spec.name, runtime_snapshot=projected, **args)
    assert b.model_spec.name not in {spec.name for spec in registry.specs(runtime_snapshot=projected, **args)}


@pytest.mark.parametrize("field", ["presentation_deferred_names", "presentation_shortlist_names"])
@pytest.mark.parametrize("names", [{"presentation_optional_b"}, ["presentation_optional_b"],
                                     frozenset({"presentation_unavailable"}), frozenset({"outside_snapshot"}), frozenset({1})])
def test_projection_rejects_mutable_or_non_authorized_names(prepared, field, names):
    _registry, snapshot, *_ = prepared
    with pytest.raises(ValueError, match="immutable subset"):
        replace(snapshot, **{field: names})


@pytest.mark.parametrize("name", ["tool_search", "list_tools", "skill_search"])
def test_original_discovery_entries_cannot_be_adaptively_deferred(prepared, name):
    registry, snapshot, *_ = prepared
    if name not in snapshot.available_tool_names:
        registry.register(OptionalTool(name, category="system"))
        snapshot = registry.runtime_snapshot()
    with pytest.raises(ValueError, match="discovery"):
        replace(snapshot, presentation_deferred_names=frozenset({name}))


def test_extra_deferral_requires_original_available_model_visible_search(prepared):
    registry, snapshot, a, *_ = prepared
    missing = registry.runtime_snapshot(allowed_tools=[a.model_spec.name])
    with pytest.raises(ValueError, match="available tool_search"):
        replace(missing, presentation_deferred_names=frozenset({a.model_spec.name}))
    hidden = tuple(replace(runtime, exposure=ToolExposure(False)) if runtime.model_spec.name == "tool_search" else runtime
                   for runtime in snapshot.runtimes)
    with pytest.raises(ValueError, match="available tool_search"):
        replace(snapshot, runtimes=hidden, snapshot_hash="", presentation_deferred_names=frozenset({a.model_spec.name}))


def test_existing_category_hidden_search_abandons_extra_hiding(prepared):
    registry, snapshot, a, *_ = prepared
    registry.catalog_deferred_categories = ["system"]
    projected = replace(snapshot, presentation_deferred_names=frozenset({a.model_spec.name}))
    assert native_schema(registry, projected) == native_schema(registry, snapshot)
    assert a.model_spec.name in native_schema(registry, projected)


def test_projection_is_immutable_and_does_not_rewrite_registry_or_new_snapshot(prepared):
    registry, snapshot, _a, b, _legacy = prepared
    projected = replace(snapshot, presentation_deferred_names=frozenset({b.model_spec.name}))
    with pytest.raises(FrozenInstanceError):
        projected.presentation_deferred_names = frozenset()
    assert b.model_spec.name in native_schema(registry, snapshot)
    assert registry.runtime_snapshot().presentation_deferred_names is None
    assert registry.tools[b.model_spec.name] is b and projected.snapshot_hash == snapshot.snapshot_hash


def test_real_plugin_projection_keeps_fixed_proxy_and_rejects_revoked_activation(tmp_path):
    from agent_py_agent.agent.plugin_runtime import PluginProxyTool, plugin_tool_name
    from agent_py_agent.tests.plugin_activation_fixtures import (
        installed_runtime_plugin,
        invoke_registered_tool,
        plugin_registry,
    )

    service = installed_runtime_plugin(tmp_path)
    enabled = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert enabled["state"] == "succeeded", enabled
    registry = plugin_registry(service, catalog_deferred_categories=[])
    try:
        registry.prepare_for_run()
        name = plugin_tool_name("sample-peek", "read")
        original = registry.runtime_snapshot()
        projected = replace(original, presentation_deferred_names=frozenset({name}))
        proxy = projected.runtime(name).handler
        assert isinstance(proxy, PluginProxyTool) and proxy is original.runtime(name).handler
        assert projected.snapshot_hash == original.snapshot_hash
        assert name not in native_schema(registry, projected)
        assert registry.search_deferred_specs(name, runtime_snapshot=projected)[0] is proxy.model_spec
        disabled = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert disabled["state"] == "succeeded", disabled
        assert not proxy.availability().available
        assert projected.runtime(name).handler is proxy
        result = invoke_registered_tool(service, registry, name, {"path": "unchanged"}, snapshot=projected)
        assert result["state"] != "succeeded", result
        assert name not in registry.runtime_snapshot().available_tool_names
    finally:
        registry.close_mcp_clients()
