# 模块用途: G5a-R1/R2（MSG-022）回归。R1：暂停运行在范围移出/未分配或业务空间归档后，原生 workflow_runs.resume 在任何控制写入
#   与副作用前被拒且运行记录不变；范围恢复后同一运行可再续跑；撤销后取消仍可收口；运行中途撤销使后续步骤在副作用前被拒、
#   脚本不执行。R2：原生服务缓存按工作流端口稳定来源失效（登记回退→显式覆盖、缺一显式、再回退）。只用 tmp 隔离 home；
#   空间归档的阻塞端口原生未接线（G4），测试只供空阻塞列表让真实归档转换运行。
#   R1补（MSG-027）：旧二参可调用上下文端口创建的运行同样以当前Actor复核冻结空间归档与项目撤销。
#   R3（MSG-027）：非allow策略（skip_if_running/queue）的续跑在撤销后于真实dispatcher首次claim绑定/排队写入前被拒，
#   运行账本与claim/队列字节不变且无瞬时写；有效对照经同一真实dispatcher建claim或排队后真实执行，排队/跳过准入
#   回执如实投影为202 accepted / 未开始拒绝 / blocked（原把准入字典当运行记录读而断连）。
#   FLAG-8（MSG-036/MSG-037/MSG-039）：排队或执行中的同键续跑与同键新运行重放沿用原准入身份（同回执、不增票据/等待线程、
#   零写入，原生入口不与活回调死锁），同键不同意图/修订在写入前幂等冲突（409未开始），同暂停运行异键竞争控制返回503未开始；
#   结清、取消或失败后均如实投影耐久终态且零写入。
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.business_context import resolve_workflow_run_context
from agent_py_agent.agent.workflows import concurrency_policy
from agent_py_agent.agent.workflows import engine as workflow_engine
from agent_py_agent.agent.workflows import run_store as workflow_run_store
from agent_py_agent.agent.workflows.business_scope import WorkflowRunContextFactory
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowGovernance, workflow_candidate_hash
from agent_py_agent.agent.workflows.review_receipts import WorkflowReviewReceipt
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_workflow_native_run import _flow, _runs, _script
from agent_py_agent.tests.test_business_v12_workflow_receipts import refused


# LLM: 两个空间都经原生spaces.create真实创建，不伪造空间记录。
# 函数用途: 创建一对测试空间（原空间与移出用空间）并返回其ID。
def _spaces(native, prefix):  # noqa: F811
    space = ok(native, 'spaces.create', {'name': prefix + '-a'}, idempotency_key=prefix + '-space-create-a')['space_id']
    other = ok(native, 'spaces.create', {'name': prefix + '-b'}, idempotency_key=prefix + '-space-create-b')['space_id']
    return space, other


# LLM: 范围变更走原生workflows.scope.set并带当前范围修订（CAS），不直写工作流存储。
# 函数用途: 以当前范围修订把工作流范围整体替换为给定范围。
def _set_scope(native, name, scope, key):  # noqa: F811
    revision = ok(native, 'workflows.get', {'workflow_ref': name})['workflow']['scope_revision']
    ok(native, 'workflows.scope.set', {'workflow_ref': name, 'scope': scope}, idempotency_key=key,
       expected_revision=revision)


# LLM: 只注入一次停止，不替换引擎或存储；协调员探针导入本函数，行为保持不变。
# 函数用途: 经原生workflows.run产生一条副作用未开始、可续跑的暂停运行。
def _paused(native, agent, monkeypatch, space, key):  # noqa: F811
    """在首个副作用之前注入一次停止：运行落为可续跑暂停，副作用未开始（原生无需对账）。"""
    real, armed = workflow_engine._check_stop, [True]

    # LLM: 仅首次进入时抛出一次中断信号，后续调用恢复原停止检查。
    # 函数用途: 在首个步骤副作用开始前构造一次确定性暂停。
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


# LLM: 撤销只经真实原生入口（范围设置、空间归档/恢复）；拒绝须在任何控制写入前发生且运行字节不变。
# 函数用途: 验证暂停运行续跑按当前范围与空间状态复核，恢复后同一运行可完成（G5a-R1）。
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

    # LLM: 经原生入口发起续跑并断言预期状态码、错误码、not_started及账本零改动。
    # 函数用途: 验证撤销状态下的续跑前置拒绝。
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


# LLM: 撤销发生在两步之间时，下一步副作用准入须在其脚本执行前拒绝，已提交的前一步结果保留。
# 函数用途: 验证运行中途撤销使下一步在脚本运行前被拒（G5a-R1）。
def test_revocation_between_steps_refuses_the_next_side_effect_before_its_script_runs(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    space, other = _spaces(native, 'mid')
    flow = _flow('mid-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({'step_id': 'calc2', 'name': 'calc2', 'mode': 'deterministic_script',
                                        'script_id': 'calc'})
    ok(native, 'workflows.create', flow, idempotency_key='mid-flow-create-001')
    scripts, real = [], workflow_engine.run_script_with_datasources

    # LLM: 首步真实脚本完成后经领域服务移出范围，验证次步在副作用开始前被拒。
    # 函数用途: 构造两步之间的确定性范围撤销。
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


# LLM: 验证显式二参可调用上下文端口创建的运行在续跑前同样以当前Actor复核空间归档与项目撤销（G5a-R1 / MSG-027），恢复后可正常续跑完成。
# 函数用途: 验证旧二参上下文端口运行在空间归档或项目失活时拒绝续跑且零写改动。
def test_callable_provider_runs_revalidate_archived_space_and_revoked_project_before_resume(native, monkeypatch):  # noqa: F811
    server, agent = native
    agent.business_active_references = lambda space_id, actor: []
    projects = {'proj-r1-callable': 'available'}

    # LLM: 仅向本Owner返回三参项目引用状态，不伪造其它引用类型。
    # 函数用途: 为测试提供可控项目可用性状态的宿主引用只读端口。
    def references(*args):
        # 宿主引用端口（原生默认只解析task，没有项目来源）：只答本Owner项目行；单参工作流索引调用如实未知。
        if len(args) == 3 and args[0] == 'project' and args[1] in projects:
            return {'owner_id': agent.home_paths.owner_id, 'ref': args[1], 'space_id': None,
                    'state': projects[args[1]]}
        return None

    # LLM: 模拟合法旧二参宿主上下文端口，只在首次启动冻结当前空间与项目引用。
    # 函数用途: 以二参可调用形式解析并冻结工作流运行业务上下文。
    def legacy_provider(actor, request):
        # 合法旧二参宿主端口：自行经共享只读解析冻结当前空间/项目；逻辑task_id是宿主自有引用而非运行ID。
        return resolve_workflow_run_context(actor=actor, workflow_run_id='host-legacy-task',
            requested_space_id=request.get('space_id'), project_ref=request.get('project_ref'))

    agent.business_reference_reader = references
    _script(agent)
    agent.workflow_engine_factory = lambda owner_home, **options: WorkflowEngine(owner_home, **options)
    agent.business_context_provider = legacy_provider
    space = ok(native, 'spaces.create', {'name': 'r1c-archived'}, idempotency_key='r1c-space-create-archive')['space_id']
    project_space = ok(native, 'spaces.create', {'name': 'r1c-project', 'default_project_ref': 'proj-r1-callable'},
                       idempotency_key='r1c-space-create-project')['space_id']
    ok(native, 'workflows.create', _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space, project_space]}),
       idempotency_key='r1c-flow-create-0001')
    archived = _paused(native, agent, monkeypatch, space, 'r1c-run-archived-0001')
    project = _paused(native, agent, monkeypatch, project_space, 'r1c-run-project-00001')
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    frozen = store.load_run(project).business_context
    assert (frozen['space_id'], frozen['project_ref'], frozen['task_id']) == (
        project_space, 'proj-r1-callable', 'host-legacy-task')
    assert store.load_run(archived).business_context['space_id'] == space
    # 端口确是旧二参可调用而非工厂类；续跑不重调它选默认。
    assert next(iter(server._business_services.values())).workflows.context_provider is legacy_provider

    # LLM: 断言原生续跑被前置拒绝并返回预期HTTP状态码、错误码与not_started且运行记录零改动。
    # 函数用途: 校验撤销条件下的续跑拒绝回执与运行账本不变性。
    def refuse(run_id, key, code, status):
        before = store.load_run(run_id).to_dict()
        got, payload = call(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key=key,
                            expected_revision=before['revision'])
        assert (got, (payload['error'] or {}).get('code'), payload['effect_outcome']) == (
            status, code, 'not_started'), payload
        assert store.load_run(run_id).to_dict() == before

    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.archive', {'space_id': space}, idempotency_key='r1c-space-archive-001', expected_revision=revision)
    refuse(archived, 'r1c-resume-archived-01', 'SPACE_ARCHIVED', 503)
    projects['proj-r1-callable'] = 'disabled'
    refuse(project, 'r1c-resume-project-001', 'REFERENCE_NOT_FOUND', 404)
    # 正对照：撤销解除后同一旧二参端口组合真实续跑两条运行并执行脚本。
    projects['proj-r1-callable'] = 'available'
    revision = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
    ok(native, 'spaces.restore', {'space_id': space}, idempotency_key='r1c-space-restore-001', expected_revision=revision)
    for run_id, key in ((archived, 'r1c-resume-restored-01'), (project, 'r1c-resume-project-ok1')):
        done = ok(native, 'workflow_runs.resume', {'workflow_run_id': run_id}, idempotency_key=key,
                  expected_revision=store.load_run(run_id).revision)
        finished = store.load_run(run_id)
        assert done['state'] == 'completed' and finished.status == 'succeeded'
        assert finished.steps[0].output_snapshot == {'total': 3}


# LLM: 缓存只按端口稳定来源失效：显式覆盖、缺一与回退各自重建，不靠对象偶然相等判断。
# 函数用途: 验证原生服务缓存随工作流端口来源切换（G5a-R2）。
def test_native_services_follow_the_workflow_port_source(native, tmp_path):  # noqa: F811
    server, agent = native
    _script(agent)
    ok(native, 'workflows.create', _flow('ports-flow'), idempotency_key='ports-flow-create-01')
    assert ok(native, 'workflows.run', {'workflow_ref': 'ports-flow'}, idempotency_key='ports-run-fallback-1'
              )['state'] == 'completed'
    built = []

    # LLM: 记录显式引擎工厂被调用的Owner根并返回真实WorkflowEngine。
    # 函数用途: 验证显式工作流引擎端口被原生服务选用。
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


# LLM: 只在宿主显式端口上设置默认并发策略，原生运行与续跑经真实dispatcher；不替换dispatcher或上下文工厂。
# 函数用途: 为测试Agent装配带指定并发策略的工作流引擎工厂与宿主上下文工厂。
def _policy_ports(agent, policy):  # noqa: F811
    """显式宿主端口：引擎工厂只设默认并发策略，使原生运行与续跑经真实dispatcher；上下文仍由宿主工厂解析。"""
    # LLM: 构造绑定指定默认并发策略的真实WorkflowEngine。
    # 函数用途: 供宿主端口按请求装配带并发策略的工作流引擎。
    def factory(owner_home, **options):
        engine = WorkflowEngine(owner_home, **options)
        engine.default_concurrency_policy = policy
        return engine

    agent.workflow_engine_factory, agent.business_context_provider = factory, WorkflowRunContextFactory()


# LLM: 证据是磁盘全部字节（含新增文件），比状态字段更严，用于证明拒绝或重放零写入。
# 函数用途: 读取指定运行目录与本Owner并发claim/队列目录的全部文件字节。
def _evidence(agent, *run_ids):  # noqa: F811
    """原运行目录与本Owner并发claim/队列目录的全部文件字节（含新增文件）。"""
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    roots = [store.run_dir(run_id) for run_id in run_ids]
    roots.append(Path(agent.home_paths.owner_home_dir) / 'data' / 'workflow_concurrency')
    return {(str(root), str(path.relative_to(root))): path.read_bytes()
            for root in roots if root.exists() for path in sorted(root.rglob('*')) if path.is_file()}


# LLM: 间谍只记录并原样调用真实写口，不改变行为；证明拒绝或重放前后连瞬时写也没有。
# 函数用途: 经原生入口续跑一次，返回HTTP状态、回执与期间发生的账本/claim/队列写入。
def _resume_recording_writes(native, run_id, key, revision, *, params=None):  # noqa: F811
    """只记录并原样调用真实运行账本写口与claim/排队写口（不替换dispatcher），证明拒绝前连瞬时写也没有。"""
    writes = []
    request = {'workflow_run_id': run_id} if params is None else params
    with pytest.MonkeyPatch.context() as patch:
        for module, name in ((workflow_run_store, '_write_json'), (concurrency_policy, '_create_claim'),
                             (concurrency_policy, '_write_queue'), (concurrency_policy, '_unlink_same')):
            real = getattr(module, name)

            # LLM: 记录写口调用名并透传到原写口实现。
            # 函数用途: 在测试窗口内捕获所有账本与并发原语写动作。
            def spy(*args, _real=real, _name=name, **kwargs):
                writes.append(_name)
                return _real(*args, **kwargs)
            patch.setattr(module, name, spy)
        status, payload = call(native, 'workflow_runs.resume', request, idempotency_key=key,
                               expected_revision=revision)
    return status, payload, writes


# LLM: claim已释放的暂停运行续跑须新建claim并改写run.json；撤销时必须在这些写入之前拒绝。
# 函数用途: 验证非allow策略续跑在撤销后于claim绑定前被拒、有效对照经真实dispatcher完成（G5a-R3）。
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

    # LLM: 记录前置磁盘字节并验证撤销状态下的续跑零写拒绝。
    # 函数用途: 断言策略续跑在claim重绑前即被当前授权/范围门拦截。
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


# LLM: 只在首步提交并发表后注入一次停止，使暂停运行带已提交副作用，claim按原规则保留。
# 函数用途: 经原生workflows.run产生一条保留claim的持有者运行。
def _paused_after_first_step(native, agent, space, key):  # noqa: F811
    """首步已提交并发表后、次步副作用前注入一次停止：暂停运行带已提交副作用，claim按原规则保留（retained）。"""
    real_save, real_stop, armed = WorkflowRunStore.save_step_result, workflow_engine._check_stop, []

    # LLM: 透传保存步骤结果并在首步成功发表后挂载一次停止信号。
    # 函数用途: 在首步持久提交后精确触发次步前暂停。
    def save_then_arm(self, workflow_run_id, step_record):
        saved = real_save(self, workflow_run_id, step_record)
        if step_record.step_id == 'calc' and step_record.status == 'succeeded':
            armed.append(True)
        return saved

    # LLM: 仅在首步已提交信号挂载时抛出一次中断，其余调用透传原检查。
    # 函数用途: 产生首步已提交、次步未开始的可续跑持有者运行。
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


# LLM: 只读原队列格式，不消费或改写票据；用于证明重放不增排队位。
# 函数用途: 返回本Owner全部工作流并发队列中的持久票据。
def _queue_tickets(agent):
    root = Path(agent.home_paths.owner_home_dir) / 'data' / 'workflow_concurrency'
    return [entry for path in sorted(root.glob('*/queue.json')) for entry in json.loads(path.read_text())['entries']]


# LLM: 等待线程按票据nonce命名；集合包含关系证明重放不新建等待线程。
# 函数用途: 返回当前存活的工作流排队等待线程标识集合。
def _queue_workers():
    return {thread.ident for thread in threading.enumerate() if thread.name.startswith('workflow-queue-')}


# LLM: 持有者真实保留claim；撤销续跑零写入被拒，有效控制排队或跳过；重放沿用原准入（同回执、不增票据/线程、零写入），
#   执行中原生重放不争抢活回调运行锁且保持同一202受理，同暂停运行异键竞争控制返回503未开始，结清后如实回放原结果。
# 函数用途: 验证非allow续跑/新运行在持有者后的准入、排队/执行中/结清重放与跳过证据不变（G5a-R3、FLAG-8）。
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
    workers = _queue_workers()
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [other]}, 'r3h-scope-other-' + tag)
    before = _evidence(agent, waiting, holder)
    status, payload, writes = _resume_recording_writes(native, waiting, 'r3h-resume-revoked-' + tag,
                                                       store.load_run(waiting).revision)
    assert (status, payload['error']['code'], payload['effect_outcome']) == (
        409, 'WORKFLOW_SPACE_MISMATCH', 'not_started'), payload
    assert writes == [] and _evidence(agent, waiting, holder) == before
    assert _queue_workers() <= workers
    # 正对照（范围恢复）：同一续跑与一条新运行经真实dispatcher遇到持有者——queue写排队票据并如实给202 accepted；
    #   skip_if_running续跑未开始（not_started）、新运行落skipped（blocked）。持有者经原生取消结清释放claim后真实执行。
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [space]}, 'r3h-scope-restore-' + tag)
    key, revision = 'r3h-resume-valid-' + tag, store.load_run(waiting).revision
    before = _evidence(agent, waiting, holder)
    status, payload, writes = _resume_recording_writes(native, waiting, key, revision)
    after_resume = _evidence(agent, waiting, holder)
    run_request = {'workflow_ref': 'r1-flow', 'space_id': space}
    fresh_status, fresh = call(native, 'workflows.run', run_request, idempotency_key='r3h-fresh-run-' + tag)
    fresh_id = fresh['data']['workflow_run_id']
    if policy == 'queue':
        assert (status, payload['data']) == (202, {'operation_id': waiting, 'state': 'accepted'}), payload
        assert (fresh_status, fresh['data']['state']) == (202, 'accepted'), fresh
        tickets, workers = _queue_tickets(agent), _queue_workers()
        assert [(entry['run_id'], entry.get('operation_key')) for entry in tickets] == [(waiting, key), (fresh_id, None)]
        queued_snapshot = _evidence(agent, waiting, holder, fresh_id)
        # 排队中的同键重放：同一回执，沿用原活票据与等待线程，不增排队位/线程且零写入；准入不改原运行修订与状态。
        replay_status, replay, replay_writes = _resume_recording_writes(native, waiting, key, revision)
        assert (replay_status, replay['data'], replay_writes) == (202, payload['data'], [])
        again_status, again = call(native, 'workflows.run', run_request, idempotency_key='r3h-fresh-run-' + tag)
        assert (again_status, again['data'], again['effect_outcome']) == (202, fresh['data'], fresh['effect_outcome'])
        # 新运行同键不同意图（inputs变更）：写入前409幂等冲突（not_started）。
        run_conflict_status, run_conflict = call(native, 'workflows.run', {**run_request, 'inputs': {'x': 9}},
                                                 idempotency_key='r3h-fresh-run-' + tag)
        assert (run_conflict_status, run_conflict['error']['code'], run_conflict['effect_outcome']) == (
            409, 'IDEMPOTENCY_CONFLICT', 'not_started'), run_conflict
        # 同键不同修订（原操作身份不同）：写入前幂等冲突（未开始），原票据与全部字节不变。
        conflict_status, conflict, conflict_writes = _resume_recording_writes(native, waiting, key, revision + 1)
        assert (conflict_status, conflict['error']['code'], conflict['effect_outcome'], conflict_writes) == (
            409, 'IDEMPOTENCY_CONFLICT', 'not_started', []), conflict
        # 同暂停运行异键竞争续跑（MSG-037 Rule 4）：排队前503 WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING（not_started），零写入。
        competing_status, competing, competing_writes = _resume_recording_writes(
            native, waiting, 'r3h-competing-queued-' + tag, revision)
        assert (competing_status, competing['error']['code'], competing['effect_outcome'], competing_writes) == (
            503, 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING', 'not_started', []), competing
        assert {'key': 'reason_code', 'value': 'control_already_pending'} in competing['error']['details']
        assert _evidence(agent, waiting, holder, fresh_id) == queued_snapshot
        assert _queue_tickets(agent) == tickets and _queue_workers() <= workers
        assert (store.load_run(waiting).status, store.load_run(waiting).revision) == ('paused_for_resume', revision)
        assert store.load_run(fresh_id).status == 'queued'
    else:
        assert (status, payload['error']['code'], payload['effect_outcome']) == (
            503, 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING', 'not_started'), payload
        # 跳过的续跑：未开始；原运行/控制证据（运行目录与claim/队列全部字节）与修订不变且无任何写入。
        assert writes == [] and after_resume == before
        assert (store.load_run(waiting).status, store.load_run(waiting).revision) == ('paused_for_resume', revision)
        assert 'concurrency_claim' not in store._read(waiting, 'run.json')
        assert (fresh_status, fresh['data']['state']) == (200, 'blocked'), fresh
        assert store.load_run(fresh_id).status == 'skipped'
    entered, release = threading.Event(), threading.Event()
    if policy == 'queue':
        real_run = ScriptRunner.run_script

        # LLM: 仅在首个脚本执行入口挂起一次事件门，保持真实dispatcher、真实执行锁与真实脚本逻辑不变。
        # 函数用途: 精确打开排队控制进入真实执行期（持execution_lock）的并发观测窗口。
        def gated(self, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                assert release.wait(20)
            return real_run(self, *args, **kwargs)
        monkeypatch.setattr(ScriptRunner, 'run_script', gated)
    try:
        cancelled = ok(native, 'workflow_runs.cancel', {'workflow_run_id': holder},
                       idempotency_key='r3h-cancel-holder-' + tag, expected_revision=store.load_run(holder).revision)
        assert cancelled['outstanding_refs'] == []
        assert store._read(holder, 'run.json')['concurrency_claim']['phase'] == 'released'
        if policy == 'queue':
            # 持有者结清后原票据被真实执行：阻在首个脚本时，原生同键重放不死锁且仍是同一202 accepted（原准入执行中），
            #   同键异修订返回409，同运行异键返回503，均不增票据/线程且零写入。
            assert entered.wait(20)
            running_snapshot = _evidence(agent, waiting, holder, fresh_id)
            running_status, running, running_writes = _resume_recording_writes(native, waiting, key, revision)
            assert (running_status, running['data'], running_writes) == (202, payload['data'], [])
            exec_conflict_status, exec_conflict, exec_conflict_writes = _resume_recording_writes(
                native, waiting, key, revision + 1)
            assert (exec_conflict_status, exec_conflict['error']['code'], exec_conflict['effect_outcome'],
                    exec_conflict_writes) == (409, 'IDEMPOTENCY_CONFLICT', 'not_started', []), exec_conflict
            exec_competing_status, exec_competing, exec_competing_writes = _resume_recording_writes(
                native, waiting, 'r3h-competing-exec-' + tag, revision)
            assert (exec_competing_status, exec_competing['error']['code'], exec_competing['effect_outcome'],
                    exec_competing_writes) == (
                503, 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING', 'not_started', []), exec_competing
            assert {'key': 'reason_code', 'value': 'control_already_pending'} in exec_competing['error']['details']
            assert _evidence(agent, waiting, holder, fresh_id) == running_snapshot
            assert [entry['run_id'] for entry in _queue_tickets(agent)] == [fresh_id] and _queue_workers() <= workers
        else:
            key, revision = 'r3h-resume-after-' + tag, store.load_run(waiting).revision
            ok(native, 'workflow_runs.resume', {'workflow_run_id': waiting}, idempotency_key=key,
               expected_revision=revision)
    finally:
        release.set()
    expected = [waiting] + ([fresh_id] if policy == 'queue' else [])
    deadline = time.monotonic() + 20
    while ((any(store._read(run_id, 'run.json')['status'] != 'succeeded'
                or (policy == 'queue' and store._read(run_id, 'run.json').get('concurrency_claim', {}).get('phase') != 'released')
                for run_id in expected) or _queue_workers())
           and time.monotonic() < deadline):
        time.sleep(0.05)
    for run_id in expected:
        finished = store.load_run(run_id)
        assert finished.status == 'succeeded', (run_id, finished.status)
        assert [step.output_snapshot for step in finished.steps] == [{'total': 3}, {'total': 3}]
    # 结清后如实回放：同键续跑回原结果（completed），同键异修订返回409，均不建claim/票据且零写入；原控制账只有这一次恢复且已完成。
    settled_snapshot = _evidence(agent, waiting, holder, fresh_id)
    replay_status, replay, replay_writes = _resume_recording_writes(native, waiting, key, revision)
    assert (replay_status, replay['data']['state'], replay_writes) == (200, 'completed', []), replay
    done_conflict_status, done_conflict, done_conflict_writes = _resume_recording_writes(
        native, waiting, key, revision + 1)
    assert (done_conflict_status, done_conflict['error']['code'], done_conflict['effect_outcome'],
            done_conflict_writes) == (409, 'IDEMPOTENCY_CONFLICT', 'not_started', []), done_conflict
    assert _evidence(agent, waiting, holder, fresh_id) == settled_snapshot
    operations = store._read(waiting, 'run.json')['control_operations']
    assert [(name, row['state']) for name, row in operations.items() if row['action'] == 'resume'] == [
        (key, 'completed')]
    if policy == 'queue':
        again_status, again = call(native, 'workflows.run', run_request, idempotency_key='r3h-fresh-run-' + tag)
        assert (again_status, again['data']['state']) == (200, 'completed'), again
    assert _queue_tickets(agent) == []


# LLM: 排队控制在真正执行时才遇到撤销：原回调在任何控制写入与副作用前被拒，claim结清释放，原控制账无此键；
#   同键重放不能沿用已失效的受理，须按当前授权如实拒绝且零写入；恢复后同键重放真实执行一次，再回放原结果。
# 函数用途: 验证排队续跑执行期被拒后的同键重放如实反映失败、恢复后只执行一次（FLAG-8）。
def test_queued_resume_refused_at_execution_replays_honestly(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    space, other = _spaces(native, 'r3f')
    flow = _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({'step_id': 'calc2', 'name': 'calc2', 'mode': 'deterministic_script',
                                        'script_id': 'calc'})
    ok(native, 'workflows.create', flow, idempotency_key='r3f-flow-create-0001')
    waiting = _paused(native, agent, monkeypatch, space, 'r3f-paused-run-0001')
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    _policy_ports(agent, 'queue')
    holder = _paused_after_first_step(native, agent, space, 'r3f-holder-run-0001').workflow_run_id
    key, revision = 'r3f-resume-control-01', store.load_run(waiting).revision
    status, payload = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting}, idempotency_key=key,
                           expected_revision=revision)
    assert (status, payload['data']) == (202, {'operation_id': waiting, 'state': 'accepted'}), payload
    [ticket] = _queue_tickets(agent)
    worker = 'workflow-queue-' + ticket['nonce']
    # 排队期间撤销范围，再经原生取消结清持有者：等待线程取得claim后原回调被拒，claim结清释放、票据移除。
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [other]}, 'r3f-scope-other-0001')
    ok(native, 'workflow_runs.cancel', {'workflow_run_id': holder}, idempotency_key='r3f-cancel-holder-01',
       expected_revision=store.load_run(holder).revision)
    for thread in [row for row in threading.enumerate() if row.name == worker]:
        thread.join(20)
    assert not any(row.name == worker for row in threading.enumerate())
    current = store._read(waiting, 'run.json')
    assert _queue_tickets(agent) == [] and current['concurrency_claim']['phase'] == 'released'
    assert key not in current.get('control_operations', {})
    assert (current['status'], current['revision']) == ('paused_for_resume', revision)
    assert not store.load_run(waiting).steps[0].output_snapshot
    # 同键重放：原受理已随执行期拒绝结束，不再报accepted；按当前授权在任何写入前如实拒绝（未开始）。
    before = _evidence(agent, waiting)
    got, refusal, writes = _resume_recording_writes(native, waiting, key, revision)
    assert (got, refusal['error']['code'], refusal['effect_outcome']) == (
        409, 'WORKFLOW_SPACE_MISMATCH', 'not_started'), refusal
    assert writes == [] and _evidence(agent, waiting) == before
    # 恢复范围后同键重放真实执行一次（无持有者，直接取得claim），再次重放回原结果且零写入。
    _set_scope(native, 'r1-flow', {'scope_mode': 'spaces', 'space_ids': [space]}, 'r3f-scope-restore-01')
    done = ok(native, 'workflow_runs.resume', {'workflow_run_id': waiting}, idempotency_key=key,
              expected_revision=revision)
    finished = store.load_run(waiting)
    assert done['state'] == 'completed' and finished.status == 'succeeded'
    assert [step.output_snapshot for step in finished.steps] == [{'total': 3}, {'total': 3}]
    got, again, writes = _resume_recording_writes(native, waiting, key, revision)
    assert (got, again['data']['state'], writes) == (200, 'completed', []), again
    operations = store._read(waiting, 'run.json')['control_operations']
    assert [(name, row['state']) for name, row in operations.items() if row['action'] == 'resume'] == [
        (key, 'completed')]


# LLM: 同一工作流下另一条暂停运行仍按正常queue策略入队执行；同键异意图（resolution_ref）在排队/执行中/结清均报409，
#   排队中的新运行与续跑被原生取消或执行期脚本失败后，重放均如实反映耐久终态（blocked/WORKFLOW_CANCELLED/failed）而非陈旧accepted。
# 函数用途: 验证不同暂停运行正常入队、治理意图冲突、排队期取消与脚本失败后的耐久诚实回放（MSG-037 Rule 3/4/5）。
def test_different_run_queues_while_changed_intent_cancel_and_failed_callback_settle_honestly(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    owner_home = Path(agent.home_paths.owner_home_dir)
    store = WorkflowRunStore(owner_home)

    # LLM: 为每一步签发独立审核回执以通过真实WorkflowGovernance放行门。
    # 函数用途: 提供真实宿主审核票据读取端口。
    def receipt_reader(reference, record, step_id):
        definition = store.load_workflow_snapshot(record.workflow_run_id)
        step = next(item for item in record.steps if item.step_id == step_id)
        return WorkflowReviewReceipt(
            receipt_ref=reference,
            owner_id=record.owner_id,
            task_id=record.business_context.get('task_id', ''),
            workflow_run_id=record.workflow_run_id,
            step_id=step_id,
            attempt_id=step.escalation_result.get('attempt_id'),
            candidate_hash=workflow_candidate_hash(definition),
            author_id='author-alice',
            reviewer_id='reviewer-bob',
            decision='approved',
            expires_at=time.time() + 3600,
        )

    # LLM: 按resolution_ref返回原运行未开始对账结论，使resolution_ref进入真实控制意图与对账链。
    # 函数用途: 提供真实宿主对账结论读取端口。
    def resolution_reader(reference, record, step):
        definition = store.load_workflow_snapshot(record.workflow_run_id)
        return {
            'owner_id': record.owner_id,
            'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context.get('task_id', ''),
            'step_id': step.step_id,
            'attempt_id': step.escalation_result.get('attempt_id'),
            'candidate_hash': workflow_candidate_hash(definition),
            'resolution_ref': reference,
            'state': 'not_started',
        }

    agent.business_governance = WorkflowGovernance(
        store,
        policy_provider=lambda _rec, step_id: {
            'minimum_reviews': 1, 'author_id': 'author-alice', 'review_refs': ['rev-' + step_id]},
        receipt_reader=receipt_reader,
        authorization_check=lambda _item, _rec: True,
        resolution_reader=resolution_reader,
    )
    space, _ = _spaces(native, 'r3m')
    flow = _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({'step_id': 'calc2', 'name': 'calc2', 'mode': 'deterministic_script',
                                        'script_id': 'calc'})
    ok(native, 'workflows.create', flow, idempotency_key='r3m-flow-create-0001')
    waiting1 = _paused_after_first_step(native, agent, space, 'r3m-paused-run-0001').workflow_run_id
    waiting2 = _paused(native, agent, monkeypatch, space, 'r3m-paused-run-0002')
    waiting_cancel = _paused(native, agent, monkeypatch, space, 'r3m-paused-cancel-1')
    waiting_fail = _paused(native, agent, monkeypatch, space, 'r3m-paused-fail-001')
    _policy_ports(agent, 'queue')
    holder = _paused_after_first_step(native, agent, space, 'r3m-holder-run-0001').workflow_run_id

    rev1 = store.load_run(waiting1).revision
    rev2 = store.load_run(waiting2).revision
    rev_cancel = store.load_run(waiting_cancel).revision
    key1, key2, key_cancel = 'r3m-resume-run1-0001', 'r3m-resume-run2-0001', 'r3m-resume-cancel-01'
    st1, p1 = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting1, 'resolution_ref': 'res-keep-01'},
                   idempotency_key=key1, expected_revision=rev1)
    # 同一工作流另一条暂停运行waiting2不受waiting1在途控制阻断，正常排队受理（202 accepted）。
    st2, p2 = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting2},
                   idempotency_key=key2, expected_revision=rev2)
    st_c, p_c = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting_cancel},
                     idempotency_key=key_cancel, expected_revision=rev_cancel)
    fresh_st, fresh = call(native, 'workflows.run', {'workflow_ref': 'r1-flow', 'space_id': space},
                           idempotency_key='r3m-fresh-cancel-001')
    fresh_cancel_id = fresh['data']['workflow_run_id']
    assert (st1, p1['data']['state'], st2, p2['data']['state'], st_c, p_c['data']['state'],
            fresh_st, fresh['data']['state']) == (
        202, 'accepted', 202, 'accepted', 202, 'accepted', 202, 'accepted')
    assert [entry['run_id'] for entry in _queue_tickets(agent)] == [waiting1, waiting2, waiting_cancel, fresh_cancel_id]

    # 同键同修订但不同意图（resolution_ref变化）：排队中409 IDEMPOTENCY_CONFLICT，零写入。
    before_intent = _evidence(agent, waiting1, waiting2, holder)
    ic_st, ic_body, ic_writes = _resume_recording_writes(
        native, waiting1, key1, rev1, params={'workflow_run_id': waiting1, 'resolution_ref': 'res-changed-02'})
    assert (ic_st, ic_body['error']['code'], ic_body['effect_outcome'], ic_writes) == (
        409, 'IDEMPOTENCY_CONFLICT', 'not_started', []), ic_body
    assert _evidence(agent, waiting1, waiting2, holder) == before_intent

    # 排队期间取消waiting_cancel与fresh_cancel_id：重放不再伪报202 accepted，而是如实返回WORKFLOW_CANCELLED/blocked且零写入。
    ok(native, 'workflow_runs.cancel', {'workflow_run_id': waiting_cancel}, idempotency_key='r3m-do-cancel-wait1',
       expected_revision=store.load_run(waiting_cancel).revision)
    ok(native, 'workflow_runs.cancel', {'workflow_run_id': fresh_cancel_id}, idempotency_key='r3m-do-cancel-fresh',
       expected_revision=store.load_run(fresh_cancel_id).revision)
    snap_cancelled = _evidence(agent, waiting_cancel, fresh_cancel_id)
    cr_st, cr_body, cr_writes = _resume_recording_writes(native, waiting_cancel, key_cancel, rev_cancel)
    assert (cr_st, cr_body['error']['code'], cr_body['effect_outcome'], cr_writes) == (
        503, 'WORKFLOW_CANCELLED', 'not_started', []), cr_body
    fr_st, fr_body = call(native, 'workflows.run', {'workflow_ref': 'r1-flow', 'space_id': space},
                          idempotency_key='r3m-fresh-cancel-001')
    assert (fr_st, fr_body['data']['state']) == (200, 'blocked'), fr_body
    assert _evidence(agent, waiting_cancel, fresh_cancel_id) == snap_cancelled

    # 释放holder后waiting1与waiting2真实执行完成，已取消的两条不执行任何步骤且队列全部清空。
    ok(native, 'workflow_runs.cancel', {'workflow_run_id': holder}, idempotency_key='r3m-cancel-holder-01',
       expected_revision=store.load_run(holder).revision)
    deadline = time.monotonic() + 20
    while ((_queue_tickets(agent) or _queue_workers()
            or any(store._read(r, 'run.json')['status'] != 'succeeded'
                   or store._read(r, 'run.json').get('concurrency_claim', {}).get('phase') != 'released'
                   for r in (waiting1, waiting2))) and time.monotonic() < deadline):
        time.sleep(0.05)
    assert store.load_run(waiting1).status == 'succeeded' and store.load_run(waiting2).status == 'succeeded'
    assert store.load_run(waiting_cancel).status == 'cancelled' and store.load_run(fresh_cancel_id).status == 'cancelled'
    assert not store.load_run(waiting_cancel).steps[0].output_snapshot
    assert not store.load_run(fresh_cancel_id).steps[0].output_snapshot
    # 结清后同键不同意图仍报409 IDEMPOTENCY_CONFLICT，零写入。
    done_ic_st, done_ic_body, done_ic_writes = _resume_recording_writes(
        native, waiting1, key1, rev1, params={'workflow_run_id': waiting1, 'resolution_ref': 'res-changed-02'})
    assert (done_ic_st, done_ic_body['error']['code'], done_ic_body['effect_outcome'], done_ic_writes) == (
        409, 'IDEMPOTENCY_CONFLICT', 'not_started', []), done_ic_body

    # 排队续跑在执行期脚本失败：运行耐久落为failed，同键重放如实返回state=failed而非completed且零写入。
    holder2 = _paused_after_first_step(native, agent, space, 'r3m-holder-run-0002').workflow_run_id
    rev_fail, key_fail = store.load_run(waiting_fail).revision, 'r3m-resume-fail-0001'
    st_f, p_f = call(native, 'workflow_runs.resume', {'workflow_run_id': waiting_fail},
                     idempotency_key=key_fail, expected_revision=rev_fail)
    assert (st_f, p_f['data']['state']) == (202, 'accepted')

    # LLM: 模拟目标运行执行期确定性脚本失败，让引擎按正式错误合同持久化failed记录。
    # 函数用途: 验证排队续跑步骤失败后的耐久终态与重放回执。
    def fail_script(*args, **kwargs):
        raise RuntimeError('deterministic script step error')

    monkeypatch.setattr(workflow_engine, 'run_script_with_datasources', fail_script)
    ok(native, 'workflow_runs.cancel', {'workflow_run_id': holder2}, idempotency_key='r3m-cancel-holder-02',
       expected_revision=store.load_run(holder2).revision)
    deadline = time.monotonic() + 20
    while ((_queue_tickets(agent) or _queue_workers()
            or store._read(waiting_fail, 'run.json')['status'] != 'failed'
            or store._read(waiting_fail, 'run.json').get('concurrency_claim', {}).get('phase') != 'retained')
           and time.monotonic() < deadline):
        time.sleep(0.05)
    failed_run = store.load_run(waiting_fail)
    assert failed_run.status == 'failed' and store._read(waiting_fail, 'run.json')['concurrency_claim']['phase'] == 'retained'
    before_fail_replay = _evidence(agent, waiting_fail)
    rf_st, rf_body, rf_writes = _resume_recording_writes(native, waiting_fail, key_fail, rev_fail)
    assert (rf_st, rf_body['data']['state'], rf_body['data']['run']['status'], rf_writes) == (
        200, 'failed', 'failed', []), rf_body
    assert _evidence(agent, waiting_fail) == before_fail_replay


# LLM: 覆盖claim已绑定但回调尚未进入execution_lock的早期执行窗口，及损坏证明拒绝伪装成同操作。
# 函数用途: 验证dispatcher在同一控制早期执行窗口复用原准入、拒绝同键异指纹与同运行异键及损坏证明（FLAG-8）。
def test_same_operation_executing_claim_keeps_original_admission(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    space, _ = _spaces(native, 'r3x')
    ok(native, 'workflows.create', _flow('r1-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]}),
       idempotency_key='r3x-flow-create-0001')
    run_id = _paused(native, agent, monkeypatch, space, 'r3x-paused-run-0001')
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    owner_home = agent.home_paths.owner_home_dir
    entered, release, results, calls = threading.Event(), threading.Event(), [], []

    # LLM: 取得claim后停在事件门上而不取run锁，精确停留在claim已绑定窗口。
    # 函数用途: 模拟首次控制请求的真实回调。
    def original():
        entered.set()
        assert release.wait(20)
        return 'original-callback'

    # LLM: 记录重复回调是否被误执行。
    # 函数用途: 作为重放与竞争调用的探测回调（绝不应被执行）。
    def duplicate():
        calls.append('duplicate')
        return 'duplicate-callback'

    first = threading.Thread(target=lambda: results.append(concurrency_policy.dispatch(
        owner_home, 'workflow', 'r1-flow', run_id, 'queue', original, _lifecycle=store,
        operation_key='r3x-control-0001', operation_fingerprint='fingerprint-a')))
    first.start()
    try:
        assert entered.wait(20)
        claim = store._read(run_id, 'run.json')['concurrency_claim']
        assert (claim['phase'], claim['record']['operation_key']) == ('executing', 'r3x-control-0001')
        before = _evidence(agent, run_id)
        assert concurrency_policy.dispatch(owner_home, 'workflow', 'r1-flow', run_id, 'queue', duplicate,
                                           _lifecycle=store, operation_key='r3x-control-0001',
                                           operation_fingerprint='fingerprint-a') == {
            'status': 'queued', 'queue_position': 0, 'run_id': run_id}
        with pytest.raises(ValueError, match='IDEMPOTENCY_CONFLICT'):
            concurrency_policy.dispatch(owner_home, 'workflow', 'r1-flow', run_id, 'queue', duplicate,
                                        _lifecycle=store, operation_key='r3x-control-0001',
                                        operation_fingerprint='fingerprint-b')
        assert concurrency_policy.dispatch(owner_home, 'workflow', 'r1-flow', run_id, 'queue', duplicate,
                                           _lifecycle=store, operation_key='r3x-control-0002',
                                           operation_fingerprint='fingerprint-c') == {
            'status': 'skipped',
            'error_code': 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING',
            'running_run_id': run_id,
            'reason_code': 'control_already_pending',
        }
        assert calls == [] and _queue_tickets(agent) == [] and _evidence(agent, run_id) == before
    finally:
        release.set()
        first.join(20)
    assert results == ['original-callback'] and calls == []
    assert store._read(run_id, 'run.json')['concurrency_claim']['phase'] == 'released'
