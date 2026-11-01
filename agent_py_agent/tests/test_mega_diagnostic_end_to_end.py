"""Real Gateway/Executor/stdio boundaries with controlled provider failure inputs."""
import errno
import json
import sys

import pytest

from agent_py_agent.agent.agent_core import provider_transient_auto_resume as retry
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, MCPStdioClient
from agent_py_agent.agent.tooling.mcp_registration import build_proxy_tool
from agent_py_agent.tests._tool_runtime_harness import execute_canonical_test_call
from agent_py_agent.tests.test_mcp_registration import _single_tool_server
from agent_py_agent.tests.test_provider_transient_turn_resume import (
    _echo_agent,
    _gateway,
    _inbox_payload,
    _processing,
    _raise,
    _run_turn,
)
from agent_py_agent.tests.test_unified_provider_connection_resume import connection_limit_error


@pytest.mark.parametrize('resume_limit', [0, 2])
def test_connection_budget_uses_existing_gateway_resume_or_manual_closeout(tmp_path, monkeypatch, resume_limit):
    error = connection_limit_error()
    retry._mark_budget_exceeded(error, 0.001)
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=resume_limit)
    paths = _gateway(agent)
    _processing(paths, 'w9-connection-budget')
    monkeypatch.setattr(request_execution, '_run_gateway_ask', _raise(error))
    response = _run_turn(agent, paths, 'w9-connection-budget')
    assert response.get('provider_error', {}).get('code') == 'websocket_connection_limit_reached'
    assert response.get('diagnostic', {}).get('code') == retry.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED
    if resume_limit:
        pending = _inbox_payload(paths, 'w9-connection-budget')
        assert pending['active_turn_recovery']['provider_resume_count'] == 1
        assert response['provider_transient_resume']['resume_count'] == 1
    else:
        assert response['status'] == 'failed'
        assert 'provider_transient_resume' not in response
        assert not (paths.inbox / 'w9-connection-budget.json').exists()


@pytest.mark.parametrize('archive_errno', [None, errno.ENOSPC, errno.EIO])
def test_real_stdio_rpc_error_reaches_executor_with_safe_data_and_unknown_effect(tmp_path, archive_errno):
    # 仅替换可控远端回包；传输、JSON-RPC、代理、权限和执行器均为生产实现。
    server = _single_tool_server('inspect')
    start = server.index('    elif method == "tools/call":')
    server = server[:start] + '''    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": request_id, "error": {
            "code": -32602, "message": "invalid", "data": {
                "reason": "missing", "field": "name", "token": "private-token"}}})
'''
    client = MCPStdioClient(MCPServerConfig(name='diagnostic', command=sys.executable,
        args=['-u', '-c', server], connect_timeout=2, timeout=2))
    transport = client.start()
    try:
        proxy = build_proxy_tool(client, 'diagnostic', client.list_tools(transport=transport)[0],
                                 effect='mutating', transport=transport)

        def archiver(call, outcome):
            if archive_errno is not None:
                raise OSError(archive_errno, 'archive failed')
            from agent_py_agent.agent.tooling.executor import ToolOutputProjection
            from agent_py_agent.agent.tooling.runtime_contracts import ToolContentBlock
            return ToolOutputProjection((ToolContentBlock('text', text=outcome.output),))

        result = execute_canonical_test_call(tmp_path, tools={proxy.model_spec.name: proxy},
            tool_name=proxy.model_spec.name, arguments={}, output_archiver=archiver).result
        assert result.handler_executed is True and result.effect_outcome == 'unknown'
        assert result.metadata['handler_details']['mcp_error']['data']['reason'] == 'missing'
        assert 'private-token' not in json.dumps(result.to_dict())
        if archive_errno is None:
            assert json.loads(result.output)['diagnostic']['mcp_error']['code'] == -32602
        else:
            assert result.error_code == 'TOOL_OUTPUT_ARCHIVE_FAILED'
            assert result.metadata['diagnostic']['errno'] == archive_errno
            assert result.failure_stage == 'persistence' and result.status == 'unknown'
    finally:
        client.stop()
        if transport.binding.process.poll() is None:
            transport.binding.process.kill()
        transport.binding.process.wait(timeout=3)


@pytest.mark.parametrize('number', [errno.ENOSPC, errno.EIO])
def test_real_agent_archive_fault_keeps_one_handler_and_one_archive_attempt(tmp_path, monkeypatch, number):
    from agent_py_agent.agent.agent_core import tool_call_archive_record as archive
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_unified_provider_connection_resume import (
        CommitOnceTool,
        ToolThenConnectionLimitBackend,
    )

    class ArchiveFaultBackend(ToolThenConnectionLimitBackend):
        def generate(self, prompt, **options):
            if self.calls == 0:
                return super().generate(prompt, **options)
            self.calls += 1
            self.messages.append(options.get('messages', []))
            return ModelResponse('archive fault reported', self.name)

    archive_calls = []

    def fail(request):
        archive_calls.append(request.call_id)
        raise OSError(number, 'disk failure', '/private/secret-file')

    monkeypatch.setattr(archive, 'externalize_tool_output_record', fail)
    agent = SimpleAgent(AgentConfig(enable_tools=True, memory_path='memory.jsonl'), tmp_path)
    backend, tool = ArchiveFaultBackend(), CommitOnceTool()
    agent.backend = backend
    agent.tools.tools[tool.model_spec.name] = tool
    result = agent.run('执行一次 commit_once 并核对结果。', save=False, allowed_tools=['commit_once'])
    assert result.response == 'archive fault reported'
    assert tool.calls == 1 and len(archive_calls) == 1
    native = json.dumps(backend.messages[-1])
    assert f'errno={number}' in native and 'storage=volatile' in native
    assert 'secret-file' not in native
