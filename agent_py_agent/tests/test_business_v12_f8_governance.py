"""F8 security boundaries; Workflow/interaction/content authorities are explicit contract ports.

These fakes do not claim real tickets, W3 persistence, W2 enqueue, or W13 HTTP integration.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling.action_policy import ActionPolicy, ActionPolicyRequest
from agent_py_agent.agent.tooling.models import (
    BaseTool,
    EffectResolverPolicy,
    ToolModelSpec,
    ToolRuntime,
    ToolRuntimePolicy,
    ToolRuntimeSnapshot,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_business_v12_f2_spaces import World, assert_code, module


def tool(world, actor=None):
    name = 'agent_py_agent.agent.tooling.business_spaces_tool'
    assert importlib.util.find_spec(name) is not None, 'native business entry is missing'
    return importlib.import_module(name).BusinessSpacesTool(world.service, actor=actor or world.actor)


def envelope(action, params, *, revision=None):
    request = {'action': action, 'params': params, 'request_id': 'request-native'}
    if action not in ('spaces.list', 'spaces.get', 'policies.get', 'policies.preview', 'interfaces.get', 'interfaces.resolve'):
        request['idempotency_key'] = 'idempotency_native_1234'
    if revision is not None:
        request['expected_revision'] = revision
    return request


def decision(native, request, root, **extra):
    runtime = ToolRuntime(native.model_spec, native.runtime_policy, native)
    snapshot = ToolRuntimeSnapshot('run-policy', (runtime,), frozenset({native.model_spec.name}), (), None)
    call = ToolCall('call-policy', native.model_spec.name, request, 'native', native.model_spec.schema_hash,
                   'run-policy', 'turn-policy', 'attempt-policy', idempotency_key=request.get('idempotency_key', ''))
    return ActionPolicy().decide(ActionPolicyRequest(call, snapshot, root, approval_mode='auto', **extra))


def test_native_entry_executes_real_domain_and_closed_result(tmp_path):
    world = World(tmp_path / 'owner')
    outcome = tool(world).execute(envelope('spaces.create', {'name': 'native作品'}))
    assert outcome is not None and outcome.ok is True
    sid = outcome.result_envelope['space_id']
    assert world.service.get(sid, actor=world.actor)['space']['name'] == 'native作品'
    replay = tool(world).execute(envelope('spaces.create', {'name': 'native作品'}))
    assert replay.result_envelope == outcome.result_envelope


def test_real_action_policy_denies_revoked_scope_before_handler(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    native = tool(world)
    world.denied_spaces.add(sid)
    result = decision(native, envelope('spaces.get', {'space_id': sid}), world.root)
    assert result.status == 'deny' and 'BUSINESS_ACCESS_DENIED' in result.reason_codes


@pytest.mark.parametrize('injected', [
    {'owner_id': 'owner-b'}, {'owner_root': '/tmp/elsewhere'}, {'actor': {'role': 'admin'}},
    {'allowed_actions': ['*']}, {'human': True},
])
def test_native_parameters_cannot_inject_identity_or_roles(tmp_path, injected):
    world = World(tmp_path / 'owner')
    params = {**envelope('spaces.create', {'name': 'malicious'}), **injected}
    outcome = tool(world).execute(params)
    assert not outcome.ok and outcome.error_code == 'TOOL_INVALID_ARGUMENTS'
    assert outcome.reported_error_code == 'INVALID_SCHEMA'
    assert outcome.result_envelope == {'error_code': 'INVALID_SCHEMA'}
    assert not world.service.state_path.exists()


def test_native_utf8_limit_is_checked_inside_real_policy_chain(tmp_path):
    world = World(tmp_path / 'owner')
    request = envelope('spaces.get', {'space_id': '汉' * 43})
    result = decision(tool(world), request, world.root)
    assert result.status == 'deny'
    assert not world.service.state_path.exists()


def test_missing_typed_actor_and_disabled_flag_fail_closed(tmp_path):
    world = World(tmp_path / 'owner')
    forged = SimpleNamespace(owner_id=world.owner, service=world.service, role='admin', allowed_actions=['*'], human=True)
    assert_code('ACTOR_REQUIRED', lambda: world.service.create({'name': 'x'}, actor=forged, idempotency_key='bad'))
    world.service.enabled = False
    assert_code('BUSINESS_NOT_ENABLED', lambda: world.create())
    assert not world.service.state_path.exists()


def test_adoption_requires_original_content_gate_not_just_business_control(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    world.add_ref('artifact_version', 'v1', space_id=sid, artifact_id='a')
    aid = world.call('assets.register', {'space_id': sid, 'name': '角色', 'kind': 'character',
        'artifact_ref': {'artifact_id': 'a', 'version_ref': 'v1'}}, key='register')['asset_id']
    for gate in (None, lambda facts: False):
        actor = replace(world.actor, content_authorizer=gate)
        assert_code('HUMAN_RESPONSE_REQUIRED', lambda: world.service.call('assets.versions.accept',
            {'space_id': sid, 'asset_id': aid, 'version_ref': 'v1', 'evidence_ref': 'model-says-human'},
            actor=actor, expected_revision=0, idempotency_key='adopt'))
    assert world.call('assets.get', {'space_id': sid, 'asset_id': aid})['accepted_version'] is None


class GovernancePort:
    """Contract fake for the ORIGINAL Workflow and interaction services, not W1 shadow storage."""
    def __init__(self):
        self.revision, self.approval, self.before_commit = 0, None, None
        self.commits, self.tickets, self.decisions = [], {}, []
        self.approval_required = True

    def get_policy(self, sid, *, actor):
        return {'policy_ref': 'workflow-governance', 'readable_rules': ['原Workflow规则'], 'revision': self.revision}

    def preview_policy(self, sid, policy, *, actor):
        return {'diff': [{'target_ref': sid, 'field': 'workflow_ref', 'before': 'old', 'after': policy['workflow_ref']}],
                'approval_required': self.approval_required, 'affected_runs': []}

    def approve(self, world, sid, policy):
        self.approval = (world.owner, world.actor.subject_id, sid, self.revision, json.dumps(policy, sort_keys=True), 'original-approval')

    def authorize_policy_change(self, sid, policy, reference, *, actor, expected_revision):
        return self.approval == (actor.owner_id, actor.subject_id, sid, expected_revision,
                                 json.dumps(policy, sort_keys=True), reference)

    def apply_policy(self, sid, policy, *, actor, expected_revision, idempotency_key, authorization_ref, commit_guard):
        if expected_revision != self.revision:
            raise module('business_access').BusinessError('REVISION_CONFLICT')
        if self.before_commit:
            self.before_commit()
        commit_guard()  # W3必须在原CAS/提交锁内复核，W1不替它写第二份政策。
        self.commits.append(dict(policy))
        self.revision += 1
        return {'policy_ref': 'workflow-governance', 'revision': self.revision,
                'effective_for': {'new_tasks': True, 'active_tasks_changed': False, 'security_revocation_immediate': True}}

    def decide_approval(self, params, *, actor, expected_revision, idempotency_key):
        ticket = self.tickets.get(params['response_ticket'])
        binding = (actor.owner_id, actor.subject_id, params['approval_id'], params['decision'], expected_revision)
        if ticket != binding:
            raise module('business_access').BusinessError('HUMAN_RESPONSE_REQUIRED')
        self.decisions.append(binding)
        approval = {'approval_id': params['approval_id'], 'thread_id': 'thread', 'task_id': 'stask', 'run_id': 'run',
            'revision': expected_revision + 1, 'state': params['decision'], 'summary': '',
            'expires_at': '2099-01-01T00:00:00Z', 'allowed_decisions': []}
        return {'approval': approval, 'state': params['decision']}


def governance_world(tmp_path):
    backend = GovernancePort()
    world = World(tmp_path / 'owner', governance=backend)
    sid = world.create()['space_id']
    for kind, ref in (('workflow', 'governance-v2'), ('release_scope', 'release-v2'), ('budget', 'budget-v2')):
        world.add_ref(kind, ref, space_id=sid)
    return world, backend, sid


def test_governance_without_original_backend_is_not_ready(tmp_path):
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    assert_code('HOST_NOT_READY', lambda: world.call('policies.get', {'space_id': sid}))


def test_get_and_preview_use_original_rules_without_business_writes(tmp_path):
    world, backend, sid = governance_world(tmp_path)
    before = world.service.state_path.read_bytes()
    assert world.call('policies.get', {'space_id': sid})['readable_rules'] == ['原Workflow规则']
    result = world.call('policies.preview', {'space_id': sid, 'policy': {'workflow_ref': 'governance-v2'}})
    assert result['approval_required'] is True and result['affected_runs'] == []
    assert backend.commits == [] and before == world.service.state_path.read_bytes()


def test_widening_without_real_approval_does_not_commit(tmp_path):
    world, backend, sid = governance_world(tmp_path)
    params = {'space_id': sid, 'policy': {'workflow_ref': 'governance-v2', 'automatic_release_scope_ref': 'release-v2', 'budget_ref': 'budget-v2'},
              'authorization_ref': 'model-invented'}
    assert_code('HUMAN_RESPONSE_REQUIRED', lambda: world.call('policies.apply', params, revision=0, key='change'))
    assert backend.commits == [] and backend.revision == 0


def test_real_port_approval_is_bound_to_exact_change_owner_and_revision(tmp_path):
    world, backend, sid = governance_world(tmp_path)
    policy = {'workflow_ref': 'governance-v2'}
    backend.approve(world, sid, policy)
    result = world.call('policies.apply', {'space_id': sid, 'policy': policy, 'authorization_ref': 'original-approval'}, revision=0, key='change')
    assert result['revision'] == 1 and backend.commits == [policy]
    assert result['effective_for']['active_tasks_changed'] is False
    assert 'workflow-governance' not in world.service.state_path.read_text()


@pytest.mark.parametrize('revoke', ['scope', 'approval'])
def test_revocation_between_preview_and_original_commit_is_immediate(tmp_path, revoke):
    world, backend, sid = governance_world(tmp_path)
    policy = {'workflow_ref': 'governance-v2'}
    backend.approve(world, sid, policy)
    backend.before_commit = (lambda: world.denied_spaces.add(sid)) if revoke == 'scope' else (lambda: setattr(backend, 'approval', None))
    assert_code('BUSINESS_ACCESS_DENIED' if revoke == 'scope' else 'HUMAN_RESPONSE_REQUIRED', lambda: world.call('policies.apply',
        {'space_id': sid, 'policy': policy, 'authorization_ref': 'original-approval'}, revision=0, key='change'))
    assert backend.commits == []


def test_ticket_decision_does_not_treat_model_human_or_opaque_text_as_authority(tmp_path):
    world, backend, sid = governance_world(tmp_path)
    params = {'approval_id': 'approval', 'decision': 'approved', 'response_ticket': 'invented-ticket'}
    assert_code('HUMAN_RESPONSE_REQUIRED', lambda: world.call('approvals.decide', params, revision=0, key='decision'))
    assert backend.decisions == []
    # W13真实票据的签发、activation/interaction/attempt/expiry核验不在本片伪造。
    backend.tickets['contract-ticket'] = (world.owner, world.actor.subject_id, 'approval', 'approved', 0)
    result = world.call('approvals.decide', {**params, 'response_ticket': 'contract-ticket'}, revision=0, key='decision')
    assert result['state'] == 'approved' and result['approval']['revision'] == 1


def test_legacy_tool_without_business_context_keeps_original_action_policy(tmp_path):
    class ReadTool(BaseTool):
        model_spec = ToolModelSpec('legacy_read', 'existing pure read', {'type': 'object', 'properties': {}, 'additionalProperties': False})
        runtime_policy = ToolRuntimePolicy(EffectResolverPolicy('read_only'))
    assert decision(ReadTool(), {}, tmp_path).status == 'allow'


def test_native_duplicate_mounts_fail_before_policy_allows_effect(tmp_path):
    world = World(tmp_path / 'owner')
    request = envelope('spaces.create', {'name': '重复挂载', 'mounts': [{'package_ref': 'pkg'}, {'package_ref': 'pkg'}]})
    result = decision(tool(world), request, world.root)
    assert result.status == 'deny'
    assert not world.service.state_path.exists()


def test_existing_tool_can_receive_only_host_typed_context_and_rechecks_live_scope(tmp_path):
    from dataclasses import fields

    from agent_py_agent.agent.conversation.business_context import resolve_business_context

    assert {'business_actor', 'business_context'} <= {f.name for f in fields(ActionPolicyRequest)}
    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    frozen = resolve_business_context(actor=world.actor, task_id='stask-original', requested_space_id=sid,
        project_ref=None, binding_id=None)
    class ReadTool(BaseTool):
        model_spec = ToolModelSpec('legacy_read', 'existing pure read', {'type': 'object', 'properties': {}, 'additionalProperties': False})
        runtime_policy = ToolRuntimePolicy(EffectResolverPolicy('read_only'))
    native = ReadTool()
    assert decision(native, {}, world.root, business_actor=world.actor, business_context=frozen).status == 'allow'
    world.service.update(sid, {'name': '新偏好'}, actor=world.actor, expected_revision=0, idempotency_key='new-name')
    assert frozen.space_revision == 0
    assert decision(native, {}, world.root, business_actor=world.actor, business_context=frozen).status == 'allow'
    world.denied_spaces.add(sid)
    assert decision(native, {}, world.root, business_actor=world.actor, business_context=frozen).status == 'deny'
    assert decision(native, {}, world.root, business_actor=world.actor, business_context={'role': 'admin'}).status == 'deny'
    assert decision(native, {}, world.root, business_context=frozen).status == 'deny'


def test_native_original_backend_error_after_commit_is_unknown_not_not_started(tmp_path):
    world, backend, sid = governance_world(tmp_path)
    policy = {'workflow_ref': 'governance-v2'}
    backend.approve(world, sid, policy)
    original = backend.apply_policy
    def committed_then_error(*args, **kwargs):
        original(*args, **kwargs)
        raise module('business_access').BusinessError('REVISION_CONFLICT')
    backend.apply_policy = committed_then_error
    outcome = tool(world).execute(envelope('policies.apply',
        {'space_id': sid, 'policy': policy, 'authorization_ref': 'original-approval'}, revision=0))
    assert backend.commits == [policy] and backend.revision == 1
    assert outcome.ok is False and outcome.reported_error_code == 'REVISION_CONFLICT'
    assert outcome.effect_outcome == 'unknown'


def test_original_executor_reaches_native_domain_and_rejects_revoked_scope(tmp_path):
    from agent_py_agent.agent.tooling.executor import ToolExecutor, ToolExecutorRequest

    world = World(tmp_path / 'owner')
    sid = world.create()['space_id']
    native = tool(world)
    runtime = ToolRuntime(native.model_spec, native.runtime_policy, native)
    snapshot = ToolRuntimeSnapshot('run-original-executor', (runtime,), frozenset({'business_spaces'}), (), None)
    call = ToolCall('call-original-executor', 'business_spaces', envelope('spaces.get', {'space_id': sid}),
        'native', native.model_spec.schema_hash, snapshot.run_id, 'turn-original', 'attempt-original')
    request = ToolExecutorRequest(call=call, runtime_snapshot=snapshot, workspace_root=world.root,
        workspace_roots=(world.root,), operation_store_required=False)
    # 原local_unmanaged只读测试入口，不把它宣称为W13注册/managed operation store端到端。
    execution = ToolExecutor().execute(request)
    assert execution.result.ok and execution.result.handler_executed
    assert json.loads(execution.result.output)['space']['space_id'] == sid
    before = world.service.state_path.read_bytes()
    world.denied_spaces.add(sid)
    denied = ToolExecutor().execute(request)
    assert not denied.result.ok and denied.result.handler_executed is False
    assert 'BUSINESS_ACCESS_DENIED' in denied.decision.reason_codes
    assert before == world.service.state_path.read_bytes()
