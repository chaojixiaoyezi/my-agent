from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.agent_activity import _MAIN_ACTIVITY_LIVE_PHASES
from agent_py_agent.agent.plugin_display import service as display_service
from agent_py_agent.agent.plugin_display.protocol import (
    DISPLAY_EXTENSION,
    MAX_LINE_CHARS,
    MAX_TEXT_LINES,
    PanelDeclaration,
    normalize_display,
    validate_panels,
)
from agent_py_agent.agent.plugin_display.service import (
    ERROR_BACKOFF_SECONDS,
    IDLE_CLOSE_SECONDS,
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
    lines = ["\x1b[31mred\x07"] + ["x" * (MAX_LINE_CHARS + 5)] + ["y"] * (MAX_TEXT_LINES + 3)
    out = normalize_display("text", {"lines": lines})
    assert out["lines"][0] == "[31mred"
    assert len(out["lines"]) == MAX_TEXT_LINES and len(out["lines"][1]) == MAX_LINE_CHARS
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
        self.service = PluginDisplayService(installations=lambda _owner: tuple(self.rows),
                                            client_factory=self._factory, clock=lambda: self.now,
                                            executor=self.executor)

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
    many = tuple(("pet", "line") for _ in range(display_service.MAX_REQUESTED_PANELS + 5))
    out = h.service.panels(PanelQuery(object(), "owner", "thread:t1", {}, many))
    assert len(out) == display_service.MAX_REQUESTED_PANELS


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
