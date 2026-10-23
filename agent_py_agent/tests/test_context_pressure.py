# LLM: 用原估算/校准入口与合成观测验证来源，不访问真实供应商，不把估计称为实际usage。
# 模块用途: 核验保守值、等值校准、纯诊断层归因与原预算的一致性。
import copy
import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import context_pressure as pressure
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


# LLM: 仅提供已有运行入口要求的最小宿主字段；跨owner比值读取关闭，避免测试读取用户真实校准数据。
# 函数用途: 创建不联网的计量对象与原生输入。
def _inputs():
    agent = SimpleNamespace(config=AgentConfig(model_context_window_tokens=128_000,
        memory_context_calibration_carry_enabled=False),
        backend=SimpleNamespace(context_window_tokens=128_000, name="fake", model_name="test-model"))
    params = SimpleNamespace(context_scope="default", save=False, live_archive_state={},
        tool_protocol_snapshot=make_test_protocol_snapshot(source_protocol="native"),
        tool_context=["private-guidance"], tool_ir_history=[],
        provider_history_messages=[{"role": "user", "content": "private-history"}])
    return agent, params


# LLM: 直接构造同表面数值观测，不伪造真实HTTP成功；覆盖校准生效但总数恰好不变与旧基线缩短失效。
# 函数用途: 验证公开raw/校准数和measurement_kind保留原校准公式。
@pytest.mark.parametrize(("observed_raw", "provider", "scope", "expected", "kind"), [
    (0, 0, "current_run", 1000, "conservative_estimate"),
    (1000, 1000, "current_run", 1000, "calibrated_estimate"),
    (1000, 500, "current_run", 500, "calibrated_estimate"),
    (1200, 600, "current_run", 1000, "conservative_estimate"),
    (1200, 600, "durable_thread", 500, "calibrated_estimate"),
    (1200, 600, "owner_ratio", 500, "calibrated_estimate"),
])
def test_snapshot_reports_raw_and_calibration_basis(monkeypatch, observed_raw, provider, scope, expected, kind):
    agent, params = _inputs()
    components = {"prompt_tokens": 400, "messages_tokens": 300, "runtime_guidance_tokens": 100, "tool_schema_tokens": 200}
    monkeypatch.setattr(pressure, "_model_visible_context_components", lambda *_args: ("native", 1000, components, "a" * 64))
    params.live_archive_state[pressure._PROVIDER_CONTEXT_OBSERVATION_KEY] = {
        "schema": pressure._PROVIDER_CONTEXT_OBSERVATION_SCHEMA, "raw_estimated_tokens": observed_raw,
        "provider_input_tokens": provider, "context_surface_fingerprint": "a" * 64, "_calibration_scope": scope}
    snapshot = pressure.model_visible_context_snapshot(agent, params, "private-prompt")
    public = snapshot.to_public_dict()
    assert snapshot.current_tokens == expected == pressure.model_visible_context_tokens(agent, params, "private-prompt")
    assert public.get("raw_estimated_tokens") == 1000
    assert public.get("calibrated_tokens") == expected
    assert public.get("measurement_kind") == kind
    assert public.get("counter_id") == ("provider_calibrated_v1" if kind == "calibrated_estimate" else "conservative_cjk_utf8_v1")
    assert public.get("count_scope") == "full_outbound_request"
    assert sum(public[key] for key in components) == expected
    assert snapshot.context_window_tokens == 128_000 and snapshot.compact_trigger_tokens == 115_200
    assert public["estimated"] is True and "private" not in str(public)


# LLM: 诊断沿真实计量组件，但不触发store水合、owner磁盘读取、修改live memo或消费历史/引导。
# 函数用途: 验证纯出站层分解与预检共源、字段可复算且不泄漏内容。
def test_outbound_layer_breakdown_is_pure_and_matches_preflight(monkeypatch):
    agent, params = _inputs()
    helper = getattr(pressure, "outbound_request_layer_breakdown", None)
    assert callable(helper), "缺少同源出站层归因入口"
    monkeypatch.setattr(pressure, "resolve_native_tools", lambda *_args: [
        {"name": "private-tool", "description": "private-schema", "input_schema": {"type": "object"}}])

    # LLM: 纯诊断不得为了展示偷偷读线程或owner磁盘；缺已加载校准时应如实返回保守估计。
    # 函数用途: 把意外诊断I/O转为可见失败。
    def forbidden_read(*_args, **_kwargs):
        raise AssertionError("diagnostic attempted store or calibration I/O")

    agent.conversation_store = SimpleNamespace(threads=SimpleNamespace(load_report=forbidden_read))
    params.task_attributes = {"conversation_thread_id": "never-read"}
    monkeypatch.setattr(pressure, "read_owner_ratio", forbidden_read)
    original = copy.deepcopy(params.live_archive_state)
    inputs = copy.deepcopy((params.provider_history_messages, params.tool_context, params.tool_ir_history))
    first = helper(agent, params, "private-prompt")
    assert params.live_archive_state == original
    assert first["measurement_kind"] == "conservative_estimate"
    assert first["raw_estimated_tokens"] == first["calibrated_tokens"]
    assert len(first["context_surface_fingerprint"]) == 64
    raw = first["raw_estimated_tokens"]
    params.live_archive_state[pressure._PROVIDER_CONTEXT_OBSERVATION_KEY] = {
        "schema": pressure._PROVIDER_CONTEXT_OBSERVATION_SCHEMA, "raw_estimated_tokens": raw,
        "provider_input_tokens": raw // 2, "context_surface_fingerprint": first["context_surface_fingerprint"]}
    original = copy.deepcopy(params.live_archive_state)
    result = helper(agent, params, "private-prompt")
    assert result["calibrated_tokens"] == raw // 2
    assert result["measurement_kind"] == "calibrated_estimate"
    assert sum(result["components"].values()) == result["calibrated_tokens"]
    assert result["counter_id"] == "provider_calibrated_v1" and result["count_scope"] == "full_outbound_request"
    assert params.live_archive_state == original
    assert (params.provider_history_messages, params.tool_context, params.tool_ir_history) == inputs
    snapshot = pressure.model_visible_context_snapshot(agent, params, "private-prompt")
    assert snapshot.current_tokens == result["calibrated_tokens"]
    assert snapshot.raw_estimated_tokens == result["raw_estimated_tokens"]
    assert "private-" not in json.dumps(result)


# LLM: 标准dataclass（含frozen/init=False）是宿主可传入参数；纯诊断不得重跑构造或写共享历史。
# 函数用途: 复现独立复核指出的replace重建副作用，并验证诊断副本真正隔离。
@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("state_init", [False, True])
def test_diagnostic_copy_does_not_rerun_dataclass_constructor(frozen, state_init):
    # LLM: 构造时有可观测副作用，用来区分浅复制和重新初始化；数据仅在本测试内。
    # 类用途: 模拟带构造钩子的普通或冻结宿主参数。
    @dataclass(frozen=frozen)
    class Params:
        history: list = field(default_factory=list)
        live_archive_state: dict = field(default_factory=dict, init=state_init)
        tool_protocol_snapshot: object = field(default_factory=lambda: make_test_protocol_snapshot(source_protocol="text"))

        # LLM: 只在首次构造追加标记，复制不得再次调用它。
        # 函数用途: 暴露诊断错误重建参数导致的共享列表修改。
        def __post_init__(self):
            self.history.append("initialized")

    params = Params()
    params.live_archive_state["keep"] = "original"
    original = copy.deepcopy((params.history, params.live_archive_state))
    isolated = pressure._diagnostic_context_params(params)
    assert params.history == original[0]
    assert isolated is not params and type(isolated) is type(params)
    isolated.live_archive_state["keep"] = "clone-only"
    agent, _params = _inputs()
    pressure.outbound_request_layer_breakdown(agent, params, "synthetic")
    assert (params.history, params.live_archive_state) == original
