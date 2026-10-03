from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_activity import _MAIN_ACTIVITY_LIVE_PHASES
from agent_py_agent.agent.plugin_channel import ChannelCall, PluginChannelPool
from agent_py_agent.agent.plugin_display import service as display_service
from agent_py_agent.agent.plugin_display.protocol import (
    DISPLAY_EXTENSION,
    MAX_LINE_CHARS,
    MAX_TEXT_LINE_COUNT,
    PanelDeclaration,
    normalize_display,
    validate_panels,
)
from agent_py_agent.agent.plugin_display.service import (
    ERROR_BACKOFF_SECONDS,
    IDLE_CLOSE_SECONDS,
    DisplayWiring,
    PanelQuery,
    PluginDisplayService,
    project_topics,
)
from agent_py_agent.agent.plugin_manifest import (
    PLUGIN_PACKAGE_SCHEMA,
    PLUGIN_PACKAGE_SCHEMA_V2,
    PluginManifest,
    PluginPackageError,
)

# ── 协议层 ──────────────────────────────────────────────────────────────


def test_panel_declaration_rejects_unknown_kind_topic_and_bad_id():
    PanelDeclaration("activity", "当前活动", "text", ("activity",))
    for args in (("Bad Id", "t", "text", ("activity",)), ("ok", "t", "chart", ("activity",)),
                 ("ok", "t", "text", ("secrets",)), ("ok", "t", "text", ()), ("ok", "", "text", ("activity",)),
                 ("ok", "t\x1b[2J", "text", ("activity",))):
        with pytest.raises(ValueError):
            PanelDeclaration(*args)


def test_validate_panels_limits_count_and_duplicate_ids():
    panel = PanelDeclaration("a", "A", "text", ("activity",))
    with pytest.raises(ValueError):
        validate_panels((panel, panel))
    with pytest.raises(ValueError):
        validate_panels(tuple(PanelDeclaration(f"p{i}", "P", "text", ("activity",)) for i in range(3)))


def test_normalize_display_truncates_and_strips_control_characters():
    lines = ["\x1b[31mred\x07"] + ["x" * (MAX_LINE_CHARS + 5)] + ["y"] * (MAX_TEXT_LINE_COUNT + 3)
    out = normalize_display("text", {"lines": lines})
    assert out["lines"][0] == "[31mred"
    assert len(out["lines"]) == MAX_TEXT_LINE_COUNT and len(out["lines"][1]) == MAX_LINE_CHARS
    assert out["truncated"] is True
    table = normalize_display("table", {"columns": ["a", "b"], "rows": [["1"], ["1", "2", "3"]]})
    assert table["rows"] == [["1", ""], ["1", "2"]] and table["truncated"] is True
    status = normalize_display("status", {"fields": [{"label": "状态", "value": True}]})
    assert status["fields"] == [{"label": "状态", "value": "是"}]
    for kind, bad in (("text", {"lines": "x"}), ("table", {"columns": [], "rows": []}),
                      ("status", {"fields": ["x"]}), ("text", {"lines": [{"x": 1}]}), ("text", [])):
        with pytest.raises(ValueError):
            normalize_display(kind, bad)


def test_project_topics_uses_exact_host_phases_and_public_fields_only():
    activity = {"active_task_count": 1, "compact_count": 2, "subagents": [{}, {}],
                "main_activity": {"phase": "tool", "activity": "运行命令", "started_at": 1.0, "updated_at": 2.0,
                                  "task_id": "secret-task", "projection_id": "p"}}
    both = project_topics(activity, ("activity", "run_state"))
    assert both["run_state"] == {"state": "working", "active_task_count": 1, "subagent_count": 2}
    assert "task_id" not in json.dumps(both) and "projection_id" not in json.dumps(both)
    waiting = project_topics({"main_activity": {"phase": "waiting_permission"}}, ("run_state",))
    assert waiting["run_state"]["state"] == "waiting"
    # 子串相近但不在宿主阶段白名单里的值不能被当成工作中
    assert project_topics({"main_activity": {"phase": "tooling_done"}}, ("run_state",))["run_state"]["state"] == "idle"
    assert project_topics({}, ("run_state",))["run_state"]["state"] == "idle"
    assert "waiting_permission" in _MAIN_ACTIVITY_LIVE_PHASES


# ── 包描述 ──────────────────────────────────────────────────────────────


def _manifest_payload(**overrides) -> dict:
    payload = {
        "schema_version": PLUGIN_PACKAGE_SCHEMA, "plugin_id": "demo", "version": "0.1.0", "summary": "演示",
        "entry_module": "demo", "entry_wheel": "wheels/demo-0.1.0-py3-none-any.whl",
        "wheels": [{"path": "wheels/demo-0.1.0-py3-none-any.whl", "sha256": "0" * 64}],
        "actions": [{"name": "run", "summary": "运行", "arguments": [], "kind": "tool", "target": "run",
                     "available": True}],
        "default_action": "run",
        "tools": [{"name": "run", "description": "运行", "input_schema": {"type": "object", "properties": {}},
                   "requested_effect": "read_only"}],
        "settings_schema": {"type": "object", "properties": {}},
    }
    payload.update(overrides)
    return payload


def test_manifest_without_panels_keeps_v1_bytes():
    payload = _manifest_payload()
    manifest = PluginManifest.from_payload(payload)
    out = manifest.to_payload()
    assert out["schema_version"] == PLUGIN_PACKAGE_SCHEMA and "panels" not in out
    assert PluginManifest.from_payload(out) == manifest


def test_display_only_manifest_roundtrips_as_v2():
    payload = _manifest_payload(
        schema_version=PLUGIN_PACKAGE_SCHEMA_V2, tools=[], default_action="show",
        actions=[{"name": "show", "summary": "显示面板", "arguments": [], "kind": "display", "target": "line",
                  "available": True}],
        panels=[{"id": "line", "title": "活动", "kind": "text", "topics": ["activity"]}],
    )
    manifest = PluginManifest.from_payload(payload)
    assert manifest.panels[0].id == "line" and manifest.tools == ()
    out = manifest.to_payload()
    assert out["schema_version"] == PLUGIN_PACKAGE_SCHEMA_V2
    assert PluginManifest.from_payload(out) == manifest


@pytest.mark.parametrize("change", [
    # v1 不能带 panels；v2 必须带 panels；展示动作必须指向本包面板；没有工具也没有面板不合法
    {"panels": [{"id": "line", "title": "活动", "kind": "text", "topics": ["activity"]}]},
    {"schema_version": PLUGIN_PACKAGE_SCHEMA_V2, "panels": []},
    {"schema_version": PLUGIN_PACKAGE_SCHEMA_V2,
     "panels": [{"id": "line", "title": "活动", "kind": "text", "topics": ["activity"]}],
     "actions": [{"name": "show", "summary": "显示", "arguments": [], "kind": "display", "target": "other",
                  "available": True}], "default_action": "show"},
    {"tools": [], "actions": [], "default_action": ""},
])
def test_manifest_rejects_inconsistent_panels(change):
    with pytest.raises(PluginPackageError):
        PluginManifest.from_payload(_manifest_payload(**change))


# ── 展示服务 ────────────────────────────────────────────────────────────


class _ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run_all(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **_kwargs):
        self.jobs.clear()


class _FakeTransport:
    def __init__(self, plugin):
        self.plugin = plugin

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.plugin.calls.append((method, params))
        if self.plugin.fail:
            raise self.plugin.fail
        return {"display": {"lines": [f"state={params['topics']['run_state']['state']}"]}}


class _FakePlugin:
    def __init__(self, installation, *, capable=True):
        self.installation = installation
        self.calls = []
        self.stopped = 0
        self.fail = None
        self.revoked = False
        self.capabilities = {"experimental": {DISPLAY_EXTENSION: {"versions": ["1"]}}} if capable else {}
        self.activation_ref = SimpleNamespace(require=self._require)

    def _require(self):
        if self.revoked:
            raise ValueError("revoked")
        return self.installation

    def start(self):
        return _FakeTransport(self)

    def stop(self):
        self.stopped += 1
        return SimpleNamespace(confirmed=True)


def _installation(activation_id="act-1"):
    panel = PanelDeclaration("line", "活动", "text", ("run_state",))
    return SimpleNamespace(manifest=SimpleNamespace(plugin_id="pet", panels=(panel,)),
                           activation=SimpleNamespace(activation_id=activation_id), enabled=True)


class _Harness:
    def __init__(self, *, capable=True):
        self.now = 100.0
        self.rows = [_installation()]
        self.plugins = []
        self.executor = _ManualExecutor()
        self.capable = capable
        self.service = PluginDisplayService(wiring=DisplayWiring(
            installations=lambda _owner: tuple(self.rows), client_factory=self._factory,
            clock=lambda: self.now, executor=self.executor))

    def _factory(self, _owner, installation):
        plugin = _FakePlugin(installation, capable=self.capable)
        self.plugins.append(plugin)
        return plugin

    def query(self, phase="tool"):
        activity = {"active_task_count": 1, "main_activity": {"phase": phase}} if phase else {}
        return self.service.panels(PanelQuery(object(), "owner", "thread:t1", activity, (("pet", "line"),)))


def test_first_query_loads_then_returns_rendered_panel_without_rerendering_same_input():
    h = _Harness()
    assert h.query()[0]["state"] == "loading"
    h.executor.run_all()
    first = h.query()[0]
    assert first["state"] == "ready" and first["display"]["lines"] == ["state=working"]
    assert h.executor.jobs == [] and len(h.plugins[0].calls) == 1


def test_changed_input_rerenders_and_intermediate_inputs_are_coalesced():
    h = _Harness()
    h.query("tool")
    h.executor.run_all()
    h.query("waiting_permission")
    h.query(None)  # 在途前又变化一次：只保留最新一份输入
    assert h.query(None)[0]["state"] == "refreshing"
    h.executor.run_all()
    assert h.query(None)[0]["display"]["lines"] == ["state=idle"]
    assert len(h.plugins[0].calls) == 2


def test_revocation_discards_results_and_stops_connection():
    h = _Harness()
    h.query()
    h.executor.run_all()
    h.plugins[0].revoked = True
    h.query("waiting_permission")
    h.executor.run_all()
    assert h.plugins[0].stopped == 1
    h.rows = []
    assert h.query()[0]["state"] == "unavailable"


def test_disabled_plugin_is_retired_on_next_query():
    h = _Harness()
    h.query()
    h.executor.run_all()
    h.rows = []
    result = h.query()
    assert result[0]["state"] == "unavailable" and h.plugins[0].stopped == 1
    assert h.service._results == {} and h.service._connections == {}


def test_revoked_connection_is_dropped_from_pool_after_failed_drain():
    """通道抛出撤销时，展示服务必须丢掉该连接的结果缓存与待发槽，不能写回任何结果。"""
    h = _Harness()
    h.query()
    h.executor.run_all()
    h.plugins[0].revoked = True
    h.query("waiting_permission")
    h.executor.run_all()
    assert h.service._connections == {} and h.service._results == {} and h.service._slots == {}
    assert h.plugins[0].stopped == 1


def test_plugin_without_display_capability_is_unavailable():
    h = _Harness(capable=False)
    h.query()
    h.executor.run_all()
    assert h.query()[0]["state"] == "unavailable"


def test_render_failure_marks_error_and_backs_off():
    h = _Harness()
    h.query()
    h.executor.run_all()
    h.plugins[0].fail = TimeoutError("slow")
    h.query("waiting_permission")
    h.executor.run_all()
    errored = h.query("waiting_permission")[0]
    assert errored["state"] == "error" and "slow" not in errored.get("error", "")
    assert h.executor.jobs == []  # 退避期内不再请求
    h.plugins[0].fail = None
    h.now += ERROR_BACKOFF_SECONDS + 1
    h.query("waiting_permission")
    h.executor.run_all()
    assert h.query("waiting_permission")[0]["state"] == "ready"


def test_idle_connection_is_closed_and_restarted_on_demand():
    h = _Harness()
    h.query()
    h.executor.run_all()
    h.now += IDLE_CLOSE_SECONDS + 1
    h.service.panels(PanelQuery(object(), "owner", "thread:t1", {}, ()))
    assert h.plugins[0].stopped == 1
    h.query("waiting_permission")
    h.executor.run_all()
    assert len(h.plugins) == 2 and h.query("waiting_permission")[0]["state"] == "ready"


def test_invalid_plugin_output_becomes_error_not_exception(monkeypatch):
    h = _Harness()
    monkeypatch.setattr(_FakeTransport, "request", lambda *_a, **_k: {"display": {"lines": "bad"}})
    h.query()
    h.executor.run_all()
    assert h.query()[0]["state"] == "error"


def test_requested_panel_count_is_bounded():
    h = _Harness()
    many = tuple(("pet", "line") for _ in range(display_service.MAX_REQUESTED_PANEL_COUNT + 5))
    out = h.service.panels(PanelQuery(object(), "owner", "thread:t1", {}, many))
    assert len(out) == display_service.MAX_REQUESTED_PANEL_COUNT


def test_context_topic_forwards_only_public_numbers():
    from agent_py_agent.agent.plugin_display.service import project_topics

    activity = {"compact_count": 2, "context_usage": {
        "schema": "x", "estimated": True, "context_window_tokens": 200000, "compact_trigger_tokens": 180000,
        "current_tokens": 36900, "messages_tokens": 9950, "runtime_guidance_tokens": 1200,
        "tool_schema_tokens": 25000, "protocol": "native", "prompt": "不应转发"}}
    context = project_topics(activity, ("context",))["context"]
    assert context == {"known": True, "compact_count": 2, "context_window_tokens": 200000,
                       "compact_trigger_tokens": 180000, "current_tokens": 36900, "messages_tokens": 9950,
                       "runtime_guidance_tokens": 1200, "tool_schema_tokens": 25000, "estimated": True}
    empty = project_topics({}, ("context",))["context"]
    assert empty["known"] is False and empty["current_tokens"] == 0


def test_sessions_topic_is_lazy_and_whitelisted():
    from agent_py_agent.agent.plugin_display.service import _session_rows, project_topics

    calls = []

    def provider():
        calls.append(1)
        return [{"session_id": "sess_1", "updated_at": 2.0, "created_at": 1.0, "channel": "chat", "current": True,
                 "metadata": {"title": "私有"}}, "坏行", {"session_id": 3}]

    rows = _session_rows(provider)
    assert rows == [{"session_id": "sess_1", "updated_at": 2.0, "created_at": 1.0, "channel": "chat", "current": True}]
    assert project_topics({}, ("sessions",), rows)["sessions"] == {"items": rows}
    assert "sessions" not in project_topics({}, ("activity",), rows)
    assert _session_rows(None) == [] and len(calls) == 1

    def broken():
        raise OSError("disk")

    assert _session_rows(broken) == []


# LLM: 共用池里不只有面板：同一 owner 下已启用、但没有面板的插件（只带事件的 v8 插件）的连接
#   归事件中心管，面板服务的回收绝不能顺手摘掉它；安装表读不到时也不能当成"没有启用插件"去回收。
#   下面两条由 ae 复审探针 test_b2_shared_pool_probe.py 转正（原样场景，2026-10-03）。
def _event_only_installation():
    return SimpleNamespace(manifest=SimpleNamespace(plugin_id="watch", panels=()),
                           activation=SimpleNamespace(activation_id="act-watch"), enabled=True)


# LLM: 造一个"面板服务和事件中心共用同一个池"的现场：池由外部注入，事件中心先在上面建好 watch 连接。
# 函数用途: 返回（共用池, 面板服务, 已建好的插件客户端列表）。
def _shared_pool_setup(rows_provider):
    plugins = []

    def factory(_owner, installation):
        plugin = _FakePlugin(installation)
        plugins.append(plugin)
        return plugin

    pool = PluginChannelPool(client_factory=factory, clock=lambda: 100.0)
    service = PluginDisplayService(wiring=DisplayWiring(installations=rows_provider, clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    watch = _event_only_installation()
    # 另一个调用方（事件中心）在共用池上建好 watch 的连接并发出一次请求
    connection = pool.acquire("owner", watch, 100.0)
    pool.request(connection, ChannelCall(owner=object(), method="my-agent/events.observe",
                                         params={"topics": {"run_state": {"state": "x"}}}))
    return pool, service, plugins


def test_panel_query_keeps_other_consumers_connection_for_enabled_plugin():
    pet, watch = _installation(), _event_only_installation()
    pool, service, plugins = _shared_pool_setup(lambda _owner: (pet, watch))
    service.panels(PanelQuery(object(), "owner", "thread:t1", {}, (("pet", "line"),)))
    assert ("owner", "act-watch") in pool.connections, "启用中的 watch 连接被面板查询摘掉了"
    assert plugins[0].stopped == 0, "启用中的 watch 插件进程被面板查询停掉了"


def test_unreadable_install_table_does_not_tear_down_shared_connections():
    pool, service, plugins = _shared_pool_setup(lambda _owner: None)  # 安装表临时读不到（_enabled_installations 返回 None）
    service.panels(PanelQuery(object(), "owner", "thread:t1", {}, (("pet", "line"),)))
    assert ("owner", "act-watch") in pool.connections and plugins[0].stopped == 0, \
        "安装表读一次失败，就把这个 owner 在共用池里的所有连接都停了"


# LLM: 面板服务在锁外回收时要遍历池的连接表；池必须给加锁快照，不能把活字典交出去，
#   否则后台渲染的撤销或以后 B3 的建/删连接会在遍历中途改字典，抛
#   RuntimeError: dictionary changed size during iteration，让面板请求直接失败。
#   这条用例让"取快照"和删除交错，并用不可变类型断言钉住快照语义。
def test_service_retire_tolerates_concurrent_connection_removal():
    h = _Harness()
    h.query()
    h.executor.run_all()

    pool = h.service._pool
    real_owner_keys = pool.owner_keys
    seen = []

    def owner_keys_then_remove(owner_key):
        keys = real_owner_keys(owner_key)
        seen.append(keys)
        # 另一个调用方（后台渲染或事件中心）此刻删掉一条连接
        pool.retire_stale(owner_key, set())
        return keys

    pool.owner_keys = owner_keys_then_remove
    result = h.query()  # 不应抛 RuntimeError
    assert result[0]["state"] in {"loading", "refreshing", "ready", "error", "unavailable"}
    assert seen and isinstance(seen[0], tuple), "池必须返回不可变快照，不能交出活字典"


# LLM: 转正 9b 探针 2（回收窗口）。面板服务读到"当前有效激活"之后，别的调用方（以后的事件中心）
#   为一个刚启用的激活建了连接；这个激活不在本轮 valid 里、也不该被本轮回收摘掉——
#   回收必须有正面过期证据（自己管过 + 建在本轮时间界之前），不能只凭一份可能过时的 valid。
def test_retire_keeps_connection_created_after_valid_snapshot():
    pet = _installation("act-pet")
    fresh = _installation("act-fresh")
    pool = PluginChannelPool(client_factory=lambda _o, inst: _FakePlugin(inst), clock=lambda: 100.0)
    service = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (pet,),
                                                        clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    # 两个激活都在面板服务管过之后失效（valid 里都没有），但时间界卡在它们中间
    pool.acquire("owner", pet, 100.0)
    service._slot(("owner", "act-pet"))
    service._slot(("owner", "act-fresh"))
    seq_before_fresh = pool.current_seq()
    pool.acquire("owner", fresh, 100.0)  # 时间界之后才建出来的连接

    service._retire("owner", {"act-gone"}, 100.0, seq_before_fresh)

    assert ("owner", "act-fresh") in pool.connections, "时间界之后建的连接被误摘"
    assert ("owner", "act-pet") not in pool.connections, "时间界之前就失效的连接没有被回收"
    pool.close()


# LLM: 转正 9b 探针 5（时间界必须在读安装表之前取）。上一条直接调 _retire、时间界是外部传入的，
#   盖不住"取水位的语句在 panels() 里的位置"。这条走真实 panels() 入口：读表回调里模拟并发的
#   另一个面板查询——它拿到的是新表，为刚启用的 X 建了展示槽和池连接；本轮读到的是旧表（没有 X），
#   X 不在 valid 里。正确顺序下 X 的创建序号晚于时间界，本轮回收不能碰它；把取时间界的语句挪到
#   读表之后，X 就会被当成"时间界之前的失效连接"误摘。
def test_panels_takes_watermark_before_reading_install_table():
    pool = PluginChannelPool(client_factory=lambda _o, inst: _FakePlugin(inst), clock=lambda: 100.0)
    holder: dict = {}

    def stale_table_read(_owner):
        # 读表期间：并发的另一个面板查询（拿到的是新表）为刚启用的 X 建了展示槽与连接
        with holder["service"]._lock:
            holder["service"]._slot(("owner", "act-x"))
        pool.acquire("owner", _installation("act-x"), 100.0)
        return ()  # 本轮读到的是 X 启用之前的旧表

    service = PluginDisplayService(wiring=DisplayWiring(installations=stale_table_read, clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    holder["service"] = service
    service.panels(PanelQuery(object(), "owner", "thread:t1", {}, ()))

    assert ("owner", "act-x") in pool.connections, "读表期间别的查询为新启用插件建的连接被本轮回收摘掉了"
    pool.close()
    service.close()


# LLM: 转正 9b 的 N3 覆盖：回收范围必须看"自己管"。别人的连接（面板服务从来没有过它的展示槽）
#   即使已失效、且建在时间界之前，面板服务也不能摘——那是事件中心（以后 B3）的连接，由它们自己回收。
def test_retire_leaves_other_consumers_connection_untouched():
    other = _installation("act-other")
    pool = PluginChannelPool(client_factory=lambda _o, inst: _FakePlugin(inst), clock=lambda: 100.0)
    service = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (), clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    pool.acquire("owner", other, 100.0)  # 别人建的连接：面板服务没有这个激活的展示槽
    seq_before = pool.current_seq()
    service._retire("owner", set(), 100.0, seq_before)

    assert ("owner", "act-other") in pool.connections, "面板服务摘掉了别人的连接（这个激活没有它的展示槽）"
    pool.close()


# LLM: 读表失败（None）时不能当成"没有启用插件"去回收——包括面板服务自己管过的连接。
#   交给池的有效集合只在读表成功时才有意义；读表失败这一轮必须一条都不摘，等下一轮读表成功再收。
def test_unreadable_install_table_keeps_services_own_connections():
    pet = _installation("act-pet")
    pool = PluginChannelPool(client_factory=lambda _o, inst: _FakePlugin(inst), clock=lambda: 100.0)
    service = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: None, clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    pool.acquire("owner", pet, 100.0)
    service._slot(("owner", "act-pet"))  # 面板服务管过的连接（此前查询建过）
    service.panels(PanelQuery(object(), "owner", "thread:t1", {}, ()))

    assert ("owner", "act-pet") in pool.connections, "读表失败被当成空集合，面板服务自己的连接被摘了"
    pool.close()
    service.close()


# LLM: 插件收紧成无面板（同一激活）但仍在启用列表里时，交给池的有效集合必须包含它——
#   否则面板服务自己管过的连接会被当成"失效激活"摘掉。active（有面板）和 valid（已启用）
#   是两回事：前者只决定显示什么，后者决定回收保护谁。
def test_panel_less_enabled_activation_keeps_managed_connection():
    tightened = SimpleNamespace(manifest=SimpleNamespace(plugin_id="pet", panels=()),
                                activation=SimpleNamespace(activation_id="act-pet"), enabled=True)
    pool = PluginChannelPool(client_factory=lambda _o, inst: _FakePlugin(inst), clock=lambda: 100.0)
    service = PluginDisplayService(wiring=DisplayWiring(installations=lambda _owner: (tightened,),
                                                        clock=lambda: 100.0,
                                                        executor=_ManualExecutor(), pool=pool))
    pool.acquire("owner", tightened, 100.0)
    service._slot(("owner", "act-pet"))  # 面板服务管过它（收紧前有面板）
    service.panels(PanelQuery(object(), "owner", "thread:t1", {}, ()))

    assert ("owner", "act-pet") in pool.connections, "已启用但无面板的激活被当成失效，连接被误摘"
    pool.close()
    service.close()
