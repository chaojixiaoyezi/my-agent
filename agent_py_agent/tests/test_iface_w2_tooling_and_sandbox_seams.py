"""IFACE-W2 的宿主接口回归；隔离临时目录，假传输不代表真实网络或平台验收。"""
from __future__ import annotations

import inspect
import io
import json
import subprocess
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from agent_py_agent.agent import plugin_environment_process, plugin_sandbox
from agent_py_agent.agent.agent_core.tool_guard import call_guardrail as guard
from agent_py_agent.agent.attempt import sandbox as attempt_sandbox
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.contracts.gates.models import GateFinding
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.agent.tooling import background_process_launch as background
from agent_py_agent.agent.tooling import (
    mcp_managed_process,
    shell,
    web_fetch_runtime,
    web_fetch_tools,
)
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.tooling.sandbox import SandboxReadiness
from agent_py_agent.agent.tooling.web import WebFetchTool as LegacyWebFetchTool
from agent_py_agent.agent.tooling.web_fetch_runtime import FetchRawRequest, RawResponseParts
from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
from agent_py_agent.tests._managed_process_harness import managed_request
from agent_py_agent.tests.test_tool_call_guardrail_runtime import (
    _agent,
    _canonical_call,
    _decision,
    _params,
)


@pytest.fixture(autouse=True)
def _pure_launcher_unit_boundary(monkeypatch):
    # 本文件的 launcher 全是假对象，不应登记进真实进程回收器。
    # 直接调用原启动实现仍经过全部产品准入；另硬断言任何真实 Popen 都不允许。
    original = inspect.unwrap(background.start_background_process)

    def unit_start(*args, **kwargs):
        return original(*args, **kwargs)

    for module in (background, shell, mcp_managed_process, plugin_environment_process):
        monkeypatch.setattr(module, "start_background_process", unit_start)
    monkeypatch.setattr(subprocess, "Popen", Mock(side_effect=AssertionError("纯接缝单测不得启动真实进程")))


class _Falsey:
    def __bool__(self):
        return False


def _observation_inputs(agent, params):
    call = _canonical_call(agent, params, {"tool": "write_file", "path": "out", "content": "same"})
    return call, ToolResult.succeeded(call, "same")


def test_observation_default_matches_explicit_existing_policy():
    params = _params(task_attributes={"repeated_success_hint_threshold": 2})
    implicit, explicit = _agent(), _agent()
    call, result = _observation_inputs(implicit, params)
    left, right = [], []
    for _ in range(6):
        left.append(guard.record_tool_guard_observation(implicit, params, call, result))
        right.append(guard.record_tool_guard_observation(explicit, params, call, result,
                                                       policy=guard.repeated_success_observation))
    assert left == right and [bool(value) for value in left] == [False, True] * 3
    assert guard.tool_guardrail_records(implicit) == guard.tool_guardrail_records(explicit)


def test_observation_custom_policy_gets_bounded_host_facts_and_only_soft_message():
    agent, params, seen = _agent(), _params(task_attributes={"repeated_success_hint_threshold": 7}), []
    call, result = _observation_inputs(agent, params)

    class Policy(_Falsey):
        def __call__(self, facts, records, *, threshold):
            seen.append((facts, records, threshold))
            return GateFinding("CUSTOM_OBSERVATION", message="hint" * 200)

    for _ in range(260):
        assert guard.record_tool_guard_observation(agent, params, call, result, policy=Policy()) == ("hint" * 200)[:500]
    assert len(guard.tool_guardrail_records(agent)) == 256
    assert all(facts.tool_name == "write_file" and facts.observed_success and threshold == 7
               for facts, _, threshold in seen)
    assert all(isinstance(records, tuple) and 1 <= len(records) <= 256 for _, records, _ in seen)
    assert _decision(agent, params, {"tool": "write_file", "path": "out", "content": "same"}).allowed


def test_observation_policy_cannot_override_outer_no_progress_gate():
    agent, params = _agent(), _params(task_attributes={"readonly_no_progress_threshold": 1})
    payload = {"tool": "tool_search"}
    call = _canonical_call(agent, params, payload)
    for _ in range(4):
        guard.record_tool_guard_observation(agent, params, call, ToolResult.succeeded(call, "same"),
                                            policy=lambda *args, **kwargs: None)
    decision = _decision(agent, params, payload)
    assert not decision.allowed and "TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED" in decision.reason_codes
    policy = Mock(side_effect=AssertionError("宿主门已有裁决，不得改走自定义观察"))
    assert guard.record_tool_guard_observation(agent, params, call, ToolResult.succeeded(call, "same"), policy=policy) == ""
    policy.assert_not_called()


def test_custom_observation_cannot_rewrite_host_guard_records():
    agent, params = _agent(), _params()
    call, result = _observation_inputs(agent, params)

    def policy(facts, records, *, threshold):
        for record in records:
            record.clear()
        return None

    guard.record_tool_guard_observation(agent, params, call, result, policy=policy)
    record, = guard.tool_guardrail_records(agent)
    assert record["tool_name"] == "write_file" and record["observed_success"] is True


def _web(tmp_path, raw_fetcher=None, *, resolver=None):
    base = LegacyWebFetchTool(max_chars=4000, artifact_root=tmp_path, cache_ttl_seconds=0,
                             resolver=resolver or (lambda host: ("8.8.8.8",)), timeout=5)
    return web_fetch_tools.WebFetchTool(web_fetch_tools.WebRuntimeDeps(
        max_chars=base.max_chars, timeout=base.timeout, resolver=base.resolver,
        normalize_url=base.normalize_url, response_preview_chars=base.response_preview_chars,
        network_safety_error=base.network_safety_error, format_http_error=base.format_http_error,
        artifact_root=tmp_path, cache_ttl_seconds=0, gateway_endpoint_config=base.gateway_endpoint_config,
        raw_fetcher=raw_fetcher,
    ))


@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("injected", [False, True])
def test_raw_fetcher_default_late_binding_and_injected_single_and_extract(tmp_path, monkeypatch, extract, injected):
    calls = []

    class Fetcher(_Falsey):
        def __call__(self, request, *, format_http_error, resolve_pin):
            pin = resolve_pin(request.url)
            assert pin.error is None and pin.ip == "8.8.8.8"
            calls.append(request)
            return RawResponseParts(200, {"Content-Type": "text/plain"}, b"host response", request.url)

    fetcher = Fetcher()
    tool = _web(tmp_path, fetcher if injected else None)
    monkeypatch.setattr(web_fetch_tools, "fetch_raw_response", Mock(side_effect=AssertionError("选错默认传输")) if injected else fetcher)
    params = {"url": "https://example.com/a", "method": "POST", "headers": {"X-Test": "yes"}, "body": "payload"}
    if extract:
        params.update(mode="extract", urls=[params["url"], "https://example.com/b"])
    result = tool.execute(params)
    assert result.ok, result.output
    assert len(calls) == (2 if extract else 1)
    assert all(request.method == "POST" and request.data == b"payload" and request.headers["X-Test"] == "yes" for request in calls)
    assert all(0 < request.timeout <= 5 and request.max_bytes == 1_000_000 for request in calls)
    assert "raw_fetcher" not in tool.model_spec.input_schema["properties"]


@pytest.mark.parametrize("extract", [False, True])
def test_raw_fetcher_cannot_run_before_initial_private_address_gate(tmp_path, extract):
    fetcher = Mock(side_effect=AssertionError("私网拒绝前不得进入传输"))
    tool = _web(tmp_path, fetcher, resolver=lambda host: ("10.0.0.7",))
    result = tool.execute({"urls": ["https://private.example/a"]} if extract else {"url": "https://private.example/a"})
    assert not result.ok
    assert "NETWORK_PRIVATE_IP_BLOCKED" in result.output or result.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
    fetcher.assert_not_called()


@pytest.mark.parametrize("extract", [False, True])
def test_injected_fetcher_keeps_per_hop_pin_and_blocks_redirect_to_private_ip(tmp_path, monkeypatch, extract):
    sends = []

    def send(hop):
        sends.append(hop)
        response = SimpleNamespace(status=302, headers={}, getheader=lambda name: "http://10.0.0.7/private")
        return SimpleNamespace(close=lambda: None), response

    def fetcher(request, **kwargs):
        return web_fetch_runtime.fetch_raw_response(request, **kwargs)

    monkeypatch.setattr(web_fetch_runtime, "_send_pinned", send)
    tool = _web(tmp_path, fetcher)
    result = tool.execute({"urls": ["https://example.com/a"]} if extract else {"url": "https://example.com/a"})
    assert not result.ok and len(sends) == 1
    assert sends[0].target.ip == "8.8.8.8"
    assert "NETWORK_PRIVATE_HOST_BLOCKED" in result.output or result.error_code == "NETWORK_PRIVATE_HOST_BLOCKED"


def test_injected_fetcher_preserves_authorized_origin_gate(tmp_path):
    seen = []

    def fetcher(request, *, resolve_pin, **kwargs):
        pin = resolve_pin("https://another.example/resource")
        seen.append(pin)
        return pin.error

    tool = _web(tmp_path, fetcher)
    request = FetchRawRequest("web_fetch", "https://example.com/a", "GET", {}, None, 5,
                              authorization_origin="https://example.com:443")
    # 使用凭据模块本身的规范 origin，不靠手写 URL 格式模拟授权。
    from agent_py_agent.agent.user_space.task_credentials import credential_origin

    request = replace(request, authorization_origin=credential_origin(request.url))
    result = tool._raw_response(request, format_http_error=tool.format_http_error, resolve_pin=tool._resolve_pin)
    assert result.error_code == "TOOL_PERMISSION_DENIED" and seen[0].ip is None


@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("failure", ["oversized", "timeout", "cancelled", "unsafe_final"])
def test_injected_raw_output_cannot_bypass_host_limits(tmp_path, monkeypatch, extract, failure):
    token, clock = CancellationToken(), {"now": 100.0}

    def fetcher(request, **kwargs):
        if failure == "timeout":
            clock["now"] += 10
        if failure == "cancelled":
            token.cancel()
        return RawResponseParts(200, {"Content-Type": "text/plain"},
                                b"x" * (request.max_bytes + 1) if failure == "oversized" else b"fixture",
                                "http://10.0.0.7/private" if failure == "unsafe_final" else request.url)

    monkeypatch.setattr(web_fetch_tools, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
    tool = _web(tmp_path, fetcher)
    with bind_cancellation_token(token):
        result = tool.execute({"urls": ["https://example.com/a"]} if extract else {"url": "https://example.com/a"})
    expected = {"oversized": "ARTIFACT_TOO_LARGE", "timeout": "TOOL_TIMEOUT", "cancelled": "CANCELLED",
                "unsafe_final": "NETWORK_PRIVATE_HOST_BLOCKED"}[failure]
    assert not result.ok and (result.error_code == expected or expected in result.output)
    assert not list(tmp_path.iterdir()), "失败响应不得归档为成功 artifact"


def test_raw_fetcher_cancelled_before_dispatch_is_not_called(tmp_path):
    fetcher, token = Mock(), CancellationToken()
    token.cancel()
    with bind_cancellation_token(token):
        result = _web(tmp_path, fetcher).execute({"url": "https://example.com/a"})
    assert result.error_code == "CANCELLED"
    fetcher.assert_not_called()


def test_raw_fetcher_preserves_host_secret_redaction(tmp_path):
    tool = _web(tmp_path, lambda request, **kwargs: RawResponseParts(
        200, {"Content-Type": "text/plain"}, b"synthetic-credential", request.url))
    tool.credential_resolver = lambda ref, url: ({"Authorization": "synthetic-credential"}, ("synthetic-credential",))
    result = tool.execute({"url": "https://example.com/a", "credential_ref": "fixture-reference"})
    assert result.ok and "[private]" in result.output and "synthetic-credential" not in result.output


def test_injected_fetcher_rechecks_dns_when_redirect_returns_to_initial_url(tmp_path, monkeypatch):
    answers = iter((("8.8.8.8",), ("10.0.0.7",)))
    sends = []

    def send(hop):
        sends.append(hop)
        response = SimpleNamespace(status=302, headers={}, getheader=lambda name: hop.url)
        return SimpleNamespace(close=lambda: None), response

    monkeypatch.setattr(web_fetch_runtime, "_send_pinned", send)
    tool = _web(tmp_path, web_fetch_runtime.fetch_raw_response, resolver=lambda host: next(answers))
    result = tool.execute({"url": "https://example.com/a"})
    assert not result.ok and result.error_code == "NETWORK_DNS_REBINDING_BLOCKED"
    assert len(sends) == 1


def test_injected_fetcher_does_not_dispatch_after_dns_uses_deadline(tmp_path, monkeypatch):
    clock = {"now": 10.0}
    fetcher = Mock()

    def resolver(host):
        clock["now"] += 6
        return ("8.8.8.8",)

    monkeypatch.setattr(web_fetch_tools, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
    result = _web(tmp_path, fetcher, resolver=resolver).execute({"url": "https://example.com/a"})
    assert not result.ok and result.error_code == "TOOL_TIMEOUT"
    fetcher.assert_not_called()


class _SandboxFactory(_Falsey):
    def __init__(self, *, ready=True, platform_name="Test"):
        self.ready, self.platform_name, self.instances, self.events = ready, platform_name, [], []

    def __call__(self, spec):
        factory = self

        class Backend:
            _ready = None

            def probe(self, *, binary_only=False):
                factory.events.append((self, "probe", binary_only))
                return SandboxReadiness(factory.ready, "FIXTURE", "isolated fixture")

            def require_ready(self):
                if self._ready is None:
                    self._ready = self.probe()
                report = self._ready
                if not report.ready:
                    raise attempt_sandbox.SandboxUnavailableError("SANDBOX_UNAVAILABLE: fixture")
                return report

            def build_argv(self, command_argv):
                self.require_ready()
                factory.events.append((self, "build", tuple(command_argv)))
                if factory.platform_name == "Linux":
                    return ["fixture-sandbox", "--chdir", str(spec.attempt_view), "--", *command_argv]
                if factory.platform_name == "Darwin":
                    return ["fixture-sandbox", "-p", "fixture-policy", "--", *command_argv]
                return ["fixture-sandbox", "--", *command_argv]

        backend = Backend()
        self.instances.append((spec, backend))
        return backend


class _Executor(_Falsey):
    def __init__(self, *, code=0, error=None):
        self.code, self.error, self.requests = code, error, []

    def run(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(request.command, self.code, "fixture-stdout\n", "fixture-stderr")


@pytest.mark.parametrize("code", [0, 9])
def test_sync_executor_receives_host_sandbox_and_scrubbed_environment(tmp_path, monkeypatch, code):
    factory, executor = _SandboxFactory(), _Executor(code=code)
    monkeypatch.setenv("IFACE_HOST_PRIVATE", "fixture-only")
    tool = shell.ShellTool(tmp_path, executor=executor, sandbox_factory=factory,
                           options=shell.ShellToolOptions(host_credential_env_names=frozenset({"IFACE_HOST_PRIVATE"})))
    result = tool.execute({"command": "echo fixture", "timeout": 11, "__sandbox_write_roots": [str(tmp_path)]})
    assert result.ok is (code == 0)
    assert result.error_code == ("" if code == 0 else "COMMAND_FAILED")
    assert result.result_envelope["process"]["return_code"] == code
    assert result.result_envelope["display"]["stdout"] == "fixture-stdout\n"
    assert len(executor.requests) == len(factory.instances) == 1
    request, (spec, backend) = executor.requests[0], factory.instances[0]
    assert isinstance(request, shell.ShellCommandRequest)
    assert request.cwd == tmp_path and request.timeout == 11 and not request.shell
    assert request.argv[:2] == ["fixture-sandbox", "--"]
    assert request.argv[-1] == "echo fixture" and "IFACE_HOST_PRIVATE" not in request.env
    assert spec.extra_write_roots == (tmp_path,) and not spec.implicit_attempt_write_roots
    assert factory.events[0] == (backend, "probe", False) and factory.events[1][0] is backend
    assert {"executor", "sandbox_factory", "launcher"}.isdisjoint(tool.model_spec.input_schema["properties"])
    assert {"executor", "sandbox_factory", "launcher"}.isdisjoint(tool.runtime_policy.input_policy.internal_parameters)


def test_default_sync_execution_still_uses_existing_process_wait_chain(tmp_path, monkeypatch):
    factory, proc, seen = _SandboxFactory(), object(), []
    monkeypatch.setattr(attempt_sandbox, "AttemptExecutionSandbox", factory)
    tool = shell.ShellTool(tmp_path)
    assert tool.availability().available
    monkeypatch.setattr(shell, "_start_attempt_shell_process", lambda argv, use_shell, target, actual: seen.append(
        (argv, use_shell, target, actual)) or proc)
    monkeypatch.setattr(shell, "_communicate_process", lambda actual, **kw: (
        subprocess.CompletedProcess(kw["command"], 0, "default-output", "") if actual is proc else pytest.fail("进程身份漂移")))
    result = tool.execute({"command": "echo default"})
    assert result.ok and "default-output" in result.output
    assert len(factory.instances) == 2 and factory.events[0][1:] == ("probe", True)
    assert seen == [(["fixture-sandbox", "--", *factory.events[-1][2]], False, tmp_path, tool)]


@pytest.mark.parametrize("reason", ["dangerous", "unmanaged_background", "outside_owner", "deadline", "cancelled", "sandbox"])
def test_custom_executor_cannot_bypass_host_command_scope_or_readiness(tmp_path, monkeypatch, reason):
    owner, external = tmp_path / "owner", tmp_path / "external"
    owner.mkdir()
    external.mkdir()
    factory, executor, token = _SandboxFactory(ready=reason != "sandbox"), _Executor(), CancellationToken()
    tool = shell.ShellTool(owner, executor=executor, sandbox_factory=factory,
                           options=shell.ShellToolOptions(owner_scope_root=str(owner)))
    params = {"command": "echo allowed"}
    if reason == "dangerous":
        params["command"] = "rm -rf /"
    if reason == "unmanaged_background":
        params["command"] += " &"
    if reason == "outside_owner":
        params.update(working_dir=str(external), __access_mode="full-access")
    if reason == "deadline":
        monkeypatch.setenv("MY_AGENT_TOOL_DEADLINE_UNIX", "1")
    if reason == "cancelled":
        token.cancel()
    with bind_cancellation_token(token):
        result = tool.execute(params)
    assert not result.ok and executor.requests == []
    if reason == "sandbox":
        assert result.error_code == "SANDBOX_UNAVAILABLE" and result.effect_outcome == "not_started"
    if reason not in {"sandbox", "cancelled"}:
        assert factory.instances == []


@pytest.mark.parametrize("error,expected,effect", [
    (OSError("fixture execution"), "TOOL_OPERATION_OUTCOME_UNKNOWN", "unknown"),
    (subprocess.TimeoutExpired("fixture", 1), "TOOL_TIMEOUT", "unknown"),
    (shell.CommandInterruptedError("fixture"), "CANCELLED", ""),
])
def test_custom_executor_errors_keep_existing_host_verdicts(tmp_path, error, expected, effect):
    tool = shell.ShellTool(tmp_path, executor=_Executor(error=error), sandbox_factory=_SandboxFactory())
    result = tool.execute({"command": "echo fixture"})
    assert not result.ok and (result.error_code, result.effect_outcome) == (expected, effect)


@pytest.mark.parametrize("platform_name", ["Linux", "Darwin"])
@pytest.mark.parametrize("injected", [False, True])
def test_plugin_sandbox_factory_keeps_same_backend_and_host_overlays(tmp_path, monkeypatch, platform_name, injected):
    factory = _SandboxFactory(platform_name=platform_name)
    hidden = tmp_path / "private"
    hidden.mkdir()
    spec = attempt_sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path, hidden_paths=(hidden,))
    monkeypatch.setattr(plugin_sandbox, "platform", SimpleNamespace(system=lambda: platform_name))
    if not injected:
        monkeypatch.setattr(plugin_sandbox, "AttemptExecutionSandbox", factory)
    command = plugin_sandbox.sandboxed_plugin_argv(["host-program", "arg"], spec,
                                                  **({"sandbox_factory": factory} if injected else {}))
    assert command[-3:] == ["--", "host-program", "arg"]
    assert len(factory.instances) == 1 and factory.instances[0][0] is spec
    backend = factory.instances[0][1]
    assert factory.events == [(backend, "probe", False), (backend, "build", ("host-program", "arg"))]
    if platform_name == "Linux":
        assert command[1:5] == ["--tmpfs", str(hidden), "--remount-ro", str(hidden)]
    else:
        assert "deny process-exec" in command[2] and str(hidden) in command[2]


def test_sandbox_factory_preflight_and_execution_fail_closed(tmp_path, monkeypatch):
    factory = _SandboxFactory(ready=False)
    monkeypatch.setattr(plugin_sandbox, "platform", SimpleNamespace(system=lambda: "Test"))
    assert plugin_sandbox.plugin_sandbox_problem(False, tmp_path, sandbox_factory=factory) == ""
    assert not factory.instances
    assert plugin_sandbox.plugin_sandbox_problem(True, tmp_path, sandbox_factory=factory) == "sandbox_unavailable"
    spec = attempt_sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path)
    with pytest.raises(attempt_sandbox.SandboxUnavailableError):
        plugin_sandbox.sandboxed_plugin_argv(["host-program"], spec, sandbox_factory=factory)
    assert all(event[1] == "probe" for event in factory.events)


@pytest.mark.parametrize("entry", ["shell", "plugin"])
def test_host_requires_ready_even_when_custom_backend_build_does_not(tmp_path, entry):
    build = Mock(return_value=["fixture-sandbox", "--", "host-program"])
    backend = SimpleNamespace(
        probe=Mock(return_value=SandboxReadiness(False, "FIXTURE", "unavailable")),
        require_ready=Mock(side_effect=attempt_sandbox.SandboxUnavailableError("fixture unavailable")),
        build_argv=build,
    )
    factory = Mock(return_value=backend)
    if entry == "shell":
        executor = _Executor()
        result = shell.ShellTool(tmp_path, executor=executor, sandbox_factory=factory).execute({"command": "echo fixture"})
        assert not result.ok and result.error_code == "SANDBOX_UNAVAILABLE" and not executor.requests
    else:
        spec = attempt_sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path)
        with pytest.raises(attempt_sandbox.SandboxUnavailableError):
            plugin_sandbox.sandboxed_plugin_argv(["host-program"], spec, sandbox_factory=factory)
    backend.require_ready.assert_called_once_with()
    build.assert_not_called()


@pytest.mark.parametrize("confirmed", [False, True])
def test_injected_executor_timeout_requires_original_cleanup_proof(tmp_path, confirmed):
    receipt = ProcessTerminationReceipt("fixture", confirmed, -15, 1)
    error = shell.CommandTimeoutError("fixture", 1, "partial-output", "", receipt, confirmed)
    tool = shell.ShellTool(tmp_path, executor=_Executor(error=error), sandbox_factory=_SandboxFactory())
    result = tool.execute({"command": "echo fixture"})
    assert result.error_code == "TOOL_TIMEOUT" and "partial-output" in result.output
    assert result.effect_outcome == ("failed" if confirmed else "unknown")
    assert result.result_envelope["process"]["termination"]["confirmed"] is confirmed


def _hosted_fixture(tmp_path, process=None):
    # 纯托管句柄替身，不创建进程或伪造出生身份；生命周期实测由相邻原生测试负责。
    return background.HostedBackgroundProcess(
        process if process is not None else SimpleNamespace(wait=Mock(return_value=0)),
        {"session_id": "bg-fixture", "pid": 0, "started_at": 0, "status": "exited", "exit_code": 0},
        tmp_path / "store",
    )


class _Launcher(_Falsey):
    def __init__(self, hosted):
        self.hosted, self.calls = hosted, []

    def __call__(self, request, *, startup_timeout_seconds=3.0):
        self.calls.append((request, startup_timeout_seconds))
        self.hosted = replace(self.hosted, store_root=request.store_root)
        return self.hosted


def test_background_launcher_gets_same_validated_request_without_shadow_store(tmp_path):
    hosted, authorized = _hosted_fixture(tmp_path), Mock()
    launcher = _Launcher(hosted)
    request = managed_request(tmp_path, authority_check=authorized, listen_scope="loopback", deadline_monotonic=time.monotonic() + 10)
    assert background.start_background_process(request, startup_timeout_seconds=1.5, launcher=launcher) is launcher.hosted
    assert launcher.calls == [(request, 1.5)]
    assert authorized.call_args_list == [call()] * 2
    assert not request.store_root.exists()


@pytest.mark.parametrize("reason", ["argv", "authority", "deadline", "cancelled", "owner_conflict"])
def test_background_launcher_cannot_bypass_admission_or_scope_validation(tmp_path, reason):
    launcher, token = _Launcher(object()), CancellationToken()

    def refuse():
        raise PermissionError("fixture authority revoked")

    if reason == "owner_conflict":
        with pytest.raises(ValueError, match="conflict"):
            managed_request(tmp_path, execution_scope=background.ProcessExecutionScope(owner_home=str(tmp_path / "other")))
    else:
        request = managed_request(tmp_path, argv=[] if reason == "argv" else ["host-program"],
                                  authority_check=refuse if reason == "authority" else None,
                                  deadline_monotonic=time.monotonic() - 1 if reason == "deadline" else 0)
        if reason == "cancelled":
            token.cancel()
        with bind_cancellation_token(token), pytest.raises((ValueError, PermissionError, TimeoutError, ToolCancelled)):
            background.start_background_process(request, launcher=launcher)
        assert not request.store_root.exists()
    assert not launcher.calls


def test_shell_background_wires_launcher_after_sandbox_and_keeps_scopes(tmp_path, monkeypatch):
    factory, authorize = _SandboxFactory(), Mock()
    hosted = _hosted_fixture(tmp_path)
    launcher = _Launcher(hosted)
    attached = SimpleNamespace(pid=41, child_pid=42, session_id="bg-fixture")
    attach = Mock(return_value=attached)
    monkeypatch.setattr(shell, "process_registry", SimpleNamespace(attach=attach))
    monkeypatch.setattr(shell, "_record_background_job", Mock())
    monkeypatch.setattr(shell, "_background_start_outcome", lambda **kw: ToolHandlerOutcome("run_command", True, "fixture-handoff"))
    tool = shell.ShellTool(tmp_path, sandbox_factory=factory, launcher=launcher)
    result = tool.execute({"command": "echo fixture", "run_in_background": True,
                           "__run_scope": {"owner_home": str(tmp_path), "run_id": "run-fixture", "thread_id": "thread-fixture"}},
                          context=SimpleNamespace(execution_authority_check=authorize))
    assert result.ok and result.output == "fixture-handoff" and len(launcher.calls) == 1
    request, timeout = launcher.calls[0]
    assert request.argv[:2] == ["fixture-sandbox", "--"] and timeout == 3.0
    assert request.access_scope.owner_home == request.execution_scope.owner_home == str(tmp_path)
    assert request.execution_scope.run_id == "run-fixture" and request.authority_check is authorize
    assert request.listen_scope == "loopback" and request.listen_scope_enforce
    attach.assert_called_once_with(launcher.hosted.record, launcher.hosted.process, launcher.hosted.store_root)
    assert authorize.call_args_list == [call()] * 2


@pytest.mark.parametrize("injected", [False, True])
def test_mcp_launcher_preserves_activation_stdio_and_timeout(tmp_path, monkeypatch, injected):
    owner = resolve_owner_home(tmp_path)
    activation = PluginActivationRef.from_owner(owner, "fixture-plugin", "a" * 64)
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(b"out"), stderr=io.BytesIO(b"err"))
    hosted = _hosted_fixture(tmp_path, process)
    launcher, checks = _Launcher(hosted), []
    monkeypatch.setattr(PluginActivationRef, "require", lambda self, **kw: checks.append((self, kw)))
    if not injected:
        monkeypatch.setattr(mcp_managed_process, "start_background_process", launcher)
    result = mcp_managed_process.ManagedMCPProcess.launch(activation, argv=["host-program"], cwd=tmp_path,
        environment={"FIXTURE": "value"}, timeout=1.25, **({"launcher": launcher} if injected else {}))
    try:
        assert result.hosted is launcher.hosted and result.activation is activation
        assert isinstance(process.stdout, io.TextIOWrapper) and process.stdout.read() == "out"
        request, timeout = launcher.calls[0]
        assert timeout == 1.25 and request.activation is activation
        assert request.io_mode == "stdio" and request.stop_on_launcher_exit and request.log_path is None
        assert request.execution_scope.owner_home == request.access_scope.owner_home == str(owner.home_dir)
        assert request.env == {"FIXTURE": "value"}
        assert checks == ([(activation, {"allow_preparing": True})] * 2 if injected else [])
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def test_mcp_injected_launcher_cannot_bypass_revoked_activation(tmp_path):
    activation = PluginActivationRef.from_owner(resolve_owner_home(tmp_path), "missing-plugin", "b" * 64)
    launcher = _Launcher(object())
    with pytest.raises((ValueError, PermissionError)):
        mcp_managed_process.ManagedMCPProcess.launch(activation, argv=["host-program"], cwd=tmp_path,
                                                     environment={}, timeout=1, launcher=launcher)
    assert not launcher.calls


@pytest.mark.parametrize("injected", [False, True])
def test_environment_launcher_keeps_authority_deadline_capture_and_scopes(tmp_path, monkeypatch, injected):
    deadline = time.monotonic() + 20
    process = SimpleNamespace(wait=Mock(return_value=0))
    hosted = _hosted_fixture(tmp_path, process)
    launcher, authorize = _Launcher(hosted), Mock()

    def request(argv, cwd, environment, end, log_path):
        log_path.write_bytes(b"probe-result")
        return managed_request(tmp_path, argv=list(argv), cwd=cwd, env=environment, log_path=log_path,
            deadline_monotonic=end, authority_check=authorize, stop_on_launcher_exit=True, retain_until_consumed=True)

    operation = SimpleNamespace(authorize=authorize, launch_request=request)
    if not injected:
        monkeypatch.setattr(plugin_environment_process, "start_background_process", launcher)
    monkeypatch.setattr(plugin_environment_process, "_wait_for_preparation", lambda *args: {"status": "exited", "exit_code": 0})
    output = plugin_environment_process.run_environment_process(("host-program", "probe"), cwd=tmp_path,
        environment={"FIXTURE": "value"}, deadline=deadline, operation=operation, stage="probe", capture=True,
        **({"launcher": launcher} if injected else {}))
    assert output == b"probe-result" and len(launcher.calls) == 1
    launched, timeout = launcher.calls[0]
    assert launched.deadline_monotonic == deadline and timeout == 3.0
    assert launched.retain_until_consumed and launched.stop_on_launcher_exit
    assert launched.authority_check is authorize and launched.env == {"FIXTURE": "value"}
    assert launched.execution_scope.owner_home == launched.access_scope.owner_home == str(tmp_path)
    assert authorize.call_count == (3 if injected else 1)
    process.wait.assert_called_once_with(timeout=2.0)


def test_environment_injected_launcher_never_runs_after_operation_revocation(tmp_path):
    launcher = _Launcher(object())
    operation = SimpleNamespace(authorize=Mock(side_effect=PermissionError("fixture revoked")))
    with pytest.raises(PermissionError):
        plugin_environment_process.run_environment_process(("host-program",), cwd=tmp_path, environment={},
            deadline=time.monotonic() + 10, operation=operation, stage="venv", launcher=launcher)
    assert not launcher.calls and not (tmp_path / "venv.log").exists()


@pytest.mark.parametrize("failure", ["cancelled", "deadline", "authority", "store"])
def test_launcher_handoff_rechecks_authority_and_cleans_original_request(tmp_path, monkeypatch, failure):
    hosted = _hosted_fixture(tmp_path)
    token, clock = CancellationToken(), {"now": 100.0}
    authorized = Mock(side_effect=[None, PermissionError("fixture revoked")]) if failure == "authority" else Mock()
    request = managed_request(tmp_path, authority_check=authorized, deadline_monotonic=105)
    cleanup = Mock(return_value=SimpleNamespace(record=hosted.record, confirmed=True))
    monkeypatch.setattr(background, "stop_process_session", cleanup)
    monkeypatch.setattr(background, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    def launcher(request, **kwargs):
        if failure == "cancelled":
            token.cancel()
        if failure == "deadline":
            clock["now"] = 106
        return replace(hosted, store_root=tmp_path / "foreign" if failure == "store" else request.store_root)

    with bind_cancellation_token(token), pytest.raises(background.BackgroundLaunchError) as error:
        background.start_background_process(request, launcher=launcher)
    assert error.value.cleanup_confirmed and error.value.record is hosted.record
    store, selected = cleanup.call_args.args
    assert store.root == request.store_root and selected is hosted.record
    assert cleanup.call_args.kwargs == {"host_process": hosted.process}


def test_opaque_background_exception_never_claims_not_started(tmp_path):
    def launcher(request, **kwargs):
        raise OSError("fixture after possible spawn")

    tool = shell.ShellTool(tmp_path, sandbox_factory=_SandboxFactory(), launcher=launcher)
    result = tool.execute({"command": "echo fixture", "run_in_background": True,
                           "__run_scope": {"owner_home": str(tmp_path), "run_id": "run-fixture"}})
    assert not result.ok and result.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"
    assert result.effect_outcome == "unknown" and result.result_envelope["process"]["status"] == "unknown"


@pytest.mark.parametrize("platform_name", ["Linux", "Darwin"])
def test_plugin_backend_with_incompatible_argv_fails_closed(tmp_path, monkeypatch, platform_name):
    monkeypatch.setattr(plugin_sandbox, "platform", SimpleNamespace(system=lambda: platform_name))
    spec = attempt_sandbox.AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path)
    with pytest.raises(attempt_sandbox.SandboxUnavailableError, match="BACKEND_ARGV_FORMAT_UNSUPPORTED"):
        plugin_sandbox.sandboxed_plugin_argv(["host-program"], spec, sandbox_factory=_SandboxFactory())


def test_default_background_still_uses_original_store_and_host_handoff(tmp_path, monkeypatch):
    request = managed_request(tmp_path, authority_check=Mock())
    process = SimpleNamespace(pid=4102)
    popen = Mock(return_value=process)
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(background, "capture_process_birth_token", lambda pid: f"fixture:{pid}")
    monkeypatch.setattr(background, "_process_instance_terminated", lambda *args: False)

    def observe(store, session_id, pid, birth, timeout):
        with store.transaction() as transaction:
            current = transaction.load(session_id)
            transaction.write({**current, "status": "running", "pid": pid, "pid_birth_token": birth,
                               "child_pid": 4103, "child_pid_birth_token": "fixture:4103",
                               "child_launch_started": True, "started_at": time.time()})

    monkeypatch.setattr(background, "_observe_startup", observe)
    hosted = background.start_background_process(request, launcher=None)
    assert hosted.process is process and hosted.store_root == request.store_root
    assert hosted.record["handoff_confirmed"] and hosted.record["status"] == "running"
    assert hosted.record["execution_scope"]["run_id"] == request.execution_scope.run_id
    assert background.ProcessSessionStore(request.store_root).load(hosted.record["session_id"]).record == hosted.record
    assert request.authority_check.call_count == 3
    popen.assert_called_once()
    assert popen.call_args.kwargs["env"] == request.env
    assert "agent_py_agent.agent.tooling.background_process_host" in popen.call_args.args[0]
    assert not list((request.store_root / ".launches").glob("*.json"))


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_rejected_stdio_handoff_closes_unclaimed_pipes_even_if_cleanup_fails(tmp_path, monkeypatch, cleanup_error):
    process = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO())
    hosted = _hosted_fixture(tmp_path, process)
    launcher = _Launcher(hosted)
    request = managed_request(tmp_path, io_mode="stdio", log_path=None, max_log_bytes=0,
                              stop_on_launcher_exit=True, authority_check=Mock(side_effect=[None, PermissionError("revoked")]))
    cleanup = Mock(side_effect=RuntimeError("fixture cleanup unknown")) if cleanup_error else Mock(
        return_value=SimpleNamespace(record=hosted.record, confirmed=True))
    monkeypatch.setattr(background, "stop_process_session", cleanup)
    with pytest.raises(background.BackgroundLaunchError) as error:
        background.start_background_process(request, launcher=launcher)
    assert error.value.cleanup_confirmed is (not cleanup_error)
    assert all(stream.closed for stream in (process.stdin, process.stdout, process.stderr))
    cleanup.assert_called_once()


@pytest.mark.parametrize("extract", [False, True])
@pytest.mark.parametrize("failure", ["deadline", "cancelled"])
def test_final_endpoint_check_cannot_publish_after_cancel_or_deadline(tmp_path, monkeypatch, extract, failure):
    clock, token = {"now": 100.0}, CancellationToken()

    def resolver(host):
        if host == "final.example":
            if failure == "deadline":
                clock["now"] += 10
            else:
                token.cancel()
        return ("8.8.8.8",)

    monkeypatch.setattr(web_fetch_tools, "time", SimpleNamespace(monotonic=lambda: clock["now"], time=time.time))
    tool = _web(tmp_path, lambda request, **kwargs: RawResponseParts(
        200, {"Content-Type": "text/plain"}, b"fixture", "https://final.example/resource"), resolver=resolver)
    with bind_cancellation_token(token):
        result = tool.execute({"urls": ["https://example.com/a"]} if extract else {"url": "https://example.com/a"})
    expected = "TOOL_TIMEOUT" if failure == "deadline" else "CANCELLED"
    assert not result.ok and (result.error_code == expected or expected in result.output)
    assert not list(tmp_path.iterdir())
