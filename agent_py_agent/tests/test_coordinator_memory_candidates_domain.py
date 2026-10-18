"""六路径接口真实HTTP组件；临时接原producer/complete，发行dispatcher保持memory503。"""
import json

import pytest

from agent_py_agent.tests.test_coordinator_agents_domain import submit
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_memory_preview_transaction import read, seed


# LLM: 仅测试接真实管理producer与待验领域context；原权限/holder/业务/complete全保，不能把临时接线算发行endpoint验收。
# 函数用途: 在实际HTTP工作栈执行C4并在原活窗口完成同一回执。
def install_domain(monkeypatch, *, before=None, after=None):
    from agent_py_agent.agent.gateway_parts import http_service
    from agent_py_agent.agent.gateway_parts.coordinator_memory_candidates import (
        execute_coordinator_memory_candidates,
    )
    from agent_py_agent.agent.gateway_parts.coordinator_owner_management import (
        complete_owner_management_operation,
        coordinator_owner_management_operation,
    )
    from agent_py_agent.agent.gateway_parts.http_handlers import require_trusted_source

    observed = []

    def handle(handler, server):
        if require_trusted_source(handler):
            return
        body = handler._read_json()
        try:
            with coordinator_owner_management_operation(handler, server, body) as (source, receipt, scope):
                if source is not None:
                    observed.append(source)
                    if before:
                        before(source)
                    with execute_coordinator_memory_candidates(source, source.http_origin[3], scope) as (result, thread_id):
                        if after:
                            after(source, result)
                        receipt = complete_owner_management_operation(source, result, conversation_thread_id=thread_id)
            handler._send_json(200, receipt.to_dict())
        except BaseException as error:
            observed.append(error)
            handler._send_json(503, {'error_type': type(error).__name__})

    monkeypatch.setattr(http_service, 'handle_control', handle)
    return observed


def details(case, row):
    status, receipt = submit(case, '/memory candidates preview ' + row.candidate_id, 'preview-original')
    assert status == 200 and receipt['state'] == 'completed' and receipt['result']['ok'], receipt
    return receipt['result']['details']


def test_production_fresh_memory_lists_empty_owner_without_agent(cold):  # noqa: F811
    status, result = submit(cold, '/memory candidates', 'fresh-empty-owner')
    assert status == 200 and result['ok'] and result['details']['count'] == 0, result
    assert cold.server.agent is None and cold.constructions == []


@pytest.mark.parametrize('cold', ['local', 'feishu'], indirect=True)
@pytest.mark.parametrize('action,expected,status', [('confirm', 'completed', 'promoted'),
    ('reject', 'rejected', 'rejected'), ('cancel', 'cancelled', 'pending_review')])
def test_same_holder_original_seven_actions_and_real_formal_effect(cold, monkeypatch, action, expected, status):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import coordinator_client_memory as client
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as module

    row = seed(cold)
    instances, promotions = [], []
    run, ensure = module._run_candidate_action, client.ensure_owner_memory_promotion

    def observe(source, command, scope, resources, thread_id):
        instances.append(resources)
        assert source._management_memory_binding.evidence.active
        return run(source, command, scope, resources, thread_id)

    def promotion(resources):
        promotions.append(resources)
        return ensure(resources)

    monkeypatch.setattr(module, '_run_candidate_action', observe)
    monkeypatch.setattr(client, 'ensure_owner_memory_promotion', promotion)
    observed = install_domain(monkeypatch)
    listing = submit(cold, '/memory candidates', 'list-original')
    shown = submit(cold, '/memory candidates show ' + row.candidate_id, 'show-original')
    assert listing[0] == shown[0] == 200 and listing[1]['result']['details']['count'] == 1
    assert shown[1]['result']['details']['value']['content'] == row.content
    preview = details(cold, row)
    assert not promotions and all(value is instances[0] for value in instances)
    assert instances[0].promotion is None
    code, done = submit(cold, preview[action+'_command'], 'action-original')
    assert code == 200 and done['result']['details']['state'] == expected, (done, observed)
    assert done['result']['ok'] and len(promotions) == (0 if action == 'cancel' else 1)
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == status
    formal = instances[0].long_term.all()
    assert len(formal) == (1 if action == 'confirm' else 0)
    if formal:
        assert formal[0].content == row.content
    original_raw = read(cold, preview['nonce'])[1]
    assert submit(cold, preview[action+'_command'], 'action-original') == (code, done)
    code, replay = submit(cold, preview['confirm_command'], 'action-new-message')
    assert code == 200 and replay['result']['details']['state'] == expected
    code, receipt = submit(cold, preview['receipt_command'], 'receipt-original')
    assert code == 200 and receipt['result']['details']['state'] == expected
    assert read(cold, preview['nonce'])[1] == original_raw
    assert all(value is instances[0] for value in instances)
    assert len(promotions) == (0 if action == 'cancel' else 1)
    assert not instances[0].long_term._pending_access
    assert all(not source._management_memory_binding.evidence.active for source in observed)
    assert cold.server.agent is None and cold.constructions == []


def test_consumed_unknown_does_not_construct_promotion_on_late_message(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import coordinator_client_memory as client
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as module

    row, failures, instances = seed(cold), [], []
    run = module._run_candidate_action

    def observe(source, command, scope, resources, thread_id):
        instances.append(resources)
        return run(source, command, scope, resources, thread_id)

    def fail(resources):
        failures.append(resources)
        raise OSError('synthetic promotion construction failure')

    monkeypatch.setattr(module, '_run_candidate_action', observe)
    monkeypatch.setattr(client, 'ensure_owner_memory_promotion', fail)
    observed = install_domain(monkeypatch)
    preview = details(cold, row)
    code, result = submit(cold, preview['confirm_command'], 'construct-fails')
    assert code == 200 and result['result']['details']['state'] == 'consumed_unknown', (result, observed)
    assert len(failures) == 1 and instances[-1].promotion is None
    original = read(cold, preview['nonce'])[1]
    for action in ('confirm', 'receipt', 'cancel'):
        code, result = submit(cold, preview[action+'_command'], 'late-'+action)
        assert code == 200 and result['result']['details']['state'] == 'consumed_unknown'
    assert len(failures) == 1 and read(cold, preview['nonce'])[1] == original
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == 'pending_review'


def test_formal_success_finish_failure_keeps_real_partial_and_never_repromotes(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import coordinator_memory_candidates as module
    from agent_py_agent.agent.gateway_parts import memory_preview_transaction as transaction

    row, instances = seed(cold), []
    write, run = transaction.write_private_json_file_atomic_no_newline_unlocked, module._run_candidate_action

    def observe(source, command, scope, resources, thread_id):
        instances.append(resources)
        return run(source, command, scope, resources, thread_id)

    def fail(path, payload, **kwargs):
        if payload['result']['details']['state'] == 'completed':
            raise OSError('synthetic finish failure')
        return write(path, payload, **kwargs)

    monkeypatch.setattr(module, '_run_candidate_action', observe)
    monkeypatch.setattr(transaction, 'write_private_json_file_atomic_no_newline_unlocked', fail)
    observed = install_domain(monkeypatch)
    preview = details(cold, row)
    code, result = submit(cold, preview['confirm_command'], 'finish-failed')
    assert code == 200 and result['result']['details']['state'] == 'consumed_unknown', (result, observed)
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == 'promoted'
    assert len(instances[0].long_term.all()) == 1
    original = read(cold, preview['nonce'])[1]
    code, projected = submit(cold, preview['confirm_command'], 'finish-late')
    assert code == 200 and projected['result']['details']['state'] == 'consumed_unknown'
    assert read(cold, preview['nonce'])[1] == original and len(instances[0].long_term.all()) == 1


@pytest.mark.parametrize('failure', ['ordinary', 'native'])
def test_parent_unknown_after_c4_scope_exit_uses_still_live_factory_seal(cold, monkeypatch, failure):  # noqa: F811
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
    from agent_py_agent.agent.gateway_parts import control_operation_service as ops

    error = LocalControlCoordinatorUnavailable('synthetic native') if failure == 'native' else OSError('ordinary domain')
    calls, writer = [], ops._write_control_operation_json

    def write(path, payload, **kwargs):
        if payload['state'] == 'terminal_unknown':
            calls.append(True)
            assert kwargs['owner_management_source']._management_memory_binding.evidence.active
        return writer(path, payload, **kwargs)

    monkeypatch.setattr(ops, '_write_control_operation_json', write)

    def after(source, result):
        raise error

    observed = install_domain(monkeypatch, after=after)
    status, result = submit(cold, '/memory candidates', 'domain-exit')
    assert status == 503 and observed[-1] is error, (result, observed)
    source = observed[0]
    receipt = ops.read_gateway_control_operation(source.paths, source.receipt.operation_id, read_only=True)
    assert calls == ([] if failure == 'native' else [True])
    assert receipt.state == ('executing' if failure == 'native' else 'terminal_unknown')
    assert not source._management_memory_binding.evidence.active and not source.lease._commit_pins


@pytest.mark.parametrize('action', ['confirm', 'reject', 'cancel'])
@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
def test_original_group_configure_identity_is_separate_from_monitor_grant(cold, monkeypatch, action):  # noqa: F811
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.memory_store.candidates import CandidateService
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        OwnerIdentity,
        ensure_owner_home,
        home_paths_with_owner,
    )

    home = home_paths_with_owner(home_paths(cold.server.control_plane.home_root),
        ensure_owner_home(cold.server.control_plane.home_root, OwnerIdentity.provider_group('feishu', 'oc_coldagents')))
    candidates = CandidateService(home.owner_memory_candidates_jsonl)
    row = candidates.observe(CandidateObservation(candidate_type='project', content='本群项目使用UTC',
        subject_key='group.project', scope=MemoryScope('personal', 'personal'), origin='reviewed',
        promotion_target='long_term'))
    row = candidates.transition(row, 'pending_review', reviewer='alice')
    observed = install_domain(monkeypatch)
    code, receipt = submit(cold, '/memory candidates preview '+row.candidate_id, 'group-preview', as_group=True)
    assert code == 200 and receipt['result']['ok'], (receipt, observed)
    preview = receipt['result']['details']
    code, receipt = submit(cold, preview[action+'_command'], 'group-action', as_group=True)
    expected = {'confirm': 'completed', 'reject': 'rejected', 'cancel': 'cancelled'}[action]
    assert code == 200 and receipt['result']['details']['state'] == expected, (receipt, observed)
    assert not cold.seeded.memory_candidates.list()
    assert candidates.get(row.candidate_id).status == {'confirm': 'promoted', 'reject': 'rejected', 'cancel': 'pending_review'}[action]
    assert all(source.receipt_owner.owner_kind == 'group' for source in observed)
    assert cold.constructions == [] and cold.server.agent is None


def test_real_producer_revocation_during_review_keeps_consumed_unknown(cold, monkeypatch):  # noqa: F811
    from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
    from agent_py_agent.agent.gateway_parts import coordinator_client_memory as client
    from agent_py_agent.agent.memory_store.promotion import MemoryPromotionService

    row, current = seed(cold), []
    original, ensured = MemoryPromotionService.review, client.ensure_owner_memory_promotion

    def ensure(resources):
        return ensured(resources)

    monkeypatch.setattr(client, 'ensure_owner_memory_promotion', ensure)

    def review(service, *args, **kwargs):
        current[-1].lifetime.close()
        return original(service, *args, **kwargs)

    monkeypatch.setattr(MemoryPromotionService, 'review', review)
    observed = install_domain(monkeypatch, before=current.append)
    preview = details(cold, row)
    code, receipt = submit(cold, preview['confirm_command'], 'review-revoked')
    assert code == 503 and isinstance(observed[-1], LocalControlCoordinatorUnavailable), (receipt, observed)
    source = current[-1]
    # 原perform在进入current producer前就拒绝已关闭lifetime；不为此伪造首槽来源。
    assert source._native_source_failure is None
    nonce, raw = read(cold, preview['nonce'])
    assert nonce.result['details']['state'] == 'consumed_unknown'
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == 'pending_review'
    code, receipt = submit(cold, preview['confirm_command'], 'later-review-revoked')
    assert code == 200 and receipt['result']['details']['state'] == 'consumed_unknown'
    assert read(cold, preview['nonce'])[1] == raw
