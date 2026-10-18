"""共享压缩真实C callback与原索引；不启动W/native/模型，不伪造请求取得全局写权。"""
import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.common import global_write_guard
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInExecutorError
from agent_py_agent.agent.gateway_parts import multiprocess_mode as mode
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.multiprocess_background_discovery import (
    CoordinatorBackgroundRequests,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError
from agent_py_agent.agent.gateway_parts.multiprocess_maintenance import CoordinatorIndexCompact
from agent_py_agent.agent.gateway_parts.multiprocess_route import WorkerPartition
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space import home_index_compact as compact
from agent_py_agent.agent.user_space.home_indexes import (
    _latest_unique_refs,
    replace_home_index_snapshots,
)
from agent_py_agent.agent.user_space.home_layout import home_paths


# LLM: 只准备原投影文件，不给C造claim/owner权限；shared writer使用真实producer固定的原提交来源。
# 函数用途: 在进入C前写两条同key记录，使实际流式压缩确实需要替换文件。
def write_index(home):
    path = home.global_index_active_tasks_jsonl
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps({'owner_id': 'original-owner', 'task_id': 'same-task', 'status': s})+'\n'
                            for s in ('RUNNING', 'DONE')))
    return path


# LLM: 只读本次临时目录名与字节，发现原guard前发生的锁、tmp、sidecar写；不输出正文。
# 函数用途: 保存共享索引目录的完整物理快照。
def physical(root):
    return {str(p.relative_to(root)): None if p.is_dir() else p.read_bytes() for p in root.rglob('*')}


# LLM: 原C guard/controller/control/runtime全真实；无Agent/请求/假ready。下一次owner发现尚未到时，但真实tick仍会运行共享压缩。
# 函数用途: 为实际生产callback测试准备临时C及其原关闭入口。
@contextmanager
def actual(tmp_path, monkeypatch):
    root = (tmp_path/'home').resolve()
    monkeypatch.setenv('MY_AGENT_HOME', str(root))
    assert root != (Path.home()/'.my-agent').resolve()
    monkeypatch.setattr(compact, 'COMPACT_MIN_BYTES', 1)
    config = AgentConfig(my_agent_home=str(root), model_backend='echo', owner_maintenance_scan_interval_seconds=1)
    home = home_paths(root)
    path = write_index(home)
    paths = gateway_paths_from_root(tmp_path/'gateway')
    guard = coordinator_guard(paths.root, 2)
    guard.__enter__()
    closed = []
    def close():
        if not closed:
            closed.append(True)
            guard.__exit__(None, None, None)
    try:
        controller = prepare_coordinator_takeover(paths, 2, 'index-C')
        control = GatewayControlPlane(config, root, paths, 2, controller.c_run_id, agent_root=tmp_path/'workspace')
        runtime = GatewayDrainRuntime(control)
        source = CoordinatorBackgroundRequests(control, drain_runtime=runtime)
        source._next_poll_at = time.monotonic()+100
        yield home, path, source, close
    finally:
        close()


def test_real_c_tick_passes_actual_commit_and_preserves_index_read_results(tmp_path, monkeypatch):
    with actual(tmp_path, monkeypatch) as (home, path, source, _close):
        before = _latest_unique_refs(path, key_fields=('owner_id', 'task_id'), limit=0)
        assert source.tick() == 0
        assert _latest_unique_refs(path, key_fields=('owner_id', 'task_id'), limit=0) == before
        assert path.read_text().count('\n') == 1
        state = json.loads(compact._sidecar_path(path).read_text())
        assert state['entries'][str(path)]['bytes'] == path.stat().st_size
        assert state['entries'][str(path)]['at'] > 0


@pytest.mark.parametrize('actor', ['worker', 'host'])
@pytest.mark.parametrize('entry', ['file', 'due', 'global', 'sidecar', 'replace'])
def test_shared_write_entry_refuses_before_locks_staging_or_sidecar(tmp_path, monkeypatch, actor, entry):
    home = home_paths(tmp_path/'home')
    path = write_index(home)
    compact._LAST_COMPACT_BYTES[str(path)] = path.stat().st_size
    compact._LAST_COMPACT_AT[str(path)] = 1
    before = physical(home.root)
    operations = {
        'file': lambda: compact.compact_index_file(path),
        'due': lambda: compact.compact_index_if_due(path),
        'global': lambda: compact.compact_global_indexes_if_due(home),
        'sidecar': lambda: compact.save_compact_state(path),
        'replace': lambda: replace_home_index_snapshots(home, owners=(), task_refs=(), run_refs=(), agent_refs=()),
    }
    with monkeypatch.context() as changed:
        changed.setattr(mode, '_WORKER_PARTITION', WorkerPartition(0, 2, 'negative-role') if actor == 'worker' else None)
        changed.setattr(global_write_guard, '_restricted_executor', actor == 'host')
        with pytest.raises((GlobalWriteError, global_write_guard.GlobalWriteInWorkerError)):
            operations[entry]()
    assert physical(home.root) == before


@pytest.mark.parametrize('fault', ['producer_stop', 'lease_close'])
def test_long_scan_outside_gate_then_closed_source_cannot_publish_index_or_sidecar(tmp_path, monkeypatch, fault):
    with actual(tmp_path, monkeypatch) as (home, path, source, close):
        original = compact._write_kept_lines
        before = path.read_bytes()
        def after_staging(*args):
            assert not source.runtime._gate._is_owned()
            assert not source._controller._mutex._is_owned()
            kept = original(*args)
            source.runtime.stop_producers() if fault == 'producer_stop' else close()
            return kept
        monkeypatch.setattr(compact, '_write_kept_lines', after_staging)
        with pytest.raises(GlobalWriteError):
            CoordinatorIndexCompact(source.control, commit_transition=source._commit_main).run(home)
        assert path.read_bytes() == before
        assert not compact._sidecar_path(path).exists()
        assert not list(path.parent.glob('*.compact'))
        assert str(path) not in compact._LAST_COMPACT_AT


def test_actual_c_tail_append_and_sidecar_share_original_gate_and_late_callback_expires(tmp_path, monkeypatch):
    with actual(tmp_path, monkeypatch) as (home, path, source, _close):
        scan, save = compact._scan_last_line_per_key, compact.save_compact_state
        tail = b'{"owner_id":"later","task_id":"later","status":"DONE"}\n'
        callbacks = []
        def append_after_scan(*args):
            assert not source.runtime._gate._is_owned()
            result = scan(*args)
            with path.open('ab') as stream:
                stream.write(tail)
            return result
        def same_gate_save(target):
            assert source.runtime._gate._is_owned() and source._controller._mutex._is_owned()
            assert source.runtime._lease_origin._lifecycle_gate._is_owned()
            assert path.read_bytes().endswith(tail)
            return save(target)
        def transition(operation):
            callbacks.append(operation)
            return source._commit_main(operation)
        monkeypatch.setattr(compact, '_scan_last_line_per_key', append_after_scan)
        monkeypatch.setattr(compact, 'save_compact_state', same_gate_save)
        rows = CoordinatorIndexCompact(source.control, commit_transition=transition).run(home, now=1000)
        assert any(row.compacted for row in rows)
        before = physical(home.root)
        with pytest.raises(ValueError, match='commit_window'):
            callbacks[0]()
        assert physical(home.root) == before
        assert compact._LAST_COMPACT_AT[str(path)] == 1000


@pytest.mark.parametrize('kind', ['global', 'context', 'tool', 'bounded', 'unknown', 'host'])
def test_real_typed_contract_errors_propagate_same_object_instead_of_io_error(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable

    home = home_paths(tmp_path/'home')
    path = write_index(home)
    monkeypatch.setattr(compact, 'COMPACT_MIN_BYTES', 1)
    # 这里只注入异常以验证catch透明；该合成诊断不安装scope、claim或任何写权限。
    errors = {
        'global': GlobalWriteError('coordinator_required', 'global_index_compact'),
        'context': RequestExecutionContextUnavailable('missing'),
        'tool': ToolWriteContextUnavailable('closed'),
        'bounded': BoundedCallExitUnconfirmedError('observed-call', 'worker_exit_unconfirmed'),
        'unknown': RequestExecutionOutcomeUncertainError({'id': 'diagnostic', 'execution_attempt_id': 'diagnostic',
                    'lease_epoch': 1, 'lease_owner': 'diagnostic'}, 'index.commit'),
        'host': GlobalWriteInExecutorError('global_index_compact'),
    }
    error = errors[kind]
    def reject(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(compact, 'compact_index_file', reject)
    before = physical(home.root)
    with pytest.raises(type(error)) as caught:
        compact.compact_global_indexes_if_due(home)
    assert caught.value is error and physical(home.root) == before


def test_original_rebuild_preview_remains_readonly_for_restricted_executor(tmp_path, monkeypatch):
    from agent_py_agent.agent.user_space.home_index_rebuild import rebuild_home_indexes

    home = home_paths(tmp_path/'home')
    write_index(home)
    before = physical(home.root)
    monkeypatch.setattr(global_write_guard, '_restricted_executor', True)
    assert rebuild_home_indexes(home, apply=False).applied is False
    assert physical(home.root) == before
    with pytest.raises(GlobalWriteInExecutorError):
        rebuild_home_indexes(home, apply=True)
    assert physical(home.root) == before


def test_ordinary_same_code_error_keeps_original_per_file_io_isolation(tmp_path, monkeypatch):
    home = home_paths(tmp_path/'home')
    path = write_index(home)
    monkeypatch.setattr(compact, 'COMPACT_MIN_BYTES', 1)
    error = RuntimeError('ordinary failure')
    error.error_code = 'ACTIVE_TURN_OUTCOME_UNCERTAIN'
    def ordinary(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(compact, 'compact_index_file', ordinary)
    before = physical(home.root)
    results = compact.compact_global_indexes_if_due(home)
    row = next(item for item in results if item.path == path)
    assert not row.compacted and row.reason == 'io_error'
    assert physical(home.root) == before
