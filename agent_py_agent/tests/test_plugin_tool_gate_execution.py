"""B5 第二段：真实执行器 + 真实 B2 池，只有安装与传输是替身，不启用真实 v8。"""
from __future__ import annotations

import ast
import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_channel import PluginChannelPool
from agent_py_agent.agent.plugin_events.declarations import PluginToolGateDeclaration
from agent_py_agent.agent.plugin_events.tool_gate import GateCall, GateTarget, PluginToolGate
from agent_py_agent.agent.plugin_events.tool_gate_review import (
    GateWiring,
    PluginGateReviewer,
    plugin_gate_timeout_seconds,
)
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.parameter_registry import classify_safety, parameter_registry
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome
from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
    runtime_snapshot_for_tools,
)


class _Probe(BaseTool):
    model_spec = make_test_model_spec("gate_probe", input_schema={"type": "object", "additionalProperties": True})
    runtime_policy = make_test_runtime_policy()

    def __init__(self):
        self.executed = []

    def execute(self, arguments):
        self.executed.append(arguments)
        return ToolHandlerOutcome("gate_probe", True, "executed")


# LLM: 这是隔离的传输替身，仍真实运行池的启动/单在途/发送前后代次核对；不启动真进程。
class _Transport:
    def __init__(self, client):
        self.client = client

    def request(self, method, params, *, timeout, authority_check):
        authority_check()
        self.client.harness.sent.append((self.client.row.activation.activation_id, method, params, timeout))
        return self.client.harness.answer(self.client.row, params, timeout)


class _Client:
    def __init__(self, h, row):
        self.harness, self.row = h, row
        self.capabilities = h.capabilities
        self.activation_ref = SimpleNamespace(require=self.require)

    def require(self):
        current = next((item for item in self.harness.rows if item.manifest.plugin_id == self.row.manifest.plugin_id), None)
        if current is None or current.activation != self.row.activation:
            raise ValueError("revoked")
        return current

    def start(self):
        return _Transport(self)

    def stop(self):
        return None


class _Harness:
    def __init__(self):
        self.rows = [self.row()]
        self.sent = []
        self.capabilities = {"experimental": {"my-agent/tool-gate": {"versions": ["1"]}}}
        self.answer = lambda _row, _params, _timeout: {"verdict": "ask", "reason_code": "CONFIRM"}
        self.owner = SimpleNamespace(home_dir=Path("isolated-owner"))
        self.config = SimpleNamespace(plugin_tool_gate_timeout_ms=200)
        self.pool = PluginChannelPool(client_factory=lambda _owner, row: _Client(self, row))
        self.reviewer = PluginGateReviewer(GateWiring(self.owner, self.config, lambda: self.pool,
                                                     lambda _owner: tuple(self.rows)))

    @staticmethod
    def row(plugin="guard", activation="act-1"):
        gate = PluginToolGateDeclaration("check", ("gate_probe",), (), "none")
        return SimpleNamespace(manifest=SimpleNamespace(plugin_id=plugin, version="1.0.0", tool_gates=(gate,)),
                               enabled=True, activation=SimpleNamespace(activation_id=activation))

    def call(self, tmp_path):
        probe = _Probe()
        snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
        call = canonical_test_call(snapshot, "gate_probe", {})
        return GateCall(call, "read_only", "main", True)


def test_timeout_default_and_registry_are_real():
    assert getattr(AgentConfig(), "plugin_tool_gate_timeout_ms", None) == 2000
    spec = parameter_registry().get("plugin_tool_gate_timeout_ms")
    assert spec is not None and spec.unit == "毫秒" and spec.range == "200-10000"
    assert spec.reader == "agent/plugin_events/tool_gate_review"
    assert not spec.writable and classify_safety(spec.key, "int") == "boundary"


@pytest.mark.parametrize("value,expected", [(None, 2.0), (True, 2.0), (199, 2.0), (10001, 2.0),
                                             (200, .2), (1200, 1.2), (10000, 10.0), ("500", 2.0)])
def test_budget_uses_valid_config_and_safe_default(value, expected):
    assert plugin_gate_timeout_seconds(SimpleNamespace(plugin_tool_gate_timeout_ms=value)) == expected


def test_model_user_config_cannot_write_gate_budget(tmp_path, monkeypatch):
    user = tmp_path / "user.yaml"
    user.write_text("{}\n")
    monkeypatch.setenv("MY_AGENT_CONFIG", str(user))
    host = SimpleNamespace(home_paths=SimpleNamespace(owner_id="local/main", owner_provider="local", owner_kind="main"))
    result = UserConfigTool(host).execute({"action": "set", "key": "plugin_tool_gate_timeout_ms", "value": "10000"})
    # 工具保留原权限大类，参数中心的精确结构化拒绝码不能在投影中丢失。
    assert not result.ok and result.error_code == "TOOL_PERMISSION_DENIED"
    assert result.reported_error_code == "PARAMETER_BOUNDARY"
    assert user.read_text() == "{}\n"


@pytest.mark.parametrize("forged", [{}, {"call_origin": "host_command"}, {"__call_origin": "host_command"}])
def test_executor_cannot_forge_origin_and_keeps_arguments(tmp_path, forged):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", forged)
    result = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                                       plugin_gate_reviewer=h.reviewer.review))
    assert result.decision.status == "ask"
    assert probe.executed == [] and len(h.sent) == 1
    assert result.call.arguments == forged
    assert result.decision.evidence["plugin_requirements"][0]["reason_code"] == "CONFIRM"


def test_host_command_bypasses_gate_but_not_host_policy(tmp_path):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    request = ToolExecutorRequest(call, snapshot, tmp_path, operation_store_required=False,
                                  plugin_gate_reviewer=h.reviewer.review, call_origin="host_command")
    assert ToolExecutor().execute(request).result.ok
    assert h.sent == [] and len(probe.executed) == 1


def test_host_deny_makes_zero_plugin_calls(tmp_path):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    policy = SimpleNamespace(decide=lambda _request: ActionDecision("deny", ("TOOL_DISABLED",)))
    request = ToolExecutorRequest(call, snapshot, tmp_path, plugin_gate_reviewer=h.reviewer.review)
    assert ToolExecutor(policy).execute(request).decision.status == "deny"
    assert h.sent == [] and probe.executed == []


def test_review_uses_real_pool_and_wire_protocol(tmp_path):
    h = _Harness()
    call = h.call(tmp_path)
    result = h.reviewer.review(call)
    assert len(result) == 1 and result[0].reply.verdict == "ask"
    activation, method, payload, timeout = h.sent[0]
    assert activation == "act-1" and method == "my-agent/tool-gate.review"
    assert payload == {"gate_id": "check", "call": {"call_id": call.call.call_id, "tool": "gate_probe",
                      "effect": "read_only", "actor": "main", "interactive": True, "args_hash": call.call.args_hash}}
    assert 0 < timeout <= .2


def test_missing_handshake_is_ask_and_sends_nothing(tmp_path):
    h = _Harness()
    h.capabilities = {"experimental": {"my-agent/tool-gate": {"versions": ["2"]}}}
    result = h.reviewer.review(h.call(tmp_path))
    assert len(result) == 1 and result[0].outcome == "unavailable"
    assert result[0].reply.reason_code == "PLUGIN_GATE_UNAVAILABLE" and h.sent == []


def test_disable_discards_reply_but_reactivation_asks_new_instance(tmp_path):
    h = _Harness()
    def replace_instance(row, _params, _timeout):
        if row.activation.activation_id == "act-1":
            h.rows = [h.row(activation="act-2")]
            return {"verdict": "deny", "reason_code": "OLD"}
        return {"verdict": "ask", "reason_code": "NEW"}
    h.answer = replace_instance
    result = h.reviewer.review(h.call(tmp_path))
    assert [item.outcome for item in result] == ["revoked", "ok"]
    assert result[-1].target.activation_id == "act-2" and result[-1].reply.reason_code == "NEW"
    assert [item[0] for item in h.sent] == ["act-1", "act-2"]


def test_stop_without_replacement_does_not_require_confirmation(tmp_path):
    h = _Harness()
    def stop(_row, _params, _timeout):
        h.rows = []
        return {"verdict": "deny", "reason_code": "OLD"}
    h.answer = stop
    result = h.reviewer.review(h.call(tmp_path))
    assert len(result) == 1 and result[0].outcome == "revoked"


def test_two_plugins_are_asked_in_parallel_and_one_total_budget(tmp_path):
    h = _Harness()
    h.rows.append(h.row("second", "act-second"))
    barrier = threading.Barrier(2)
    def answer(_row, _params, timeout):
        barrier.wait(timeout=timeout)
        return {"verdict": "ask", "reason_code": "PARALLEL"}
    h.answer = answer
    result = h.reviewer.review(h.call(tmp_path))
    assert len(result) == 2 and all(item.outcome == "ok" for item in result)
    assert len(h.sent) == 2


def test_queue_wait_is_timeout_ask_not_allow(tmp_path):
    h = _Harness()
    connection = h.pool.acquire(str(h.owner.home_dir), h.rows[0], time.monotonic())
    connection.request_lock.acquire()
    started = time.monotonic()
    try:
        result = h.reviewer.review(h.call(tmp_path))
    finally:
        connection.request_lock.release()
    assert len(result) == 1 and result[0].reply.verdict == "ask"
    assert result[0].outcome == "timeout" and h.sent == []
    assert time.monotonic() - started < 1.0


# LLM: 静态盘点只认构造字段，不把字符串命中或工具参数当可信来源；输出仍由调用方逐项断言。
# 函数用途: 抽出宿主构造的显式来源值，让反证测试保持单层结构。
def _constructor_origins(source):
    tree = ast.parse(source.read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "ToolExecutorRequest"]
    values = [[kw.value for kw in node.keywords if kw.arg == "call_origin"] for node in calls]
    return [origins for origins in values if origins]


def test_only_two_host_constructors_set_trusted_origin():
    root = Path(__file__).parents[1] / "agent"
    configured = []
    for name in ("plugin_management.py", "plugin_invocation.py", "tooling/registry.py",
                 "contracts/main_agent_foundation_contract_cases.py"):
        for origins in _constructor_origins(root / name):
            assert len(origins) == 1 and isinstance(origins[0], ast.Constant)
            assert origins[0].value == "host_command"
            configured.append(name)
    assert sorted(configured) == ["plugin_invocation.py", "plugin_management.py"]


def test_gateway_provider_returns_the_same_b2_pool_without_new_pool(monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_service, plugin_panels_http

    sentinel = object()
    server = SimpleNamespace(plugin_channel_pool=sentinel)
    monkeypatch.setattr(http_service, "_server_instance", server)
    provider = getattr(plugin_panels_http, "current_plugin_channel_pool", lambda: None)
    assert provider() is sentinel
    assert server.plugin_channel_pool is sentinel


def test_registry_real_entry_passes_host_reviewer(tmp_path):
    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    h, probe = _Harness(), _Probe()
    params = ToolRegistryParams(tmp_path, 100, 10, 10, 100, 1, 10, 10, False,
                                operation_store_required=False)
    # 空壳版本没有字段时仍能在执行结果断言上有效红测。
    object.__setattr__(params, "plugin_gate_reviewer", h.reviewer.review)
    registry = ToolRegistry(params)
    registry.register(probe)
    snapshot = registry.runtime_snapshot(run_id="test-run")
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot)
    assert execution.decision.status == "ask"
    assert len(h.sent) == 1 and probe.executed == []


def test_composition_root_builds_owner_reviewer_without_starting_gateway(tmp_path, monkeypatch):
    from agent_py_agent.agent import core
    from agent_py_agent.agent.gateway_parts import plugin_panels_http
    from agent_py_agent.agent.plugin_events import tool_gate_review

    h = _Harness()
    monkeypatch.setattr(tool_gate_review, "enabled_gate_installations", lambda _owner: tuple(h.rows))
    monkeypatch.setattr(plugin_panels_http, "current_plugin_channel_pool", lambda: h.pool, raising=False)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path))
    build = getattr(core, "_build_plugin_gate_reviewer", lambda _agent, _config: None)
    callback = build(agent, AgentConfig())
    assert callback is not None
    assert len(callback(h.call(tmp_path))) == 1 and len(h.sent) == 1


@pytest.mark.parametrize("owner_type,context,expected", [("main_agent", {}, "main"),
    ("subagent", {}, "subagent"), ("main_agent", {"actor": "decision"}, "decision")])
def test_auto_host_allow_is_tightened_for_all_actors(tmp_path, owner_type, context, expected):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe}, owner_type=owner_type)
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, approval_mode="auto", trusted_run_context=context,
        plugin_gate_reviewer=h.reviewer.review))
    assert execution.decision.status == "ask" and probe.executed == []
    assert h.sent[0][2]["call"]["actor"] == expected


def test_extra_response_arguments_never_replace_real_parameters(tmp_path):
    h, probe = _Harness(), _Probe()
    h.answer = lambda _row, _params, _timeout: {"verdict": "allow_as_is", "reason_code": "OK",
                                              "arguments": {"forged": True}, "call_origin": "host_command"}
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {"original": True})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, plugin_gate_reviewer=h.reviewer.review))
    assert execution.result.ok and probe.executed == [{"original": True}]
    assert execution.call.arguments == {"original": True}


def test_new_activation_after_budget_exhaustion_still_asks(tmp_path):
    h = _Harness()
    def change(_row, _params, _timeout):
        h.rows = [h.row(activation="act-2")]
        time.sleep(.25)
        return {"verdict": "allow_as_is", "reason_code": "OLD"}
    h.answer = change
    result = h.reviewer.review(h.call(tmp_path))
    assert result[0].outcome == "revoked"
    assert result[-1].target.activation_id == "act-2"
    assert result[-1].outcome == "timeout" and result[-1].reply.verdict == "ask"


def test_unreadable_installation_table_does_not_become_host_allow(tmp_path):
    h, probe = _Harness(), _Probe()
    h.reviewer = PluginGateReviewer(GateWiring(h.owner, h.config, lambda: h.pool, lambda _owner: None))
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, plugin_gate_reviewer=h.reviewer.review))
    assert execution.decision.status == "ask" and probe.executed == [] and h.sent == []


def test_invocation_constructor_has_at_most_four_parameters():
    import inspect

    from agent_py_agent.agent.plugin_invocation import _prepare_invocation

    assert len(inspect.signature(_prepare_invocation).parameters) <= 4


def test_closed_channel_but_still_active_plugin_must_not_relax(tmp_path):
    h = _Harness()
    h.pool.close()
    reviews = h.reviewer.review(h.call(tmp_path))
    assert len(reviews) == 1 and reviews[0].outcome == "unavailable"
    assert reviews[0].reply.verdict == "ask" and h.sent == []


def test_executor_plugin_deny_preserves_permission_error_contract(tmp_path):
    h, probe = _Harness(), _Probe()
    h.answer = lambda *_args: {"verdict": "deny", "reason_code": "BLOCKED"}
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, plugin_gate_reviewer=h.reviewer.review))
    assert execution.result.error_code == "PLUGIN_GATE_DENIED"
    assert execution.result.error_category == "permission" and not execution.result.retryable
    assert not execution.result.handler_executed and execution.result.effect_outcome == "not_started"
    assert probe.executed == []


@pytest.mark.parametrize("arguments,expected", [({"patch": "x" * 5000}, True),
    ({"patch": "short"}, False), ({"patch": '"' * 3000}, True)])
def test_full_projection_reports_actual_truncation(tmp_path, arguments, expected):
    h = _Harness()
    context = h.call(tmp_path)
    context = replace(context, call=replace(context.call, arguments=arguments))
    gate = PluginToolGateDeclaration("full-check", ("gate_probe",), (), "full")
    target = GateTarget("guard", "1.0.0", "act-1", gate)
    payload = PluginToolGate.request_payload(target, context)
    assert payload["call"].get("arguments_truncated") is expected
    assert len(json.dumps(payload["call"]["arguments"], ensure_ascii=False, separators=(",", ":"))) <= 4000
    assert context.call.arguments == arguments


def test_none_projection_does_not_report_truncation(tmp_path):
    h = _Harness()
    target = GateTarget("guard", "1.0.0", "act-1", h.rows[0].manifest.tool_gates[0])
    payload = PluginToolGate.request_payload(target, h.call(tmp_path))
    assert "arguments" not in payload["call"] and "arguments_truncated" not in payload["call"]


def test_slow_start_consumes_configured_200ms_total_budget(tmp_path, monkeypatch):
    h = _Harness()
    original = _Client.start
    def slow_start(client):
        time.sleep(.6)
        return original(client)
    monkeypatch.setattr(_Client, "start", slow_start)
    started = time.monotonic()
    reviews = h.reviewer.review(h.call(tmp_path))
    elapsed = time.monotonic() - started
    assert .15 <= elapsed < .45
    assert len(reviews) == 1 and reviews[0].outcome == "timeout"
    assert reviews[0].reply.verdict == "ask"


def test_executor_noninteractive_plugin_confirmation_is_unavailable(tmp_path):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, trusted_run_context={"interactive": False},
        plugin_gate_reviewer=h.reviewer.review))
    assert execution.result.error_code == "PLUGIN_GATE_APPROVAL_UNAVAILABLE"
    assert execution.result.error_category == "permission" and not execution.result.retryable
    assert not execution.result.handler_executed and probe.executed == []


def test_executor_plugin_confirmation_has_stable_json_reference(tmp_path):
    h, probe = _Harness(), _Probe()
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, trusted_run_context={"interactive": True},
        plugin_gate_reviewer=h.reviewer.review))
    reference = execution.decision.evidence.get("plugin_gate_ref")
    assert isinstance(reference, str) and reference
    binding = json.loads(reference)
    assert all(binding[key] == getattr(call, key) for key in
               ("operation_id", "call_id", "idempotency_key", "args_hash"))
    assert binding["gates"][0]["activation_id"] == "act-1"
    assert binding["gates"][0]["gate_id"] == "check"
    assert "message" not in binding["gates"][0]


@pytest.mark.parametrize("mode", ["session_cache", "owner_grant", "autonomous", "auto"])
def test_executor_reference_reaches_real_gateway_confirmation(tmp_path, monkeypatch, mode):
    from agent_py_agent.agent.agent_core.tool_loop import round_execution
    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        ToolApprovalRequest,
    )
    from agent_py_agent.agent.gateway_parts import stream_approval
    from agent_py_agent.agent.gateway_parts.approval_session import ToolApprovalSessionCache

    h, probe, events = _Harness(), _Probe(), []
    h.answer = lambda *_args: {"verdict": "ask", "reason_code": "CONFIRM", "message": "核对\n\u202e\u2028" + "x" * 100}
    snapshot = runtime_snapshot_for_tools({"gate_probe": probe})
    call = canonical_test_call(snapshot, "gate_probe", {})
    execution = ToolExecutor().execute(ToolExecutorRequest(call, snapshot, tmp_path,
        operation_store_required=False, approval_mode="auto" if mode == "auto" else "ask",
        trusted_run_context={"interactive": True}, plugin_gate_reviewer=h.reviewer.review))
    cache = ToolApprovalSessionCache()
    if mode == "session_cache":
        cache.approve("scope", f"gate_probe:{call.args_hash}")
    stream = stream_approval.StreamApproval(tmp_path / "chunks", events.append, lambda: None)
    stream.configure(cache, lambda: "scope")
    monkeypatch.setattr(stream_approval, "wait_for_gateway_permission_decision",
        lambda _path, req, **_kw: ToolApprovalDecision(req.permission_id, "denied"))
    def consumer(value, **_kw):
        request = ToolApprovalRequest.from_mapping(value)
        assert request.binding.get("plugin_gate_ref")
        assert [item["label"] for item in request.options] == ["仅本次", "拒绝"]
        assert request.description.startswith("[插件 guard 要求确认：CONFIRM 核对")
        assert all(char not in request.description for char in ("\n", "\u202e", "\u2028"))
        options_class = getattr(stream_approval, "StreamApprovalRequestOptions", None)
        provider = (lambda req: ToolApprovalDecision(req.permission_id, "approved")) if mode in ("owner_grant", "autonomous") else None
        if options_class is None:
            return stream.request(value, interactive=True, mode_decision_provider=provider)
        return stream.request(value, options=options_class(True, provider))
    params = SimpleNamespace(effective_on_chunk=SimpleNamespace(request_permission=consumer),
        request_id="integration", cancellation_token=None, runtime_rejected_actions=[],
        runtime_approved_actions=[], tool_runtime_snapshot=snapshot)
    request = SimpleNamespace(params=params, tool_rounds=1, actor="model")
    monkeypatch.setattr(round_execution, "_closed_admission_execution", lambda *_args: None)
    monkeypatch.setattr(round_execution, "_approval_description", lambda *_args: "gate_probe")
    monkeypatch.setattr(round_execution, "_owner_grant_key", lambda *_args: "gate_probe:mode=write")
    resolved = round_execution._resolve_tool_approval(request, 1, call, execution)
    assert resolved.result.error_code == "APPROVAL_REJECTED"
    assert events[0]["kind"] == "permission_requested" and probe.executed == []


def test_stream_request_options_have_fixed_small_signature():
    import inspect

    from agent_py_agent.agent.gateway_parts.stream_approval import StreamApproval

    assert len(inspect.signature(StreamApproval.request).parameters) <= 4
