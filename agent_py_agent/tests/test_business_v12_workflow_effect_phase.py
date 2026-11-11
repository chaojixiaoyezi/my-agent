# 模块用途: FLAG-5（MSG-038）回归测试：验证工作流步骤副作用阶段载体（WorkflowEffectPhase）与控制阶段
#   （preflight/resume/cancel）严格分离；名为cancel/resume/preflight的合法步骤必须经过发布审核门、
#   workflows.run执行授权与G5a-R1冻结空间/业务上下文复核，同时真实取消收口、恢复对账与外部字符串authorizer语义保持不变。
from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.business_access import BusinessActor
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.workflows import engine as workflow_engine
from agent_py_agent.agent.workflows import governance as governance_mod
from agent_py_agent.agent.workflows.business_scope import WorkflowScopeService
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.governance import WorkflowGovernance, workflow_candidate_hash
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.agent.workflows.script_runner import ScriptRunner
from agent_py_agent.tests.test_business_v12_background_scheduling import (  # noqa: F401 - pytest fixture
    call,
    native,
    ok,
)
from agent_py_agent.tests.test_business_v12_f4_workflows import definition as f4_definition
from agent_py_agent.tests.test_business_v12_workflow_native_run import _flow, _runs, _script
from agent_py_agent.tests.test_workflow_script_engine import script, step, workflow


# LLM: 验证名为cancel/resume/preflight的合法步骤不能借控制阶段同名旁路跳过WorkflowReleaseGate，
#   且policy_provider收到的step_id仍为纯str类型；批准回执就绪后真实控制preflight/resume正常放行并完成该步骤。
# 函数用途: 参数化校验三种控制同名步骤的治理拦截、纯str策略入参与批准后续跑通过（FLAG-5）。
@pytest.mark.parametrize('reserved_name', ['cancel', 'resume', 'preflight'])
def test_control_named_steps_enforce_release_gate_and_receive_plain_str_step_id(tmp_path, reserved_name):
    store = WorkflowRunStore(tmp_path)
    allowed = [False]
    seen_policy_types = []
    effects = tmp_path / 'effects'
    effects.mkdir()
    counter = effects / f'effect-{reserved_name}'
    script(
        tmp_path,
        f'from pathlib import Path\n'
        f'p = Path({str(counter)!r})\n'
        f'p.write_text((p.read_text() if p.exists() else "") + "executed")\n'
        f'print(\'{{"value": 1}}\')\n',
    )

    # LLM: 记录policy_provider收到的step_id运行时精确类型并返回对应审核引用策略。
    # 函数用途: 验证治理门向宿主policy_provider传递纯str步骤ID而非子类对象。
    def policy_provider(_record, step_id):
        seen_policy_types.append((type(step_id), step_id))
        return {'author_id': 'writer', 'review_refs': [f'review-{reserved_name}'], 'minimum_reviews': 1}

    # LLM: 按当前步骤与尝试签发结构化审核回执字典。
    # 函数用途: 为治理发布门提供宿主审核票据读取实现。
    def receipt_reader(ref, record, step_id):
        target = next(item for item in record.steps if item.step_id == step_id)
        return {
            'receipt_ref': ref,
            'owner_id': 'alice',
            'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context.get('task_id', ''),
            'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id)),
            'author_id': 'writer',
            'reviewer_id': 'independent-reviewer',
            'decision': 'approved',
            'expires_at': 9999999999,
        }

    guard = WorkflowGovernance(
        store,
        policy_provider=policy_provider,
        receipt_reader=receipt_reader,
        authorization_check=lambda _receipt, _record: allowed[0],
    )
    engine = WorkflowEngine(tmp_path, run_authorizer=guard.require_effect)
    engine.runner = ScriptRunner(tmp_path, extra_write_roots=(effects,))

    first = engine.run_workflow(workflow(step(reserved_name)))
    assert first.status == 'failed' and not counter.exists()
    assert first.steps[0].error_message == 'WORKFLOW_REVIEW_NOT_CURRENT_OR_INDEPENDENT'
    assert seen_policy_types == [(str, reserved_name)]
    assert all(tp is str for tp, _ in seen_policy_types)

    allowed[0] = True
    done = engine.resume_workflow_run(first.workflow_run_id)
    assert done.status == 'succeeded' and counter.read_text() == 'executed'
    assert done.steps[0].escalation_result['review_refs'] == [f'review-{reserved_name}']
    assert all(tp is str and sid == reserved_name for tp, sid in seen_policy_types)


# LLM: 验证名为cancel的步骤在业务层必须要求workflows.run权限并执行G5a-R1冻结空间归档复核，
#   而同一归档空间内挂在cancel步骤前的暂停运行仍可经真实workflow_runs.cancel收口。
# 函数用途: 区分名为cancel的步骤副作用与真实取消控制的操作权限及归档空间复核语义（FLAG-5 + G5a-R1）。
def test_step_named_cancel_requires_run_permission_and_revalidates_archived_space(native, monkeypatch):  # noqa: F811
    server, agent = native
    agent.business_active_references = lambda _space_id, _actor: []
    _script(agent)
    space = ok(native, 'spaces.create', {'name': 'flag5-cancel-space'},
               idempotency_key='flag5-space-create-01')['space_id']
    flow = _flow('flag5-cancel-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({
        'step_id': 'cancel',
        'name': 'cancel',
        'mode': 'deterministic_script',
        'script_id': 'calc',
    })
    ok(native, 'workflows.create', flow, idempotency_key='flag5-cancel-flow-001')

    # 先产生一条首步暂停、尚有名为cancel的步骤待执行的运行，稍后验证归档空间下真取消控制仍能收口。
    real_stop, armed = workflow_engine._check_stop, [True]

    # LLM: 首次进入步骤前触发一次确定性暂停，随后恢复原停止检查。
    # 函数用途: 构造待取消控制收口的暂停工作流运行。
    def stop_once():
        if armed:
            armed.clear()
            raise InterruptedError('pause before first effect')
        return real_stop()

    before_ids = {row.workflow_run_id for row in _runs(agent)}
    monkeypatch.setattr(workflow_engine, '_check_stop', stop_once)
    try:
        call(native, 'workflows.run', {'workflow_ref': 'flag5-cancel-flow', 'space_id': space},
             idempotency_key='flag5-paused-spare-01')
    finally:
        monkeypatch.setattr(workflow_engine, '_check_stop', real_stop)
    [spare] = [row.workflow_run_id for row in _runs(agent) if row.workflow_run_id not in before_ids]

    # 运行中途在首步calc完成后归档空间：次步虽名为cancel，仍须按副作用复核冻结空间并在脚本启动前拒绝。
    executed_scripts, real_run = [], workflow_engine.run_script_with_datasources

    # LLM: 在首步脚本执行完成后经原生HTTP入口归档冻结空间，检测次步cancel是否在脚本前被拒。
    # 函数用途: 构造首步与名为cancel的次步之间的空间归档状态变化。
    def run_then_archive(*args, **kwargs):
        executed_scripts.append(args[1])
        result = real_run(*args, **kwargs)
        if len(executed_scripts) == 1:
            rev = ok(native, 'spaces.get', {'space_id': space})['space']['revision']
            ok(native, 'spaces.archive', {'space_id': space},
               idempotency_key='flag5-archive-mid-001', expected_revision=rev)
        return result

    monkeypatch.setattr(workflow_engine, 'run_script_with_datasources', run_then_archive)
    receipt = ok(native, 'workflows.run', {'workflow_ref': 'flag5-cancel-flow', 'space_id': space},
                 idempotency_key='flag5-mid-archive-run1')
    monkeypatch.setattr(workflow_engine, 'run_script_with_datasources', real_run)

    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    record = store.load_run(receipt['workflow_run_id'])
    first, second = record.steps
    assert executed_scripts == ['calc']
    assert first.status == 'succeeded' and first.output_snapshot == {'total': 3}
    assert (second.step_id, second.status, second.error_code) == ('cancel', 'failed', 'SPACE_ARCHIVED')
    assert record.status == 'failed' and receipt['state'] == 'failed'

    # 同一归档空间下，真正的workflow_runs.cancel控制仍能正常收回spare运行。
    cancelled = ok(native, 'workflow_runs.cancel', {'workflow_run_id': spare},
                   idempotency_key='flag5-true-cancel-001', expected_revision=store.load_run(spare).revision)
    assert cancelled['outstanding_refs'] == [] and store.load_run(spare).status == 'cancelled'

    # 仅授workflow_runs.cancel不授workflows.run的Actor：不能借步骤名为cancel绕过workflows.run授权，但可真取消。
    tmp_home = Path(agent.home_paths.owner_home_dir) / 'isolated_perm_check'
    tmp_home.mkdir(parents=True, exist_ok=True)
    script(tmp_home)
    spaces = BusinessSpaceService(tmp_home, owner_id='alice', enabled=True)
    full_actor = BusinessActor('alice', 'alice-full', spaces, lambda _a, _f: True)
    cancel_only_actor = BusinessActor(
        'alice', 'alice-cancel-only', spaces,
        lambda action, _f: action in ('workflow_runs.cancel', 'spaces.get'),
    )
    sp_id = spaces.call('spaces.create', {'name': 'perm-space'}, actor=full_actor,
                        idempotency_key='flag5-perm-space-0001')['space_id']
    actions_checked = []

    # LLM: 记录每次业务授权动作名并按当前Actor自身权限表裁决。
    # 函数用途: 捕获步骤cancel与控制cancel各自请求的真实权限动作名。
    def auth_hook(current_actor, action, ref):
        actions_checked.append(action)
        return current_actor.authorize(action, {'workflow_ref': ref})

    svc = WorkflowScopeService(
        tmp_home,
        owner_id='alice',
        authorize=auth_hook,
        context_provider=lambda _act, _req: BusinessExecutionContext(
            'alice', sp_id, None, 'stask-flag5', 1, None, (), (), 'business.v1'),
        engine_factory=lambda home, **opts: WorkflowEngine(home, **opts),
    )
    svc.execute(
        'workflows.create',
        {'definition': f4_definition('perm-cancel-step', steps=[step('cancel')]),
         'scope': {'scope_mode': 'spaces', 'space_ids': [sp_id]}},
        actor=full_actor,
        idempotency_key='flag5-perm-create-001',
    )
    # 先由有权限Actor创建一条暂停运行，再让仅有workflow_runs.cancel的Actor尝试续跑步骤cancel（应拒）与取消运行（应通）。
    local_store = WorkflowRunStore(tmp_home)
    armed.append(True)
    monkeypatch.setattr(workflow_engine, '_check_stop', stop_once)
    try:
        with pytest.raises(InterruptedError):
            svc.execute('workflows.run', {'workflow_ref': 'perm-cancel-step', 'space_id': sp_id},
                        actor=full_actor, idempotency_key='flag5-perm-run-000001')
    finally:
        monkeypatch.setattr(workflow_engine, '_check_stop', real_stop)
    [paused_row] = local_store.list_runs(workflow_id='perm-cancel-step', limit=10)
    paused_id = paused_row.workflow_run_id
    rev_paused = paused_row.revision

    # 直接以仅持workflow_runs.cancel的Actor对步骤cancel调用authorize门：必须按workflows.run拒绝。
    bound_engine = svc._engine(cancel_only_actor)
    actions_checked.clear()
    with pytest.raises(PermissionError):
        bound_engine._admit_effect(local_store.load_run(paused_id), local_store.load_run(paused_id).steps[0])
    assert actions_checked == ['workflows.run']

    cancel_res = svc.execute('workflow_runs.cancel', {'workflow_run_id': paused_id},
                             actor=cancel_only_actor, expected_revision=rev_paused,
                             idempotency_key='flag5-perm-cancel-001')
    assert cancel_res['state'] == 'completed' and local_store.load_run(paused_id).status == 'cancelled'


# LLM: 验证名为resume的步骤在普通运行与带resolution_ref续跑中均走步骤副作用准入，
#   既不误触发无治理运行上的WORKFLOW_RECONCILIATION_REQUIRED，也不在治理续跑时误走require_resolution。
# 函数用途: 验证名为resume的步骤不与恢复控制阶段混淆（FLAG-5）。
def test_step_named_resume_does_not_trigger_control_reconciliation_or_resolution_branch(native, monkeypatch):  # noqa: F811
    server, agent = native
    _script(agent)
    space = ok(native, 'spaces.create', {'name': 'flag5-resume-space'},
               idempotency_key='flag5-resume-space-01')['space_id']
    flow = _flow('flag5-resume-flow', scope={'scope_mode': 'spaces', 'space_ids': [space]})
    flow['definition']['steps'].append({
        'step_id': 'resume',
        'name': 'resume',
        'mode': 'deterministic_script',
        'script_id': 'calc',
    })
    ok(native, 'workflows.create', flow, idempotency_key='flag5-resume-flow-001')

    # 无governance时：首步已产生effect_started后运行次步resume，不得误触发控制阶段专属WORKFLOW_RECONCILIATION_REQUIRED。
    plain = ok(native, 'workflows.run', {'workflow_ref': 'flag5-resume-flow', 'space_id': space},
               idempotency_key='flag5-resume-plain-01')
    store = WorkflowRunStore(agent.home_paths.owner_home_dir)
    plain_record = store.load_run(plain['workflow_run_id'])
    assert plain['state'] == 'completed' and plain_record.status == 'succeeded'
    assert [s.output_snapshot for s in plain_record.steps] == [{'total': 3}, {'total': 3}]

    # 挂载governance并经resolution_ref续跑首步未知现场：控制resume消费resolution_ref，次步resume仍必须消费独立审核回执。
    resolution_calls, receipt_steps = [], []

    # LLM: 为calc与resume两步分别签发独立审核回执并记录实际消费的步骤ID。
    # 函数用途: 验证次步resume真实进入WorkflowReleaseGate而非跳过或走对账口。
    def receipt_reader(reference, record, step_id):
        receipt_steps.append((reference, step_id))
        target = next(item for item in record.steps if item.step_id == step_id)
        return {
            'receipt_ref': reference,
            'owner_id': record.owner_id,
            'task_id': record.business_context.get('task_id', ''),
            'workflow_run_id': record.workflow_run_id,
            'step_id': step_id,
            'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id)),
            'author_id': 'author-a',
            'reviewer_id': 'reviewer-b',
            'decision': 'approved',
            'expires_at': time.time() + 3600,
        }

    # LLM: 仅对首步calc返回未开始对账结论，若次步resume误调本端口则记录并导致断言失败。
    # 函数用途: 提供首步恢复对账结论并监控resolution_reader调用次数。
    def resolution_reader(reference, record, target):
        resolution_calls.append((reference, target.step_id))
        return {
            'owner_id': record.owner_id,
            'workflow_run_id': record.workflow_run_id,
            'task_id': record.business_context.get('task_id', ''),
            'step_id': target.step_id,
            'attempt_id': target.escalation_result['attempt_id'],
            'candidate_hash': workflow_candidate_hash(store.load_workflow_snapshot(record.workflow_run_id)),
            'resolution_ref': reference,
            'state': 'not_started',
        }

    agent.business_governance = WorkflowGovernance(
        store,
        policy_provider=lambda _rec, sid: {
            'minimum_reviews': 1, 'author_id': 'author-a', 'review_refs': [f'rev-{sid}']},
        receipt_reader=receipt_reader,
        authorization_check=lambda _item, _rec: True,
        resolution_reader=resolution_reader,
    )
    agent.workflow_engine_factory = lambda home, **opts: WorkflowEngine(home, **opts)
    agent.business_context_provider = workflow_engine_context_factory()

    real_stop, armed = workflow_engine._check_stop, [True]

    # LLM: 在首步启动前触发一次中断以产生可带resolution_ref续跑的暂停运行。
    # 函数用途: 构造受治理的暂停运行供resolution_ref续跑验证。
    def stop_first():
        if armed:
            armed.clear()
            raise InterruptedError('pause governed run before step 0')
        return real_stop()

    before_gov = {row.workflow_run_id for row in _runs(agent)}
    monkeypatch.setattr(workflow_engine, '_check_stop', stop_first)
    try:
        call(native, 'workflows.run', {'workflow_ref': 'flag5-resume-flow', 'space_id': space},
             idempotency_key='flag5-resume-gov-run1')
    finally:
        monkeypatch.setattr(workflow_engine, '_check_stop', real_stop)
    [gov_run_id] = [row.workflow_run_id for row in _runs(agent) if row.workflow_run_id not in before_gov]

    resumed = ok(
        native,
        'workflow_runs.resume',
        {'workflow_run_id': gov_run_id, 'resolution_ref': 'res-step-calc-01'},
        idempotency_key='flag5-resume-gov-ctl1',
        expected_revision=store.load_run(gov_run_id).revision,
    )
    gov_done = store.load_run(gov_run_id)
    assert resumed['state'] == 'completed' and gov_done.status == 'succeeded'
    assert resolution_calls == [('res-step-calc-01', 'calc')]
    assert ('rev-resume', 'resume') in receipt_steps
    assert gov_done.steps[1].escalation_result['review_refs'] == ['rev-resume']


# LLM: 返回标准宿主工作流运行上下文工厂实例，避免测试手写假上下文。
# 函数用途: 为原生治理测试提供真实WorkflowRunContextFactory。
def workflow_engine_context_factory():
    from agent_py_agent.agent.workflows.business_scope import WorkflowRunContextFactory
    return WorkflowRunContextFactory()


# LLM: 验证外部自定义字符串run_authorizer(record, phase)保持100%向后兼容（isinstance(phase, str)与字符串相等均成立），
#   同时WorkflowEffectPhase精确区分步骤副作用与控制阶段。
# 函数用途: 守护外部run_authorizer回调兼容性与WorkflowEffectPhase类型契约（FLAG-5）。
def test_external_string_run_authorizer_backward_compatibility(tmp_path):
    WorkflowEffectPhase = governance_mod.WorkflowEffectPhase
    script(tmp_path)
    observed = []

    # LLM: 模拟外部宿主只按str协议消费phase的自定义run_authorizer。
    # 函数用途: 记录每次回调收到的phase字符串兼容性与阶段载体身份。
    def custom_authorizer(_record, phase):
        observed.append((isinstance(phase, str), isinstance(phase, WorkflowEffectPhase), str(phase), phase == 'cancel'))
        return None

    engine = WorkflowEngine(tmp_path, run_authorizer=custom_authorizer)
    record = engine.run_workflow(workflow(step('cancel')))
    assert record.status == 'succeeded'
    engine.cancel_workflow_run(record.workflow_run_id, expected_revision=record.revision, operation_key='ext-cancel-01')

    assert observed == [
        (True, False, 'preflight', False),
        (True, True, 'cancel', True),
        (True, False, 'cancel', True),
    ]
