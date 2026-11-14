"""W2 (#03/#04/#05/#06/#07/#09) 回归单测：DAG取消与兜底跳过、并发holder_phase、未结算收口、DAG恢复与定时工具上下文。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.common.cancellation import (
    CancellationToken,
    ToolCancelled,
    bind_cancellation_token,
)
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.execution import execute_scheduled_target
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.tool_work import execute_scheduled_tool
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolResult
from agent_py_agent.agent.workflows import concurrency_policy
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore, _concurrency_settled
from agent_py_agent.agent.workflows.script_runner import ScriptRunner


def _dag_definition(steps, *, workflow_id='wf_w2', max_parallel_steps=4):
    normalized = []
    for item in steps:
        spec = dict(item)
        spec.setdefault('name', spec['step_id'])
        normalized.append(spec)
    return {
        'descriptor': {
            'id': workflow_id,
            'kind': 'workflow',
            'name': workflow_id,
            'owner_id': 'alice',
            'metadata': {
                'execution_mode': 'dag',
                'max_parallel_steps': max_parallel_steps,
            },
        },
        'steps': normalized,
    }


class _StubAgentSteps:
    def __init__(self, handler):
        self._handler = handler

    def submit(self, run, spec, record):
        self._handler(run, spec, record)


class _SpyStore(WorkflowRunStore):
    def __init__(self, owner_home, on_save_step=None):
        super().__init__(owner_home)
        self._on_save_step = on_save_step

    def save_step_result(self, workflow_run_id, step, *, resuming=False):
        result = super().save_step_result(workflow_run_id, step, resuming=resuming)
        if self._on_save_step is not None:
            self._on_save_step(workflow_run_id, step, result)
        return result


def _scheduler_harness(tmp_path, *, business_spaces_enabled=False, extra_tools=True):
    home = tmp_path / 'home'
    workspace = tmp_path / 'workspace'
    workspace.mkdir(parents=True, exist_ok=True)
    agent = SimpleAgent(
        AgentConfig(
            my_agent_home=str(home),
            enable_tools=extra_tools,
            business_spaces_enabled=business_spaces_enabled,
            memory_path='memory.jsonl',
            orphan_supervision_interval_seconds=0,
        ),
        workspace,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        'canonical_user_id': 'user-1',
        'channel': 'chat',
        'channel_conversation_id': 'conv-w2',
        'channel_user_id': 'u-w2',
        'cwd': str(workspace),
        'now': 900,
    })
    return agent, store, thread


def _claim_due_rule(agent, store, *, now=1000):
    agent.scheduler_service.monitor_jitter_window_seconds = 0
    agent.scheduler_service.enqueue_ready_runs(now=now)
    signal = store.wakes.pending()[0]
    claim = agent.scheduler_service.claim_wake(signal, now=now).claim
    agent.scheduler_repository.mark_run_running(claim.run_id, claim.claim_id, now=now)
    kwargs = {
        'thread_id': claim.run['thread_id'],
        'task_id': claim.run_id,
        'reason': 'scheduled_run',
        'now': now,
    }
    return signal, claim, kwargs


def test_dag_cancel_beats_waiting_for_agent_and_preserves_sibling_receipts(tmp_path):
    wait_entered = threading.Event()
    release_wait = threading.Event()
    store = WorkflowRunStore(tmp_path)

    def _analyze(spec, _context, _resolved):
        if spec['step_id'] == 'branch_done':
            return {'done': 1}
        assert wait_entered.wait(timeout=3.0)
        threading.Timer(0.05, release_wait.set).start()
        raise ToolCancelled('cancel-wins-over-waiting')

    def _agent_submit(_run, _spec, record):
        record.escalation_result['submission_state'] = 'submitted'
        record.escalation_result['task_id'] = 'stask-wait-1'
        wait_entered.set()
        assert release_wait.wait(timeout=3.0)

    engine = WorkflowEngine(
        tmp_path,
        workspace_root=tmp_path,
        analysis_handler=_analyze,
        agent_steps=_StubAgentSteps(_agent_submit),
    )
    definition = _dag_definition([
        {'step_id': 'branch_done', 'mode': 'llm_skill_analysis'},
        {'step_id': 'branch_wait', 'mode': 'agent_task'},
        {'step_id': 'branch_cancel', 'mode': 'llm_skill_analysis'},
        {
            'step_id': 'join',
            'mode': 'llm_skill_analysis',
            'depends_on': ['branch_done', 'branch_wait', 'branch_cancel'],
        },
    ])

    with pytest.raises(ToolCancelled) as exc_info:
        engine.run_workflow(definition, workflow_run_id='dag-cancel-win')

    persisted = store.load_run('dag-cancel-win')
    assert persisted is not None
    assert persisted.status != 'waiting_for_agent'
    assert exc_info.value.record.status != 'waiting_for_agent'
    steps = {s.step_id: s for s in persisted.steps}
    assert steps['branch_done'].status == 'succeeded'
    assert steps['branch_done'].output_snapshot == {'done': 1}
    assert steps['join'].status == 'pending'


def test_dag_check_stop_before_waiting_for_agent_honors_cancellation(tmp_path):
    token = CancellationToken()
    done_finished = threading.Event()

    def _on_save(_run_id, step, _saved):
        if step.step_id == 'branch_wait' and step.status == 'waiting_for_agent':
            token.cancel('stop-before-waiting-commit')

    spy_store = _SpyStore(tmp_path, on_save_step=_on_save)

    def _analyze(_spec, _context, _resolved):
        done_finished.set()
        return {'sibling': 'ok'}

    def _agent_submit(_run, _spec, record):
        assert done_finished.wait(timeout=3.0)
        record.escalation_result['submission_state'] = 'submitted'
        record.escalation_result['task_id'] = 'stask-wait-2'

    engine = WorkflowEngine(
        tmp_path,
        workspace_root=tmp_path,
        analysis_handler=_analyze,
        agent_steps=_StubAgentSteps(_agent_submit),
    )
    engine.store = spy_store
    definition = _dag_definition([
        {'step_id': 'branch_done', 'mode': 'llm_skill_analysis'},
        {'step_id': 'branch_wait', 'mode': 'agent_task'},
        {'step_id': 'join', 'mode': 'llm_skill_analysis', 'depends_on': ['branch_done', 'branch_wait']},
    ])

    with bind_cancellation_token(token):
        with pytest.raises(ToolCancelled):
            engine.run_workflow(definition, workflow_run_id='dag-check-stop-waiting')

    persisted = spy_store.load_run('dag-check-stop-waiting')
    assert persisted is not None
    assert persisted.status != 'waiting_for_agent'
    steps = {s.step_id: s for s in persisted.steps}
    assert steps['branch_done'].status == 'succeeded'
    assert steps['branch_done'].output_snapshot == {'sibling': 'ok'}
    assert steps['branch_wait'].escalation_result.get('submission_state') == 'submitted'
    assert steps['join'].status == 'pending'


def test_concurrency_dispatch_emits_holder_phase_for_executing_and_retained(tmp_path):
    started = threading.Event()
    release = threading.Event()
    results = {}

    def _hold(_spec, _context, _resolved):
        started.set()
        assert release.wait(timeout=5.0)
        return {'held': True}

    engine = WorkflowEngine(tmp_path, workspace_root=tmp_path, analysis_handler=_hold)
    store = engine.store
    definition = _dag_definition(
        [{'step_id': 'only', 'mode': 'llm_skill_analysis'}],
        workflow_id='wf_holder_phase',
    )

    worker = threading.Thread(
        target=lambda: results.setdefault(
            'first',
            engine.run_workflow(definition, workflow_run_id='run-live', concurrency_policy='skip_if_running'),
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert started.wait(timeout=3.0)
        live_skip = concurrency_policy.dispatch(
            tmp_path,
            'workflow',
            'wf_holder_phase',
            'run-second-live',
            'skip_if_running',
            lambda: {'unexpected': True},
            _lifecycle=store,
        )
    finally:
        release.set()
        worker.join(timeout=5.0)

    assert results['first'].status == 'succeeded'
    assert live_skip['status'] == 'skipped'
    assert live_skip['error_code'] == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
    assert live_skip['running_run_id'] == 'run-live'
    assert live_skip['holder_phase'] == 'executing'

    def _unknown_agent(_run, _spec, record):
        record.escalation_result['submission_state'] = 'unknown'
        record.escalation_result['task_id'] = 'stask-retained'

    agent_engine = WorkflowEngine(
        tmp_path,
        workspace_root=tmp_path,
        agent_steps=_StubAgentSteps(_unknown_agent),
    )
    agent_def = _dag_definition(
        [{'step_id': 'wait_step', 'mode': 'agent_task'}],
        workflow_id='wf_retained_phase',
    )
    first_wait = agent_engine.run_workflow(
        agent_def,
        workflow_run_id='run-retained',
        concurrency_policy='skip_if_running',
    )
    assert first_wait.status == 'waiting_for_agent'

    retained_skip = concurrency_policy.dispatch(
        tmp_path,
        'workflow',
        'wf_retained_phase',
        'run-second-retained',
        'skip_if_running',
        lambda: {'unexpected': True},
        _lifecycle=store,
    )
    assert retained_skip['status'] == 'skipped'
    assert retained_skip['error_code'] == 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'
    assert retained_skip['running_run_id'] == 'run-retained'
    assert retained_skip['holder_phase'] == 'retained'

    # 兼容守卫: _lifecycle=None 时不附加 holder_phase；仅非空 business_context 才透传
    no_ctx_holder = threading.Event()
    no_ctx_release = threading.Event()

    def _plain_holder():
        no_ctx_holder.set()
        assert no_ctx_release.wait(timeout=3.0)
        return {'done': True}

    t = threading.Thread(
        target=lambda: concurrency_policy.dispatch(
            tmp_path, 'workflow', 'wf_plain', 'run-plain-1', 'skip_if_running', _plain_holder
        ),
        daemon=True,
    )
    t.start()
    try:
        assert no_ctx_holder.wait(timeout=3.0)
        skip_no_lc = concurrency_policy.dispatch(
            tmp_path,
            'workflow',
            'wf_plain',
            'run-plain-2',
            'skip_if_running',
            lambda: {'unexpected': True},
            business_context={'space_id': 'sp-1'},
        )
        assert 'holder_phase' not in skip_no_lc
        assert skip_no_lc['business_context'] == {'space_id': 'sp-1'}
    finally:
        no_ctx_release.set()
        t.join(timeout=3.0)


@pytest.mark.parametrize(
    'holder_phase,expected_status,expected_code',
    [
        ('executing', 'skipped', 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING'),
        ('retained', 'failed', 'SCHEDULER_EXECUTION_TARGET_UNAVAILABLE'),
        (None, 'failed', 'SCHEDULER_EXECUTION_TARGET_UNAVAILABLE'),
    ],
)
def test_scheduled_target_workflow_skip_handles_executing_vs_retained_holder_phase(
    tmp_path, monkeypatch, holder_phase, expected_status, expected_code
):
    from agent_py_agent.agent.scheduler import execution as sched_exec

    agent, store, thread = _scheduler_harness(tmp_path / f'case_{holder_phase}')
    agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name='目标跳过区分',
            prompt='',
            thread_id=thread.thread_id,
            schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
            execution={'mode': 'isolated'},
            execution_target={
                'kind': 'workflow',
                'asset_id': 'wf_target_skip',
                'pinned_version': 1,
                'input_ref': {'inputs': {}},
            },
            now=900,
        )
    )
    signal, claim, kwargs = _claim_due_rule(agent, store, now=1000)

    skip_body = {
        'status': 'skipped',
        'error_code': 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING',
        'running_run_id': 'run-prior-99',
    }
    if holder_phase is not None:
        skip_body['holder_phase'] = holder_phase

    monkeypatch.setattr(
        sched_exec,
        '_execute_target_tool',
        lambda *_args, **_kwargs: (
            SimpleNamespace(result=ToolResult(call_id='c-skip', tool_name='workflows', status='succeeded')),
            json.dumps(skip_body),
        ),
    )

    execute_scheduled_target(agent, agent.scheduler_service, claim, signal, kwargs)
    history, errors = agent.scheduler_repository.history(limit=5)
    assert not errors
    row = next(item for item in history if item['run_id'] == claim.run_id)
    assert row['status'] == expected_status
    assert row['error_code'] == expected_code
    assert store.wakes.pending_one(signal.wake_signal_id) is None

    if expected_status == 'skipped':
        assert store.tasks.load(claim.run_id).status == 'cancelled'
    else:
        assert store.tasks.load(claim.run_id).status == 'abandoned'
        outcome = row['target_outcome']
        assert outcome['status'] == 'failed'
        assert outcome['reason_code'] == 'prior_run_unsettled'
        assert outcome['running_run_id'] == 'run-prior-99'
        assert outcome['effect_outcome'] == 'not_started'
        assert outcome['next_action']
        assert 'run-prior-99' in outcome['error_message']
        assert 'not_started' in outcome['error_message']


def test_scheduled_target_monitor_pool_retained_skip_fails_unavailable(tmp_path):
    agent, store, thread = _scheduler_harness(tmp_path / 'pool_retained')
    agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name='池返回未收口',
            prompt='',
            thread_id=thread.thread_id,
            schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
            execution={'mode': 'isolated'},
            execution_target={
                'kind': 'workflow',
                'asset_id': 'wf_pool_skip',
                'pinned_version': 1,
                'input_ref': {'inputs': {}},
            },
            now=900,
        )
    )
    signal, claim, kwargs = _claim_due_rule(agent, store, now=1000)

    class _FakeFuture:
        def result(self):
            return {
                'status': 'skipped',
                'error_code': 'WORKFLOW_RUN_SKIPPED_ALREADY_RUNNING',
                'holder_phase': 'retained',
                'running_run_id': 'run-pool-retained',
            }

    agent.scheduler_service.monitor_pool = SimpleNamespace(submit=lambda *a, **kw: _FakeFuture())
    execute_scheduled_target(agent, agent.scheduler_service, claim, signal, kwargs)
    history, _ = agent.scheduler_repository.history(limit=5)
    row = next(item for item in history if item['run_id'] == claim.run_id)
    assert row['status'] == 'failed'
    assert row['error_code'] == 'SCHEDULER_EXECUTION_TARGET_UNAVAILABLE'
    assert row['target_outcome']['reason_code'] == 'prior_run_unsettled'
    assert row['target_outcome']['running_run_id'] == 'run-pool-retained'
    assert row['target_outcome']['effect_outcome'] == 'not_started'


def test_unavailable_agent_executor_does_not_admit_effect_and_non_agent_terminal_settles(tmp_path):
    admitted_phases = []
    engine = WorkflowEngine(
        tmp_path,
        workspace_root=tmp_path,
        agent_steps=None,
        run_authorizer=lambda _run, phase: admitted_phases.append(type(phase).__name__),
    )
    definition = _dag_definition(
        [{'step_id': 'agent_one', 'mode': 'agent_task'}],
        workflow_id='wf_no_agent_exec',
    )

    first = engine.run_workflow(definition, workflow_run_id='run-unavail-1', concurrency_policy='skip_if_running')
    assert first.status == 'failed'
    assert first.steps[0].error_message == 'WORKFLOW_AGENT_EXECUTOR_UNAVAILABLE'
    assert 'WorkflowEffectPhase' not in admitted_phases
    assert not first.steps[0].escalation_result.get('effect_started')
    assert _concurrency_settled(first) is True

    # ephemeral_debug=True 同样在 _admit_effect 之前拒绝
    admitted_phases.clear()
    debug_res = engine.debug_step(definition, 'agent_one', allow_side_effects=True)
    assert debug_res['status'] == 'failed'
    assert 'WorkflowEffectPhase' not in admitted_phases

    # 同一 workflow 下次调度不能被假 retained claim 卡死
    second = engine.run_workflow(definition, workflow_run_id='run-unavail-2', concurrency_policy='skip_if_running')
    assert isinstance(second, WorkflowRunRecord)
    assert second.workflow_run_id == 'run-unavail-2'
    assert second.steps[0].error_message == 'WORKFLOW_AGENT_EXECUTOR_UNAVAILABLE'

    # 非 agent_task 步骤终态证明矩阵
    def _rec(step_status, escalation, *, mode='tool', run_status='failed'):
        step = StepExecutionRecord(
            step_id='s1',
            name='s1',
            mode=mode,
            status=step_status,
            started_at=1.0,
            ended_at=2.0,
            duration_ms=10.0,
            llm_involved=False,
            escalation_result=escalation,
        )
        return WorkflowRunRecord(
            workflow_run_id='r-check',
            workflow_id='wf-check',
            owner_id='alice',
            trigger_kind='manual',
            status=run_status,
            started_at=1.0,
            ended_at=2.0,
            duration_ms=10.0,
            steps=(step,),
            final_output={},
            effect_outcome='failed',
        )

    assert _concurrency_settled(
        _rec('failed', {'effect_started': True, 'tool_receipt': {'status': 'failed', 'effect_outcome': 'failed'}})
    ) is True
    assert _concurrency_settled(
        _rec(
            'cancelled',
            {'effect_started': True, 'subworkflow_receipt': {'status': 'cancelled', 'effect_outcome': 'failed'}},
            mode='subworkflow',
            run_status='cancelled',
        )
    ) is True
    assert _concurrency_settled(
        _rec('failed', {'effect_started': True, 'effect_outcome': 'failed'}, mode='deterministic_script')
    ) is True
    assert _concurrency_settled(
        _rec('failed', {'effect_started': True, 'effect_outcome': 'unknown'})
    ) is False
    assert _concurrency_settled(
        _rec('failed', {'effect_started': True, 'effect_outcome': 'failed', 'tool_operation_binding': {'op': '1'}})
    ) is False
    assert _concurrency_settled(
        _rec('waiting_for_agent', {'effect_started': True, 'effect_outcome': 'failed'})
    ) is False
    assert _concurrency_settled(
        _rec('failed', {'effect_started': True, 'effect_outcome': 'failed', 'cleanup_error': 'disk error'})
    ) is False


def test_dag_resume_preserves_succeeded_sibling_validates_ancestors_and_scopes_scripts(tmp_path):
    # 1) 交错顺序: [root, branch_a(首轮失败), branch_b(首轮成功且仅依赖root), join(依赖a,b)]
    for from_step_arg in (None, 'branch_a'):
        case_dir = tmp_path / f'resume_{from_step_arg or "auto"}'
        case_dir.mkdir(parents=True, exist_ok=True)
        counts = {'root': 0, 'branch_a': 0, 'branch_b': 0, 'join': 0}
        fail_a = {'flag': True}

        def _analyze(spec, context, _resolved, _counts=counts, _fail_a=fail_a):
            sid = spec['step_id']
            _counts[sid] += 1
            prior = context.prior_outputs
            if sid == 'root':
                return {'seed': 10}
            if sid == 'branch_a':
                if _fail_a['flag']:
                    raise RuntimeError('transient branch_a failure')
                return {'a': prior['root']['seed'] + 1}
            if sid == 'branch_b':
                return {'b': prior['root']['seed'] + 42}
            return {
                'sum': prior['branch_a']['a'] + prior['branch_b']['b'],
            }

        engine = WorkflowEngine(case_dir, workspace_root=case_dir, analysis_handler=_analyze)
        definition = _dag_definition([
            {'step_id': 'root', 'mode': 'llm_skill_analysis'},
            {'step_id': 'branch_a', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'branch_b', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
            {'step_id': 'join', 'mode': 'llm_skill_analysis', 'depends_on': ['branch_a', 'branch_b']},
        ])
        failed_run = engine.run_workflow(definition, workflow_run_id=f'dag-resume-{from_step_arg or "auto"}')
        assert failed_run.status == 'failed'
        assert counts == {'root': 1, 'branch_a': 1, 'branch_b': 1, 'join': 0}

        fail_a['flag'] = False
        resumed = engine.resume_workflow_run(failed_run.workflow_run_id, from_step_id=from_step_arg)
        assert resumed.status == 'succeeded'
        assert counts['root'] == 1
        assert counts['branch_b'] == 1  # 已成功兄弟绝不重跑
        assert counts['branch_a'] == 2
        assert counts['join'] == 1
        assert resumed.final_output == {'sum': 63}

    # 2) 前序校验按 DAG 祖先链而非数组线性切片:
    #    [root(ok), branch_a(fail), branch_c(仅依赖root且fail), join(依赖a,c)]
    anc_dir = tmp_path / 'ancestors'
    anc_dir.mkdir(parents=True, exist_ok=True)
    fail_flags = {'branch_a': True, 'branch_c': True}

    def _anc_analyze(spec, _context, _resolved):
        sid = spec['step_id']
        if fail_flags.get(sid):
            raise RuntimeError(f'{sid} fail')
        return {'ok': sid}

    anc_engine = WorkflowEngine(anc_dir, workspace_root=anc_dir, analysis_handler=_anc_analyze)
    anc_def = _dag_definition([
        {'step_id': 'root', 'mode': 'llm_skill_analysis'},
        {'step_id': 'branch_a', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
        {'step_id': 'branch_c', 'mode': 'llm_skill_analysis', 'depends_on': ['root']},
        {'step_id': 'join', 'mode': 'llm_skill_analysis', 'depends_on': ['branch_a', 'branch_c']},
    ])
    anc_run = anc_engine.run_workflow(anc_def, workflow_run_id='dag-ancestor-check')
    assert anc_run.status == 'failed'

    # join 的祖先 branch_a 未成功 -> 必须拒绝
    with pytest.raises(ValueError, match='不能跳过未成功的前序步骤'):
        anc_engine.resume_workflow_run(anc_run.workflow_run_id, from_step_id='join')

    # branch_c 的祖先仅 root(已成功) -> 不受前面同级 branch_a 失败阻拦
    fail_flags['branch_c'] = False
    after_c = anc_engine.resume_workflow_run(anc_run.workflow_run_id, from_step_id='branch_c')
    step_map = {s.step_id: s for s in after_c.steps}
    assert step_map['branch_c'].status == 'succeeded'

    # 3) _resume_script_gate 仅检查被恢复步骤及下游依赖闭合集上的脚本变更
    script_dir = tmp_path / 'scripts_scope'
    script_dir.mkdir(parents=True, exist_ok=True)
    scripts = ScriptStore(script_dir)
    flag_file = script_dir / 'fail_a.txt'
    flag_file.write_text('fail', encoding='utf-8')
    scripts.save_script(
        AssetDescriptor(id='sc_a', kind='script', name='sc_a'),
        f'from pathlib import Path\nif Path({str(flag_file)!r}).read_text() == "fail": raise RuntimeError("a")\nprint(\'{{"a":1}}\')',
    )
    scripts.save_script(
        AssetDescriptor(id='sc_b', kind='script', name='sc_b'),
        'print(\'{"b":2}\')',
    )
    scripts.save_script(
        AssetDescriptor(id='sc_join', kind='script', name='sc_join'),
        'print(\'{"joined":true}\')',
    )
    sc_engine = WorkflowEngine(script_dir, workspace_root=script_dir)
    sc_engine.runner = ScriptRunner(script_dir, workspace_root=script_dir, extra_write_roots=(script_dir,))
    sc_def = _dag_definition([
        {'step_id': 'step_a', 'mode': 'deterministic_script', 'script_id': 'sc_a'},
        {'step_id': 'step_b', 'mode': 'deterministic_script', 'script_id': 'sc_b'},
        {
            'step_id': 'step_join',
            'mode': 'deterministic_script',
            'script_id': 'sc_join',
            'depends_on': ['step_a', 'step_b'],
        },
    ])
    sc_run = sc_engine.run_workflow(sc_def, workflow_run_id='dag-script-scope')
    assert sc_run.status in ('failed', 'paused_for_resume')

    # 修改已成功旁路 sibling sc_b 并删去其历史快照：恢复 step_a 不得因 unrelated step_b 触发 SCRIPT_REVISION_DRIFT
    frozen_b = sc_engine.store.load_workflow_snapshot(sc_run.workflow_run_id)['_frozen_scripts']['step_b']
    blob_b = script_dir / 'data/asset_evolution/blobs' / frozen_b['code_sha256'][:2] / frozen_b['code_sha256']
    if blob_b.exists():
        blob_b.unlink()
    flag_file.write_text('ok', encoding='utf-8')
    scripts.save_script(AssetDescriptor(id='sc_b', kind='script', name='sc_b'), 'print(\'{"b":999}\')')
    resumed_without_drift = sc_engine.resume_workflow_run(sc_run.workflow_run_id, from_step_id='step_a')
    assert resumed_without_drift.status == 'succeeded'

    # 修改下游依赖 step_join 的脚本并删去其历史快照：恢复 step_a 时必须拦下并等待 allow_script_drift
    flag_file.write_text('fail', encoding='utf-8')
    sc_run_2 = sc_engine.run_workflow(sc_def, workflow_run_id='dag-script-downstream')
    assert sc_run_2.status in ('failed', 'paused_for_resume')
    frozen_join = sc_engine.store.load_workflow_snapshot(sc_run_2.workflow_run_id)['_frozen_scripts']['step_join']
    blob_join = (
        script_dir / 'data/asset_evolution/blobs' / frozen_join['code_sha256'][:2] / frozen_join['code_sha256']
    )
    if blob_join.exists():
        blob_join.unlink()
    flag_file.write_text('ok', encoding='utf-8')
    scripts.save_script(
        AssetDescriptor(id='sc_join', kind='script', name='sc_join'),
        'print(\'{"joined":false}\')',
    )
    blocked = sc_engine.resume_workflow_run(sc_run_2.workflow_run_id, from_step_id='step_a')
    assert blocked.status == 'paused_for_resume'
    assert blocked.current_step_id == 'step_join'
    assert blocked.steps[2].error_code == 'SCRIPT_REVISION_DRIFT'
    resumed_after_allow = sc_engine.resume_workflow_run(
        sc_run_2.workflow_run_id, from_step_id='step_a', allow_script_drift=True
    )
    assert resumed_after_allow.status == 'succeeded'


def test_dag_conditional_skipped_upstream_with_fallback_runs_consumer_in_empty_active_wave(tmp_path):
    seen_inputs = {}

    def _analyze(spec, context, _resolved):
        sid = spec['step_id']
        seen_inputs[sid] = dict(context.step_inputs)
        if sid == 'prep':
            return {'flag': False, 'fallback_x': 'fallback-default'}
        if sid == 'opt':
            return {'x': 'from-opt'}
        return {'used': context.step_inputs.get('val')}

    engine = WorkflowEngine(tmp_path, workspace_root=tmp_path, analysis_handler=_analyze)
    definition = _dag_definition([
        {'step_id': 'prep', 'mode': 'llm_skill_analysis'},
        {
            'step_id': 'opt',
            'mode': 'llm_skill_analysis',
            'depends_on': ['prep'],
            'condition': {'op': 'eq', 'ref': {'step_id': 'prep', 'key': 'flag'}, 'value': True},
        },
        {
            'step_id': 'fallback_consumer',
            'mode': 'llm_skill_analysis',
            'inputs_map': {
                'val': {
                    'step_id': 'opt',
                    'key': 'x',
                    'fallback': {'step_id': 'prep', 'key': 'fallback_x'},
                }
            },
        },
        {
            'step_id': 'strict_consumer',
            'mode': 'llm_skill_analysis',
            'inputs_map': {'val': {'step_id': 'opt', 'key': 'x'}},
        },
    ])

    run = engine.run_workflow(definition, workflow_run_id='dag-fallback-after-skip')
    assert run.status == 'succeeded'
    steps = {s.step_id: s for s in run.steps}
    assert steps['opt'].status == 'skipped'
    assert steps['strict_consumer'].status == 'skipped'
    assert steps['fallback_consumer'].status == 'succeeded'
    assert steps['fallback_consumer'].output_snapshot == {'used': 'fallback-default'}
    assert seen_inputs['fallback_consumer']['val'] == 'fallback-default'


def test_scheduled_tool_work_validates_and_propagates_business_context(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_only_execution
    from agent_py_agent.agent.gateway_parts.business_api import local_business_context

    # 1) 有效业务上下文透传至 RunParams.business_context
    agent, store, thread = _scheduler_harness(tmp_path / 'valid_ctx', business_spaces_enabled=True)
    authority, services = local_business_context(agent)
    space = services.spaces.create(
        {'name': 'w2-sched-space'},
        actor=services.actor(authority),
        idempotency_key='w2-sp-create',
    )
    valid_ctx = BusinessExecutionContext(
        owner_id=agent.home_paths.owner_id,
        space_id=space['space_id'],
        project_ref=None,
        task_id='stask_w2_tool_1',
        space_revision=space.get('revision', 0),
        binding_id=None,
        selected_method_refs=(),
        asset_version_refs=(),
        schema_version='business-execution-context.v1',
    )
    job = agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name='带业务上下文的NO工具',
            prompt='',
            thread_id=thread.thread_id,
            schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
            rules=[
                {
                    'id': 'probe',
                    'name': '探测',
                    'use_llm': False,
                    'schedule': {'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
                    'tool': {'name': 'run_command', 'arguments': {'command': 'printf ok'}},
                }
            ],
            business_context=valid_ctx.to_dict(),
            now=900,
        )
    )[0]
    signal, claim, kwargs = _claim_due_rule(agent, store, now=1000)

    captured_params = []
    monkeypatch.setattr(
        tool_only_execution,
        'execute_tool_work',
        lambda _ag, params, **_kw: (
            captured_params.append(params)
            or SimpleNamespace(
                result=ToolResult(call_id='c-tool', tool_name='run_command', status='succeeded'),
                call=SimpleNamespace(call_id='c-tool'),
            ),
            '{"ok":true}',
        ),
    )
    execute_scheduled_tool(agent, agent.scheduler_service, claim, signal, kwargs)
    assert len(captured_params) == 1
    assert captured_params[0].business_context == valid_ctx

    # 2) run/job business_context 不一致触发 ScheduleValidationError -> 拒绝于未启动态
    bad_agent, bad_store, bad_thread = _scheduler_harness(tmp_path / 'invalid_ctx', business_spaces_enabled=True)
    bad_job = bad_agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name='失效业务上下文拒绝',
            prompt='',
            thread_id=bad_thread.thread_id,
            schedule={'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
            rules=[
                {
                    'id': 'probe',
                    'name': '探测',
                    'use_llm': False,
                    'schedule': {'kind': 'every', 'every_seconds': 600, 'anchor_at': 1000, 'timezone': 'UTC'},
                    'tool': {'name': 'run_command', 'arguments': {'command': 'printf ok'}},
                }
            ],
            business_context=valid_ctx.to_dict(),
            now=900,
        )
    )[0]
    bad_signal, bad_claim, bad_kwargs = _claim_due_rule(bad_agent, bad_store, now=1000)
    tampered_ctx = {**valid_ctx.to_dict(), 'space_id': 'sp-other-tampered'}
    tampered_claim = SimpleNamespace(
        run_id=bad_claim.run_id,
        claim_id=bad_claim.claim_id,
        run={**bad_claim.run, 'business_context': tampered_ctx},
    )

    invocations = []
    monkeypatch.setattr(
        tool_only_execution,
        'execute_tool_work',
        lambda *_a, **_kw: invocations.append(1) or pytest.fail('校验失败不得执行工具'),
    )
    execute_scheduled_tool(bad_agent, bad_agent.scheduler_service, tampered_claim, bad_signal, bad_kwargs)
    assert invocations == []
    assert bad_store.tasks.load(bad_claim.run_id).status == 'abandoned'
    assert bad_store.wakes.pending_one(bad_signal.wake_signal_id) is None
    hist, _ = bad_agent.scheduler_repository.history(limit=5)
    row = next(item for item in hist if item['run_id'] == bad_claim.run_id)
    assert row['status'] == 'failed'
    assert row['error_code'] == 'SCHEDULER_BUSINESS_SOURCE_CONFLICT'
    parsed_resp = json.loads(row['response'])
    assert parsed_resp['effect_outcome'] == 'not_started'
    assert parsed_resp['next_action']


def test_dag_pre_admission_non_agent_failure_settles_while_foreach_keeps_effect_started(tmp_path):
    for idx, mode_spec in enumerate(
        (
            {'step_id': 'cond_only', 'mode': 'condition'},
            {'step_id': 'xform_only', 'mode': 'script_transform'},
            {'step_id': 'notify_only', 'mode': 'notification'},
        ),
        start=1,
    ):
        engine = WorkflowEngine(tmp_path / f'case_{idx}', workspace_root=tmp_path)
        definition = _dag_definition([mode_spec], workflow_id=f'wf_pre_admit_{mode_spec["mode"]}')
        first = engine.run_workflow(
            definition,
            workflow_run_id=f'run-{mode_spec["mode"]}-1',
            concurrency_policy='skip_if_running',
        )
        assert first.status == 'failed'
        assert first.steps[0].error_code == 'NODE_EXECUTOR_UNAVAILABLE'
        assert first.steps[0].escalation_result.get('effect_started') is False
        assert first.steps[0].escalation_result.get('effect_outcome') == 'not_started'
        assert _concurrency_settled(first) is True

        second = engine.run_workflow(
            definition,
            workflow_run_id=f'run-{mode_spec["mode"]}-2',
            concurrency_policy='skip_if_running',
        )
        assert isinstance(second, WorkflowRunRecord)
        assert second.workflow_run_id == f'run-{mode_spec["mode"]}-2'
        assert second.status == 'failed'

    exc_engine = WorkflowEngine(tmp_path / 'exc_pre_admit', workspace_root=tmp_path)
    exc_def = _dag_definition(
        [
            {
                'step_id': 'missing_input',
                'mode': 'llm_skill_analysis',
                'inputs_map': {'need': {'step_id': '$inputs', 'key': 'required_k'}},
            }
        ],
        workflow_id='wf_pre_admit_exc',
    )
    exc_first = exc_engine.run_workflow(exc_def, {}, workflow_run_id='run-exc-1', concurrency_policy='skip_if_running')
    assert exc_first.status == 'failed'
    assert exc_first.steps[0].escalation_result.get('effect_started') is False
    assert exc_first.steps[0].escalation_result.get('effect_outcome') == 'not_started'
    assert _concurrency_settled(exc_first) is True
    exc_second = exc_engine.run_workflow(exc_def, {}, workflow_run_id='run-exc-2', concurrency_policy='skip_if_running')
    assert isinstance(exc_second, WorkflowRunRecord)
    assert exc_second.workflow_run_id == 'run-exc-2'

    def _failing_foreach_analyze(spec, context, skills):
        raise ValueError('simulated batch processing error')

    fe_engine = WorkflowEngine(
        tmp_path / 'foreach_case',
        workspace_root=tmp_path,
        analysis_handler=_failing_foreach_analyze,
    )
    fe_definition = _dag_definition(
        [
            {
                'step_id': 'fe_parent',
                'mode': 'llm_skill_analysis',
                'foreach': {
                    'from': 'inputs.items',
                    'batch_size': 1,
                    'on_item_error': 'fail_fast',
                    'as': 'batch',
                },
            }
        ],
        workflow_id='wf_dag_foreach_parent_obligation',
    )
    fe_run = fe_engine.run_workflow(fe_definition, {'items': ['x']}, workflow_run_id='run-fe-1')
    assert fe_run.status == 'failed'
    assert fe_run.steps[0].escalation_result.get('effect_started') is True


def test_cancelled_non_agent_runs_release_concurrency_claim_while_preserving_retained_guards(tmp_path):
    # (a) 非 agent_task 步骤（tool_call）在 _admit_effect 后、worker 正文执行前取消暂停（paused_for_resume + effect_started=True）：
    #     未取消前仍保留并发占位（skip_if_running 跳过）；显式 cancel_workflow_run 后转 cancelled 并释放占位，后续运行正常成功。
    prebody_dir = tmp_path / 'prebody_cancel'
    prebody_calls = []
    prebody_mode = {'pause': True}

    def _prebody_dispatcher(_tool_name, _arguments, **_kwargs):
        if prebody_mode['pause']:
            raise ToolCancelled('paused-before-worker-body')
        prebody_calls.append('executed')
        return {'ok': True, 'done': True}

    pre_engine = WorkflowEngine(
        prebody_dir,
        workspace_root=prebody_dir,
    )
    pre_engine.tool_dispatcher = _prebody_dispatcher
    pre_def = _dag_definition(
        [
            {
                'step_id': 'tool_step',
                'mode': 'tool_call',
                'tool_spec': {'tool_name': 'demo_tool', 'arguments': {'x': 1}},
            }
        ],
        workflow_id='wf_prebody_cancel_claim',
    )

    with pytest.raises(ToolCancelled):
        pre_engine.run_workflow(
            pre_def,
            workflow_run_id='run-prebody-paused',
            concurrency_policy='skip_if_running',
        )
    paused = pre_engine.store.load_run('run-prebody-paused')
    assert paused is not None
    assert paused.status == 'paused_for_resume'
    assert paused.steps[0].escalation_result.get('effect_started') is True
    assert prebody_calls == []
    assert _concurrency_settled(paused) is False
    assert pre_engine.store._read('run-prebody-paused', 'run.json')['concurrency_claim']['phase'] == 'retained'

    blocked_pre = pre_engine.run_workflow(
        pre_def,
        workflow_run_id='run-prebody-blocked',
        concurrency_policy='skip_if_running',
    )
    assert isinstance(blocked_pre, dict)
    assert blocked_pre['status'] == 'skipped'
    assert blocked_pre['running_run_id'] == 'run-prebody-paused'

    outstanding = pre_engine.cancel_workflow_run(
        'run-prebody-paused',
        expected_revision=paused.revision,
        operation_key='cancel-prebody-paused',
    )
    assert outstanding == []
    cancelled_pre = pre_engine.store.load_run('run-prebody-paused')
    assert cancelled_pre.status == 'cancelled'
    assert _concurrency_settled(cancelled_pre) is True
    assert pre_engine.store._read('run-prebody-paused', 'run.json')['concurrency_claim']['phase'] == 'released'

    prebody_mode['pause'] = False
    following_pre = pre_engine.run_workflow(
        pre_def,
        workflow_run_id='run-prebody-following',
        concurrency_policy='skip_if_running',
    )
    assert isinstance(following_pre, WorkflowRunRecord)
    assert following_pre.status == 'succeeded'
    assert prebody_calls == ['executed']

    # (b) 同步非 agent_task 步骤以 effect_outcome='unknown' 失败（未取消前保留占位并拦截 skip_if_running），
    #     显式 cancel_workflow_run 后转 cancelled 并释放占位，后续运行成功。
    failed_dir = tmp_path / 'sync_failed_cancel'
    sync_mode = {'fail_unknown': True}

    def _sync_dispatcher(_tool_name, _arguments, **_kwargs):
        if sync_mode['fail_unknown']:
            return {
                'ok': False,
                'status': 'failed',
                'effect_outcome': 'unknown',
                'error_code': 'SIMULATED_IO_ERROR',
                'error_message': 'simulated I/O error with uncertain outcome',
            }
        return {'ok': True, 'recovered': True}

    sync_engine = WorkflowEngine(
        failed_dir,
        workspace_root=failed_dir,
    )
    sync_engine.tool_dispatcher = _sync_dispatcher
    sync_def = _dag_definition(
        [
            {
                'step_id': 'sync_tool',
                'mode': 'tool_call',
                'tool_spec': {'tool_name': 'demo_tool', 'arguments': {'x': 2}},
            }
        ],
        workflow_id='wf_sync_failed_cancel_claim',
    )
    first_failed = sync_engine.run_workflow(
        sync_def,
        workflow_run_id='run-sync-failed',
        concurrency_policy='skip_if_running',
    )
    assert first_failed.status == 'failed'
    assert first_failed.steps[0].escalation_result.get('effect_started') is True
    assert _concurrency_settled(first_failed) is False
    assert sync_engine.store._read('run-sync-failed', 'run.json')['concurrency_claim']['phase'] == 'retained'

    blocked_sync = sync_engine.run_workflow(
        sync_def,
        workflow_run_id='run-sync-blocked',
        concurrency_policy='skip_if_running',
    )
    assert isinstance(blocked_sync, dict)
    assert blocked_sync['status'] == 'skipped'
    assert blocked_sync['running_run_id'] == 'run-sync-failed'

    assert (
        sync_engine.cancel_workflow_run(
            'run-sync-failed',
            expected_revision=first_failed.revision,
            operation_key='cancel-sync-failed',
        )
        == []
    )
    cancelled_sync = sync_engine.store.load_run('run-sync-failed')
    assert cancelled_sync.status == 'cancelled'
    assert _concurrency_settled(cancelled_sync) is True
    assert sync_engine.store._read('run-sync-failed', 'run.json')['concurrency_claim']['phase'] == 'released'

    sync_mode['fail_unknown'] = False
    following_sync = sync_engine.run_workflow(
        sync_def,
        workflow_run_id='run-sync-following',
        concurrency_policy='skip_if_running',
    )
    assert isinstance(following_sync, WorkflowRunRecord)
    assert following_sync.status == 'succeeded'

    # (c) cleanup_error 与未结清 agent_task 即使在 run.status == 'cancelled' 时仍必须阻断释放；
    #     未取消的 failed/paused_for_resume 且 effect_started=True 也继续保留占位。
    cleanup_dir = tmp_path / 'cleanup_err_retained'

    def _cleanup_err_dispatcher(_tool_name, _arguments, **kwargs):
        kwargs['step'].escalation_result['cleanup_error'] = 'rollback unfinished'
        return {
            'ok': False,
            'status': 'failed',
            'effect_outcome': 'unknown',
            'error_code': 'SIMULATED_IO_ERROR',
            'error_message': 'simulated error during cleanup',
        }

    cleanup_engine = WorkflowEngine(
        cleanup_dir,
        workspace_root=cleanup_dir,
    )
    cleanup_engine.tool_dispatcher = _cleanup_err_dispatcher
    cleanup_def = _dag_definition(
        [
            {
                'step_id': 'cleanup_tool',
                'mode': 'tool_call',
                'tool_spec': {'tool_name': 'demo_tool', 'arguments': {'x': 3}},
            }
        ],
        workflow_id='wf_cleanup_err_retained',
    )
    cleanup_run = cleanup_engine.run_workflow(
        cleanup_def,
        workflow_run_id='run-cleanup-err',
        concurrency_policy='skip_if_running',
    )
    assert cleanup_run.status == 'failed'
    assert (
        cleanup_engine.cancel_workflow_run(
            'run-cleanup-err',
            expected_revision=cleanup_run.revision,
            operation_key='cancel-cleanup-err',
        )
        == []
    )
    cancelled_cleanup = cleanup_engine.store.load_run('run-cleanup-err')
    assert cancelled_cleanup.status == 'cancelled'
    assert _concurrency_settled(cancelled_cleanup) is False
    assert cleanup_engine.store._read('run-cleanup-err', 'run.json')['concurrency_claim']['phase'] == 'retained'

    blocked_cleanup = cleanup_engine.run_workflow(
        cleanup_def,
        workflow_run_id='run-cleanup-blocked',
        concurrency_policy='skip_if_running',
    )
    assert isinstance(blocked_cleanup, dict)
    assert blocked_cleanup['status'] == 'skipped'
    assert blocked_cleanup['running_run_id'] == 'run-cleanup-err'

    unsettled_agent_cancelled = WorkflowRunRecord(
        workflow_run_id='run-agent-cancelled-unsettled',
        workflow_id='wf-agent-check',
        owner_id='alice',
        trigger_kind='manual',
        status='cancelled',
        started_at=1.0,
        ended_at=2.0,
        duration_ms=10.0,
        steps=(
            StepExecutionRecord(
                step_id='agent_step',
                name='agent_step',
                mode='agent_task',
                status='cancelled',
                started_at=1.0,
                ended_at=2.0,
                duration_ms=10.0,
                llm_involved=True,
                escalation_result={'effect_started': True, 'task_id': 'stask-unsettled-1'},
            ),
        ),
        final_output={},
        effect_outcome='unknown',
    )
    assert _concurrency_settled(unsettled_agent_cancelled) is False
