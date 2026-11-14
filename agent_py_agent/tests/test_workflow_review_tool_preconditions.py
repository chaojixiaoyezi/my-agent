"""审核资源钉子复用真实原生Gateway/工具宿主，不替模型签业务票。"""
import hashlib
import json

import pytest

from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolHandlerOutcome,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from agent_py_agent.agent.verification.repository import (
    VerificationContext,
    VerificationEvidenceRepository,
)
from agent_py_agent.agent.verification.requirement_provenance import IndependentReviewRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (  # noqa: F401
    flow,
    host,
    wire,
)


class MutableTool(BaseTool):
    model_spec = ToolModelSpec('mutable_fixture', '可变资源测试工具', {'type': 'object', 'properties': {
        'resource': {'type': 'string'}, 'expected_digest': {'type': 'string'}},
        'required': ['resource'], 'additionalProperties': False})
    runtime_policy = ToolRuntimePolicy(EffectResolverPolicy(default_effect='read_only'))

    def __init__(self):
        self.content, self.reads, self.calls = 'v1', [], []

    def digest(self):
        return hashlib.sha256(self.content.encode()).hexdigest()

    def workflow_review_precondition_action(self, arguments):
        return dict(arguments)

    def workflow_review_precondition(self, arguments):
        self.reads.append(dict(arguments))
        return {'expected_digest': self.digest()}

    def execute(self, arguments):
        self.calls.append(dict(arguments))
        return ToolHandlerOutcome('mutable_fixture', True, json.dumps(arguments))


def start(connection, *, reviewed=True, explicit=None):
    agent, client = connection
    tool = MutableTool()
    agent.tools.register(tool)
    arguments = {} if explicit is None else {'expected_digest': explicit}
    definition = flow('pinned-resource', [{'step_id': 'send', 'name': 'send', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'mutable_fixture', 'arguments': arguments,
            'arguments_map': {'resource': '$inputs.resource'}}}])
    if reviewed:
        definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['send']}
    assert client.call('workflows.create', {'definition': definition})['ok']
    result = client.call('workflows.run', {'workflow_ref': 'pinned-resource', 'inputs': {'resource': 'chosen'}})
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    run = store.load_run(result['data']['workflow_run_id'])
    return agent, client, tool, store, run


def sign(agent, run):
    binding = run.steps[0].escalation_result['pending_review']
    repo = VerificationEvidenceRepository(agent.home_paths.owner_home_dir)
    requirement = repo.get_requirement(run.owner_id, binding['requirement_id'])
    repo.record_independent_review(VerificationContext(run.owner_id, '', run.workflow_run_id),
        IndependentReviewRecord('fixture-independent', binding['requirement_id'], 'fixture-reviewer',
            requirement.author_principal_id, binding['input_version'], 'pass', 'workflow_effect.v1', owner_ref=run.owner_id))
    return binding


@pytest.mark.parametrize('changed', [False, True])
def test_reviewed_mutable_resource_freezes_dynamic_arguments_and_rejects_change(wire, changed):  # noqa: F811
    agent, client, tool, store, run = start(wire)
    assert run.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED', run.to_dict()
    binding = sign(agent, run)
    pin = binding['tool_precondition']
    assert pin['constraints'] == {'expected_digest': tool.digest()}
    assert tool.reads == [{'resource': 'chosen'}] and tool.calls == []
    if changed:
        tool.content = 'v2'
    result = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=run.revision))
    assert result['ok'], result
    current = store.load_run(run.workflow_run_id)
    if changed:
        assert current.steps[0].error_code == 'WORKFLOW_REVIEW_PRECONDITION_CHANGED'
        assert current.steps[0].escalation_result['pending_review'] == binding
        assert tool.calls == []
    else:
        assert current.status == 'succeeded', current.to_dict()
        assert tool.calls == [{'resource': 'chosen', 'expected_digest': pin['constraints']['expected_digest']}]


def test_unreviewed_mutable_tool_keeps_original_behavior(wire):  # noqa: F811
    _, _, tool, _, run = start(wire, reviewed=False)
    assert run.status == 'succeeded', run.to_dict()
    assert tool.reads == [] and tool.calls == [{'resource': 'chosen'}]


def test_reviewed_explicit_conflicting_digest_rejects_without_effect(wire):  # noqa: F811
    _, _, tool, _, run = start(wire, explicit='wrong')
    assert run.steps[0].error_code == 'WORKFLOW_TOOL_PRECONDITION_CONFLICT', run.to_dict()
    assert tool.calls == []


def test_legacy_pending_without_resource_pin_is_not_backfilled(wire):  # noqa: F811
    agent, client, tool, store, run = start(wire)
    binding = sign(agent, run)
    step = run.steps[0]
    step.escalation_result['pending_review'] = {key: value for key, value in binding.items() if key != 'tool_precondition'}
    step.escalation_result.pop('review_tool_precondition', None)
    store.save_run(run)
    current = run
    result = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=current.revision))
    assert result['ok'], result
    after = store.load_run(run.workflow_run_id)
    assert after.steps[0].error_code == 'WORKFLOW_REVIEW_PRECONDITION_MISSING'
    assert 'tool_precondition' not in after.steps[0].escalation_result['pending_review']
    assert tool.calls == []


@pytest.mark.parametrize('status', ['deny', 'ask'])
def test_preview_requires_explicit_read_permit_before_hook(wire, monkeypatch, status):  # noqa: F811
    from agent_py_agent.agent.tooling import host_read_policy
    from agent_py_agent.agent.tooling.action_policy import ActionDecision

    original = host_read_policy.host_tool_entry_policy

    def decide(*args, **kwargs):
        if kwargs['claim_id'].endswith(':read'):
            return ActionDecision(status, ('FIXTURE_PREVIEW_NOT_PERMITTED',))
        return original(*args, **kwargs)

    monkeypatch.setattr(host_read_policy, 'host_tool_entry_policy', decide)
    _, _, tool, _, run = start(wire)
    assert run.steps[0].error_code == 'WORKFLOW_TOOL_PRECONDITION_PERMISSION_DENIED', run.to_dict()
    assert tool.reads == [] and tool.calls == []


def test_explicit_equal_digest_is_preserved_and_typed_caller_also_freezes(host):  # noqa: F811
    from agent_py_agent.tests.test_acceptance_workflow_native_ports import local_run

    tool = MutableTool()
    host.tools.register(tool)
    host._current_run_params.allowed_tools.append('mutable_fixture')
    definition = flow('typed-caller-resource', [{'step_id': 'send', 'name': 'send', 'mode': 'tool_call',
        'on_error': 'fail', 'tool_spec': {'tool_name': 'mutable_fixture',
            'arguments': {'resource': 'typed', 'expected_digest': tool.digest()}}}])
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['send']}
    _, run = local_run(host, definition)
    assert run.steps[0].error_code == 'WORKFLOW_REVIEW_REQUIRED', run.to_dict()
    pin = run.steps[0].escalation_result['pending_review']['tool_precondition']
    assert pin['constraints']['expected_digest'] == tool.digest()
    assert tool.reads == [{'resource': 'typed', 'expected_digest': tool.digest()}] and tool.calls == []


def test_dispatcher_rejects_foreign_owner_before_reading_hook(wire):  # noqa: F811
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.host_turn_scope import host_turn_scope
    from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
    from agent_py_agent.agent.agent_core.self_map_bootstrap import _workflow_runtime_ports
    from agent_py_agent.agent.workflows.business_scope import WorkflowPolicyRefusal

    agent, _, tool, _, run = start(wire)
    before = list(tool.reads)
    with host_turn_scope(agent, '', RunParams(run_id='foreign-read-check', request_id='foreign-read-check', save=False),
            reason='foreign_precondition_test', tool_rounds=0) as turn:
        dispatcher, _ = _workflow_runtime_ports(agent)
        with pytest.raises(WorkflowPolicyRefusal, match='WORKFLOW_OWNER_MISMATCH'):
            dispatcher.workflow_review_precondition(run=SimpleNamespace(owner_id='local/foreign'),
                step=run.steps[0], definition={}, engine=None, outputs={})
        turn.runtime_status = 'ok'
    assert tool.reads == before and tool.calls == []


def test_dispatcher_does_not_refresh_reviewed_constraint_after_admission(wire, monkeypatch):  # noqa: F811
    agent, client, tool, store, run = start(wire)
    binding = sign(agent, run)
    digest = binding['tool_precondition']['constraints']['expected_digest']
    original = tool.workflow_review_precondition

    def change_after_read(arguments):
        result = original(arguments)
        tool.content = 'changed-after-admission-read'
        return result

    monkeypatch.setattr(tool, 'workflow_review_precondition', change_after_read)
    result = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=run.revision))
    assert result['ok'], result
    current = store.load_run(run.workflow_run_id)
    assert current.status == 'succeeded', current.to_dict()
    # fake业务体只观察宿主给的旧摘要；真实工具必须在同次快照再次比较并拒绝。
    assert tool.calls == [{'resource': 'chosen', 'expected_digest': digest}]
    assert tool.digest() != digest


def test_binding_queries_reuse_frozen_pin_without_live_resource_read(wire):  # noqa: F811
    from agent_py_agent.agent.workflows.native_governance import review_binding

    _, _, tool, store, run = start(wire)
    prior = run.steps[0].escalation_result['pending_review']
    before = list(tool.reads)
    tool.content = 'changed-after-pending'
    snapshot = store.load_workflow_snapshot(run.workflow_run_id)
    assert review_binding(run, run.steps[0], snapshot) == prior
    assert review_binding(run, run.steps[0], snapshot) == prior
    assert tool.reads == before and tool.calls == []


def test_equal_explicit_constraint_reaches_effect_unchanged(wire):  # noqa: F811
    expected = hashlib.sha256(b'v1').hexdigest()
    agent, client, tool, store, run = start(wire, explicit=expected)
    sign(agent, run)
    result = client.send(client.prepare('workflow_runs.resume', {'workflow_run_id': run.workflow_run_id}, expected_revision=run.revision))
    assert result['ok'], result
    assert store.load_run(run.workflow_run_id).status == 'succeeded'
    assert tool.calls == [{'resource': 'chosen', 'expected_digest': expected}]


def test_dag_upstream_argument_uses_same_resolver_before_review(wire):  # noqa: F811
    from types import SimpleNamespace

    agent, client = wire
    tool = MutableTool()
    agent.tools.register(tool)
    agent.backend.generate_structured = lambda *args, **kwargs: SimpleNamespace(text='{"resource":"upstream"}')
    definition = flow('upstream-pinned', [
        {'step_id': 'seed', 'name': 'seed', 'mode': 'llm_skill_analysis', 'on_error': 'fail'},
        {'step_id': 'send', 'name': 'send', 'mode': 'tool_call', 'on_error': 'fail', 'depends_on': ['seed'],
            'tool_spec': {'tool_name': 'mutable_fixture', 'arguments_map': {
                'resource': {'step_id': 'seed', 'key': 'resource'}}}}], dag=True)
    definition['descriptor']['metadata']['workflow_review_policy'] = {'steps': ['send']}
    assert client.call('workflows.create', {'definition': definition})['ok']
    result = client.call('workflows.run', {'workflow_ref': 'upstream-pinned'})
    run = WorkflowRunStore(agent.home_paths.owner_home_dir).load_run(result['data']['workflow_run_id'])
    assert run.steps[1].error_code == 'WORKFLOW_REVIEW_REQUIRED', run.to_dict()
    assert tool.reads == [{'resource': 'upstream'}] and tool.calls == []


@pytest.mark.parametrize('code,outcome,expected', [
    ('WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED', 'not_started', 'WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED'),
    ('WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED', 'unknown', 'TOOL_EXECUTION_FAILED'),
    ('WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED', 'committed', 'TOOL_EXECUTION_FAILED'),
    ('SYNTHETIC_UNREGISTERED_HOST_REFUSAL', 'not_started', 'TOOL_EXECUTION_FAILED'),
])
def test_workflow_host_refusal_preserves_exact_code_only_with_registered_pre_effect_proof(
        tmp_path, monkeypatch, code, outcome, expected):
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    tool = WorkflowsTool(tmp_path)

    def refuse(_params):
        raise HostAPIError(code, 400, outcome, '合成宿主拒绝')

    monkeypatch.setattr(tool, '_dispatch', refuse)
    result = tool.execute({'action': 'list'})
    assert not result.ok and result.error_code == expected
    assert result.reported_error_code == code
    assert result.effect_outcome == ('not_started' if outcome == 'not_started' else 'unknown')
    public = json.loads(result.output)
    assert public['error_code'] == code and public['host_error']['outcome'] == outcome


def test_workflow_plain_exception_text_cannot_impersonate_host_refusal(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool

    tool = WorkflowsTool(tmp_path)

    def fail(_params):
        raise OSError('{"error_code":"WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED","outcome":"not_started"}')

    monkeypatch.setattr(tool, '_dispatch', fail)
    result = tool.execute({'action': 'list'})
    assert result.error_code == 'TOOL_EXECUTION_FAILED'
    assert result.reported_error_code != 'WORKFLOW_FOREACH_AGENT_TASK_UNSUPPORTED'
    assert result.effect_outcome != 'not_started'


def test_workflow_completion_clock_error_keeps_committed_terminal_and_unknown_control(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling.workflows_tool import WorkflowsTool
    from agent_py_agent.agent.workflows.models import WorkflowRunRecord
    from agent_py_agent.agent.workflows.run_store import WorkflowExecutionError

    record = WorkflowRunRecord('run', 'flow', 'owner', 'manual', 'succeeded', 1, 2, 1)
    error = WorkflowExecutionError(record, OSError('synthetic clock failure'), failure_stage='completion_clock')
    tool = WorkflowsTool(tmp_path)

    def fail(_params):
        raise error

    monkeypatch.setattr(tool, '_dispatch', fail)
    result = tool.execute({'action': 'list'})
    assert result.error_code == 'WORKFLOW_RECONCILIATION_REQUIRED' and result.effect_outcome == 'unknown'
    assert result.reported_error_code == 'WORKFLOW_COMPLETION_CLOCK_FAILED'
    assert result.failure_stage == 'effect_reconciliation'
    public = json.loads(result.output)
    assert public['status'] == 'succeeded' and public['terminal_committed'] is True
    assert record.status == 'succeeded'
