"""真实原生回执按全部步骤汇总效果，不让末步未执行吞掉前序写入。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
)
from agent_py_agent.tests.test_acceptance_workflow_native_ports import CounterTool, flow
from agent_py_agent.tests.test_acceptance_workflow_native_ports import wire as wire


@pytest.mark.parametrize('prefix,expected', [('absent', 'not_started'), ('read', 'none'),
                                             ('write', 'committed'), ('unknown_write', 'unknown')])
def test_real_http_failed_workflow_preserves_prefix_effect(wire, tmp_path, prefix, expected):
    agent, client = wire
    steps = []
    target = tmp_path / 'effect.txt'
    if prefix != 'absent':
        tool = CounterTool()
        if prefix != 'read':
            tool.runtime_policy = replace(tool.runtime_policy,
                effect_resolver=EffectResolverPolicy('mutating'),
                approval_policy=ApprovalPolicy('never'), idempotency_policy=IdempotencyPolicy('operation'))
            original = tool.execute
            def execute(params):
                target.write_text('committed', encoding='utf-8')
                if prefix == 'unknown_write':
                    raise TimeoutError('write completed but acknowledgement missing')
                return original(params)
            tool.execute = execute
        agent.tools.register(tool)
        steps.append({'step_id': 'prefix', 'name': 'prefix', 'mode': 'tool_call', 'on_error': 'fail',
                      'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}})
    steps.append({'step_id': 'analysis', 'name': 'analysis', 'mode': 'llm_skill_analysis', 'on_error': 'fail',
                  'config': {'analysis_isolation': 'lightweight_subagent'}})
    name = 'effect-' + prefix.replace('_', '-')
    assert client.call('workflows.create', {'definition': flow(name, steps)})['ok']
    started = client.call('workflows.run', {'workflow_ref': name})
    run_id = started['data']['workflow_run_id']
    result = client.call('workflow_runs.get', {'workflow_run_id': run_id})
    assert result['ok'], result
    data = result['data']
    assert data['run']['status'] == 'failed' and data['run']['effect_outcome'] == expected, data
    if prefix != 'unknown_write':
        error = data['steps'][-1]['error']
        assert error['code'] == 'WORKFLOW_ISOLATION_UNAVAILABLE'
        details = {item['key']: item['value'] for item in error['details']}
        assert details['effect_outcome'] == 'not_started'
        assert details['next_action'] == 'use_native_tool_call_steps_then_explicit_stateless_skill'
        assert '未执行模型或工具' in error['message']
    else:
        assert data['steps'][0]['error']['code'] == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert target.exists() == (prefix in ('write', 'unknown_write'))
    assert len(json.dumps(data)) > 0
