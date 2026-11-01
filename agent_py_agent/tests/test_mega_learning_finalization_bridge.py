"""W5 收尾与冻结 W6 端口的合同验证，不冒充 Scheduler/W2/W6 产品联验。

fake 只承担尚未落地的 canonical 完成复核端口；候选、收尾与原学习队列仍执行真实代码。
"""
import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import _finalization_service as mod
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.capability.skill_learning_store import (
    REQUEST_SCHEMA_VERSION,
    SkillLearningStore,
    request_key,
    utc_now,
)
from agent_py_agent.agent.conversation.business_contracts import TrustedTaskCompletion
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_mega_cache_business_recovery import business_context


def context():
    response = ModelResponse(text='已完成核对。', backend='fake')
    response.turn_end_reason = 'completed'
    return FinalizeContext(user_prompt='核对资料', final_prompt='prompt', final_response=response, memories=[],
        executed_tools=['read_file'] * 6,
        archive_tool_calls=[{'tool': 'read_file', 'ok': True, 'tool_round': i + 1, 'model_parameters': {'path': f'{i}.txt'}} for i in range(6)],
        routed_context=SimpleNamespace(matches=[], required_read_paths=[], candidate_paths=[]), resume_context_result=None,
        runtime_injections=[], compression_snapshot_id='', compression_snapshot_path='', compression_applied=False,
        request_id='request', run_id='run', task_id='runtime-task', attempt_id='attempt', source='background_main_agent',
        do_save=False, task_attributes={'conversation_thread_id': 'thread', 'business_context': business_context().to_dict()},
        recovery_task_refs=None, recovery_content_paths=None, recovery_next_actions=None, tool_rounds=6)


def completion(ctx):
    return TrustedTaskCompletion(owner_id='owner', thread_id='thread', task_id='stask-original', run_id=ctx.run_id,
        attempt_id=ctx.attempt_id, terminal_status='completed', claim_ref='claim:exact', completion_ref='completion:exact',
        space_id='space-a', project_ref='project-a', source_kind='background_main_agent', tool_rounds=ctx.tool_rounds,
        result_refs=('result:exact',), trajectory_digest='canonical-digest')


def harness(tmp_path, monkeypatch, *, include=True):
    agent = SimpleNamespace(root=tmp_path, home_paths=SimpleNamespace(owner_id='owner', owner_home_dir=tmp_path / 'home'),
        config=AgentConfig(), confirmed=None, include=include)
    legacy = []
    agent.skill_learning = SimpleNamespace(enqueue_from_finalize=lambda ctx: legacy.append(ctx))
    store_path = tmp_path / 'learning'
    calls, archives = [], []

    def enqueue_completed_task_learning(*, completion, host):
        calls.append(completion)
        # 这是冻结端口 fake，不用 DTO 的类型或 Python 构造成功冒充 canonical 证明。
        if host.confirmed != completion or not host.include or completion.tool_rounds < 6:
            return {'status': 'ineligible', 'reason': 'canonical_or_policy_not_ready'}
        key = request_key(json.dumps([completion.owner_id, completion.task_id, completion.run_id,
                                    completion.attempt_id, completion.completion_ref]))
        outcome = SkillLearningStore(store_path, store_path / 'skills').enqueue({'schema_version': REQUEST_SCHEMA_VERSION,
            'request_key': key, 'created_at': utc_now(), 'attempts': 0, 'request_id': 'request',
            'run_id': completion.run_id, 'task_id': completion.task_id, 'thread_id': completion.thread_id,
            'source': completion.source_kind, 'tool_rounds': completion.tool_rounds,
            'trajectory_digest': completion.trajectory_digest, 'tool_trace': []})
        return {'status': 'enqueued' if outcome == 'queued' else outcome, 'reason': ''}

    port = ModuleType('agent_py_agent.agent.capability.task_learning_bridge')
    port.enqueue_completed_task_learning = enqueue_completed_task_learning
    monkeypatch.setitem(sys.modules, port.__name__, port)
    # 与学习无关的用量/PTY/包审计独立测试已有覆盖；不在本合同测试启动它们的后台或读真实 owner。
    monkeypatch.setattr(mod, '_current_model_call_summary', lambda *_: {})
    monkeypatch.setattr(mod, '_reclaim_turn_pty_sessions', lambda *_: {})
    monkeypatch.setattr(mod, '_pack_verification_facts', lambda *_: None)
    monkeypatch.setattr(mod.FinalizationService, '_estimate_token_usage', lambda *_: {'turn': 0, 'cumulative': 0, 'active': 0})
    from agent_py_agent.agent.conversation import task_promotion
    monkeypatch.setattr(task_promotion, 'complete_current_conversation_task', lambda *_a, **_kw: None)
    original = mod.FinalizationService._archive_run_if_needed

    def archive(service, params):
        archives.append(params.do_save)
        return original(service, params)

    monkeypatch.setattr(mod.FinalizationService, '_archive_run_if_needed', archive)
    return agent, mod.FinalizationService(agent), calls, legacy, archives, store_path, port


@pytest.mark.parametrize('include', [True, False])
def test_pending_candidate_then_confirmed_port_and_restart_queue_replay(tmp_path, monkeypatch, include):
    agent, service, calls, legacy, archives, store_path, _port = harness(tmp_path, monkeypatch, include=include)
    ctx = context()
    result = service.finalize(ctx)
    candidate = result.tool_runtime_evidence['task_learning_candidate']
    assert candidate['status'] == 'pending_completion'
    assert 'terminal_status' not in candidate and 'claim_ref' not in candidate and 'completion_ref' not in candidate
    assert candidate['business_context'] == business_context().to_dict()
    assert candidate['runtime_task_id'] == 'runtime-task' and candidate['business_context']['task_id'] == 'stask-original'
    assert not calls and not legacy and archives == [False]
    assert not SkillLearningStore(store_path, store_path / 'skills').pending_requests()
    proof = completion(ctx)
    bound = replace(ctx, tool_runtime_evidence={'trusted_task_completion': proof})
    # 仅构造 DTO 不够；fake canonical 未确认时仍没有入队。
    service.finalize(bound)
    assert bound.tool_runtime_evidence['task_learning_enqueue']['status'] == 'ineligible'
    agent.confirmed = proof
    service.finalize(bound)
    assert bound.tool_runtime_evidence['task_learning_enqueue']['status'] == ('enqueued' if include else 'ineligible')
    # 重建真实队列实例，沿原 request_key 去重，不在 W5 新建 seen-set/完成账。
    service.finalize(bound)
    assert bound.tool_runtime_evidence['task_learning_enqueue']['status'] == ('duplicate' if include else 'ineligible')
    assert len(SkillLearningStore(store_path, store_path / 'skills').pending_requests()) == int(include)
    assert legacy == [] and archives == [False] * 4 and all(not getattr(value, 'do_save', False) for value in legacy)


@pytest.mark.parametrize('reason,status', [('aborted', 'cancelled'), ('error', 'failed'), ('interrupted', 'unfinished')])
def test_unfinished_or_cancelled_does_not_publish_success_candidate_or_call_bridge(tmp_path, monkeypatch, reason, status):
    agent, service, calls, legacy, archives, _path, _port = harness(tmp_path, monkeypatch)
    ctx = context()
    ctx.final_response.turn_end_reason, ctx.final_response.runtime_status = reason, status
    proof = completion(ctx)
    agent.confirmed = proof
    result = service.finalize(replace(ctx, tool_runtime_evidence={'trusted_task_completion': proof}))
    assert 'task_learning_candidate' not in result.tool_runtime_evidence
    assert 'trusted_task_completion' not in result.tool_runtime_evidence
    assert not calls and not legacy and archives == [False]


@pytest.mark.parametrize('bad', ['json', 'owner', 'run', 'attempt', 'thread', 'source', 'task_local', 'unknown_source'])
def test_untrusted_or_mismatched_completion_is_not_forwarded(tmp_path, monkeypatch, bad):
    agent, service, calls, legacy, _archives, _path, _port = harness(tmp_path, monkeypatch)
    ctx = context()
    proof = completion(ctx)
    if bad == 'json':
        proof = {**proof.to_dict(), 'trusted': True}
    elif bad == 'task_local':
        workspace = tmp_path / 'child-workspace'
        workspace.mkdir()
        ctx.task_attributes['agent_run_workspace_dir'] = str(workspace)
        ctx = replace(ctx, context_scope='task_local')
    elif bad == 'unknown_source':
        ctx = replace(ctx, source='unknown')
    else:
        field = {'owner': 'owner_id', 'run': 'run_id', 'attempt': 'attempt_id', 'thread': 'thread_id', 'source': 'source_kind'}[bad]
        proof = replace(proof, **{field: 'wrong'})
    agent.confirmed = proof
    service.finalize(replace(ctx, tool_runtime_evidence={'trusted_task_completion': proof}))
    assert calls == [] and legacy == []


def test_insufficient_rounds_and_unknown_bridge_error_are_not_reclassified_as_success(tmp_path, monkeypatch):
    agent, service, calls, _legacy, _archives, path, port = harness(tmp_path, monkeypatch)
    ctx = replace(context(), tool_rounds=5)
    proof = completion(ctx)
    agent.confirmed = proof
    ctx = replace(ctx, tool_runtime_evidence={'trusted_task_completion': proof})
    service.finalize(ctx)
    assert calls == [proof] and not SkillLearningStore(path, path / 'skills').pending_requests()

    def fail(*, completion, host):
        raise OSError('canonical store unavailable')

    monkeypatch.setattr(port, 'enqueue_completed_task_learning', fail)
    with pytest.raises(OSError, match='canonical store unavailable'):
        service.finalize(ctx)


def test_real_archive_start_progress_and_final_project_only_texts_and_discover_workspace(tmp_path, monkeypatch):
    from pathlib import Path

    from agent_py_agent.agent.agent_core.runtime import live_archive
    from agent_py_agent.agent.prompting_parts.runtime_injection import (
        RUNTIME_DIRECT_CHILDREN_SOURCE,
        RUNTIME_GOAL_SOURCE,
        RuntimeInjectionFragment,
    )
    from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

    workspace = f'# Current Task Workspace\n- output_dir: {tmp_path / "output"}'
    values = ['', RuntimeInjectionFragment(workspace, RUNTIME_GOAL_SOURCE),
              RuntimeInjectionFragment(None, RUNTIME_DIRECT_CHILDREN_SOURCE), ' raw\nbytes ']
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(),
        home_paths=SimpleNamespace(owner_id='owner', owner_home_dir=tmp_path))
    captured = []
    original = mod.write_runtime_fact_source

    def write(request):
        assert request.runtime_injections == ('', workspace, ' raw\nbytes ')
        path = original(request)
        payload = json.loads((Path(path) / 'task.json').read_text())
        assert payload['desired_outputs'] == [{'artifact_id': 'task_output_dir', 'kind': 'directory', 'target_path': str(tmp_path / 'output')}]
        assert 'RuntimeInjectionFragment' not in json.dumps(payload)
        captured.append(request)
        return path

    monkeypatch.setattr(mod, 'write_runtime_fact_source', write)
    monkeypatch.setattr(live_archive, 'write_runtime_fact_source', write)
    params = _tool_loop_params(runtime_injections=values, save=True)
    live_archive.write_runtime_fact_start_if_enabled(agent, params)
    live_archive.update_runtime_fact_progress_if_enabled(agent, params, tool_round=6)
    mod.FinalizationService(agent)._write_runtime_fact_source_if_needed(
        replace(context(), do_save=True, runtime_injections=values), 'final-request', {})
    assert [request.phase for request in captured] == ['started', 'tool_loop', 'final']


def test_both_real_token_estimation_boundaries_preserve_list_serialization_without_repr(tmp_path, monkeypatch):
    from dataclasses import asdict

    from agent_py_agent.agent.prompting_parts.runtime_injection import (
        RUNTIME_DIRECT_CHILDREN_SOURCE,
        RUNTIME_GOAL_SOURCE,
        RuntimeInjectionFragment,
    )

    values = ['', RuntimeInjectionFragment('full\n goal ', RUNTIME_GOAL_SOURCE),
              RuntimeInjectionFragment(None, RUNTIME_DIRECT_CHILDREN_SOURCE), ' raw ']
    expected = ['', 'full\n goal ', ' raw ']
    original = mod.estimate_tokens
    observed = []

    def estimate(value):
        if isinstance(value, list):
            observed.append(value)
        assert 'RuntimeInjectionFragment(' not in str(value)
        return original(value)

    monkeypatch.setattr(mod, 'estimate_tokens', estimate)
    agent = SimpleNamespace(root=tmp_path, config=AgentConfig(),
        home_paths=SimpleNamespace(owner_id='owner', owner_home_dir=tmp_path))
    service = mod.FinalizationService(agent)
    ctx = replace(context(), final_prompt='', runtime_injections=values)
    ledger = service._estimate_token_usage(mod._estimate_token_params(ctx, 'request'))
    result = service._build_agent_run_result(mod.BuildAgentRunResultParams(ctx, None, ledger, 'request', {}))
    assert observed.count(expected) == 2
    assert result.runtime_injection_token_estimate == original(expected)
    assert 'RuntimeInjectionFragment(' not in json.dumps(asdict(result), default=str)
