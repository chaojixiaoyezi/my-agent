"""逐次插件上下文的原 MCP/执行器组件测试；不代替实际 TUI、模型或样本功能验收。"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.plugin_runtime import PluginProxyTool, plugin_tool_name
from agent_py_agent.agent.tooling.mcp_client import MCPStdioClient
from agent_py_agent.agent.tooling.mcp_registration import MCPProxyTool, build_proxy_tool
from agent_py_agent.agent.tooling.models import BaseTool, ToolHandlerOutcome, ToolInvocationContext
from agent_py_agent.agent.workspace_read_context import WORKSPACE_READ_EXTENSION
from agent_py_agent.tests._tool_runtime_harness import (
    execute_canonical_test_call,
    make_test_model_spec,
    make_test_runtime_policy,
)
from agent_py_agent.tests.plugin_activation_fixtures import (
    _SERVER,
    installed_runtime_plugin,
    invoke_registered_tool,
    plugin_registry,
)
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _config
from agent_py_agent.tests.test_plugin_package import _manifest
from agent_py_agent.tests.test_workspace_read_context import read_context


# LLM: 替身只观察原 ToolExecutor 的注入值；不在测试端构造 ToolInvocationContext 或补业务结果。
# 类用途: 验证宿主的实际 cwd/exact/外部授权字段经过 registry 后保持原语义。
class ContextObserver(BaseTool):
    model_spec = make_test_model_spec("observe_context")
    runtime_policy = make_test_runtime_policy()

    # LLM: 原 executor 必须使用 scoped 入口；无上下文调用是测试失败。
    # 函数用途: 阻止测试误走旁路。
    def execute(self, params):
        raise AssertionError("必须使用原调用上下文")

    # LLM: 返回收到的宿主快照而不更改任何字段，供断言原生产组装链。
    # 函数用途: 观察实际执行器传入的读取上下文。
    def execute_scoped(self, params, context):
        return ToolHandlerOutcome(self.model_spec.name, True, json.dumps(context.workspace_read_context.to_payload()))


def test_registry_uses_effective_cwd_and_original_exact_external_grants(tmp_path):
    cwd, owner = tmp_path / "selected", tmp_path / "data/owners/local/main"
    tool = ContextObserver()
    execution = execute_canonical_test_call(tmp_path, tools={tool.model_spec.name: tool}, tool_name=tool.model_spec.name,
        arguments={}, owner_scope_root=str(owner), write_boundary={
            "execution_cwd": str(cwd), "read_scope_mode": "exact", "allowed_read_roots": ["one.txt"],
            "allowed_write_roots": [str(cwd)],
        })
    assert execution.result.ok, execution.result
    payload = json.loads(execution.result.output)
    assert payload["cwd"] == str(cwd.resolve())
    assert payload["read_roots"] == payload["granted_external_roots"] == [str((cwd / "one.txt").resolve())]
    assert payload["path_policy"]["owner_scope_root"] == str(owner.resolve())


# LLM: 服务只回传实际收到的 MCP 参数，测试端不替产品组装 metadata。
# 函数用途: 生成可核对 arguments 与 _meta 分离的标准输入输出组件。
def context_server(capability):
    return _ECHO_SERVER.replace('"capabilities": {"tools": {}}', f'"capabilities": {capability!r}').replace(
        'text = str(args.get("text", ""))', 'text = json.dumps({"arguments": args, "meta": params.get("_meta")})',
    )


# LLM: 代理固定测试 client 的原 transport，不借测试 helper 改变产品 capability 选择。
# 函数用途: 在原 MCP 目录基础上选择插件或普通代理。
def echo_proxy(client, transport, *, plugin=True):
    base = build_proxy_tool(client, "context", client.list_tools(transport=transport)[0], transport=transport)
    proxy_type = PluginProxyTool if plugin else MCPProxyTool
    return proxy_type(client, base.remote_tool, base.model_spec, base.runtime_policy, transport=transport)


def test_concurrent_calls_keep_cwd_arguments_and_shared_process_separate(tmp_path):
    capability = {"tools": {}, "experimental": {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}}}
    client = MCPStdioClient(_config(context_server(capability), cwd=str(tmp_path)))
    try:
        transport = client.start()
        proxy = echo_proxy(client, transport)
        def call(name):
            scope = ToolInvocationContext(None, workspace_read_context=read_context(tmp_path / name))
            result = proxy.execute_scoped({"text": name, "tool": proxy.model_spec.name}, scope)
            assert result.ok, result
            return json.loads(json.loads(result.output)["result"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(call, ("first", "第二个")))
        for name, payload in zip(("first", "第二个"), results, strict=True):
            assert payload["arguments"] == {"text": name}
            context = payload["meta"][WORKSPACE_READ_EXTENSION]
            assert context["cwd"] == str((tmp_path / name).resolve())
            assert context["read_roots"] == [context["cwd"]]
        assert client.config.cwd == str(tmp_path) and client.connection() is transport
        ordinary = echo_proxy(client, transport, plugin=False)
        result = ordinary.execute_scoped({"text": "normal"}, ToolInvocationContext(None, workspace_read_context=read_context(tmp_path)))
        assert json.loads(json.loads(result.output)["result"])["meta"] is None
        assert client.disconnect(transport=transport).confirmed
        assert client.reconnect() is not transport
        stale = proxy.execute_scoped({"text": "old"}, ToolInvocationContext(None, workspace_read_context=read_context(tmp_path)))
        assert not stale.ok and stale.effect_outcome == "not_started"
    finally:
        client.stop()


@pytest.mark.parametrize("capability", [
    {"tools": {}}, {"experimental": {WORKSPACE_READ_EXTENSION: {"versions": ["2"]}}},
    {"experimental": {WORKSPACE_READ_EXTENSION: {"versions": "1"}}}, {"experimental": []},
])
def test_only_fixed_transport_explicit_version_receives_context(tmp_path, capability):
    client = SimpleNamespace(capabilities={"experimental": {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}}})
    proxy = PluginProxyTool(client, "echo", None, None, transport=SimpleNamespace(capabilities=capability))
    assert proxy._request_meta(ToolInvocationContext(None, workspace_read_context=read_context(tmp_path))) is None


def test_supported_plugin_without_context_fails_before_tools_call(tmp_path, monkeypatch):
    capability = {"tools": {}, "experimental": {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}}}
    client = MCPStdioClient(_config(context_server(capability), cwd=str(tmp_path)))
    try:
        transport = client.start()
        proxy = echo_proxy(client, transport)
        monkeypatch.setattr(client, "call_tool", lambda *args, **kwargs: pytest.fail("缺上下文不得发送"))
        for context in (None, ToolInvocationContext(None)):
            result = proxy._execute({"text": "business", "__cwd": str(tmp_path)}, context)
            assert not result.ok and result.effect_outcome == "not_started"
    finally:
        client.stop()


def test_explicit_and_registry_calls_use_same_host_context_chain(tmp_path):
    tools = [{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
             for tool in _manifest(b"")["tools"]]
    capability = {"tools": {}, "experimental": {WORKSPACE_READ_EXTENSION: {"versions": ["1"]}}}
    source = _SERVER.replace("__TOOLS__", repr(tools)).replace('"capabilities": {"tools": {}}', f'"capabilities": {capability!r}')
    source = source.replace('value = Path(request["params"]["arguments"]["path"]).read_text()',
                            'value = json.dumps(request["params"], ensure_ascii=False)')
    service = installed_runtime_plugin(tmp_path, module_source=source)
    result = service.command("/plugins enable sample-peek", revision=service.catalog().revision, request_id="enable")
    assert result["state"] == "succeeded", result
    registry = plugin_registry(service)
    try:
        explicit = service.command('/plugins@sample-peek read "中文 input.txt"', revision=service.catalog().revision,
            request_id="explicit", request_permission=lambda p, **_: {"permission_id": p["permission_id"], "decision": "approved"})
        assert explicit["state"] == "succeeded", explicit
        registry.prepare_for_run()
        model = invoke_registered_tool(service, registry, plugin_tool_name("sample-peek", "read"), {"path": "中文 input.txt"})
        assert model["state"] == "succeeded", model
        for result in (explicit, model):
            payload = json.loads(json.loads(result["output"])["result"])
            assert payload["arguments"] == {"path": "中文 input.txt"}
            context = payload["_meta"][WORKSPACE_READ_EXTENSION]
            assert context["cwd"] == str(service.context.workspace.resolve())
            assert context["read_roots"] == [context["cwd"]]
    finally:
        registry.close_mcp_clients()
        result = service.command("/plugins disable sample-peek", revision=service.catalog().revision, request_id="disable")
        assert result["state"] == "succeeded", result
