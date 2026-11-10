# 模块用途: G5a-R1/R2（MSG-022）回归。R1：暂停运行在范围移出/未分配或业务空间归档后，原生 workflow_runs.resume 在任何控制写入
#   与副作用前被拒且运行记录不变；范围恢复后同一运行可再续跑；撤销后取消仍可收口；运行中途撤销使后续步骤在副作用前被拒、
#   脚本不执行。R2：原生服务缓存按工作流端口稳定来源失效（登记回退→显式覆盖、缺一显式、再回退）。只用 tmp 隔离 home；
#   空间归档的阻塞端口原生未接线（G4），测试只供空阻塞列表让真实归档转换运行。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.workflows import engine as workflow_engine
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
