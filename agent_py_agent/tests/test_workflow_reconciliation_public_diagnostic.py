"""未知写恢复通过原生HTTP保留具体拒绝；可信核验只消费原输出，不重做工具。"""
import json
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent.tooling.models import (
    ApprovalPolicy,
    EffectResolverPolicy,
    IdempotencyPolicy,
)
from agent_py_agent.agent.workflows.governance import WorkflowGovernance, workflow_candidate_hash
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (  # noqa: F401
    CounterTool,
    flow,
    wire,
)


@pytest.mark.parametrize('resolution', ['missing', 'unavailable', 'untrusted', 'trusted'])
def test_native_http_unknown_write_requires_host_verification_without_reexecution(wire, tmp_path, resolution):  # noqa: F811
    agent, client = wire
    output = tmp_path / 'target.txt'
    counter = CounterTool()
    counter.runtime_policy = replace(counter.runtime_policy,
        effect_resolver=EffectResolverPolicy(default_effect='mutating'),
        approval_policy=ApprovalPolicy(mode='never'), idempotency_policy=IdempotencyPolicy(scope='operation'))
    def execute(params):
        counter.calls.append(params)
        with output.open('a') as stream:
            stream.write('applied\n')
        raise TimeoutError('after target commit')
    counter.execute = execute
    agent.tools.register(counter)
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    if resolution in ('trusted', 'untrusted'):
        def identity(record, step):
            return {'owner_id': record.owner_id, 'workflow_run_id': record.workflow_run_id,
                    'task_id': record.business_context.get('task_id', ''), 'step_id': step.step_id,
                    'attempt_id': step.escalation_result['attempt_id'],
                    'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id))}
        def review(reference, record, step_id):
            step = next(item for item in record.steps if item.step_id == step_id)
            return {**identity(record, step), 'receipt_ref': reference, 'author_id': 'author',
                    'reviewer_id': 'independent-reviewer', 'decision': 'approved', 'expires_at': time.time() + 60}
        def verified(reference, record, step):
            if reference != 'verified-target':
                return None
            # 目标自身可核验结果是依据，用户传入的ref不是成功证明。
            assert output.read_text() == 'applied\n'
            return {**identity(record, step), 'resolution_ref': reference,
                    'state': 'committed', 'output': {'value': 7}}
        agent.business_governance = WorkflowGovernance(store,
            policy_provider=lambda _record, _step: {'minimum_reviews': 1, 'review_refs': ['review'], 'author_id': 'author'},
            receipt_reader=review, resolution_reader=verified,
            authorization_check=lambda receipt, _record: (receipt.get('resolution_ref') == 'verified-target'
                if isinstance(receipt, dict) else receipt.receipt_ref == 'review'))
    definition = flow('unknown-write', [{'step_id': 'write', 'name': 'write', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'native_counter', 'arguments': {'value': 7}}}])
    assert client.call('workflows.create', {'definition': definition})['ok']
    first = client.call('workflows.run', {'workflow_ref': 'unknown-write'})
    assert first['ok'], first
    run_id = first['data']['workflow_run_id']
    record = store.load_run(run_id)
    assert record.status == 'failed' and record.steps[0].error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    args = {'workflow_run_id': run_id}
    if resolution != 'missing':
        args['resolution_ref'] = 'untrusted-ref' if resolution == 'untrusted' else 'verified-target'
    result = client.send(client.prepare('workflow_runs.resume', args, expected_revision=record.revision))
    assert len(counter.calls) == 1 and output.read_text() == 'applied\n'
    if resolution == 'trusted':
        assert result['ok'] and result['data']['run']['status'] == 'succeeded', result
        assert store.load_run(run_id).steps[0].output_snapshot == {'value': 7}
    else:
        expected = 'WORKFLOW_RESOLUTION_UNAVAILABLE' if resolution == 'unavailable' else 'WORKFLOW_RECONCILIATION_REQUIRED'
        assert result['http_status'] == 409 and result['error']['code'] == expected, result
        assert result['effect_outcome'] == 'not_started'
        details = {row['key']: row['value'] for row in result['error']['details']}
        if resolution != 'untrusted':
            assert details['prior_effect_outcome'] == 'unknown'
            assert details['resolution_available'] is False
        assert details['recommended_action']
        assert store.load_run(run_id).status == 'failed'
