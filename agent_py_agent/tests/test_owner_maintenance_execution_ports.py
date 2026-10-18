# LLM: 原owner维护实消费合同；只使用临时真实C/P3/claim及main保留服务，不替代实际权限或重建维护来源。
# 模块用途: 复现已冻结maintenance来源到现main叶子签名缺口，并约束W不压shared索引。
import json
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due
from agent_py_agent.tests.test_gateway_background_curator_transactions import (
    curator_case,
    run_original,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import actual as actual
from agent_py_agent.tests.test_owner_maintenance import _home_with_old_cache


def test_count0_original_leaf_can_skip_shared_indexes_without_changing_retention(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance

    home, stale = _home_with_old_cache(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('explicit private maintenance must not compact shared indexes')

    monkeypatch.setattr(owner_maintenance, '_compact_global_indexes', forbidden)
    result = run_owner_retention_if_due(home, now=200_000, include_global_indexes=False)
    assert result.ran is True and result.status == 'success'
    assert result.apply_outcome == 'applied' and not stale.exists()
    marker = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
    assert marker['schema_version'] == 'owner-maintenance.v1'
    assert marker['indexes_compacted'] == marker['indexes_compact_failed'] == []
    assert 'memory_archive_permissions' in marker and 'run_closeout_backfill' in marker
    assert 'text_vector_cache_reclaimed' in marker and 'text_vector_cache_reclaim_error' in marker


def test_actual_c_maintenance_source_reaches_original_retention_and_owner_state(tmp_path, monkeypatch):
    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        stale = agent.home_paths.owner_cache_dir / 'old.tmp'
        assert stale.read_bytes() == b'old-cache'
        response = run_original(agent, gate, claim)
        assert response['ok'] is True
        result = response['background_result']
        assert result['source_kind'] == 'maintenance' and result['outcome'] == 'handled'
        assert result['ran'] is True and result['status'] == 'success'
        assert result['apply_outcome'] == 'applied' and not stale.exists()
        marker = json.loads((agent.home_paths.owner_data_dir / 'maintenance.json').read_text())
        assert marker['schema_version'] == 'owner-maintenance.v1'
        assert marker['indexes_compacted'] == marker['indexes_compact_failed'] == []
        assert 'run_closeout_backfill' in marker and 'memory_archive_permissions' in marker


# LLM: 只保存本次真实维护state的存在/完整字节，不读取其它owner或把摘要作准入。
# 函数用途: 核对拒绝后原来源没有被改写或补造。
def _marker_bytes(home):
    path = home.owner_data_dir / 'maintenance.json'
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize('fault', ['cancel', 'generation', 'policy', 'state'])
def test_actual_c_scan_is_outside_t_and_late_change_prevents_original_write(tmp_path, monkeypatch, fault):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.io import (
        gateway_turn_transition,
        read_json_file,
        update_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.memory_store.retention import MemoryRetentionService

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, close):
        original = MemoryRetentionService.plan
        started, release = threading.Event(), threading.Event()

        def observe(service, **kwargs):
            plan = original(service, **kwargs)
            started.set()
            assert release.wait(5)
            return plan

        monkeypatch.setattr(MemoryRetentionService, 'plan', observe)
        stale = agent.home_paths.owner_cache_dir / 'old.tmp'
        p3 = reservation_file(gate.paths, claim.request_id)
        p3_before = p3.read_bytes()
        state_before = _marker_bytes(agent.home_paths)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_original, agent, gate, claim)
            try:
                assert started.wait(5), 'main original scanner must really run'
                with gateway_turn_transition(gate.paths, claim.request_id):
                    if fault == 'cancel':
                        update_json_file_atomic(claim.path, lambda row: {**row, 'cancel_requested': True, 'turn_phase': 'closing'})
                    elif fault == 'policy':
                        path = agent.home_paths.owner_retention_json
                        policy = json.loads(path.read_text())
                        path.write_text(json.dumps({**policy, 'cache_days': 2}))
                    elif fault == 'state':
                        (agent.home_paths.owner_data_dir / 'maintenance.json').write_bytes(b'{"last_attempt_at":123}')
                if fault == 'generation':
                    close()
                state_changed = _marker_bytes(agent.home_paths)
            finally:
                release.set()
            if fault in {'cancel', 'generation'}:
                with pytest.raises(RequestExecutionOutcomeUncertainError) as raised:
                    future.result(5)
                assert raised.value.request_id == claim.request_id
            else:
                assert future.result(5)['ok'] is False
        assert stale.read_bytes() == b'old-cache'
        assert _marker_bytes(agent.home_paths) == (state_changed if fault == 'state' else state_before)
        assert p3.read_bytes() == p3_before
        assert read_json_file(claim.path)['status'] == 'processing'
        assert not (gate.paths.terminal / claim.path.name).exists()


@pytest.mark.parametrize('mismatch', ['policy', 'state'])
def test_expected_source_fingerprints_reject_before_private_retention(tmp_path, mismatch):
    import hashlib

    from agent_py_agent.agent.memory_store.retention_transition import RetentionPolicyChangedError

    home, stale = _home_with_old_cache(tmp_path)
    expected_policy = hashlib.sha256(home.owner_retention_json.read_bytes()).hexdigest()
    expected_state = 'missing'
    if mismatch == 'policy':
        expected_policy = '0' * 64
    else:
        expected_state = '0' * 64
    entered = []

    def transition(operation):
        entered.append(True)
        return operation()

    with pytest.raises(RetentionPolicyChangedError if mismatch == 'policy' else ValueError):
        run_owner_retention_if_due(home, now=200_000, include_global_indexes=False,
            commit_transition=transition, expected_policy_fingerprint=expected_policy,
            expected_state_fingerprint=expected_state)
    assert entered == [] and stale.exists() and _marker_bytes(home) is None


def test_actual_worker_private_maintenance_cannot_compact_shared_indexes(actual, monkeypatch):
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
    from agent_py_agent.agent.gateway_parts.multiprocess_dispatch import WorkerDispatchError
    from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
    from agent_py_agent.agent.user_space import owner_maintenance
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, ensure_owner_home

    binding, request, agent = actual
    # 宿主夹具显式准备原owner事实，W执行中不负责首建，权限门仍用真实claim/P3/P5。
    ensure_owner_home(agent.home_paths.root, OwnerIdentity.provider_user('synthetic', 'alice'))

    def forbidden(*_args, **_kwargs):
        raise AssertionError('worker cannot compact shared indexes')

    monkeypatch.setattr(owner_maintenance, '_compact_global_indexes', forbidden)
    with pytest.raises(WorkerDispatchError):
        run_owner_retention_if_due(agent.home_paths)
    context = {'agent': agent, 'request': request, 'request_path': binding.claim.path,
               'request_id': binding.claim.request_id}
    with worker_execution_scope(binding, agent), bridge.background_request_scope(context):
        result = run_owner_retention_if_due(agent.home_paths, include_global_indexes=True,
            commit_transition=lambda operation: bridge.background_short_operation('owner_maintenance_commit', operation))
    assert result.ran is True
    marker = json.loads((agent.home_paths.owner_data_dir / 'maintenance.json').read_text())
    assert marker['indexes_compacted'] == marker['indexes_compact_failed'] == []


# LLM: 纯catch合同使用实际异常类；bounded从真实原Thread尾部取得并真实join，不编造physical call_id或claim。
# 函数用途: 为来源未知与普通同码对照提供单个原异常对象。
@contextmanager
def _failure(kind, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.tests.test_bounded_call_error_origin import held_call

    if kind == 'bounded':
        with held_call(monkeypatch) as (error, _):
            yield error
        return
    if kind == 'outcome':
        error = RequestExecutionOutcomeUncertainError({'id': 'original', 'execution_attempt_id': 'attempt',
            'lease_epoch': 1, 'lease_owner': 'attempt'}, 'maintenance')
    elif kind == 'context':
        error = RequestExecutionContextUnavailable('original source missing')
    elif kind == 'tool':
        error = ToolWriteContextUnavailable('original tool claim missing')
    else:
        error = RuntimeError('ordinary maintenance fault')
        error.error_code = 'ACTIVE_TURN_OUTCOME_UNCERTAIN'
    yield error


# LLM: 只替换实际业务被调用叶子的故障，真实维护wrapper/catch不替换；不是权限门替身。
# 函数用途: 对各辅助业务入口注入一个原异常对象以核错误分类。
def _aux_fault(home, stage, error, monkeypatch):
    from agent_py_agent.agent.user_space import owner_maintenance

    def refuse(*_args, **_kwargs):
        raise error

    if stage == 'cache':
        from agent_py_agent.agent.memory_store import jsonl
        home.owner_memory_long_term_jsonl.write_text('')
        monkeypatch.setattr(jsonl, 'JsonlMemory', refuse)
        return lambda: owner_maintenance._reclaim_text_vector_cache_orphans(home)
    if stage == 'archive':
        from agent_py_agent.agent.memory_archive import storage
        monkeypatch.setattr(storage, 'tighten_memory_archive_permissions', refuse)
        return lambda: owner_maintenance._tighten_archive_permissions(home)
    if stage == 'global':
        from agent_py_agent.agent.user_space import home_index_compact
        monkeypatch.setattr(home_index_compact, 'compact_global_indexes_if_due', refuse)
        return lambda: owner_maintenance._compact_global_indexes(home, now=200_000)
    from agent_py_agent.agent.runtime_db import run_closeout_backfill
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    RuntimeRepository(runtime_db_path(home.owner_home_dir))
    monkeypatch.setattr(run_closeout_backfill, 'apply_run_closeout_backfill', refuse)
    return lambda: owner_maintenance._backfill_hung_runs(home, now=200_000)


@pytest.mark.parametrize('stage', ['cache', 'archive', 'backfill', 'global'])
@pytest.mark.parametrize('kind', ['outcome', 'context', 'tool', 'bounded'])
def test_actual_unknown_is_not_downgraded_by_maintenance_auxiliary(tmp_path, monkeypatch, stage, kind):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    with _failure(kind, monkeypatch) as error:
        operation = _aux_fault(home, stage, error, monkeypatch)
        with pytest.raises(type(error)) as raised:
            operation()
        assert raised.value is error


@pytest.mark.parametrize('stage', ['cache', 'archive', 'backfill', 'global'])
def test_ordinary_same_code_preserves_auxiliary_diagnostics(tmp_path, monkeypatch, stage):
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

    home = ensure_my_agent_home(tmp_path)
    with _failure('ordinary', monkeypatch) as error:
        result = _aux_fault(home, stage, error, monkeypatch)()
    if stage == 'cache':
        assert result[0] == 0 and 'RuntimeError' in result[1]
    elif stage == 'archive':
        assert 'RuntimeError' in result['error']
    elif stage == 'global':
        assert result == ([], [])
    else:
        assert result == {'executed': False, 'reason': 'error', 'error_type': 'RuntimeError'}


@pytest.mark.parametrize('kind', ['outcome', 'context', 'tool', 'bounded'])
def test_original_jsonl_cache_callback_keeps_actual_error(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    path = tmp_path / 'memory.jsonl'
    path.write_text('')
    memory = JsonlMemory(path)
    with _failure(kind, monkeypatch) as error:
        def refuse(_operation):
            raise error

        with pytest.raises(type(error)) as raised:
            memory.reclaim_text_cache_orphans(commit_transition=refuse)
        assert raised.value is error and path.read_text() == ''


@pytest.mark.parametrize('change', ['policy', 'state'])
def test_source_change_after_retention_keeps_partial_effects_but_never_overwrites_new_state(tmp_path, monkeypatch, change):
    from agent_py_agent.agent.gateway_parts.io import gateway_turn_transition
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file
    from agent_py_agent.agent.user_space import owner_maintenance

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        original = owner_maintenance._reclaim_text_vector_cache_orphans
        stale = agent.home_paths.owner_cache_dir / 'old.tmp'
        p3 = reservation_file(gate.paths, claim.request_id)
        before = p3.read_bytes()
        observed = []

        def intervene(home, **kwargs):
            assert not stale.exists(), 'main original retention must have really applied'
            with gateway_turn_transition(gate.paths, claim.request_id):
                if change == 'state':
                    (home.owner_data_dir / 'maintenance.json').write_bytes(b'{"last_attempt_at":789}')
                else:
                    policy = json.loads(home.owner_retention_json.read_text())
                    home.owner_retention_json.write_text(json.dumps({**policy, 'cache_days': 2}))
            observed.append(_marker_bytes(home))
            return original(home, **kwargs)

        monkeypatch.setattr(owner_maintenance, '_reclaim_text_vector_cache_orphans', intervene)
        response = run_original(agent, gate, claim)
        assert response['ok'] is False and observed
        assert _marker_bytes(agent.home_paths) == observed[0]
        assert not stale.exists() and p3.read_bytes() == before
        assert not (gate.paths.terminal / claim.path.name).exists()


def test_actual_archive_scanning_is_outside_t_and_each_chmod_is_inside_original_gate(tmp_path, monkeypatch):
    import os
    import stat

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        require_background_transaction,
    )
    from agent_py_agent.agent.memory_archive import storage

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        archive = agent.home_paths.owner_home_dir / 'memory_archive'
        archive.mkdir(exist_ok=True)
        data = archive / 'old.txt'
        data.write_text('synthetic archive')
        os.chmod(archive, 0o755)
        os.chmod(data, 0o644)
        scan, chmod = storage.os.scandir, storage.os.chmod
        observations = []

        def scan_observed(path):
            if str(path).startswith(str(archive)):
                with pytest.raises(RequestExecutionOutcomeUncertainError):
                    require_background_transaction('observe_archive_scan')
                observations.append('scan')
            return scan(path)

        def chmod_observed(path, mode, **kwargs):
            if str(path) in {str(archive), str(data)}:
                assert require_background_transaction('observe_archive_chmod') is None
                observations.append('chmod')
            return chmod(path, mode, **kwargs)

        monkeypatch.setattr(storage.os, 'scandir', scan_observed)
        monkeypatch.setattr(storage.os, 'chmod', chmod_observed)
        response = run_original(agent, gate, claim)
        assert response['ok'] is True
        assert 'scan' in observations and observations.count('chmod') == 2
        assert stat.S_IMODE(data.stat().st_mode) == 0o600
        assert stat.S_IMODE(archive.stat().st_mode) == 0o700


def test_actual_rcb_keeps_original_classification_cas_and_short_write_gates(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        require_background_transaction,
    )
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.run_closeout_backfill import (
        RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS,
    )
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.tests.test_run_closeout_backfill import BLOCKED_FACT, _hung_run, _run_status

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        repo = RuntimeRepository(runtime_db_path(agent.home_paths.owner_home_dir))
        rec = _hung_run(repo, 'historical-main-run', BLOCKED_FACT,
                        ended_at=time.time()-RUN_CLOSEOUT_BACKFILL_GRACE_SECONDS-3600)
        with repo._runtime_connection() as conn:
            expected_status = conn.execute('SELECT status FROM agent_attempts WHERE attempt_id=?',
                                           (rec['attempt_id'],)).fetchone()['status']
        original = RuntimeRepository.settle_agent_run
        seen = []

        def observe(actual, **kwargs):
            if kwargs.get('agent_run_id') == rec['agent_run_id']:
                assert require_background_transaction('observe_rcb_settle') is None
                seen.append(kwargs)
            return original(actual, **kwargs)

        monkeypatch.setattr(RuntimeRepository, 'settle_agent_run', observe)
        response = run_original(agent, gate, claim)
        assert response['ok'] is True and len(seen) == 1
        assert seen[0]['expected_attempt_status'] == expected_status
        assert _run_status(repo, rec) == 'failed'
        marker = json.loads((agent.home_paths.owner_data_dir / 'maintenance.json').read_text())
        assert marker['run_closeout_backfill']['executed'] is True
        assert marker['run_closeout_backfill']['settled']['failed'] == 1


def test_actual_cache_long_read_is_outside_t_and_only_original_final_write_is_inside(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.multiprocess_background_execution import (
        require_background_transaction,
    )
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
    from agent_py_agent.agent.retrieval.text_vector_cache import (
        TextVectorCache,
        index_text,
        text_content_hash,
    )

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        home = agent.home_paths
        home.owner_memory_long_term_jsonl.write_text(MemoryRecord('user', 'keep this synthetic fact').to_json()+'\n')
        key = 'test:'+text_content_hash(index_text('keep this synthetic fact', None))
        removed = 'test:'+'0'*64
        cache = home.owner_memory_long_term_jsonl.parent / 'memory_text_vectors.json'
        cache.write_text(json.dumps({key: [1.0], removed: [2.0]}))
        read = JsonlMemory._maintenance_records
        write = TextVectorCache.retain_content_hashes
        stages = []

        def observe_read(actual):
            with pytest.raises(RequestExecutionOutcomeUncertainError):
                require_background_transaction('observe_cache_read')
            stages.append('read')
            return read(actual)

        def observe_write(actual, keys):
            assert require_background_transaction('observe_cache_write') is None
            stages.append('write')
            return write(actual, keys)

        monkeypatch.setattr(JsonlMemory, '_maintenance_records', observe_read)
        monkeypatch.setattr(TextVectorCache, 'retain_content_hashes', observe_write)
        response = run_original(agent, gate, claim)
        assert response['ok'] is True and stages == ['read', 'write']
        assert json.loads(cache.read_text()) == {key: [1.0]}
        marker = json.loads((home.owner_data_dir / 'maintenance.json').read_text())
        assert marker['text_vector_cache_reclaimed'] == 1


def test_retained_commit_callback_cannot_borrow_closed_original_scope(tmp_path, monkeypatch):
    from contextvars import copy_context

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge

    with curator_case(tmp_path, monkeypatch, source_kind='maintenance') as (agent, _, _, gate, claim, _):
        original = bridge.background_short_operation
        retained = []

        def observe(phase, operation, **kwargs):
            if phase == 'owner_maintenance_commit':
                retained.append((copy_context(), operation))
            return original(phase, operation, **kwargs)

        monkeypatch.setattr(bridge, 'background_short_operation', observe)
        assert run_original(agent, gate, claim)['ok'] is True and retained
        before = _marker_bytes(agent.home_paths)
        context, operation = retained[0]
        with pytest.raises(RequestExecutionOutcomeUncertainError):
            context.run(operation)
        assert _marker_bytes(agent.home_paths) == before
