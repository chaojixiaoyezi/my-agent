"""隔离的 W2 judge 行为探针；所有模型均为显式内存替身。"""
from __future__ import annotations

import json
import multiprocessing
import socket
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.business_spaces import gap158_w2_judge as judge
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.test_gap_w2_intercept_actions import Harness

CONFIG = {'enabled': True, 'max_calls_per_hour': 20, 'timeout_seconds': 2}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'os-home'))
    monkeypatch.setenv('MY_AGENT_HOME', str(tmp_path / 'agent-home'))
    def forbidden(*args, **kwargs):
        raise AssertionError('judge probe must not use network')
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    name = 'agent_py_agent.agent.gateway_parts.business_api_v13_gap158_w2'
    modules = v13.V13_SLICE_MODULES if name in v13.V13_SLICE_MODULES else (*v13.V13_SLICE_MODULES, name)
    for key, value in zip(('_ACTIONS', 'ALIASES', 'V13_DEFS', 'JOB_KINDS', 'COLLECTIONS'), v13._load_registry(modules)):
        monkeypatch.setattr(v13, key, value)
    assert not judge._active_jobs
    yield
    assert not judge._active_jobs


def pending(h, index=0, *, expires=None):
    now = h.host.business_v13_clock()
    call = ToolCall(tool_name='run_command', arguments={'command': 'git status'}, run_id='run-probe',
        source_protocol='native', schema_hash='sha256:probe', attempt_id='attempt-probe',
        turn_id='turn-probe', call_id='call-' + str(index), operation_id='op-' + str(index),
        idempotency_key='key-' + str(index))
    runtime = SimpleNamespace(runtime_policy=SimpleNamespace(approval_policy=SimpleNamespace(owner_grant_parameters=())))
    request = SimpleNamespace(call=call, owner_scope_root=str(h.home))
    row = judge.add_pending(request, runtime, SimpleNamespace(decision='judge', rule_id='rule-probe'),
        task_id='task-probe', now=now, timeout_seconds=1800 if expires is None else expires)
    invocation = h.invocation('intercept.pending.decide', {})
    return row, invocation, request, runtime


def current(h, row):
    return V13Store(h.home).get('policy_pending_index', row['approval_id'])


def response(decision='escalate', **usage):
    return ModelResponse(json.dumps({'decision': decision, 'reason': 'probe'}), 'probe', usage=usage)


def join(thread):
    assert isinstance(thread, threading.Thread)
    thread.join(10)
    assert not thread.is_alive()


def delegate_same(call, entry):
    return {'approval': {'approval_id': entry['approval_id'], 'state': call.params['decision']},
        'state': call.params['decision']}


def test_usage_empty_is_readonly_and_contract_closed(tmp_path):
    home = tmp_path / 'missing'
    result = judge.usage(home, {}, now=10000)
    assert result == {'window': {'since': 10000, 'until': 10000},
        'calls': 0, 'approved': 0, 'denied': 0, 'escalated_to_owner': 0, 'failed': 0,
        'input_tokens': 0, 'output_tokens': 0}
    assert not home.exists()
    v13.v13_validate_result('intercept.judge.usage', result)


@pytest.mark.parametrize('config', [{}, {'enabled': False}, {**CONFIG, 'max_calls_per_hour': 0}])
def test_disabled_and_zero_do_not_call_or_reserve(tmp_path, config):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    assert judge.schedule_judge(call, row, config, model_call=lambda request: pytest.fail('called')) is None
    assert current(h, row)['state'] == 'pending'
    assert current(h, row)['judge_state'] == 'none'
    assert not h.home.joinpath(*judge._USAGE_FILE).exists()


@pytest.mark.parametrize('decision', ['approved', 'denied'])
def test_judge_decides_via_existing_decider_and_counts_provider_usage(tmp_path, monkeypatch, decision):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    monkeypatch.setattr(judge, '_delegate', delegate_same)
    seen = []
    def model(request):
        seen.append(request)
        return response(decision, input_tokens=9, cache_read_input_tokens=3, cache_creation_input_tokens=2, output_tokens=7)
    join(judge.schedule_judge(call, row, CONFIG, model_call=model, clock=h.host.business_v13_clock))
    assert len(seen) == 1
    assert seen[0].purpose == 'command_approval_judge'
    assert seen[0].response_schema == judge.JUDGE_RESPONSE_SCHEMA
    assert seen[0].tools is None
    saved = current(h, row)
    assert (saved['state'], saved['judge_state'], saved['decided_by']) == (decision, 'decided', 'judge')
    result = judge.usage(h.home, {}, now=h.host.business_v13_clock())
    assert result['calls'] == 1 and result[decision] == 1
    assert result['input_tokens'] == 14 and result['output_tokens'] == 7
    v13.v13_validate_result('intercept.judge.usage', result)
    assert judge.schedule_judge(call, row, CONFIG, model_call=model, clock=h.host.business_v13_clock) is None
    assert len(seen) == 1


@pytest.mark.parametrize('bad', [
    {'decision': 'escalate', 'reason': 'review'},
    {'decision': 'approved', 'reason': 'forged', 'response_ticket': 'model-ticket'},
    {'decision': 'approve', 'reason': 'wrong enum'},
    ModelResponse('{"decision":"approved","decision":"denied","reason":"x"}', 'probe'),
    ModelResponse('{"decision":"approved","reason":"x"}', 'probe', truncated=True),
    RuntimeError('model failed'),
])
def test_errors_escalation_extra_ticket_never_decide(tmp_path, monkeypatch, bad):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    monkeypatch.setattr(judge, '_delegate', lambda *args: pytest.fail('must not decide'))
    calls = []
    def model(request):
        calls.append(request)
        if isinstance(bad, Exception):
            raise bad
        return bad
    join(judge.schedule_judge(call, row, CONFIG, model_call=model, clock=h.host.business_v13_clock))
    assert len(calls) == 1
    saved = current(h, row)
    assert saved['state'] == 'pending'
    assert saved['judge_state'] == ('none' if isinstance(bad, dict) and bad['decision'] == 'escalate' else 'failed')
    assert judge.schedule_judge(call, row, CONFIG, model_call=model, clock=h.host.business_v13_clock) is None
    assert len(calls) == 1


def test_timeout_keeps_physical_slot_until_model_exit_and_ignores_late_approval(tmp_path, monkeypatch):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    other, other_call, _, _ = pending(h, 1)
    monkeypatch.setitem(sys.modules, 'agent_py_agent.agent.business_spaces.gap158_limits',
        SimpleNamespace(POLICY_JUDGE_WORKERS=1))
    monkeypatch.setattr(judge, '_delegate', lambda *args: pytest.fail('late approval'))
    entered, release, timed_out = threading.Event(), threading.Event(), threading.Event()
    real_state = judge._judge_state
    def tracked(*args, **kwargs):
        result = real_state(*args, **kwargs)
        if args[2] == 'failed':
            timed_out.set()
        return result
    monkeypatch.setattr(judge, '_judge_state', tracked)
    def blocked(request):
        entered.set()
        assert release.wait(8)
        return response('approved', input_tokens=4, output_tokens=2)
    thread = judge.schedule_judge(call, row, {**CONFIG, 'timeout_seconds': 1}, model_call=blocked,
        clock=h.host.business_v13_clock)
    try:
        assert entered.wait(5) and timed_out.wait(5)
        assert thread.is_alive() and len(judge._active_jobs) == 1
        assert current(h, row)['state'] == 'pending' and current(h, row)['judge_state'] == 'failed'
        assert judge.schedule_judge(other_call, other, CONFIG, model_call=blocked,
            clock=h.host.business_v13_clock) is None
    finally:
        release.set()
        join(thread)
    assert current(h, row)['state'] == 'pending'
    result = judge.usage(h.home, {}, now=h.host.business_v13_clock())
    assert result['calls'] == 1 and result['failed'] == 1 and result['approved'] == 0
    assert result['input_tokens'] == 4


def test_delegate_mismatched_state_is_unknown_not_approved(tmp_path, monkeypatch):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    monkeypatch.setattr(judge, '_delegate', lambda call, entry: {
        'approval': {'approval_id': entry['approval_id'], 'state': 'denied'}, 'state': 'denied'})
    with pytest.raises(HostAPIError) as caught:
        judge.decide_pending(replace(call, params={'approval_id': row['approval_id'], 'decision': 'approved'}))
    assert caught.value.code == 'OUTCOME_UNKNOWN' and caught.value.outcome == 'unknown'
    assert current(h, row)['state'] == 'deciding'


def test_decision_replay_requires_same_request_and_actor(tmp_path, monkeypatch):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    monkeypatch.setattr(judge, '_delegate', delegate_same)
    call = replace(call, params={'approval_id': row['approval_id'], 'decision': 'approved', 'reason': 'same'})
    first = judge.decide_pending(call)
    assert judge.decide_pending(call) == first
    for changed, actor in [({**call.params, 'decision': 'denied'}, 'owner'),
            ({**call.params, 'reason': 'changed'}, 'owner'), (call.params, 'judge')]:
        with pytest.raises(HostAPIError) as caught:
            judge.decide_pending(replace(call, params=changed), decided_by=actor)
        assert caught.value.code == 'IDEMPOTENCY_CONFLICT'
    with pytest.raises(HostAPIError):
        judge.decide_pending(replace(call, action='intercept.rules.delete'))


def _reserve_process(home, barrier, queue):
    from agent_py_agent.agent.business_spaces import gap158_w2_judge as module
    barrier.wait()
    queue.put(module._reserve_usage(Path(home), uuid4().hex, 1, 10000))


def test_rate_limit_is_durable_across_spawned_processes(tmp_path):
    home = tmp_path / 'owner'
    ctx = multiprocessing.get_context('spawn')
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    children = [ctx.Process(target=_reserve_process, args=(str(home), barrier, queue)) for _ in range(2)]
    for process in children:
        process.start()
    try:
        results = [queue.get(timeout=20) for _ in children]
        assert sorted(results) == [False, True]
    finally:
        for process in children:
            process.join(20)
            assert process.exitcode == 0
    assert not judge._reserve_usage(home, uuid4().hex, 1, 10001)
    assert judge._reserve_usage(home, uuid4().hex, 1, 13600)
    assert len(judge._read_usage(home)['records']) == 1


def test_schedule_enforces_the_persistent_rate_gate(tmp_path):
    h = Harness(tmp_path / 'root')
    seen = []
    def model(request):
        seen.append(request)
        return response()
    for number in range(2):
        row, call, _, _ = pending(h, number)
        join(judge.schedule_judge(call, row, {**CONFIG, 'max_calls_per_hour': 1}, model_call=model,
            clock=h.host.business_v13_clock))
        assert current(h, row)['state'] == 'pending'
        assert current(h, row)['judge_state'] == 'none'
    assert len(seen) == 1
    assert judge.usage(h.home, {}, now=h.host.business_v13_clock())['calls'] == 1


def test_usage_intersects_retention_and_filters_windows(tmp_path):
    home = tmp_path / 'owner'
    a, b = uuid4().hex, uuid4().hex
    assert judge._reserve_usage(home, a, 10, 10000)
    judge._record_usage(home, a, 10001, called=True, outcome='approved', response=response('approved', prompt_tokens=8, completion_tokens=3))
    assert judge._reserve_usage(home, b, 10, 11000)
    judge._record_usage(home, b, 11001, called=True, outcome='escalate')
    result = judge.usage(home, {'since': 0}, now=11001)
    assert result['window'] == {'since': 10000, 'until': 11001}
    assert result['calls'] == 2 and result['approved'] == 1 and result['escalated_to_owner'] == 1
    assert judge.usage(home, {'until': 10500}, now=11001)['calls'] == 1
    assert judge.usage(home, {'until': 9999}, now=11001)['calls'] == 0
    assert judge.usage(home, {'until': 9999}, now=11001)['window'] == {'since': 10000, 'until': 10000}
    assert judge.usage(home, {'since': 12000}, now=11001)['calls'] == 0
    assert judge.usage(home, {}, now=15000)['window'] == {'since': 11400, 'until': 15000}
    assert judge.usage(home, {}, now=15000)['calls'] == 0


@pytest.mark.parametrize(('decision', 'late_publish'), [('approved', False), ('denied', True)])
def test_native_permission_and_auxiliary_ledger_end_to_end(tmp_path, monkeypatch, decision, late_publish):
    from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
    from agent_py_agent.agent.common.json_io import read_json_object_report
    from agent_py_agent.agent.conversation.agent_tool_approval import publish_agent_tool_approval
    from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree
    agent, _, child = _bound_agent_tree(tmp_path)
    agent.config = replace(agent.config, business_spaces_enabled=True)
    calls = []
    class Backend:
        name = 'memory-probe'
        stream_enabled = False
        supports_provider_request_options = True
        def generate_structured(self, prompt, *, response_schema, on_chunk=None, request_options=None):
            calls.append((json.loads(prompt), response_schema, request_options))
            return response(decision, prompt_tokens=11, completion_tokens=6)
    agent.backend = Backend()
    tool_call = ToolCall(call_id='wire-call', tool_name='controlled_exec', arguments={'command': ['python3', '-V']},
        source_protocol='native', schema_hash='sha256:wire', run_id=child.id, turn_id='wire-turn', attempt_id='wire-attempt')
    request = SimpleNamespace(call=tool_call, owner_scope_root=str(agent.home_paths.owner_home_dir))
    runtime = SimpleNamespace(runtime_policy=SimpleNamespace(approval_policy=SimpleNamespace(owner_grant_parameters=())))
    row = judge.add_pending(request, runtime, SimpleNamespace(decision='judge', rule_id='wire-rule'), now=time.time())
    original = build_tool_approval_request(tool_call, request_id='wire-turn', round_number=1, call_index=0,
        description='controlled_exec python3 -V')
    handle = None
    awaiting = threading.Event()
    native_record = judge._native_record
    def observed_record(*args, **kwargs):
        try:
            return native_record(*args, **kwargs)
        except FileNotFoundError:
            awaiting.set()
            raise
    monkeypatch.setattr(judge, '_native_record', observed_record)
    if not late_publish:
        handle = publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id, request_value=original)
    errors = []
    native = judge._native_model_caller
    delegate = judge._delegate
    def observed_native(*args, **kwargs):
        try:
            return native(*args, **kwargs)
        except Exception as error:
            errors.append(error)
            raise
    def observed_delegate(*args, **kwargs):
        try:
            return delegate(*args, **kwargs)
        except Exception as error:
            errors.append(error)
            raise
    monkeypatch.setattr(judge, '_native_model_caller', observed_native)
    monkeypatch.setattr(judge, '_delegate', observed_delegate)
    captures = []
    frozen = v13.v13_call_frozen
    def observe_frozen(call, action, params, **kwargs):
        if action == 'approvals.decide':
            facts = {'owner_id': call.owner_id, **params}
            captures.append((call.context, facts))
            with pytest.raises(HostAPIError):
                call.context.check(action, {**facts, 'approval_id': 'another-approval'})
            with pytest.raises(HostAPIError):
                call.context.check('tools.grant', facts)
        return frozen(call, action, params, **kwargs)
    monkeypatch.setattr(v13, 'v13_call_frozen', observe_frozen)
    thread = judge.schedule_for_request(request, runtime, row, CONFIG, agent)
    if late_publish:
        assert awaiting.wait(5)
        assert calls == []
        handle = publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id, request_value=original)
    join(thread)
    if errors:
        raise errors[0]
    canonical = read_json_object_report(handle.path, context='judge.probe.canonical').payload
    assert canonical['status'] == 'decided'
    assert canonical['decision']['decision'] == decision
    assert canonical['request'] == original.to_dict()
    saved = V13Store(agent.home_paths.owner_home_dir).get('policy_pending_index', row['approval_id'])
    assert saved['state'] == decision and saved['decided_by'] == 'judge'
    assert len(calls) == 1
    assert calls[0][1] == judge.JUDGE_RESPONSE_SCHEMA
    assert calls[0][0]['tool_call']['arguments'] == tool_call.arguments
    assert calls[0][2].system_instruction
    records = [item for item in model_call_ledger(agent).records() if item.metadata.get('purpose') == 'command_approval_judge']
    assert len(records) == 1
    assert records[0].run_id == child.id and records[0].metadata['task_id'] == ''
    assert records[0].metadata['thread_id'] == child.agent_thread_id
    result = judge.usage(agent.home_paths.owner_home_dir, {}, now=time.time())
    assert result['calls'] == result[decision] == 1
    assert result['input_tokens'] == 11 and result['output_tokens'] == 6
    assert len(captures) == 1
    with pytest.raises(HostAPIError):
        captures[0][0].check('approvals.decide', captures[0][1])


def test_missing_real_backend_stays_pending_without_fake_success(tmp_path):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    join(judge.schedule_judge(call, row, CONFIG, clock=h.host.business_v13_clock))
    assert current(h, row)['state'] == 'pending' and current(h, row)['judge_state'] == 'failed'
    assert judge.usage(h.home, {}, now=h.host.business_v13_clock())['calls'] == 0


def test_timer_start_failure_releases_worker_slot(tmp_path, monkeypatch):
    h = Harness(tmp_path / 'root')
    row, call, _, _ = pending(h)
    def broken_start(self):
        raise RuntimeError('timer start failure')
    monkeypatch.setattr(threading.Timer, 'start', broken_start)
    join(judge.schedule_judge(call, row, CONFIG, model_call=lambda request: pytest.fail('must not invoke'),
        clock=h.host.business_v13_clock))
    assert not judge._active_jobs
    assert current(h, row)['state'] == 'pending' and current(h, row)['judge_state'] == 'failed'


@pytest.mark.parametrize('parts', [judge._USAGE_PARTS, judge._USAGE_FILE])
def test_usage_symlink_is_not_followed(tmp_path, parts):
    home, external = tmp_path / 'home', tmp_path / 'external'
    external.mkdir()
    target = home.joinpath(*parts)
    target.parent.mkdir(parents=True)
    target.symlink_to(external, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        judge._reserve_usage(home, uuid4().hex, 1, 10000)
    assert list(external.iterdir()) == []


def test_corrupt_and_duplicate_usage_fail_closed(tmp_path):
    home, job_id = tmp_path / 'home', uuid4().hex
    judge._reserve_usage(home, job_id, 2, 10000)
    path = home.joinpath(*judge._USAGE_FILE)
    state = json.loads(path.read_text())
    state['records'].append(dict(state['records'][0]))
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        judge._reserve_usage(home, uuid4().hex, 2, 10000)
    with pytest.raises(ValueError):
        judge.usage(home, {}, now=10000)


# 以下用真实 ActionPolicy、V13 CRUD、分段历史和原审批 binding 做组合回归。
def policy_request(h, arguments, *, command=False, boundary=None, approval='never', mode='auto'):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicyRequest
    from agent_py_agent.agent.tooling.models import (
        ApprovalPolicy,
        EffectResolverPolicy,
        IdempotencyPolicy,
        ToolModelSpec,
        ToolRuntime,
        ToolRuntimePolicy,
        ToolRuntimeSnapshot,
    )
    name = 'run_command' if command else 'read_file'
    spec = ToolModelSpec(name, '隔离策略回归', {'type': 'object', 'properties': {
        'command': {'type': 'string'}, 'path': {'type': 'string'}, 'url': {'type': 'string'},
        'working_dir': {'type': 'string'}}, 'additionalProperties': False})
    effect = EffectResolverPolicy(strategy='command', command_parameter='command') if command else EffectResolverPolicy()
    runtime = ToolRuntime(spec, ToolRuntimePolicy(effect, approval_policy=ApprovalPolicy(mode=approval),
        idempotency_policy=IdempotencyPolicy(scope='operation')), SimpleNamespace(agent=h.host))
    snapshot = ToolRuntimeSnapshot('policy-run', (runtime,), frozenset({name}), (), None)
    call = ToolCall('policy-call', name, arguments, 'native', spec.schema_hash,
        'policy-run', 'policy-turn', 'policy-attempt', operation_id='policy-operation',
        idempotency_key='policy-call-key')
    return ActionPolicyRequest(call, snapshot, h.host.workspace_root, owner_scope_root=str(h.home),
        write_boundary=boundary or {}, approval_mode=mode, now=h.host.business_v13_clock())


def configured(h, **values):
    h.host._capability_config_runtime_snapshot = SimpleNamespace(config=SimpleNamespace(**values))


def test_switches_off_match_original_gate_matrix_without_policy_io(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w2_policy_store as store
    from agent_py_agent.agent.tooling import action_policy as gate
    h = Harness(tmp_path / 'root')
    cases = [({'path': 'file'}, False), ({'path': '../outside'}, False),
        ({'invalid': 'schema'}, False), ({'command': 'cat plain.txt'}, True),
        ({'command': 'mkdir local'}, True), ({'command': 'shutdown'}, True)]
    for arguments, shell in cases:
        request = policy_request(h, arguments, command=shell)
        with monkeypatch.context() as patch:
            patch.setattr(store, 'get_snapshot', lambda *a, **kw: pytest.fail('disabled policy I/O'))
            actual = gate.ActionPolicy().decide(request)
            # 仅去掉本次新增门，原路径、命令、审批、沙箱和返回证据逐字比较。
            patch.setattr(gate, '_configured_policy_decision', lambda *a, **kw: None)
            original = gate.ActionPolicy().decide(request)
        assert actual.to_dict() == original.to_dict()


def test_command_deny_ask_and_explicit_allow_do_not_bypass_original_approval(tmp_path):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy
    h = Harness(tmp_path / 'root')
    configured(h, command_approval_filter_enabled=True)
    created = h.call('intercept.rules.create', {'name': 'command', 'tool': 'run_*', 'decision': 'deny'})
    request = policy_request(h, {'command': 'cat file.txt'}, command=True)
    denied = ActionPolicy().decide(request)
    assert denied.status == 'deny' and denied.reason_codes == ('POLICY_RULE_DENIED',)
    rid = created['rule']['rule_id']
    h.call('intercept.rules.update', {'rule_id': rid, 'decision': 'ask'}, revision=1)
    asked = ActionPolicy().decide(request)
    assert asked.status == 'ask' and asked.approval_request['kind'] == 'command_approval_filter'
    page = h.call('intercept.pending.list')
    assert len(page['items']) == 1 and page['items'][0]['rule_id'] == rid
    expected = build_tool_approval_request(request.call, request_id='policy-turn', round_number=1,
        call_index=0, description='original consumer')
    assert page['items'][0]['approval_id'] == expected.permission_id
    approved = expected.approved_binding(__import__('agent_py_agent.agent.contracts.tool_approval',
        fromlist=['ToolApprovalDecision']).ToolApprovalDecision(expected.permission_id, 'approved'))
    assert ActionPolicy().decide(replace(request, write_boundary={'approved_actions': [approved]})).status == 'allow'
    changed = replace(request.call, arguments={'command': 'cat another.txt'})
    mismatch = ActionPolicy().decide(replace(request, call=changed, write_boundary={'approved_actions': [approved]}))
    assert mismatch.status != 'allow'
    h.call('intercept.rules.update', {'rule_id': rid, 'decision': 'allow'}, revision=2)
    retained = ActionPolicy().decide(policy_request(h, {'command': 'cat file.txt'}, command=True,
        approval='always', mode='ask'))
    assert retained.status == 'ask' and retained.approval_request['kind'] == 'tool_action'
    records = h.call('intercept.history.list')['items']
    assert [row['decision'] for row in records][:2] == ['deny', 'ask']
    assert records[-1]['decision'] == 'allow'


@pytest.mark.parametrize('command', ['cat secret/file', 'cat ./secret/file',
    'head -n 1 secret/file', 'bash -c "cat secret/file"'])
def test_relative_workspace_scope_denies_shell_operands_and_nested_literal(tmp_path, command):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy
    h = Harness(tmp_path / 'root')
    configured(h, target_scope_policy_enabled=True)
    h.call('scope_policy.rules.create', {'kind': 'path_deny', 'pattern': 'secret'})
    result = ActionPolicy().decide(policy_request(h, {'command': command}, command=True))
    assert result.status == 'deny' and result.reason_codes == ('TARGET_SCOPE_DENIED',)


def test_domain_suffix_idna_dot_and_label_boundaries_through_store_and_gate(tmp_path):
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy
    h = Harness(tmp_path / 'root')
    configured(h, target_scope_policy_enabled=True)
    h.call('scope_policy.rules.create', {'kind': 'domain_allow', 'pattern': '.BÜCHER.example.'})
    for host, expected in [('api.xn--bcher-kva.example', 'allow'), ('badxn--bcher-kva.example', 'deny')]:
        decision = ActionPolicy().decide(policy_request(h, {'url': 'https://' + host + '/'}))
        assert decision.status == expected


def test_corrupt_rule_file_diagnoses_once_and_keeps_tool_path_unblocked(tmp_path, caplog):
    from agent_py_agent.agent.business_spaces import gap158_w2_policy_store as store
    from agent_py_agent.agent.tooling.action_policy import ActionPolicy
    h = Harness(tmp_path / 'root')
    configured(h, command_approval_filter_enabled=True)
    h.call('intercept.rules.create', {'name': 'deny', 'tool': '*', 'decision': 'deny'})
    (h.home / 'data/business_v13/policy_command_rules.json').write_text('{broken')
    store._warning_generations.clear()
    request = policy_request(h, {'command': 'cat file'}, command=True)
    with caplog.at_level('WARNING', logger=store.__name__):
        assert ActionPolicy().decide(request).status == 'allow'
        first = [row for row in caplog.records if 'policy_command_rules' in row.message]
        assert len(first) == 1
        assert ActionPolicy().decide(request).status == 'allow'
        assert len([row for row in caplog.records if 'policy_command_rules' in row.message]) == 1


def test_history_cursor_rotation_owner_binding_and_append_only(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w2_policy_history as history
    original_limit = history.limit_value
    monkeypatch.setattr(history, 'limit_value', lambda key: {'POLICY_HISTORY_SEGMENT_BYTES': 700,
        'POLICY_HISTORY_MAX_SEGMENTS': 3, 'POLICY_HISTORY_SCAN_MAX_RECORDS': 3}.get(key, original_limit(key)))
    h = Harness(tmp_path / 'root')
    saved = [history.append_record(h.home, {'at': number, 'tool': 'read_file',
        'decision': 'deny', 'rule_source': 'global', 'execution_id': 'execution-' + str(number)}) for number in range(5)]
    page = h.call('intercept.history.list', {'limit': 1})
    cursor = page['next_cursor']
    assert cursor and page['items'] == [saved[0]]
    other = Harness(tmp_path / 'root', 'other')
    assert other.call('intercept.history.list')['items'] == []
    with pytest.raises(ValueError) as caught:
        other.call('intercept.history.list', {'limit': 1, 'cursor': cursor})
    assert caught.value.code == 'CURSOR_EXPIRED'
    rows = list(page['items'])
    while page['next_cursor']:
        page = h.call('intercept.history.list', {'limit': 1, 'cursor': page['next_cursor']})
        rows.extend(page['items'])
    assert rows == saved
    assert h.call('intercept.history.execution', {'execution_id': 'execution-2'}) == {'decision': saved[2]}
    names = history._index(h.home)
    previous = (h.home.joinpath(*history._PARTS, names[-1])).read_bytes()
    history.append_record(h.home, {'tool': 'read_file', 'decision': 'ask', 'rule_source': 'global'})
    if history._index(h.home)[-1] == names[-1]:
        assert (h.home.joinpath(*history._PARTS, names[-1])).read_bytes().startswith(previous)
    for number in range(20):
        history.append_record(h.home, {'at': number, 'tool': 'read_file', 'decision': 'deny', 'rule_source': 'global'})
    assert len(history._index(h.home)) <= 3
    with pytest.raises(ValueError) as caught:
        h.call('intercept.history.list', {'limit': 1, 'cursor': cursor})
    assert caught.value.code == 'CURSOR_EXPIRED'


def test_task_summary_uses_latest_fixed_window_and_never_splits_groups(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces import gap158_w2_policy_history as history
    original = history.limit_value
    monkeypatch.setattr(history, 'limit_value', lambda key: 4 if key == 'POLICY_TASK_SUMMARY_SCAN_RECORDS' else original(key))
    h = Harness(tmp_path / 'root')
    for task in ['old', 'old', 'a', 'b', 'a', 'c']:
        history.append_record(h.home, {'at': 10, 'task_id': task, 'tool': 'read_file',
            'decision': 'deny', 'rule_source': 'global'})
    result, rows = h.call('intercept.task.list', {'limit': 1}), []
    while True:
        rows.extend(result['items'])
        assert result['coverage'] == 'partial'
        if not result['next_cursor']:
            break
        # 游标维持旧字节上界；新写入不改变当前摘要窗口。
        history.append_record(h.home, {'task_id': 'new', 'tool': 'read_file', 'decision': 'allow', 'rule_source': 'global'})
        result = h.call('intercept.task.list', {'limit': 1, 'cursor': result['next_cursor']})
    assert [row['task_id'] for row in rows] == ['a', 'b', 'c']
    assert [row['deny'] for row in rows] == [2, 1, 1]
