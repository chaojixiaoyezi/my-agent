# LLM: 用临时宿主和真实运行/执行器验证两个接缝；注册状态逐测试恢复，不发网络请求。
# 模块用途: 锁定默认循环兼容性、可信驱动注入、后端注册与原权限边界。
from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import replace
from typing import get_type_hints

import pytest

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopRunResult
from agent_py_agent.agent.agent_core.runtime import loop_support
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeLoopParams
from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
from agent_py_agent.agent.backends.tool_ir import AssistantTurn
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.settings.model_provider_schema import (
    BACKENDS,
    ModelProfileError,
    validate_model,
)
from agent_py_agent.tests._tool_runtime_harness import canonical_test_call


# LLM: 模型显式注入为本地 echo；全部存储只落 pytest 临时目录。
# 函数用途: 构造能走原 Agent.run 生命周期的离线宿主。
@pytest.fixture
def host(tmp_path):
    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path / "workspace")
    agent.backend = EchoBackend()
    return agent


# LLM: 运行参数保留真实快照和原身份字段；不替换权限或历史装配函数。
# 函数用途: 给循环入口提供本次运行的最小合法输入。
def runtime_params(host, **changes):
    params = RuntimeLoopParams("离线核对", "离线核对", [], [], None, "",
        run_id="iface-run", task_id="iface-task", request_id="iface-request", attempt_id="iface-attempt",
        tool_runtime_snapshot=host.tools.runtime_snapshot(run_id="iface-run"),
        tool_protocol_snapshot=host.backend.probe_tool_capability())
    from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

    params.tool_protocol_snapshot = make_test_protocol_snapshot(run_id=params.run_id)
    return replace(params, **changes)


# LLM: 未实现的公开接缝必须先断言失败，不把导入错误当成测试证据。
# 函数用途: 确认注册表存在，并逐用例隔离进程内注册状态。
@pytest.fixture
def registry(monkeypatch):
    name = "agent_py_agent.agent.backends.registry"
    assert importlib.util.find_spec(name) is not None, "backend registry seam is missing"
    module = importlib.import_module(name)
    monkeypatch.setattr(module, "_BACKENDS", {})
    return module


# LLM: 协议仅供宿主类型检查，不添加可序列化的模型参数。
# 函数用途: 检查驱动接缝的结构化调用合同。
def test_driver_protocol_contract():
    assert hasattr(loop_support, "ToolLoopDriver"), "tool loop driver protocol is missing"
    protocol = loop_support.ToolLoopDriver
    assert protocol._is_protocol
    assert list(inspect.signature(protocol.__call__).parameters) == ["self", "agent", "params"]
    assert get_type_hints(protocol.__call__) == {
        "agent": object, "params": ToolLoopExecuteParams, "return": ToolLoopRunResult,
    }


# LLM: 未注入与显式 None 都走同一原循环；只记录入口，不替代实际生成及收尾。
# 函数用途: 证明旧调用方的默认行为不变。
@pytest.mark.parametrize("explicit_none", [False, True])
def test_default_driver_runs_original_agent_lifecycle(host, monkeypatch, explicit_none):
    original, calls = loop_support.execute_tool_loop, []

    # LLM: 包装仍调用真实循环，不伪造业务结果。
    # 函数用途: 记录默认路径接收到的参数。
    def execute(agent, params):
        calls.append((agent, params))
        return original(agent, params)

    monkeypatch.setattr(loop_support, "execute_tool_loop", execute)
    if explicit_none:
        host._loop_driver = None
    result = host.run("默认接缝", save=False, resume_context=False, allowed_tools=[])
    assert len(calls) == 1 and calls[0][0] is host
    assert calls[0][1].allowed_tools == []
    assert "默认接缝" in result.response and result.runtime_status == "ok"


# LLM: 假值 callable 仍是显式注入；不能用布尔 or 错退回默认循环。
# 类用途: 实现可信宿主驱动并保留原循环与参数身份。
class RecordingDriver:
    # LLM: 仅持有原循环引用与本测试观察列表。
    # 函数用途: 初始化可调用的结构化驱动。
    def __init__(self, execute, *, truthy=True):
        self.execute, self.truthy, self.calls = execute, truthy, []

    # LLM: 布尔值与可调用合同无关，不承担注册或权限判断。
    # 函数用途: 覆盖假值驱动的边界。
    def __bool__(self):
        return self.truthy

    # LLM: 原参数不复制或改写，实际运行仍由原工具循环完成。
    # 函数用途: 记录一次宿主调用并委托执行。
    def __call__(self, agent: object, params: ToolLoopExecuteParams) -> ToolLoopRunResult:
        self.calls.append((agent, params))
        return self.execute(agent, params)


# LLM: 新驱动必须由宿主属性选择；默认入口若被误用立即失败。
# 函数用途: 验证函数对象之外的协议实现及原 attempt 正常结算。
@pytest.mark.parametrize("truthy", [False, True])
def test_custom_driver_preserves_original_loop_and_attempt_lifecycle(host, monkeypatch, truthy):
    driver = RecordingDriver(loop_support.execute_tool_loop, truthy=truthy)
    host._loop_driver = driver
    monkeypatch.setattr(loop_support, "execute_tool_loop", lambda *_: pytest.fail("default driver used"))
    previous = getattr(host, "_current_run_params", None)
    result = host.run("注入接缝", save=False, resume_context=False, allowed_tools=[])
    assert len(driver.calls) == 1 and driver.calls[0][0] is host
    params = driver.calls[0][1]
    assert params.allowed_tools == [] and params.attempt_id
    assert "注入接缝" in result.response and result.runtime_status == "ok"
    assert getattr(host, "_current_run_params", None) is previous
    repo = host.subagents.runtime_db
    attempt = repo.get_attempt(params.attempt_id)
    assert attempt["status"] == "done"
    assert not repo.has_active_exec_lock(attempt["agent_run_id"])


# LLM: 驱动可交回原 replace 合同的最终参数；外层必须消费它而非旧输入。
# 函数用途: 验证工具账、轮数、插话与原生历史仍从最终参数返回。
def test_custom_driver_final_params_are_used_for_settlement(host):
    seen, response = [], ModelResponse("最终响应", "test")

    # LLM: 只构造无副作用的结果，替换后的账本内容可被精确观察。
    # 函数用途: 返回带最终参数的自定义循环结果。
    def driver(agent, params):
        seen.append(params)
        final = replace(params, executed_tools=["read_file"], archive_tool_calls=[{"marker": "final"}],
            active_turn_user_inputs=[{"text": "补充"}], tool_ir_history=[AssistantTurn(text="已有历史")])
        return ToolLoopRunResult("最终提示", response, 3, final)

    host._loop_driver = driver
    result = loop_support._execute_runtime_loop(host, runtime_params(host, allowed_tools=["read_file"]))
    assert len(seen) == 1 and seen[0].allowed_tools == ["read_file"]
    assert (result.final_prompt, result.final_response, result.tool_rounds) == ("最终提示", response, 3)
    assert result.executed_tools == ["read_file"]
    assert result.archive_tool_calls == [{"marker": "final"}]
    assert result.active_turn_user_inputs == [{"text": "补充"}]
    assert "已有历史" in json.dumps(result.canonical_native_messages, ensure_ascii=False)
    assert "最终响应" in json.dumps(result.canonical_native_messages, ensure_ascii=False)


# LLM: 自定义驱动的失败与中断仍走原 partial-turn 回调，且原异常身份不被改写。
# 函数用途: 检验失败和取消情况下的历史保存。
@pytest.mark.parametrize("error_type,status", [(RuntimeError, "failed"), (InterruptedError, "cancelled"),
    (KeyboardInterrupt, "cancelled")])
def test_custom_driver_keeps_partial_history_and_reraises(host, error_type, status):
    saved, error = [], error_type("synthetic driver failure")

    # LLM: 模拟发生后的原生历史，不执行工具或写文件。
    # 函数用途: 在精确异常前留下可持久化的事实。
    def driver(agent, params):
        params.tool_ir_history.append(AssistantTurn(text="中途事实"))
        params.executed_tools.append("read_file")
        params.archive_tool_calls.append({"marker": "partial"})
        raise error

    host._loop_driver = driver
    with pytest.raises(error_type) as caught:
        loop_support._execute_runtime_loop(host, runtime_params(host, partial_turn_callback=saved.append))
    assert caught.value is error and len(saved) == 1
    assert saved[0].runtime_status == status
    assert saved[0].executed_tools == ["read_file"]
    assert saved[0].archive_tool_calls == [{"marker": "partial"}]
    assert "中途事实" in json.dumps(saved[0].canonical_native_messages, ensure_ascii=False)


# LLM: 原 Agent.run 负责异常结算；原生历史把 KeyboardInterrupt 记取消，但原外层 attempt 记 failed，本接缝不改这项旧合同。
# 函数用途: 检验失败、取消和键盘中断仍结束准确的运行代次并恢复参数作用域。
@pytest.mark.parametrize("error_type,status", [(RuntimeError, "failed"), (InterruptedError, "cancelled"),
    (KeyboardInterrupt, "failed")])
@pytest.mark.parametrize("injected", [False, True])
def test_custom_driver_error_settles_original_attempt(host, monkeypatch, error_type, status, injected):
    seen, error = [], error_type("driver lifecycle failure")
    previous = getattr(host, "_current_run_params", None)

    # LLM: 只记录宿主签发的运行身份，异常原对象必须到达外层。
    # 函数用途: 从自定义驱动抛出一次可精确核对的失败。
    def driver(agent, params):
        seen.append(params)
        raise error

    if injected:
        host._loop_driver = driver
    else:
        monkeypatch.setattr(loop_support, "execute_tool_loop", driver)
    with pytest.raises(error_type) as caught:
        host.run("异常结算", allowed_tools=[], save=False, resume_context=False)
    assert caught.value is error and len(seen) == 1
    attempt = host.subagents.runtime_db.get_attempt(seen[0].attempt_id)
    assert attempt["status"] == status
    assert not host.subagents.runtime_db.has_active_exec_lock(attempt["agent_run_id"])
    assert getattr(host, "_current_run_params", None) is previous


# LLM: 注入接缝位于原快照检查之后，不让无效快照触达驱动。
# 函数用途: 验证原宿主前置拒绝不变。
@pytest.mark.parametrize("field", ["tool_runtime_snapshot", "tool_protocol_snapshot"])
def test_custom_driver_cannot_skip_snapshot_validation(host, field):
    calls = []
    host._loop_driver = lambda *args: calls.append(args)
    with pytest.raises(RuntimeError, match="snapshot is missing"):
        loop_support._execute_runtime_loop(host, runtime_params(host, **{field: None}))
    assert calls == []


# LLM: 可信驱动使用原执行器和本次快照；不能通过更换循环扩大工具授权或路径范围。
# 函数用途: 在真实 Agent.run 绑定下检查 allowlist、owner 路径和模型额外参数门。
@pytest.mark.parametrize("case,code", [("allowlist", "TOOL_NOT_IN_RUNTIME_SNAPSHOT"),
    ("owner_path", "PATH_OWNER_SCOPE_BLOCKED"), ("injection", "TOOL_INVALID_ARGUMENTS")])
def test_injected_driver_keeps_tool_permission_and_scope_gates(host, tmp_path, case, code):
    observed = []
    outside = tmp_path / "foreign" / "private.txt"
    outside.parent.mkdir()
    outside.write_text("foreign-private-marker", encoding="utf-8")
    allowed = [] if case == "allowlist" else ["read_file"]

    # LLM: 原 ToolCall、快照、write_boundary 和 attempt 逐项透传；不直接调用工具 handler。
    # 函数用途: 尝试一个应被原执行器拒绝的调用。
    def driver(agent, params):
        arguments = {"path": str(outside)}
        if case == "injection":
            arguments["_loop_driver"] = "untrusted"
        call = replace(canonical_test_call(params.tool_runtime_snapshot, "read_file", arguments),
            attempt_id=params.attempt_id)
        result = agent.tools.execute_tool(call, runtime_snapshot=params.tool_runtime_snapshot,
            write_boundary=params.write_boundary).result
        observed.append(result)
        return ToolLoopRunResult("权限核对", ModelResponse("已拒绝", "test"), 1, params)

    host._loop_driver = driver
    result = host.run("权限核对", allowed_tools=allowed, save=False, resume_context=False,
        write_boundary={"effective_owner_scope_root": str(host.root),
            "execution_workspace_roots": [str(host.root)], "execution_cwd": str(host.root)})
    assert result.response == "已拒绝" and len(observed) == 1
    assert not observed[0].ok and not observed[0].handler_executed
    assert observed[0].error_code == code
    assert "foreign-private-marker" not in observed[0].output


# LLM: 同一原执行器也必须允许有效权限，不能以全部拒绝伪装为边界通过；只读本测试临时文件。
# 函数用途: 证明自定义驱动能用宿主授权读取工作区内数据。
def test_injected_driver_can_read_authorized_path(host):
    target = host.root / "authorized.txt"
    target.write_text("authorized-content-marker", encoding="utf-8")
    observed = []

    # LLM: 逐项透传原快照、attempt 和边界，不绕过 handler 前置校验。
    # 函数用途: 沿正式执行器读取一个已授权的临时文件。
    def driver(agent, params):
        call = replace(canonical_test_call(params.tool_runtime_snapshot, "read_file", {"path": str(target)}),
            attempt_id=params.attempt_id)
        observed.append(agent.tools.execute_tool(call, runtime_snapshot=params.tool_runtime_snapshot,
            write_boundary=params.write_boundary).result)
        return ToolLoopRunResult("授权核对", ModelResponse("已读取", "test"), 1, params)

    host._loop_driver = driver
    host.run("授权核对", allowed_tools=["read_file"], save=False, resume_context=False,
        write_boundary={"effective_owner_scope_root": str(host.root),
            "execution_workspace_roots": [str(host.root)], "execution_cwd": str(host.root)})
    assert len(observed) == 1 and observed[0].ok and observed[0].handler_executed
    assert "authorized-content-marker" in observed[0].output


# LLM: 模型只见原工具 JSON 合同，不得出现宿主 callable 注入字段。
# 函数用途: 检验工具输入面未扩大。
def test_driver_and_registry_are_not_exposed_in_tool_json(host):
    schemas = json.dumps([spec.input_schema for spec in host.tools.specs(include_orchestration=True)])
    for field in ("_loop_driver", "loop_driver", "backend_constructor", "register_backend"):
        assert f'"{field}"' not in schemas


# LLM: 注册表不执行 constructor，不隐式导入插件；返回冻结的名称快照。
# 函数用途: 验证空表、精确查找、构造器身份及不可变快照。
def test_registry_lookup_is_lazy_exact_and_names_are_frozen(registry):
    calls = []

    # LLM: 构造仅在测试显式调用解析结果后发生。
    # 函数用途: 返回本地后端并记录传入配置身份。
    def constructor(config):
        calls.append(config)
        return EchoBackend()

    assert registry.registered_backend_names() == frozenset()
    assert registry.resolve_registered_backend("missing") is None
    registry.register_backend("custom_protocol", constructor)
    snapshot = registry.registered_backend_names()
    assert type(snapshot) is frozenset and snapshot == frozenset({"custom_protocol"})
    assert registry.resolve_registered_backend("custom_protocol") is constructor
    assert registry.resolve_registered_backend("CUSTOM_PROTOCOL") is None and calls == []
    config = object()
    assert isinstance(registry.resolve_registered_backend("custom_protocol")(config), EchoBackend)
    assert calls == [config]
    registry.register_backend("other_protocol", EchoBackend)
    assert snapshot == frozenset({"custom_protocol"})
    assert registry.registered_backend_names() == frozenset({"custom_protocol", "other_protocol"})


# LLM: 宿主可显式重新登记同名适配器；旧名称快照不持有可变构造器表。
# 函数用途: 锁定进程内重登记的确定性行为。
def test_registry_reregistration_replaces_constructor(registry):
    registry.register_backend("custom_protocol", lambda config: EchoBackend())

    # LLM: 替换构造器与旧构造器身份不同，登记时不得提前执行。
    # 函数用途: 提供同名登记的新构造器。
    def replacement(config):
        return EchoBackend()

    registry.register_backend("custom_protocol", replacement)
    assert registry.resolve_registered_backend("custom_protocol") is replacement
    assert registry.registered_backend_names() == frozenset({"custom_protocol"})


# LLM: 非法注册输入必须在写表前拒绝；不把数据当成可执行构造器。
# 函数用途: 检验名称和可调用对象的最小宿主合同。
@pytest.mark.parametrize("name,constructor,error", [("", EchoBackend, ValueError),
    ("   ", EchoBackend, ValueError), (None, EchoBackend, ValueError),
    ([], EchoBackend, ValueError), (" padded", EchoBackend, ValueError),
    ("two words", EchoBackend, ValueError), ("nul\x00", EchoBackend, ValueError),
    ("custom_protocol", None, TypeError)])
def test_registry_rejects_invalid_registration_atomically(registry, name, constructor, error):
    registry.register_backend("kept", EchoBackend)
    with pytest.raises(error):
        registry.register_backend(name, constructor)
    assert registry.registered_backend_names() == frozenset({"kept"})
    assert registry.resolve_registered_backend("kept") is EchoBackend


# LLM: 校验必须动态读名称，不能在导入时冻结；原决策用途和能力字段规则仍适用。
# 函数用途: 验证登记前拒绝、登记后接纳以及不放宽原模型校验。
def test_model_validation_accepts_only_registered_custom_protocol(registry):
    model = {"provider_id": "test-provider", "model_name": "custom-model", "model_backend": "custom_protocol"}
    builtins = BACKENDS.copy()
    with pytest.raises(ModelProfileError):
        validate_model(model)
    registry.register_backend("custom_protocol", lambda config: EchoBackend())
    assert builtins == BACKENDS and "custom_protocol" not in BACKENDS
    assert validate_model(model) == {**model, "model_context_window_tokens": None,
        "capability": "agentic", "enabled": True}
    for change in ({"capability": "decision"}, {"model_name": ""}, {"model_context_window_tokens": True},
        {"enabled": "yes"}, {"provider_id": "../escape"}, {"structured_output": "json_object"}):
        with pytest.raises(ModelProfileError):
            validate_model({**model, **change})


# LLM: 原内置枚举不被注册操作改写；TypeSafe 和 Echo 的既有用途约束照旧。
# 函数用途: 逐协议验证兼容性与未知/非字符串输入拒绝。
@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_builtin_model_validation_is_unchanged(backend):
    capability = "decision" if backend == "typesafe_decision" else "agentic"
    model = {"provider_id": "provider", "model_name": "model", "model_backend": backend, "capability": capability}
    assert validate_model(model)["model_backend"] == backend
    assert validate_model(model)["capability"] == capability


# LLM: 没有可信宿主登记时，持久字段不能扩大可接受协议集合。
# 函数用途: 保留未知和错误类型的协议输入防线。
@pytest.mark.parametrize("backend", ["unknown_protocol", "", None, [], 1])
def test_unregistered_or_malformed_protocol_is_rejected(backend):
    with pytest.raises(ModelProfileError):
        validate_model({"provider_id": "provider", "model_name": "model", "model_backend": backend})
