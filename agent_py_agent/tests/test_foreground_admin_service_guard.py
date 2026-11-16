"""真实出生身份、自停拒门与独立审批诊断回归。"""
from __future__ import annotations

import json
import os
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.daemon_metadata import build_process_identity
from agent_py_agent.agent.tooling.action_policy import ActionDecision
from agent_py_agent.agent.tooling.active_service_guard import active_service_stop_denial
from agent_py_agent.agent.tooling.executor import _decision_result
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


@pytest.mark.parametrize('role', ['gateway', 'adapter', 'supervisor'])
def test_exact_birth_checked_services_no_signal(tmp_path, role):
    root = tmp_path / 'owners/local/main/workspace/runtime/services/gateway'
    root.mkdir(parents=True)
    record = {**build_process_identity(), 'kind': f'my-agent-{role}'}
    path = root / f'{role}.pid'
    path.write_text(json.dumps(record), encoding='utf-8')
    command = f'kill -TERM {os.getpid()}'
    assert active_service_stop_denial(command, data_root=tmp_path) is None
    denial = active_service_stop_denial(command, data_root=tmp_path, foreground_admin_full_access=True)
    assert denial['effect_outcome'] == 'not_started'
    assert denial['error_code'] == 'ACTIVE_SERVICE_SELF_STOP_BLOCKED'
    assert active_service_stop_denial(f'my-agent {role} stop', data_root=tmp_path, foreground_admin_full_access=True)
    record['start_time'] = 'not-the-birth'
    path.write_text(json.dumps(record), encoding='utf-8')
    assert active_service_stop_denial(command, data_root=tmp_path, foreground_admin_full_access=True) is None


def test_approval_diagnostic_does_not_authorize_or_require_admin():
    call = ToolCall(call_id='approval', tool_name='run_command', arguments={}, source_protocol='native',
        schema_hash='sha256:test', run_id='run', turn_id='turn', attempt_id='attempt')
    decision = ActionDecision('ask', ('APPROVAL_REQUIRED',), {})
    identity = {'actor': 'private-actor', 'owner': 'local/main',
        'required_identity': '当前owner的已授权用户', 'next_step': '请原会话批准；未执行。'}
    result = _decision_result(call, decision, 0, host_authorization_identity=identity)
    assert result.status == 'approval_required'
    assert result.error_code == 'APPROVAL_REQUIRED'
    assert result.metadata['error_details']['actor'] == 'private-actor'
    assert result.metadata['error_details']['required_identity'] == identity['required_identity']
    missing = _decision_result(call, decision, 0)
    assert missing.metadata['error_details']['actor'] == 'unknown'
    assert replace(decision, status='deny').status == 'deny'
