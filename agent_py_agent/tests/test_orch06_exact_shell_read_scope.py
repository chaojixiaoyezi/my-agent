"""精确委派读范围沿真实 Shell 沙箱执行，不能退回 owner 全读。"""
import json
import sys

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.backends.base import EchoBackend
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


@pytest.mark.sandbox_capability('nested_sandbox_exec' if sys.platform == 'darwin' else 'linux_bwrap')
@pytest.mark.parametrize('target,expected_ok', [('allowed', True), ('blocked', False)])
def test_exact_shell_reads_only_declared_input(tmp_path, target, expected_ok):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    owner = agent.home_paths.owner_home_dir
    allowed, blocked, output = owner / 'allowed.txt', owner / 'blocked.txt', owner / 'output'
    allowed.write_text('AUTHORIZED_READ_MARKER')
    blocked.write_text('FORBIDDEN_READ_MARKER')
    output.mkdir()

    class Backend(EchoBackend):
        calls = 0
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'read-input', 'name': 'run_command',
                    'input': {'command': 'cat ../' + target + '.txt', 'working_dir': str(output), 'timeout': 10},
                }])
            return ModelResponse(text='完成', backend=self.name)

    agent.backend = Backend()
    result = agent.run('按结构化范围读取输入', params=RunParams(
        request_id='exact-read', run_id='exact-read', task_id='exact-read',
        allowed_tools=['run_command'], save=False, write_boundary={
            'task_dir': str(output), 'execution_cwd': str(output),
            'allowed_write_roots': [str(output)], 'allowed_read_roots': [str(allowed), str(output)],
            # 宽 owner/workspace 只提供定位，不能扩大精确读白名单。
            'owner_workspace_dir': str(owner), 'read_scope_mode': 'exact', 'shell_access_mode': 'workspace-write',
        }))
    row = result.archive_tool_calls[0]
    assert row['ok'] is expected_ok, (row.get('error_code'), row.get('output_preview'), row.get('tool_result_envelope'))
    assert row['tool_result_envelope']['process']['status'] == 'exited'
    assert 'read_scope=exact' in row['output_preview']
    wire = json.dumps(row, ensure_ascii=False)
    assert 'FORBIDDEN_READ_MARKER' not in wire
    if expected_ok:
        assert 'AUTHORIZED_READ_MARKER' in wire


def test_exact_shell_unsupported_backend_does_not_start(tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt import sandbox
    from agent_py_agent.agent.tooling.shell import _sandbox_exec

    monkeypatch.setattr(sandbox.platform, 'system', lambda: 'UnsupportedOS')
    marker = tmp_path / 'effect.txt'
    with pytest.raises(sandbox.SandboxUnavailableError, match='SYSTEM_READ_ROOTS_UNAVAILABLE'):
        _sandbox_exec('touch effect.txt', tmp_path, tmp_path,
            write_roots=(tmp_path,), read_roots=(),
            host_policy=sandbox.SandboxHostPolicy(exact_read_scope=True))
    assert not marker.exists()


def test_exact_scope_rejects_terminal_start_before_handler(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend='echo'), tmp_path)
    marker = agent.home_paths.owner_home_dir / 'not-created.txt'

    class Backend(EchoBackend):
        calls = 0
        def generate(self, prompt, on_chunk=None, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(text='', backend=self.name, tool_use_blocks=[{
                    'id': 'unsupported-terminal', 'name': 'terminal_session',
                    'input': {'action': 'start', 'command': 'touch ' + str(marker)},
                }])
            return ModelResponse(text='未执行', backend=self.name)

    agent.backend = Backend()
    result = agent.run('隔离终端范围测试', params=RunParams(
        request_id='exact-terminal', run_id='exact-terminal', task_id='exact-terminal',
        allowed_tools=['terminal_session'], save=False, write_boundary={
            'read_scope_mode': 'exact', 'allowed_read_roots': [], 'allowed_write_roots': [],
        }))
    row = result.archive_tool_calls[0]
    assert row['error_code'] == 'SANDBOX_UNAVAILABLE'
    assert row['handler_executed'] is False
    assert not marker.exists()
    assert 'EXACT_PROCESS_READ_SCOPE_UNSUPPORTED' in row['output_preview']
