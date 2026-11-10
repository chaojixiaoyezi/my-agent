# 模块用途: G5a-R1/R2（MSG-022）回归。R1：暂停运行在范围移出/未分配或业务空间归档后，原生 workflow_runs.resume 在任何控制写入
#   与副作用前被拒且运行记录不变；范围恢复后同一运行可再续跑；撤销后取消仍可收口；运行中途撤销使后续步骤在副作用前被拒、
#   脚本不执行。R2：原生服务缓存按工作流端口稳定来源失效（登记回退→显式覆盖、缺一显式、再回退）。只用 tmp 隔离 home；
#   空间归档的阻塞端口原生未接线（G4），测试只供空阻塞列表让真实归档转换运行。
#   R3（MSG-027）：非allow策略（skip_if_running/queue）的续跑在撤销后于真实dispatcher首次claim绑定/排队写入前被拒，
#   运行账本与claim/队列字节不变且无瞬时写；有效对照经同一真实dispatcher建claim或排队后真实执行，排队/跳过准入
#   回执如实投影为202 accepted / 未开始拒绝 / blocked（原把准入字典当运行记录读而断连）。
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.workflows import concurrency_policy
from agent_py_agent.agent.workflows import engine as workflow_engine
from agent_py_agent.agent.workflows import run_store as workflow_run_store
from agent_py_agent.agent.workflows.business_scope import WorkflowRunContextFactory
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_native_run import _flow, _runs, _script
from agent_py_agent.tests.test_business_v12_workflow_receipts import refused


def _spaces(native, prefix):  # noqa: F811
    space = ok(native, 'spaces.create', {'name': prefix + '-a'}, idempotency_key=prefix + '-space-create-a')['space_id']
    other = ok(native, 'spaces.create', {'name': prefix + '-b'}, idempotency_key=prefix + '-space-create-b')['space_id']
    return space, other


def _set_scope(native, name, scope, key):  # noqa: F811
    revision = ok(native, 'workflows.get', {'workflow_ref': name})['workflow']['scope_revision']
    ok(native, 'workflows.scope.set', {'workflow_ref': name, 'scope': scope}, idempotency_key=key,
       expected_revision=revision)


def _paused(native, agent, monkeypatch, space, key):  # noqa: F811
    """在首个副作用之前注入一次停止：运行落为可续跑暂停，副作用未开始（原生无需对账）。"""
    real, armed = workflow_engine._check_stop, [True]

    def stop_once():
        if armed:
            armed.clear()
            raise InterruptedError('simulated stop before the first effect')
        return real()

    before = {row.workflow_run_id for row in _runs(agent)}
    monkeypatch.setattr(workflow_engine, '_check_stop', stop_once)
    try:
        call(native, 'workflows.run', {'workflow_ref': 'r1-flow', 'space_id': space}, idempotency_key=key)
    finally:
        monkeypatch.setattr(workflow_engine, '_check_stop', real)
    [paused] = [row for row in _runs(agent) if row.workflow_run_id not in before]
    assert (paused.status, paused.effect_outcome) == ('paused_for_resume', 'not_started')
    assert not paused.steps[0].escalation_result.get('effect_started') and not paused.steps[0].output_snapshot
    return paused.workflow_run_id


def test_paused_run_resume_rechecks_current_scope_and_space_before_any_control_write(native, monkeypatch):  # noqa: F811
    server, agent = native
    agent.business_active_references = lambda space_id, actor: []
    _script(agent)
    space, other = _spaces(native, 'r1')
    ok(native, 'workflows.create', _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='r1-flow-create-0001')
    run_id = _paused(native, agent, monkeypatch, space, 'r1-flow-run-000001')
    spare = _paused(native, agent, monkeypatch, space, 'r1-flow-run-000002')
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)

    def resume(key, code, status=409):
        before = store.load_run(run_id).to_dict()
        if status == 409:
            refused(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, code, idempotency_key=key,
                    expected_revision=before['revision'])
        else:
            got, payload = call(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key=key,
                                expected_revision=before['revision'])
            assert (got, payload['error']['code'], payload['effect_outcome']) == (status, code, 'not_started'), payload
        assert store.load_run(run_id).to_dict() == before

    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [other]}, 'r1-scope-other-0001')
    resume('r1-resume-other-0001', 'WORKFLOW_SPACE_MISMATCH')
    _set_scope(native, 'r1-flow', {'scope_mode': 'unassigned', 'space_ids': []}, 'r1-scope-unassigned-01')
    resume('r1-resume-unassigned1', 'WORKFLOW_UNASSIGNED')
    # 业务空间归档：范围已恢复，但冻结空间不再活跃，续跑在控制写入前被拒，不重选别的空间。
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [space]}, 'r1-scope-restore-0001')
    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.archive', {'space_id': space}, idempotency_key='r1-space-archive-0001', expected_revision=revision)
    # 冻结状态表对 SPACE_ARCHIVED 一律给 503（既有映射，未改）；关键事实是 not_started 且记录不变。
    resume('r1-resume-archived-01', 'SPACE_ARCHIVED', status=503)
    # 撤销后取消仍可收回另一条暂停运行。
    cancelled = ok(native, 'workflow_runs.cancel', {'workflow_run_id': spare}, idempotency_key='r1-cancel-archived-1',
                   expected_revision=store.load_run(spare).revision)
    assert cancelled['outstanding_refs'] == [] and store.load_run(spare).status == 'cancelled'
    # 正对照：空间恢复后同一运行真实续跑并执行脚本，证明上面的拒绝来自撤销而非其它门。
    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.restore', {'space_id': space}, idempotency_key='r1-space-restore-0001', expected_revision=revision)
    done = ok(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key='r1-resume-restored-01',
              expected_revision=store.load_run(run_id).revision)
    finished = store.load_run(run_id)
    assert done['state'] == 'completed' and finished.status == 'succeeded'
    assert finished.steps[0].output_snapshot == {'total': 3}


def test_revocation_between_steps_refuses_the_next_side_effect_before_its_script_runs(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    space, other = _spaces(native, 'mid')
    flow = _flow('mid-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({'step_id': 'calc2', 'name': 'calc2', 'mode': 'deterministic_script',
                                        'script_id': 'calc'})
    ok(native, 'workflows.create', flow, idempotency_key='mid-flow-create-001')
    scripts, real = [], workflow_engine.run_script_with_datasources

    def run_then_revoke(*args, **kwargs):
        scripts.append(args[1])
        result = real(*args, **kwargs)
        if len(scripts) == 1:
            # 第一步脚本完成后、第二步准入前，经同一领域服务把范围移到另一空间（并发撤销）。
            services = next(iter(server._business_services.values()))
            current = services.workflows.execute('workflows.get', {'workflow_ref': 'mid-flow'}, actor=_trusted)
            services.workflows.execute('workflows.scope.set', {'workflow_ref': 'mid-flow',
                'scope': {'scope_mode': 'spaces', 'space_ids': [other]}}, actor=_trusted,
                expected_revision=current['workflow']['scope_revision'], idempotency_key='mid-scope-revoke-001')
        return result

    _trusted = SimpleNamespace(owner_id=agent.home_paths.owner_id, authorize=lambda action, facts: True)
    monkeypatch.setattr(workflow_engine, 'run_script_with_datasources', run_then_revoke)
    receipt = ok(native, 'workflows.run', {'workflow_ref': 'mid-flow', 'space_id': space},
                 idempotency_key='mid-flow-run-000001')
    record = WorkflowRunStore(agent.home_paths.owner_home_dir).load_run(receipt['workflow_run_id'])
    first, second = record.steps
    assert scripts == ['calc'] and first.status == 'succeeded', (first.error_code, first.error_message)
    assert first.output_snapshot == {'total': 3}
    assert (second.status, second.error_code) == ('failed', 'WORKFLOW_SPACE_MISMATCH')
    assert record.status != 'succeeded' and receipt['state'] != 'completed'


def test_native_services_follow_the_workflow_port_source(native, tmp_path):  # noqa: F811
    server, agent = native
    _script(agent)
    ok(native, 'workflows.create', _flow('ports-flow'), idempotency_key='ports-flow-create-01')
    assert ok(native, 'workflows.run', {'workflow_ref': 'ports-flow'}, idempotency_key='ports-run-fallback-1'
              )['state'] == 'completed'
    built = []

    def factory(owner_home, **options):
        built.append(owner_home)
        return WorkflowEngine(owner_home, **options)

    # 登记回退建好缓存后换成显式端口：新请求必须用显式工厂。
    agent.workflow_engine_factory, agent.business_context_provider = factory, WorkflowRunContextFactory()
    assert ok(native, 'workflows.run', {'workflow_ref': 'ports-flow'}, idempotency_key='ports-run-explicit-1'
              )['state'] == 'completed'
    assert len(built) == 1
    # 只剩一半显式端口：如实未就绪，不回落到旧缓存或登记工具，也不建运行。
    agent.business_context_provider = None
    runs = len(_runs(agent))
    status, payload = call(native, 'workflows.run', {'workflow_ref': 'ports-flow'}, idempotency_key='ports-run-partial-01')
    assert (status, payload['error']['code'], payload['effect_outcome']) == (503, 'HOST_NOT_READY', 'not_started')
    assert len(_runs(agent)) == runs and len(built) == 1
    # 撤去显式端口：回到同owner登记工具。
    agent.workflow_engine_factory = None
    assert ok(native, 'workflows.run', {'workflow_ref': 'ports-flow'}, idempotency_key='ports-run-fallback-2'
              )['state'] == 'completed'
    assert len(built) == 1
    assert next(iter(server._business_services.values())).workflow_port_source[0] == 'registry'


def _policy_ports(agent, policy):  # noqa: F811
    """显式宿主端口：引擎工厂只设默认并发策略，使原生运行与续跑经真实dispatcher；上下文仍由宿主工厂解析。"""
    def factory(owner_home, **options):
        engine = WorkflowEngine(owner_home, **options)
        engine.default_concurrency_policy = policy
        return engine

    agent.workflow_engine_factory, agent.business_context_provider = factory, WorkflowRunContextFactory()


def _evidence(agent, *run_ids):  # noqa: F811
    """原运行目录与本Owner并发claim/队列目录的全部文件字节（含新增文件）。"""
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    roots = [store.run_dir(run_id) for run_id in run_ids]
    roots.append(Path(agent.home_paths.owner_home_dir) / 'data' / 'workflow_concurrency')
    return {(str(root), str(path.relative_to(root))): path.read_bytes()
            for root in roots if root.exists() for path in sorted(root.rglob('*')) if path.is_file()}


def _resume_recording_writes(native, run_id, key, revision):  # noqa: F811
    """只记录并原样调用真实运行账本写口与claim/排队写口（不替换dispatcher），证明拒绝前连瞬时写也没有。"""
    writes = []
    with pytest.MonkeyPatch.context() as patch:
        for module, name in ((workflow_run_store, '_write_json'), (concurrency_policy, '_create_claim'),
                             (concurrency_policy, '_write_queue'), (concurrency_policy, '_unlink_same')):
            real = getattr(module, name)

            def spy(*args, _real=real, _name=name, **kwargs):
                writes.append(_name)
                return _real(*args, **kwargs)
            patch.setattr(module, name, spy)
        status, payload = call(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key=key,
                               expected_revision=revision)
    return status, payload, writes


@pytest.mark.parametrize('policy', ['skip_if_running', 'queue'])
def test_policy_resume_refuses_revoked_run_before_claim_rebinding(native, monkeypatch, policy):  # noqa: F811
    server, agent = native
    agent.business_active_references = lambda space_id, actor: []
    _script(agent)
    _policy_ports(agent, policy)
    tag = policy.replace('_', '-')
    space, other = _spaces(native, 'r3-' + tag)
    ok(native, 'workflows.create', _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='r3-flow-create-' + tag)
    run_id = _paused(native, agent, monkeypatch, space, 'r3-paused-run-' + tag)
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    # 原运行确经真实dispatcher取得claim；暂停时无未结副作用，claim已释放。续跑按原策略须新建claim并绑定run.json。
    claim = store._read(run_id, 'run.json')['concurrency_claim']
    assert (claim['record']['policy'], claim['phase']) == (policy, 'released')

    def refuse(key, code, status):
        before = _evidence(agent, run_id)
        got, payload, writes = _resume_recording_writes(native, run_id, key, store.load_run(run_id).revision)
        assert (got, payload['error']['code'], payload['effect_outcome']) == (status, code, 'not_started'), payload
        assert writes == [] and _evidence(agent, run_id) == before

    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [other]}, 'r3-scope-other-' + tag)
    refuse('r3-resume-moved-' + tag, 'WORKFLOW_SPACE_MISMATCH', 409)
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [space]}, 'r3-scope-back-' + tag)
    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.archive', {'space_id': space}, idempotency_key='r3-space-archive-' + tag,
       expected_revision=revision)
    refuse('r3-resume-archived-' + tag, 'SPACE_ARCHIVED', 503)
    # 正对照：空间恢复后同一运行经真实dispatcher重绑原claim、执行脚本并释放。
    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.restore', {'space_id': space}, idempotency_key='r3-space-restore-' + tag,
       expected_revision=revision)
    done = ok(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key='r3-resume-valid-' + tag,
              expected_revision=store.load_run(run_id).revision)
    finished = store.load_run(run_id)
    assert done['state'] == 'completed' and finished.status == 'succeeded'
    assert finished.steps[0].output_snapshot == {'total': 3}
    assert store._read(run_id, 'run.json')['concurrency_claim']['phase'] == 'released'


def _paused_after_first_step(native, agent, space, key):  # noqa: F811
    """首步已提交并发表后、次步副作用前注入一次停止：暂停运行带已提交副作用，claim按原规则保留（retained）。"""
    real_save, real_stop, armed = WorkflowRunStore.save_step_result, workflow_engine._check_stop, []

    def save_then_arm(self, workflow_run_id, step_record):
        saved = real_save(self, workflow_run_id, step_record)
        if step_record.step_id == 'calc' and step_record.status == 'succeeded':
            armed.append(True)
        return saved

    def stop_when_armed():
        if armed:
            armed.clear()
            raise InterruptedError('simulated stop between committed steps')
        return real_stop()

    before = {row.workflow_run_id for row in _runs(agent)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(WorkflowRunStore, 'save_step_result', save_then_arm)
        patch.setattr(workflow_engine, '_check_stop', stop_when_armed)
        call(native, 'workflows.run', {'workflow_ref': 'r1-flow', 'space_id': space}, idempotency_key=key)
    [paused] = [row for row in _runs(agent) if row.workflow_run_id not in before]
    return paused


@pytest.mark.parametrize('policy', ['skip_if_running', 'queue'])
def test_policy_resume_behind_a_claim_holder_refuses_revoked_run_without_any_write(native, monkeypatch, policy):  # noqa: F811
    server, agent = native
    _script(agent)
    tag = policy.replace('_', '-')
    space, other = _spaces(native, 'r3h-' + tag)
    flow = _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({'step_id': 'calc2', 'name': 'calc2', 'mode': 'deterministic_script',
                                        'script_id': 'calc'})
    ok(native, 'workflows.create', flow, idempotency_key='r3h-flow-create-' + tag)
    waiting = _paused(native, agent, monkeypatch, space, 'r3h-paused-run-' + tag)  # 登记回退端口：allow，无claim。
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    _policy_ports(agent, policy)
    holder = _paused_after_first_step(native, agent, space, 'r3h-holder-run-' + tag).workflow_run_id
    # 持有者：同策略原生运行首步已提交、次步前暂停，带未对账副作用（unknown）而保留claim；等待者只能排队或被跳过。
    assert (store.load_run(holder).status, store.load_run(holder).effect_outcome) == ('paused_for_resume', 'unknown')
    assert 'concurrency_claim' not in store._read(waiting, 'run.json')
    assert store._read(holder, 'run.json')['concurrency_claim']['phase'] == 'retained'
    workers = {thread.ident for thread in threading.enumerate() if thread.name.startswith('workflow-queue-')}
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [other]}, 'r3h-scope-other-' + tag)
    before = _evidence(agent, waiting, holder)
    status, payload, writes = _resume_recording_writes(native, waiting, 'r3h-resume-revoked-' + tag,
                                                       store.load_run(waiting).revision)
    assert (status, payload['error']['code'], payload['effect_outcome']) == (
        409, 'WORKFLOW_SPACE_MISMATCH', 'not_started'), payload
    assert writes == [] and _evidence(agent, waiting, holder) == before
    assert {thread.ident for thread in threading.enumerate() if thread.name.startswith('workflow-queue-')} <= workers
    # 正对照（范围恢复）：同一续跑与一条新运行经真实dispatcher遇到持有者——queue写排队票据并如实给202 accepted；
    #   skip_if_running续跑未开始（not_started）、新运行落skipped（blocked）。持有者经原生取消结清释放claim后真实执行。
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [space]}, 'r3h-scope-restore-' + tag)
    status, payload = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting},
                           idempotency_key='r3h-resume-valid-' + tag, expected_revision=store.load_run(waiting).revision)
    fresh_status, fresh = call(native, 'workflows.run', {'workflow_ref': 'r1-flow', 'space_id': space},
                               idempotency_key='r3h-fresh-run-' + tag)
    if policy == 'queue':
        assert (status, payload['data']) == (202, {'operation_id': waiting, 'state': 'accepted'}), payload
        assert (fresh_status, fresh['data']['state']) == (202, 'accepted'), fresh
    else:
        assert (status, payload['error']['code'], payload['effect_outcome']) == (
            503, 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING', 'not_started'), payload
        assert store.load_run(waiting).status == 'paused_for_resume'
        assert (fresh_status, fresh['data']['state']) == (200, 'blocked'), fresh
        assert store.load_run(fresh['data']['workflow_run_id']).status == 'skipped'
    cancelled = ok(native, 'workflow_runs.cancel', {'workflow_run_id': holder}, idempotency_key='r3h-cancel-holder-' + tag,
                   expected_revision=store.load_run(holder).revision)
    assert cancelled['outstanding_refs'] == [] and store._read(holder, 'run.json')['concurrency_claim']['phase'] == 'released'
    if policy == 'skip_if_running':
        ok(native, 'workflow_runs.resume', {'workflow_run_id': waiting}, idempotency_key='r3h-resume-after-' + tag,
           expected_revision=store.load_run(waiting).revision)
    expected = [waiting] + ([fresh['data']['workflow_run_id']] if policy == 'queue' else [])
    deadline = time.monotonic() + 20
    while (any(store._read(run_id, 'run.json')['status'] != 'succeeded' for run_id in expected)
           and time.monotonic() < deadline):
        time.sleep(0.05)
    for run_id in expected:
        finished = store.load_run(run_id)
        assert finished.status == 'succeeded', (run_id, finished.status)
        assert [step.output_snapshot for step in finished.steps] == [{'total': 3}, {'total': 3}]
