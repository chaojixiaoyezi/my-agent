"""W5（PD-4 / BIZ-012）运行时行为回归：空 step_id 断点投影、工作流引用覆盖与取消/未结清阻断、资产到运行锁序、维护结构化诊断。"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.assets import asset_root_lock_held
from agent_py_agent.agent.assets.descriptor import AssetDescriptor
from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.conversation.business_access import BusinessActor
from agent_py_agent.agent.conversation.business_contracts import BusinessExecutionContext
from agent_py_agent.agent.conversation.business_spaces import BusinessSpaceService
from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
from agent_py_agent.agent.workflows import engine as engine_module
from agent_py_agent.agent.workflows import reference_sources as reference_sources_module
from agent_py_agent.agent.workflows.business_scope import (
    WorkflowPolicyRefusal,
    WorkflowScopeService,
)
from agent_py_agent.agent.workflows.engine import WorkflowEngine
from agent_py_agent.agent.workflows.models import StepExecutionRecord, WorkflowRunRecord
from agent_py_agent.agent.workflows.orphan_maintenance import (
    cleanup_status,
    cleanup_status_diagnostics,
    maintain_owner_workflows,
)
from agent_py_agent.agent.workflows.reference_sources import build_workflow_reference_reader
from agent_py_agent.agent.workflows.run_list_index import RunListIndex
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore, _RunBusy


def _service(
    owner_home: Path,
    *,
    reference_reader=None,
    use_default_reader: bool = False,
    engine_factory=None,
    with_space: bool = False,
    clock=None,
) -> tuple[WorkflowScopeService, BusinessActor, str | None]:
    spaces = BusinessSpaceService(owner_home, owner_id='alice', enabled=True)
    actor = BusinessActor('alice', 'w5-user', spaces, lambda action, facts: True)
    space_id = (
        spaces.call('spaces.create', {'name': 'w5-space'}, actor=actor, idempotency_key='w5-space-create-0001')['space_id']
        if with_space
        else None
    )
    reader = build_workflow_reference_reader(owner_home) if (use_default_reader and reference_reader is None) else reference_reader
    factory = (lambda home, **kwargs: WorkflowEngine(home, **kwargs)) if engine_factory is None else engine_factory
    context_provider = (
        (lambda _actor_obj, _req: BusinessExecutionContext('alice', space_id, None, 'stask-w5', 1, None, (), (), 'business-context.v1'))
        if with_space
        else None
    )
    service = WorkflowScopeService(
        owner_home,
        owner_id='alice',
        authorize=lambda current, action, ref: current is actor,
        reference_reader=reader,
        engine_factory=factory,
        context_provider=context_provider,
        clock=clock,
    )
    return service, actor, space_id


def _workflow_definition(
    workflow_id: str,
    *,
    steps: list[dict] | None = None,
) -> dict:
    return {
        'descriptor': {
            'id': workflow_id,
            'kind': 'workflow',
            'name': workflow_id,
            'access_targets': [],
            'data_sources': [],
            'credential_refs': [],
            'io_contract': {},
        },
        'steps': [] if steps is None else steps,
    }


def _create_script(owner_home: Path, script_id: str, body: str) -> None:
    ScriptStore(owner_home).save_script(AssetDescriptor(script_id, 'script', script_id), body)


def _create_workflow(
    service: WorkflowScopeService,
    actor: BusinessActor,
    workflow_id: str,
    *,
    steps: list[dict] | None = None,
    scope: dict | None = None,
    key_suffix: str = 'create',
) -> dict:
    params = {'definition': _workflow_definition(workflow_id, steps=steps)}
    if scope is not None:
        params['scope'] = scope
    return service.execute(
        'workflows.create',
        params,
        actor=actor,
        idempotency_key=f'{workflow_id}-{key_suffix}-0001',
    )


def _run_record(
    workflow_id: str,
    run_id: str,
    *,
    status: str = 'succeeded',
    steps: tuple[StepExecutionRecord, ...] = (),
    current_step_id: str = '',
    breakpoint_data: dict | None = None,
    now: float = 100.0,
) -> WorkflowRunRecord:
    return WorkflowRunRecord(
        workflow_run_id=run_id,
        workflow_id=workflow_id,
        owner_id='alice',
        trigger_kind='manual',
        status=status,
        started_at=now,
        ended_at=0.0 if status in ('queued', 'running', 'waiting_for_agent', 'paused_for_resume', 'cancelling') else now + 1.0,
        duration_ms=10.0,
        current_step_id=current_step_id,
        steps=steps,
        breakpoint=breakpoint_data,
        execution_definition_ref='sha256:test-definition',
        effect_outcome='not_started',
    )


def test_paused_workflow_without_current_step_validates_against_nullable_breakpoint_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PD-4 / V05-07：未推进 current_step_id 且/或错误码为空时，workflow_runs.get/.list 通过严格 Schema 校验。"""
    api, actor, space_id = _service(tmp_path, with_space=True)
    # 1. 真实引擎在空步骤或尚未推进 current_step_id 时中断：_pause_failed_run 生成 current_step_id == '' 且无 step_id 的断点。
    _create_workflow(
        api,
        actor,
        'bp-empty-steps',
        steps=[],
        scope={'scope_mode': 'spaces', 'space_ids': [space_id]},
    )
    real_check_stop = engine_module._check_stop
    armed = [True]

    def stop_once() -> None:
        if armed:
            armed.clear()
            raise InterruptedError('paused without current step')
        real_check_stop()

    monkeypatch.setattr(engine_module, '_check_stop', stop_once)
    with pytest.raises(InterruptedError):
        api.execute(
            'workflows.run',
            {'workflow_ref': 'bp-empty-steps', 'space_id': space_id},
            actor=actor,
            idempotency_key='bp-empty-run-00001',
        )

    run_list = api.execute('workflow_runs.list', {'workflow_ref': 'bp-empty-steps'}, actor=actor)
    business_action_spec('workflow_runs.list').validate_result(run_list)
    assert len(run_list['items']) == 1
    paused_run_id = run_list['items'][0]['workflow_run_id']

    run_detail = api.execute('workflow_runs.get', {'workflow_run_id': paused_run_id}, actor=actor)
    business_action_spec('workflow_runs.get').validate_result(run_detail)
    assert run_detail['run']['status'] == 'paused_for_resume'
    assert run_detail['breakpoint'] is not None
    assert run_detail['breakpoint']['step_id'] is None
    assert run_detail['breakpoint']['reason_code'] == 'WORKFLOW_PAUSED'

    # 2. 真实引擎推进了步骤 s1 时中断：保留 s1 并同样通过校验。
    _create_script(tmp_path, 'step-one', 'print(\'{"ok": true}\')')
    _create_workflow(
        api,
        actor,
        'bp-with-step',
        steps=[{'step_id': 's1', 'name': 'step1', 'mode': 'deterministic_script', 'script_id': 'step-one'}],
        scope={'scope_mode': 'spaces', 'space_ids': [space_id]},
    )
    armed.append(True)
    with pytest.raises(InterruptedError):
        api.execute(
            'workflows.run',
            {'workflow_ref': 'bp-with-step', 'space_id': space_id},
            actor=actor,
            idempotency_key='bp-step-run-000001',
        )
    with_step_list = api.execute('workflow_runs.list', {'workflow_ref': 'bp-with-step'}, actor=actor)
    business_action_spec('workflow_runs.list').validate_result(with_step_list)
    with_step_detail = api.execute(
        'workflow_runs.get',
        {'workflow_run_id': with_step_list['items'][0]['workflow_run_id']},
        actor=actor,
    )
    business_action_spec('workflow_runs.get').validate_result(with_step_detail)
    assert with_step_detail['breakpoint']['step_id'] == 's1'
    assert with_step_detail['breakpoint']['reason_code'] == 'WORKFLOW_PAUSED'

    # 3. 两条直接存储边界：空 current_step_id + 空 reason_code -> 回退 WORKFLOW_PAUSED；仅 breakpoint.step_id -> 保留。
    run_store = WorkflowRunStore(tmp_path)
    empty_reason_run = _run_record(
        'bp-empty-steps',
        'wfrun-empty-reason',
        status='paused_for_resume',
        current_step_id='',
        breakpoint_data={'error_code': '', 'reason_code': '', 'can_resume': True},
    )
    run_store.create_run(empty_reason_run)
    empty_detail = api.execute('workflow_runs.get', {'workflow_run_id': 'wfrun-empty-reason'}, actor=actor)
    business_action_spec('workflow_runs.get').validate_result(empty_detail)
    assert empty_detail['breakpoint'] == {
        'step_id': None,
        'reason_code': 'WORKFLOW_PAUSED',
        'can_resume': True,
        'required_action': 'resume',
    }

    bp_step_run = _run_record(
        'bp-empty-steps',
        'wfrun-bp-step',
        status='paused_for_resume',
        current_step_id='',
        breakpoint_data={'step_id': 'step-bp', 'reason_code': 'MANUAL_CHECK', 'can_resume': False},
    )
    run_store.create_run(bp_step_run)
    bp_detail = api.execute('workflow_runs.get', {'workflow_run_id': 'wfrun-bp-step'}, actor=actor)
    business_action_spec('workflow_runs.get').validate_result(bp_detail)
    assert bp_detail['breakpoint']['step_id'] == 'step-bp'
    assert bp_detail['breakpoint']['reason_code'] == 'MANUAL_CHECK'


def test_cancelling_and_legacy_unindexed_runs_block_workflow_recycle_and_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIZ-012：cancelling、终态未结清义务、pending/legacy/未扫描完 SQLite 索引全数严格拦截回收，并免于无界枚举。"""
    api, actor, _ = _service(tmp_path, use_default_reader=True)
    run_store = WorkflowRunStore(tmp_path)

    # 1. cancelling 运行必须阻断 recycle。
    _create_workflow(api, actor, 'wf-cancelling')
    run_store.create_run(_run_record('wf-cancelling', 'wfrun-cancelling-1', status='cancelling'))
    with pytest.raises(WorkflowPolicyRefusal) as cancelling_err:
        api.execute(
            'workflows.recycle',
            {'workflow_ref': 'wf-cancelling'},
            actor=actor,
            expected_revision=1,
            idempotency_key='recycle-cancelling-1',
        )
    assert cancelling_err.value.error_code == 'WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN'
    assert cancelling_err.value.details['blocker_reason_code'] == 'ACTIVE_WORKFLOW_RUN'

    # 2. 大量已结清 runs 与 1 条未结清 agent_task 终态 run 共存时，open_ref_runs 必须精确命中而不被分页截断。
    _create_workflow(api, actor, 'wf-unsettled')
    for idx in range(105):
        run_store.create_run(_run_record('wf-unsettled', f'wfrun-settled-{idx:03d}', now=200.0 + idx))
    unsettled_step = StepExecutionRecord(
        step_id='agent-1',
        name='agent',
        mode='agent_task',
        status='failed',
        started_at=310.0,
        ended_at=311.0,
        duration_ms=1000.0,
        llm_involved=False,
        escalation_result={'task_id': 'stask-outstanding-1', 'submission_state': 'committed'},
    )
    run_store.create_run(
        _run_record('wf-unsettled', 'wfrun-unsettled-1', status='cancelled', steps=(unsettled_step,), now=310.0)
    )
    # 完整索引查询不得触发 runs 目录 iterdir 全扫。
    runs_root = run_store.run_dir('wfrun-unsettled-1').parent
    real_iterdir = Path.iterdir

    def guarded_iterdir(self: Path):
        if self == runs_root:
            raise AssertionError('healthy complete open_ref_runs must not walk runs_root')
        return real_iterdir(self)

    monkeypatch.setattr(Path, 'iterdir', guarded_iterdir)
    status_unsettled = cleanup_status(api, api._get('wf-unsettled'))
    assert any(
        b['ref'] == 'wfrun-unsettled-1' and b['reason_code'] == 'ACTIVE_WORKFLOW_RUN'
        for b in status_unsettled['blockers']
    )
    monkeypatch.undo()

    # 3. 缺少 open_ref_scan（模拟旧 v4 库升级、已有 runs 行）且单次受限回填尚未扫完时，fail-closed 为 REFERENCE_COVERAGE_UNKNOWN。
    _create_workflow(api, actor, 'wf-legacy')
    legacy_dir = runs_root / 'wfrun-legacy-disk'
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_record = _run_record('wf-legacy', 'wfrun-legacy-disk', status='cancelling')
    (legacy_dir / 'run.json').write_text(json.dumps(legacy_record.to_dict()), encoding='utf-8')
    index = RunListIndex(tmp_path)
    with index.connection() as conn:
        conn.execute('DROP TABLE IF EXISTS open_ref_runs')
        conn.execute('DROP TABLE IF EXISTS open_ref_scan')
        conn.execute('PRAGMA user_version = 4')
    with monkeypatch.context() as mp:
        mp.setattr(reference_sources_module, '_max_open_ref_backfill_batches', 1)
        with pytest.raises(WorkflowPolicyRefusal) as unknown_err:
            api.execute(
                'workflows.recycle',
                {'workflow_ref': 'wf-legacy'},
                actor=actor,
                expected_revision=1,
                idempotency_key='recycle-legacy-unknown',
            )
    assert unknown_err.value.error_code == 'WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN'
    assert unknown_err.value.details['blocker_reason_code'] == 'REFERENCE_COVERAGE_UNKNOWN'

    # 回填一次，收口未扫描状态。
    run_store.list_runs(limit=200)
    index.backfill(run_store, batch_size=500)

    # 4. 存在 pending 行（模拟写 run.json 后索引发布失败）时，必须 fail-closed 阻断任意 workflow 回收。
    _create_workflow(api, actor, 'wf-clean-target')
    index.mark_pending('wfrun-pending-x')
    status_pending = cleanup_status(api, api._get('wf-clean-target'))
    assert 'REFERENCE_COVERAGE_UNKNOWN' in {b['reason_code'] for b in status_pending['blockers']}
    index.ack_pending('wfrun-pending-x')

    # 5. 存在 legacy 行：backfill 吸收前 fail-closed；backfill 完成后旧未索引 cancelling run 进入 open_ref_runs 继续阻断原工作流，无关清洁工作流可回收。
    legacy_dir_2 = runs_root / 'wfrun-legacy-only'
    legacy_dir_2.mkdir(parents=True, exist_ok=True)
    legacy_rec_2 = _run_record('wf-legacy', 'wfrun-legacy-only', status='cancelling')
    (legacy_dir_2 / 'run.json').write_text(json.dumps(legacy_rec_2.to_dict()), encoding='utf-8')
    with index.connection() as conn:
        conn.execute('INSERT OR IGNORE INTO legacy VALUES(?)', ('wfrun-legacy-only',))
    status_legacy_marker = cleanup_status(api, api._get('wf-clean-target'))
    assert 'REFERENCE_COVERAGE_UNKNOWN' in {b['reason_code'] for b in status_legacy_marker['blockers']}

    index.backfill(run_store, batch_size=500)
    assert any(b['reason_code'] == 'ACTIVE_WORKFLOW_RUN' for b in cleanup_status(api, api._get('wf-legacy'))['blockers'])
    assert api.execute(
        'workflows.recycle',
        {'workflow_ref': 'wf-clean-target'},
        actor=actor,
        expected_revision=1,
        idempotency_key='recycle-clean-after-bf',
    )['lifecycle'] == 'recycled'


def test_concurrent_recycle_and_first_run_creation_serialize_without_orphan_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BIZ-012：WorkflowStore._locked() -> run_store._lock 串行阻止回收与首运行竞态，且两把锁在步骤执行前全部释放。"""
    # Case 1: recycle 先抢到 WorkflowStore._locked()，准备 create_run 的首次运行必须在启动步骤前被 WORKFLOW_RECYCLED 拒绝且零落盘。
    home_a = tmp_path / 'case_a'
    api_a, actor_a, space_a = _service(home_a, use_default_reader=True, with_space=True)
    executed_a = []
    _create_script(home_a, 'side-effect-a', 'print(\'{"ran": true}\')')
    _create_workflow(
        api_a,
        actor_a,
        'race-recycle-wins',
        steps=[{'step_id': 's1', 'name': 's1', 'mode': 'deterministic_script', 'script_id': 'side-effect-a'}],
        scope={'scope_mode': 'spaces', 'space_ids': [space_a]},
    )

    entered_register = threading.Event()
    recycle_committed = threading.Event()
    real_register = WorkflowStore.register_run_locked

    def pause_before_register(self, workflow_id, callback, *, guard=None):
        if workflow_id == 'race-recycle-wins' and not entered_register.is_set():
            entered_register.set()
            assert recycle_committed.wait(timeout=5.0)
        return real_register(self, workflow_id, callback, guard=guard)

    monkeypatch.setattr(WorkflowStore, 'register_run_locked', pause_before_register)
    monkeypatch.setattr(
        engine_module,
        'run_script_with_datasources',
        lambda *args, **kwargs: executed_a.append(args) or {'ran': True},
    )

    run_errors: list[BaseException] = []

    def run_worker() -> None:
        try:
            api_a.execute(
                'workflows.run',
                {'workflow_ref': 'race-recycle-wins', 'space_id': space_a},
                actor=actor_a,
                idempotency_key='race-run-loser-001',
            )
        except BaseException as exc:  # noqa: BLE001
            run_errors.append(exc)

    thread_a = threading.Thread(target=run_worker)
    thread_a.start()
    assert entered_register.wait(timeout=5.0)
    recycled = api_a.execute(
        'workflows.recycle',
        {'workflow_ref': 'race-recycle-wins'},
        actor=actor_a,
        expected_revision=1,
        idempotency_key='race-recycle-win-01',
    )
    assert recycled['lifecycle'] == 'recycled'
    recycle_committed.set()
    thread_a.join(timeout=5.0)

    assert len(run_errors) == 1
    assert isinstance(run_errors[0], WorkflowPolicyRefusal)
    assert run_errors[0].error_code == 'WORKFLOW_RECYCLED'
    assert WorkflowRunStore(home_a).list_runs(limit=10) == []
    assert executed_a == []

    # Case 2: create_run 先抢到锁，返回后资产锁与 run_store._lock 均在步骤执行前释放；并发 recycle 立刻被活跃运行阻塞而非互锁。
    monkeypatch.undo()
    home_b = tmp_path / 'case_b'
    post_create_reached = threading.Event()
    allow_execution_start = threading.Event()
    step_entered = threading.Event()
    allow_step_exit = threading.Event()
    lock_observations: dict[str, bool] = {}

    real_create_run = WorkflowRunStore.create_run
    created_run_ids: list[str] = []

    def observe_after_create_run(self, record, workflow_snapshot=None, *, asset_guard=None, require_active_asset=False):
        saved = real_create_run(
            self,
            record,
            workflow_snapshot,
            asset_guard=asset_guard,
            require_active_asset=require_active_asset,
        )
        if record.workflow_id == 'race-create-wins':
            created_run_ids.append(record.workflow_run_id)
            post_create_reached.set()
            assert allow_execution_start.wait(timeout=5.0)
        return saved

    real_run_script = engine_module.run_script_with_datasources

    def slow_script(*args, **kwargs):
        lock_observations['asset_lock_held_in_step'] = asset_root_lock_held(home_b)
        step_entered.set()
        assert allow_step_exit.wait(timeout=5.0)
        return real_run_script(*args, **kwargs)

    api_b, actor_b, space_b = _service(home_b, use_default_reader=True, with_space=True)
    _create_script(home_b, 'side-effect-b', 'print(\'{"ok": true}\')')
    _create_workflow(
        api_b,
        actor_b,
        'race-create-wins',
        steps=[{'step_id': 's1', 'name': 's1', 'mode': 'deterministic_script', 'script_id': 'side-effect-b'}],
        scope={'scope_mode': 'spaces', 'space_ids': [space_b]},
    )
    monkeypatch.setattr(WorkflowRunStore, 'create_run', observe_after_create_run)
    monkeypatch.setattr(engine_module, 'run_script_with_datasources', slow_script)

    run_results: list[dict] = []

    def create_worker() -> None:
        run_results.append(
            api_b.execute(
                'workflows.run',
                {'workflow_ref': 'race-create-wins', 'space_id': space_b},
                actor=actor_b,
                idempotency_key='race-run-winner-01',
            )
        )

    thread_b = threading.Thread(target=create_worker)
    thread_b.start()

    # create_run 返回后、步骤执行前：资产锁与 run_store._lock 均须已释放，其他线程可非阻塞直接获取。
    assert post_create_reached.wait(timeout=5.0)
    assert not asset_root_lock_held(home_b)
    run_store_b = WorkflowRunStore(home_b)
    with run_store_b._lock(created_run_ids[0], blocking=False):
        pass
    allow_execution_start.set()

    # 步骤执行中资产锁仍为未持有，并发 recycle 在 2s 内立刻完成检查并被活跃运行阻塞，原运行正常收口为 completed。
    assert step_entered.wait(timeout=5.0)
    assert lock_observations == {'asset_lock_held_in_step': False}

    t0 = time.monotonic()
    with pytest.raises(WorkflowPolicyRefusal) as concurrent_recycle_err:
        api_b.execute(
            'workflows.recycle',
            {'workflow_ref': 'race-create-wins'},
            actor=actor_b,
            expected_revision=1,
            idempotency_key='race-recycle-lose-1',
        )
    assert time.monotonic() - t0 < 2.0
    assert concurrent_recycle_err.value.error_code == 'WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN'
    assert concurrent_recycle_err.value.details['blocker_reason_code'] == 'ACTIVE_WORKFLOW_RUN'

    allow_step_exit.set()
    thread_b.join(timeout=5.0)
    assert len(run_results) == 1 and run_results[0]['state'] == 'completed'


def test_orphan_maintenance_surfaces_structured_exception_diagnostics(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """BIZ-012：reference_reader 异常记为结构化脱敏诊断与日志，且不破坏 CleanupStatus 严格 Schema。"""
    clock_now = [1000.0]

    def broken_reader(_target_id: str) -> dict:
        raise OSError('simulated I/O error Authorization: Bearer sk-live-secret999')

    api, actor, _ = _service(tmp_path, reference_reader=broken_reader, clock=lambda: clock_now[0])
    _create_workflow(
        api,
        actor,
        'diag-flow',
        scope={'scope_mode': 'unassigned', 'space_ids': []},
    )
    definition = api._get('diag-flow')

    diagnostics: dict = {}
    with caplog.at_level(logging.WARNING, logger='agent_py_agent.agent.workflows.orphan_maintenance'):
        status = cleanup_status(api, definition, diagnostics=diagnostics)
        pair_status, pair_diag = cleanup_status_diagnostics(api, definition)

    assert status == pair_status
    assert set(status) == {
        'protected',
        'orphan_since',
        'last_content_edit_at',
        'last_run_at',
        'last_restore_at',
        'eligible_at',
        'blockers',
    }
    assert status['blockers'] == [
        {'ref': 'diag-flow', 'kind': 'coverage', 'reason_code': 'REFERENCE_COVERAGE_UNKNOWN'}
    ]

    for recorded in (diagnostics, pair_diag):
        assert recorded['error_code'] == 'REFERENCE_READER_FAILED'
        assert recorded['exception_type'] == 'OSError'
        assert 'sk-live-secret999' not in recorded['summary']
        assert 'simulated I/O error' in recorded['summary']
        assert recorded['side_effect'] == 'not_started'
        assert recorded['recommended_action'] == 'inspect_reference_sources_and_retry'

    assert any('REFERENCE_READER_FAILED' in rec.getMessage() for rec in caplog.records)

    # workflows.get 必须保持 CleanupStatus additionalProperties: False 合同。
    detail = api.execute('workflows.get', {'workflow_ref': 'diag-flow'}, actor=actor)
    business_action_spec('workflows.get').validate_result(detail)
    assert detail['cleanup_status'] == status

    # workflows.recycle 将结构化诊断原样投影至 WorkflowPolicyRefusal.details。
    with pytest.raises(WorkflowPolicyRefusal) as refuse_err:
        api.execute(
            'workflows.recycle',
            {'workflow_ref': 'diag-flow'},
            actor=actor,
            expected_revision=1,
            idempotency_key='recycle-diag-000001',
        )
    err = refuse_err.value
    assert err.error_code == 'WORKFLOW_REFERENCED_OR_COVERAGE_UNKNOWN'
    assert err.details['blocker_reason_code'] == 'REFERENCE_COVERAGE_UNKNOWN'
    assert err.details['error_code'] == 'REFERENCE_READER_FAILED'
    assert err.details['exception_type'] == 'OSError'
    assert err.details['side_effect'] == 'not_started'
    assert err.details['recommended_action'] == 'inspect_reference_sources_and_retry'
    assert 'sk-live-secret999' not in err.details['summary']

    # maintain_owner_workflows 对到达保留期、遇 reader 异常的孤立资产同样保留诊断信息并记录结构化日志。
    clock_now[0] = 10_000_000.0
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='agent_py_agent.agent.workflows.orphan_maintenance'):
        maintain_owner_workflows(api, actor=actor, idempotency_key='maintain-diag-00001')
    assert any('REFERENCE_READER_FAILED' in rec.getMessage() for rec in caplog.records)


def test_concurrent_duplicate_run_registration_does_not_deadlock_with_asset_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FW41-P1-01：create_run 在 asset_root_lock 内必须非阻塞取运行锁，避免与持 execution_lock 读 ScriptStore 形成 AB/BA 死锁。"""
    api, actor, space_id = _service(tmp_path, use_default_reader=True, with_space=True)
    exec_calls: list[int] = []
    _create_script(tmp_path, 'lock-cycle-script', 'print(\'{"done": true}\')\n')
    _create_workflow(
        api,
        actor,
        'wf-lock-cycle',
        steps=[{'step_id': 's1', 'name': 's1', 'mode': 'deterministic_script', 'script_id': 'lock-cycle-script'}],
        scope={'scope_mode': 'spaces', 'space_ids': [space_id]},
    )

    idemp_key = 'same-run-idempotency-key'
    expected_run_id = hashlib.sha256((actor.owner_id + '\0workflows.run\0' + idemp_key).encode()).hexdigest()

    holding_exec_lock = threading.Event()
    allow_asset_read = threading.Event()
    paused_before_asset_read = threading.Event()

    real_exec_lock = WorkflowRunStore.execution_lock
    real_get_script = ScriptStore.get_script
    real_run_script_wd = engine_module.run_script_with_datasources

    def wrapped_run_script_wd(*args, **kwargs):
        exec_calls.append(1)
        return real_run_script_wd(*args, **kwargs)

    def wrapped_exec_lock(self: WorkflowRunStore, workflow_run_id: str):
        cm = real_exec_lock(self, workflow_run_id)
        if workflow_run_id != expected_run_id:
            return cm

        class _TrackedLock:
            def __enter__(self_inner):
                res = cm.__enter__()
                holding_exec_lock.set()
                return res

            def __exit__(self_inner, exc_type, exc, tb):
                holding_exec_lock.clear()
                return cm.__exit__(exc_type, exc, tb)

        return _TrackedLock()

    def wrapped_get_script(self: ScriptStore, script_id: str):
        # 仅在线程 1 已持有 execution_lock 进入步骤脚本执行读取资产时暂停，避免阻塞 preflight 的 _script_pins。
        if script_id == 'lock-cycle-script' and holding_exec_lock.is_set() and not paused_before_asset_read.is_set():
            assert not asset_root_lock_held(tmp_path)
            paused_before_asset_read.set()
            assert allow_asset_read.wait(timeout=5.0)
        return real_get_script(self, script_id)

    monkeypatch.setattr(WorkflowRunStore, 'execution_lock', wrapped_exec_lock)
    monkeypatch.setattr(ScriptStore, 'get_script', wrapped_get_script)
    monkeypatch.setattr(engine_module, 'run_script_with_datasources', wrapped_run_script_wd)

    t1_results: list[dict] = []
    t1_errors: list[BaseException] = []
    t2_errors: list[BaseException] = []

    def worker_one() -> None:
        try:
            res = api.execute(
                'workflows.run',
                {'workflow_ref': 'wf-lock-cycle', 'space_id': space_id},
                actor=actor,
                idempotency_key=idemp_key,
            )
            t1_results.append(res)
        except BaseException as exc:  # pragma: no cover - 仅失败诊断收集
            t1_errors.append(exc)

    def worker_two() -> None:
        try:
            store = WorkflowRunStore(tmp_path)
            frozen_snapshot = store.load_workflow_snapshot(expected_run_id)
            dup_record = _run_record('wf-lock-cycle', expected_run_id, status='running')
            dup_record = WorkflowRunRecord(**{**dup_record.to_dict(), 'owner_id': actor.owner_id})
            store.create_run(dup_record, frozen_snapshot, require_active_asset=True)
        except BaseException as exc:
            t2_errors.append(exc)

    t1 = threading.Thread(target=worker_one, name='fw41-p1-01-t1')
    t2 = threading.Thread(target=worker_two, name='fw41-p1-01-t2')
    try:
        t1.start()
        assert paused_before_asset_read.wait(timeout=5.0), 'thread 1 did not reach asset read under execution_lock'

        t2.start()
        # 线程 2 必须在线程 1 仍持有 execution_lock 且尚未读资产之前立即返回 WORKFLOW_RUN_BUSY，不能阻塞等待或持 asset 锁挂死。
        t2.join(timeout=5.0)
        assert not t2.is_alive(), 'duplicate create_run blocked inside register_run_locked'
    finally:
        allow_asset_read.set()
        t1.join(timeout=5.0)

    assert not t1.is_alive(), 'executing workflow thread hung trying to acquire asset lock'
    assert not t1_errors, f'thread 1 raised unexpectedly: {t1_errors!r}'
    assert len(t1_results) == 1 and t1_results[0]['state'] == 'completed'
    assert len(t2_errors) == 1
    busy_err = t2_errors[0]
    assert isinstance(busy_err, _RunBusy)
    assert busy_err.code == 'WORKFLOW_RUN_BUSY'
    assert busy_err.status == 409
    assert busy_err.outcome == 'not_started'
    assert busy_err.details['reason_code'] == 'WORKFLOW_EXECUTION_LOCK_HELD'
    assert busy_err.details['control_started'] is False

    # 资产读取恢复正常，终态保留为 succeeded，且脚本副作用仅发生一次、同键重放幂等。
    assert ScriptStore(tmp_path).get_script('lock-cycle-script')['id'] == 'lock-cycle-script'
    run_store = WorkflowRunStore(tmp_path)
    saved = run_store.load_run(expected_run_id)
    assert saved.status == 'succeeded'
    assert len(exec_calls) == 1
    replay = api.execute(
        'workflows.run',
        {'workflow_ref': 'wf-lock-cycle', 'space_id': space_id},
        actor=actor,
        idempotency_key=idemp_key,
    )
    assert replay['workflow_run_id'] == expected_run_id
    assert replay['state'] == 'completed'
    assert len(exec_calls) == 1


def test_open_ref_scan_upgrade_backfill_converges_without_list_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FABLE-P1-4：旧 v4 索引升级后 _check_runs 自动有界回填 open_ref_scan，无需先调 list_runs 也不走 runs 目录枚举。"""
    # 1. 约 100 条无关已结清运行在掉落 open_ref_scan 后，单次 cleanup_status 与 recycle 直接收敛且不调用 list_runs/iterdir。
    home_clean = tmp_path / 'upgrade_clean'
    api_clean, actor_clean, _ = _service(home_clean, use_default_reader=True)
    run_store_clean = WorkflowRunStore(home_clean)
    _create_workflow(api_clean, actor_clean, 'wf-other-clean')
    _create_workflow(api_clean, actor_clean, 'wf-target-clean')
    for idx in range(96):
        run_store_clean.create_run(_run_record('wf-other-clean', f'wfrun-clean-{idx:03d}', now=200.0 + idx))

    runs_root_clean = run_store_clean.run_dir('wfrun-clean-000').parent
    index_clean = RunListIndex(home_clean)
    with index_clean.connection() as conn:
        conn.execute('DROP TABLE IF EXISTS open_ref_runs')
        conn.execute('DROP TABLE IF EXISTS open_ref_scan')
        conn.execute('PRAGMA user_version = 4')

    real_iterdir = Path.iterdir
    with monkeypatch.context() as mp:
        def no_runs_walk(self: Path):
            if self == runs_root_clean:
                raise AssertionError('bounded upgrade backfill must not enumerate runs_root')
            return real_iterdir(self)

        def forbid_list_runs(self: WorkflowRunStore, *_args, **_kwargs):
            raise AssertionError('upgrade probe must converge without calling list_runs')

        mp.setattr(Path, 'iterdir', no_runs_walk)
        mp.setattr(WorkflowRunStore, 'list_runs', forbid_list_runs)
        status_clean = cleanup_status(api_clean, api_clean._get('wf-target-clean'))
        assert status_clean['blockers'] == []
        recycled = api_clean.execute(
            'workflows.recycle',
            {'workflow_ref': 'wf-target-clean'},
            actor=actor_clean,
            expected_revision=1,
            idempotency_key='recycle-clean-upgrade',
        )
        assert recycled['lifecycle'] == 'recycled'

    # 2. 同类升级场景下若目标工作流存在未结清 agent_task 终态运行，单次查询必须精确回报 ACTIVE_WORKFLOW_RUN。
    home_blocked = tmp_path / 'upgrade_blocked'
    api_blocked, actor_blocked, _ = _service(home_blocked, use_default_reader=True)
    run_store_blocked = WorkflowRunStore(home_blocked)
    _create_workflow(api_blocked, actor_blocked, 'wf-other-blocked')
    _create_workflow(api_blocked, actor_blocked, 'wf-target-blocked')
    for idx in range(70):
        run_store_blocked.create_run(_run_record('wf-other-blocked', f'wfrun-blk-other-{idx:03d}', now=200.0 + idx))
    unsettled_step = StepExecutionRecord(
        step_id='agent-1',
        name='agent',
        mode='agent_task',
        status='failed',
        started_at=310.0,
        ended_at=311.0,
        duration_ms=1000.0,
        llm_involved=False,
        escalation_result={'task_id': 'stask-upgrade-1', 'submission_state': 'committed'},
    )
    run_store_blocked.create_run(
        _run_record('wf-target-blocked', 'wfrun-target-unsettled', status='cancelled', steps=(unsettled_step,), now=310.0)
    )
    index_blocked = RunListIndex(home_blocked)
    with index_blocked.connection() as conn:
        conn.execute('DROP TABLE IF EXISTS open_ref_runs')
        conn.execute('DROP TABLE IF EXISTS open_ref_scan')
        conn.execute('PRAGMA user_version = 4')

    status_blocked = cleanup_status(api_blocked, api_blocked._get('wf-target-blocked'))
    assert any(
        b['ref'] == 'wfrun-target-unsettled' and b['reason_code'] == 'ACTIVE_WORKFLOW_RUN'
        for b in status_blocked['blockers']
    )

    # 3. 回填受单次批数上限约束：当历史条数超过单次上限时首次仍 fail-closed，后续调用继续推进并最终收敛。
    home_bounded = tmp_path / 'upgrade_bounded'
    api_bounded, actor_bounded, _ = _service(home_bounded, use_default_reader=True)
    run_store_bounded = WorkflowRunStore(home_bounded)
    _create_workflow(api_bounded, actor_bounded, 'wf-other-bounded')
    _create_workflow(api_bounded, actor_bounded, 'wf-target-bounded')
    for idx in range(45):
        run_store_bounded.create_run(_run_record('wf-other-bounded', f'wfrun-bnd-{idx:03d}', now=200.0 + idx))
    index_bounded = RunListIndex(home_bounded)
    with index_bounded.connection() as conn:
        conn.execute('DROP TABLE IF EXISTS open_ref_runs')
        conn.execute('DROP TABLE IF EXISTS open_ref_scan')
        conn.execute('PRAGMA user_version = 4')

    with monkeypatch.context() as mp:
        mp.setattr(reference_sources_module, '_max_open_ref_backfill_batches', 1)
        first_status = cleanup_status(api_bounded, api_bounded._get('wf-target-bounded'))
        assert any(b['reason_code'] == 'REFERENCE_COVERAGE_UNKNOWN' for b in first_status['blockers'])
        second_status = cleanup_status(api_bounded, api_bounded._get('wf-target-bounded'))
        assert second_status['blockers'] == []

    # 4. 回填过程遇到损坏 run.json 时必须保持 fail-closed（REFERENCE_COVERAGE_UNKNOWN），不得吞错放行。
    home_corrupt = tmp_path / 'upgrade_corrupt'
    api_corrupt, actor_corrupt, _ = _service(home_corrupt, use_default_reader=True)
    run_store_corrupt = WorkflowRunStore(home_corrupt)
    _create_workflow(api_corrupt, actor_corrupt, 'wf-other-corrupt')
    _create_workflow(api_corrupt, actor_corrupt, 'wf-target-corrupt')
    for idx in range(5):
        run_store_corrupt.create_run(_run_record('wf-other-corrupt', f'wfrun-cor-{idx:03d}', now=200.0 + idx))
    index_corrupt = RunListIndex(home_corrupt)
    with index_corrupt.connection() as conn:
        conn.execute('DROP TABLE IF EXISTS open_ref_runs')
        conn.execute('DROP TABLE IF EXISTS open_ref_scan')
        conn.execute('PRAGMA user_version = 4')
    (run_store_corrupt.run_dir('wfrun-cor-002') / 'run.json').write_text('{corrupt', encoding='utf-8')

    status_corrupt = cleanup_status(api_corrupt, api_corrupt._get('wf-target-corrupt'))
    assert any(b['reason_code'] == 'REFERENCE_COVERAGE_UNKNOWN' for b in status_corrupt['blockers'])
