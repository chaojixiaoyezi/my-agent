# LLM: 缓存钉住只改“新工具何时进原生工具表”，不改 typed 发现事实、权限或可调用范围；临时 Store 隔离真实 owner。
# 模块用途: 锁住 tool_search 与盲调补表在钉住时不改本缓存段的原生工具表、发现史照记、Compact 边界并入；关闭时保持旧行为。
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools
from agent_py_agent.agent.agent_core.tool_loop.deferred_schema_reload import (
    reload_schema_after_blind_call,
)
from agent_py_agent.agent.backends import http
from agent_py_agent.agent.conversation.tool_discovery import (
    active_tools_for_compact,
    discovery_cache_pinned,
    persist_discovered_tools,
    thread_loaded_tools,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.live_switches import (
    LIVE_SWITCH_KEYS,
    refresh_live_switches,
    register_live_config,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.user_config_capability import EFFECT_IMMEDIATE, TUNABLE_KEYS
from agent_py_agent.agent.tooling.registry import ToolRegistry, _search_deferred_tool_result
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests.cache_prefix_simulator import PrefixCacheSimulator
from agent_py_agent.tests.test_cache_prefix_regression import CacheScenarioWire
from agent_py_agent.tests.test_compactcall_websocket import _setup
from agent_py_agent.tests.test_session_task_real_chain import (
    RealChain,
    _agent_config,
    _ready_gateway_paths,
    _select_scripted_model,
)

_KEY = "tool_discovery_cache_pin_enabled"


# LLM: 只改真实 AgentConfig；注册表须在使用时读取同一对象，不给测试后门手动补值。
# 函数用途: 在不重建注册表的情况下切换缓存钉住策略。
def _pin(agent, enabled: bool) -> None:
    setattr(agent.config, _KEY, enabled)


# LLM: 默认关闭；只有布尔 True 才算打开，字符串/数字/缺配置一律按关闭，避免误开改变模型请求。
# 函数用途: 锁住开关默认值、严格读取以及 YAML 与参数中心的同名登记。
def test_pin_switch_defaults_off_and_is_registered():
    assert getattr(AgentConfig(), _KEY) is False
    assert discovery_cache_pinned(SimpleNamespace(config=SimpleNamespace(**{_KEY: True}))) is True
    for value in (False, "true", 1, None):
        assert discovery_cache_pinned(SimpleNamespace(config=SimpleNamespace(**{_KEY: value}))) is False
    assert discovery_cache_pinned(None) is False
    yaml_text = (Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml").read_text(encoding="utf-8")
    assert f"\n{_KEY}: false\n" in yaml_text
    spec = parameter_registry()[_KEY]
    assert spec.default is False and spec.category == "工具" and spec.description
    assert _KEY in LIVE_SWITCH_KEYS and _KEY in TUNABLE_KEYS
    assert spec.effect == EFFECT_IMMEDIATE and spec.writable and spec.user_editable


# LLM: 同一注册表走默认关、开、关、再开；禁止测试通过写注册表属性掩盖构造冻结。
# 函数用途: 锁住 live_config 优先于 DTO 兜底且属性只读。
def test_pin_registry_reads_live_config_in_both_directions(tmp_path, monkeypatch):
    agent, _thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    registry = agent.tools
    for enabled in (False, True, False, True):
        _pin(agent, enabled)
        assert registry.tool_discovery_cache_pin_enabled is enabled
    with pytest.raises(AttributeError):
        registry.tool_discovery_cache_pin_enabled = False


# LLM: 裸注册表无 live_config 时才采用构造 DTO；不读取全局或其它实例的配置。
# 函数用途: 验证兼容构造的默认关闭与显式开启都可用。
@pytest.mark.parametrize("pinned", [False, True])
def test_pin_registry_without_live_config_uses_params_fallback(tmp_path, monkeypatch, pinned):
    agent, _thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    params = replace(agent.tools._construction_params, live_config=None,
                     tool_discovery_cache_pin_enabled=pinned)
    registry = ToolRegistry(params)
    assert registry.tool_discovery_cache_pin_enabled is pinned


# LLM: 真实配置文件通过现有刷新入口同步到同一个已构造 Agent；不重建注册表，不直接改字段。
# 函数用途: 验证文件热刷新同步改变搜索结果的 Schema 投递模式。
def test_pin_file_refresh_changes_search_delivery_without_rebuilding_registry(tmp_path, monkeypatch):
    agent, _thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    path = tmp_path / "live-switch.yaml"
    path.write_text(f"{_KEY}: false\n", encoding="utf-8")
    agent.config.config_path = str(path)
    register_live_config(agent.config)
    registry = agent.tools
    snapshot = registry.runtime_snapshot()
    for enabled in (True, False, True):
        path.write_text(f"{_KEY}: {str(enabled).lower()}\n", encoding="utf-8")
        assert _KEY in refresh_live_switches(agent.config, force=True)
        assert agent.tools is registry and registry.tool_discovery_cache_pin_enabled is enabled
        outcome = _search_deferred_tool_result(registry, "read_file", limit=1, snapshot=snapshot)
        body = json.loads(outcome.result_envelope["tool_output_policy"]["live_prompt_output"])
        assert ("input_schema" in body["tools"][0]) is enabled
        assert body["loaded_for_next_model_call"] == ([] if enabled else ["read_file"])


# LLM: 正式参数工具负责持久写入和合法布尔解析；错误值不得产生配置或变更账副作用。
# 函数用途: 验证设置入口能查到并双向保存开关，且拒绝非法值。
def test_pin_settings_entry_is_immediate_writable_and_rejects_invalid_values(tmp_path, monkeypatch):
    agent, _thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    path = tmp_path / "settings.yaml"
    path.write_text(f"{_KEY}: false\n", encoding="utf-8")
    agent.config.config_path = str(path)
    register_live_config(agent.config)
    tool = UserConfigTool(agent)
    found = tool.execute({"action": "search", "key": _KEY})
    assert found.ok and _KEY in found.output
    for enabled in (True, False):
        result = tool.execute({"action": "set", "key": _KEY, "value": str(enabled).lower()})
        assert result.ok, result.output
        refresh_live_switches(agent.config, force=True)
        viewed = tool.execute({"action": "view", "key": _KEY})
        payload = json.loads(viewed.output)
        assert payload["parameter"]["effect_when"] == EFFECT_IMMEDIATE
        assert payload["parameter"]["running_value"] == str(enabled).lower()
        assert agent.tools.tool_discovery_cache_pin_enabled is enabled
    before = path.read_bytes()
    rejected = tool.execute({"action": "set", "key": _KEY, "value": "not-a-bool"})
    assert not rejected.ok and path.read_bytes() == before


# LLM: 钉住时结果里直接给完整定义、下一次请求的原生工具表不变；typed 加载名照旧，关闭时恢复立即加载。
# 函数用途: 验证 tool_search 的 live 结果与回合内加载名在两种开关下的差异。
def test_pinned_tool_search_inlines_schema_and_keeps_next_request_tools(tmp_path, monkeypatch):
    agent, _thread, _prior, records = _setup(tmp_path, monkeypatch, False)
    _pin(agent, True)
    outcome = _search_deferred_tool_result(agent.tools, "read_file", limit=1, snapshot=agent.tools.runtime_snapshot())
    live = json.loads(outcome.result_envelope["tool_output_policy"]["live_prompt_output"])
    assert live["loaded_for_next_model_call"] == [] and live["callable_by_name"] == ["read_file"]
    assert live["tools"][0]["name"] == "read_file" and live["tools"][0]["input_schema"]
    assert outcome.result_envelope["tool_search"]["loaded_tool_names"] == ["read_file"]
    params = SimpleNamespace(loaded_tool_names=[], live_archive_state={})
    _load_discovered_tools(params, records[0], agent=agent)
    assert params.loaded_tool_names == [] and "loaded_tool_order" not in params.live_archive_state
    _pin(agent, False)
    old_live = json.loads(_search_deferred_tool_result(
        agent.tools, "read_file", limit=1, snapshot=agent.tools.runtime_snapshot(),
    ).result_envelope["tool_output_policy"]["live_prompt_output"])
    assert "callable_by_name" not in old_live and "input_schema" not in old_live["tools"][0]
    _load_discovered_tools(params, records[0], agent=agent)
    assert params.loaded_tool_names == ["read_file"]


# LLM: 钉住时线程只记发现史与版本，下一回合原生工具表不变；Compact 选择器仍会把它并入下一段。
# 函数用途: 验证跨回合持久化与 Compact 边界的并入路径。
def test_pinned_discovery_records_history_only_and_folds_at_compact(tmp_path, monkeypatch):
    agent, thread, _prior, records = _setup(tmp_path, monkeypatch, False)
    _pin(agent, True)
    params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    persist_discovered_tools(agent, params, records[0])
    current = agent.conversation_store.threads.load(thread.thread_id)
    assert thread_loaded_tools(agent, params) == set()
    assert "read_file" in current.metadata["discovered_tool_history"]
    assert current.metadata["discovered_tool_revisions"]["read_file"] >= 1
    assert "read_file" in active_tools_for_compact(current, recent_used_tool_names=("read_file",))
    _pin(agent, False)
    persist_discovered_tools(agent, params, records[0])
    assert thread_loaded_tools(agent, params) == {"read_file"}


# LLM: 盲调参数失败时，钉住只把参数速查放进结果尾部，不把工具塞进下一次请求；发现史照记，供 Compact 并入。
# 函数用途: 验证盲调补表在钉住与关闭两种开关下的差异。
def test_pinned_blind_call_inlines_micro_schema_without_loading(tmp_path, monkeypatch):
    agent, thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    _pin(agent, True)
    snapshot = agent.tools.runtime_snapshot()

    def run():
        params = SimpleNamespace(loaded_tool_names=[], tool_runtime_snapshot=snapshot, allowed_tools=None,
                                 live_archive_state={})
        record = {"tool": "read_file", "ok": False, "tool_execution": {"failure_stage": "validation"}}
        return params, record, reload_schema_after_blind_call(agent, params, record)

    params, record, hint = run()
    assert "[tool-schema-inline] read_file" in hint
    assert params.loaded_tool_names == [] and "loaded_tool_order" not in params.live_archive_state
    assert record["schema_reload"]["schema_inline_only"] is True
    assert record["schema_reload"]["loaded_micro_schema"]["name"] == "read_file"
    thread_params = SimpleNamespace(task_attributes={"conversation_thread_id": thread.thread_id})
    persist_discovered_tools(agent, thread_params, record)
    current = agent.conversation_store.threads.load(thread.thread_id)
    assert thread_loaded_tools(agent, thread_params) == set()
    assert "read_file" in current.metadata["discovered_tool_history"]
    _pin(agent, False)
    params, record, hint = run()
    assert "[tool-schema-loaded] read_file" in hint and params.loaded_tool_names == ["read_file"]


# 函数用途: 取某次请求里 tool_search 结果的 JSON 正文（首行是运行状态头，正文后可能还有锚点或事实行）。
def _search_result_body(payload: dict) -> dict:
    content = next(message["content"] for message in payload["messages"] if message.get("role") == "tool")
    return json.JSONDecoder().raw_decode(content.split("\n", 1)[1])[0]


# 类用途: 场景脚本 —— SIM-SEARCH 回合第一次调用按精确名检索折叠工具 web_fetch，之后收尾；其余标记沿用原脚本。
class _SearchScenarioWire(CacheScenarioWire):
    def _script_for(self, marker: str, tool_calls_done: int) -> dict:
        if marker.startswith("SIM-SEARCH") and tool_calls_done == 0:
            return {"tool": "tool_search", "arguments": {"names": ["web_fetch"]}}
        return super()._script_for(marker, tool_calls_done)


# LLM: 走真实 SimpleAgent 装配（配置 -> 注册表）与真实 Gateway 回合，只把供应商传输换成前缀缓存模拟器。
# 函数用途: 锁住开关经 core 装配真正生效：钉住时检索后的同回合与下一回合原生工具表逐字不变、前缀整段命中；关闭时照旧当场加载。
@pytest.mark.parametrize("pinned", [True, False])
def test_real_chain_tool_search_changes_native_tools_only_when_unpinned(tmp_path, monkeypatch, pinned):
    from agent_py_agent.agent.core import SimpleAgent

    config = replace(_agent_config(tmp_path, model_reasoning_effort="max"), api_base="https://api.deepseek.com/v1",
                     model_name="deepseek-v4-flash", **{_KEY: pinned})
    agent = SimpleAgent(config, tmp_path / "root")
    _select_scripted_model(agent, config)
    simulator = PrefixCacheSimulator()
    wire = _SearchScenarioWire(simulator)
    monkeypatch.setattr(http, "post_json", wire)
    chain = RealChain(agent, _ready_gateway_paths(agent), wire, {}, scheduler=None)
    chain.open_session("SIM")
    chain.ask("SIM", "SIM-SEARCH 先找网页抓取工具。")
    chain.ask("SIM", "SIM-ACK 下一轮。")
    tools = [[str((row.get("function") or {}).get("name") or "") for row in payload.get("tools") or []]
             for payload in wire.payloads]
    assert len(tools) == 3 and "web_fetch" not in tools[0], [len(names) for names in tools]
    thread = agent.conversation_store.threads.load(chain.threads["SIM"])
    assert "web_fetch" in thread.metadata["discovered_tool_history"]
    body = _search_result_body(wire.payloads[1])
    if pinned:
        assert tools[1] == tools[0] and tools[2] == tools[0]
        assert simulator.calls[1].hit_tokens == simulator.calls[0].prompt_tokens
        assert "web_fetch" not in thread.loaded_tool_names
        assert body["callable_by_name"] == ["web_fetch"] and body["loaded_for_next_model_call"] == []
        assert body["tools"][0]["input_schema"]
    else:
        assert "web_fetch" in tools[1] and "web_fetch" in tools[2]
        assert simulator.calls[1].hit_tokens < simulator.calls[0].prompt_tokens
        assert "callable_by_name" not in body and body["loaded_for_next_model_call"] == ["web_fetch"]


# LLM: 同类入口：凭据发现（web_fetch）与能力包工具也经同一归档加载名进入下一次请求；钉住时同样留到 Compact 边界。
# 函数用途: 锁住非 tool_search 的归档携带加载在两种开关下的差异。
@pytest.mark.parametrize("envelope", [{"capability_package_tools": {"loaded_tool_names": ["read_file"]}},
                                      {"credential_discovery": {}}])
def test_pinned_archive_carried_loads_wait_for_compact(tmp_path, monkeypatch, envelope):
    agent, _thread, _prior, _records = _setup(tmp_path, monkeypatch, False)
    record = {"tool": "probe", "ok": True, "tool_result_envelope": envelope}
    expected = ["read_file"] if "capability_package_tools" in envelope else ["web_fetch"]
    for pinned, loaded in ((True, []), (False, expected)):
        _pin(agent, pinned)
        params = SimpleNamespace(loaded_tool_names=[], live_archive_state={})
        _load_discovered_tools(params, record, agent=agent)
        assert params.loaded_tool_names == loaded
