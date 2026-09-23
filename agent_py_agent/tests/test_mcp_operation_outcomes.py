"""MCP 完整业务失败与传输未知经原执行器落账；假服务不代替真实 TUI 验收。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.runtime_db.executor_liveness import attempt_executor
from agent_py_agent.agent.runtime_db.host_commands import HostCommandRequest
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPToolInfo, _normalize_call_result
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
from agent_py_agent.agent.tooling.models import ResourceScopePolicy
from agent_py_agent.agent.tooling.runtime_contracts import tool_arguments_hash
from agent_py_agent.agent.tooling.tool_operation_coordinator import replay_completed_tool_operation
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call


# LLM: 服务替身只返回经真实协议校验的结果或指定传输异常；不伪造操作结算和权限。
# 类用途: 计数原代理实际发起的调用，验证失败重放不会再次执行服务。
class ResponseClient:
    # LLM: response 是原始 MCP 响应或传输错误，测试可以在下一次独立请求前改变它。
    # 函数用途: 保存内存服务响应和调用次数，不启动进程。
    def __init__(self, response):
        self.response = response
        self.calls = 0

    # LLM: 此替身只证明调用结果处理；真实连接退出另由生命周期测试覆盖。
    # 函数用途: 提供固定可用性以进入原工具执行器。
    def is_running(self):
        return True

    # LLM: 使用真实 CallToolResult 校验，坏响应不能伪装业务失败；调用计数仅存在于测试内存。
    # 函数用途: 返回测试协议响应或抛出指定异常。
    def call_tool(self, name, arguments, **options):
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return _normalize_call_result(self.response)


# LLM: 每个请求沿生产 HostCommand 身份、attempt executor 和 ManagedOperationStore 执行；不直接改 SQL。
# 函数用途: 在临时数据库调用原代理，返回持久操作和原执行结果供测试核对。
def execute_request(tmp_path, repo, proxy, request_id):
    request = HostCommandRequest("owner-a", "actor-a", "cli", "thread-a", request_id,
                                 proxy.model_spec.name, tool_arguments_hash({}).removeprefix("sha256:"))
    binding = repo.register_host_command(request)
    repo.create_attempt(binding.agent_run_id, reuse_pending=True, reject_running=True,
                        expected_pending_attempt_id=binding.attempt_id)
    store = ManagedOperationStore(repo)
    with attempt_executor(repo, binding.run_id, binding.attempt_id):
        execution = execute_canonical_test_call(
            tmp_path, tools={proxy.model_spec.name: proxy}, tool_name=proxy.model_spec.name, arguments={},
            run_id=binding.run_id, attempt_id=binding.attempt_id, operation_id=request.operation_id,
            operation_owner_id=request.owner_id, operation_store=store, operation_store_required=True,
            trusted_run_context={"run_scope": {"task_id": binding.task_id}},
        )
    record = store.get_tool_operation(owner_id=request.owner_id, run_id=binding.run_id,
        task_id=binding.task_id, attempt_id=binding.attempt_id, operation_id=request.operation_id,
        tool_name=proxy.model_spec.name, args_hash=tool_arguments_hash({}))
    return execution.result, record


def test_complete_mcp_error_is_persisted_replayable_and_releases_scope(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    client = ResponseClient({"content": [{"type": "text", "text": "业务失败，可能已有部分写入"}], "isError": True})
    proxy = build_proxy_tool(client, "sample", MCPToolInfo(name="work", description="测试", input_schema={}), effect="mutating")
    proxy.runtime_policy = replace(proxy.runtime_policy, resource_scopes=ResourceScopePolicy(
        "declared", static_scopes=("logical:mcp:sample:work",)))
    result, record = execute_request(tmp_path, repo, proxy, "first")
    assert record.status == "failed"
    assert not result.ok and result.handler_executed
    assert record.result["effect_outcome"] == "failed"
    assert json.loads(record.result["output"])["error"] == "业务失败，可能已有部分写入"
    replay = replay_completed_tool_operation(record)
    assert not replay.ok and replay.effect_outcome == "failed"
    assert replay.output == record.result["output"] and client.calls == 1
    client.response = {"content": [{"type": "text", "text": "下一次独立操作"}]}
    recovered, next_record = execute_request(tmp_path, repo, proxy, "second")
    assert recovered.ok and next_record.status == "succeeded" and client.calls == 2


@pytest.mark.parametrize("response", [
    MCPError("响应丢失", code="MCP_TIMEOUT", effect_outcome="unknown"),
    MCPError("连接断开", code="MCP_CONNECTION_CLOSED", effect_outcome="unknown"),
    {"content": [], "isError": "true"},
    RuntimeError("代理内部异常"),
])
def test_missing_or_invalid_mcp_result_remains_unknown(tmp_path, response):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    client = ResponseClient(response)
    proxy = build_proxy_tool(client, "sample", MCPToolInfo(name="work", description="测试", input_schema={}), effect="mutating")
    result, record = execute_request(tmp_path, repo, proxy, "unknown")
    assert not result.ok and record.status == "unknown" and client.calls == 1


def test_unsent_mcp_refusal_remains_not_started(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    client = ResponseClient(MCPError("已撤销", code="PLUGIN_ACTIVATION_UNAVAILABLE", effect_outcome="not_started"))
    proxy = build_proxy_tool(client, "sample", MCPToolInfo(name="work", description="测试", input_schema={}), effect="mutating")
    result, record = execute_request(tmp_path, repo, proxy, "refused")
    assert not result.ok and record.status == "failed"
    assert record.result["effect_outcome"] == "not_started"
