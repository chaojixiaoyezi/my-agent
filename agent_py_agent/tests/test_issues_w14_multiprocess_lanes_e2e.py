"""W14：真实协调维护入口、逐写 transition、空 owner 成本与失效来源拒绝。"""
from __future__ import annotations

import gc
import hashlib
import io
import json
import os
import sqlite3
import time
import traceback
from functools import wraps

import pytest

from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.request_execution_ports import RequestExecutionContextUnavailable
from agent_py_agent.agent.contracts.optimization_contracts import StoreSchemaNewerError
from agent_py_agent.agent.memory_store import candidates as memory_candidates
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.memory_store.retention_scan import file_fingerprint
from agent_py_agent.agent.memory_store.retention_transition import RetentionPolicyChangedError
from agent_py_agent.agent.user_space import owner_maintenance, storage_lanes
from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
from agent_py_agent.agent.user_space.owner_maintenance_transition import run_with_transition
from agent_py_agent.agent.user_space.owner_quota import owner_logical_usage_bytes
from agent_py_agent.agent.user_space.storage_lanes import LANE_BINDINGS, StorageLaneRequest
from agent_py_agent.tests.test_issues_w14_lane_wiring_e2e import (
    _assert_live,
    _assert_retired,
    _fd_count,
    _observe_state,
)
from agent_py_agent.tests.test_issues_w14_lane_wiring_e2e import lane_world as lane_world


class _RecordingTransition:
    """复制原协调测试的同步短门；仅记录调用并执行 operation，不伪造维护结果。"""

    def __init__(self, home, *, revoke_after=None):
        self.home = home
        self.active = False
        self.commits = []
        self.revoke_after = revoke_after
        self.revoked = RequestExecutionContextUnavailable('fixture transition revoked')

    def __call__(self, operation):
        if self.revoke_after is not None and len(self.commits) >= self.revoke_after:
            raise self.revoked
        assert not self.active
        path = self.home.owner_data_dir / 'maintenance.json'
        before = path.read_bytes() if path.exists() else None
        self.active = True
        try:
            result = operation()
        finally:
            self.active = False
        after = path.read_bytes() if path.exists() else None
        self.commits.append('state' if before != after else 'domain')
        return result


def _watch_state_commits(monkeypatch, home):
    """观察原 canonical 状态写者，重复同字节写也必须分别计数。"""
    writes = []
    original = owner_maintenance.write_json_file_atomic_unlocked

    @wraps(original)
    def write(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == home.owner_data_dir / 'maintenance.json':
            writes.append(path)
        return result

    monkeypatch.setattr(owner_maintenance, 'write_json_file_atomic_unlocked', write)
    return writes


def _watch_mutations(monkeypatch, transition):
    """透明观察真实文件变更、SQLite 写语句及车道写门；不替换任何领域算法。"""
    unfenced, lane_writes = [], set()

    def install(name, original, observer):
        monkeypatch.setattr(os, name, observer)
        # nofollow 按 callable 身份检查能力；观察器仍透传原 dir_fd/follow_symlinks 参数和系统调用。
        for attribute in ('supports_dir_fd', 'supports_fd', 'supports_follow_symlinks'):
            supported = getattr(os, attribute)
            if original in supported:
                monkeypatch.setattr(os, attribute, supported | {observer})

    def watch(name):
        original = getattr(os, name)

        @wraps(original)
        def called(*args, **kwargs):
            result = original(*args, **kwargs)
            if not transition.active:
                unfenced.append((name, str(args[0])))
            return result

        install(name, original, called)

    for name in ('unlink', 'remove', 'rename', 'replace', 'rmdir', 'mkdir', 'chmod', 'truncate'):
        watch(name)

    original_open = io.open

    class ObservedFile:
        """r+b 打开既有锁不等于写入；只在真实 write/truncate 时记录副作用。"""

        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __iter__(self):
            return iter(self.handle)

        def write(self, *args):
            if not transition.active:
                unfenced.append(('write', str(self.handle.name)))
            return self.handle.write(*args)

        def truncate(self, *args):
            if not transition.active:
                unfenced.append(('truncate', str(self.handle.name)))
            return self.handle.truncate(*args)

        def writelines(self, *args):
            if not transition.active:
                unfenced.append(('writelines', str(self.handle.name)))
            return self.handle.writelines(*args)

    @wraps(original_open)
    def opened(file, mode='r', *args, **kwargs):
        handle = original_open(file, mode, *args, **kwargs)
        if any(flag in mode for flag in 'wx') and not transition.active:
            unfenced.append(('io.open:' + mode, str(file)))
        return ObservedFile(handle) if any(flag in mode for flag in 'wax+') else handle

    monkeypatch.setattr(io, 'open', opened)
    original_os_open = os.open

    @wraps(original_os_open)
    def descriptor(path, flags, *args, **kwargs):
        try:
            os.stat(path, dir_fd=kwargs.get('dir_fd'), follow_symlinks=False)
            exists = True
        except FileNotFoundError:
            exists = False
        creates = flags & os.O_CREAT and not exists
        fd = original_os_open(path, flags, *args, **kwargs)
        if (creates or flags & os.O_TRUNC) and not transition.active:
            callers = ' -> '.join(f'{frame.filename}:{frame.lineno}#{frame.name}'
                for frame in traceback.extract_stack(limit=32)[:-1])
            unfenced.append(('os.open:write', str(path), callers))
        return fd

    install('open', original_os_open, descriptor)
    original_connect = sqlite3.connect

    @wraps(original_connect)
    def connected(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        pending_write = False

        def trace(statement):
            nonlocal pending_write
            kind = statement.lstrip().split(None, 1)[0].upper()
            if kind in {'INSERT', 'UPDATE', 'DELETE', 'CREATE', 'DROP', 'ALTER', 'REPLACE', 'VACUUM'}:
                pending_write = True
                if not transition.active:
                    unfenced.append('sqlite:' + kind)
            elif kind in {'COMMIT', 'END', 'ROLLBACK'}:
                if kind != 'ROLLBACK' and pending_write and not transition.active:
                    unfenced.append('sqlite:COMMIT')
                pending_write = False

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(sqlite3, 'connect', connected)
    original_write = StorageLaneRequest.run_write

    def lane_write(request, operation):
        assert request.coordinator and request.commit is not None

        def entered():
            assert transition.active
            lane_writes.add(request.kind)
            return operation()

        return original_write(request, entered)

    monkeypatch.setattr(StorageLaneRequest, 'run_write', lane_write)
    return unfenced, lane_writes


def test_transition_wires_all_lanes_and_fences_real_mutations(lane_world, monkeypatch):
    world = lane_world
    transition = _RecordingTransition(world.home)
    before = owner_logical_usage_bytes(world.home.owner_home_dir, exact=True)
    seen, revisited = set(), set()
    repeated_all = False
    gc.collect()
    before_fds = _fd_count()
    with monkeypatch.context() as patch:
        unfenced, lane_writes = _watch_mutations(patch, transition)
        state_writes = _watch_state_commits(patch, world.home)
        now = world.now
        for tick in range(24):
            result = run_with_transition(world.home, now, transition)
            assert result.ran and result.apply_outcome == 'applied'
            marker, kinds = _observe_state(world.home, coordinator=True)
            if seen == set(LANE_BINDINGS):
                revisited.update(kinds)
                repeated_all |= kinds == set(LANE_BINDINGS)
            seen.update(kinds)
            if tick == 0:
                assert world.orphan.exists(), '七天宽限期前不能清扫旧索引节点'
            if not marker['maintenance_partial'] and not world.orphan.exists() and repeated_all:
                break
            now += 7 * 86400 if tick == 0 else (1 if marker['maintenance_partial'] else
                MemoryRetentionPolicy().maintenance_interval_seconds)
        else:
            pytest.fail('协调维护未在有界拍数内消费宽限证明并重新走完十种车道')
    gc.collect()
    assert _fd_count() - before_fds == 0
    assert seen == revisited == set(LANE_BINDINGS)
    assert repeated_all
    # LocalStore 经 global_write_commit_scope 使用相同原写门，其余车道经 run_write。
    assert lane_writes == set(LANE_BINDINGS) - {'local_events'}
    assert transition.commits.count('state') == tick + 1
    assert len(state_writes) == tick + 1
    _assert_retired(world)
    assert owner_logical_usage_bytes(world.home.owner_home_dir, exact=True) < before
    assert unfenced == []


def test_empty_owner_has_one_state_commit_and_no_lane_commits(tmp_path, monkeypatch):
    home = ensure_my_agent_home(tmp_path / 'empty')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    transition = _RecordingTransition(home)
    writes = []
    original_write = StorageLaneRequest.run_write

    def write(request, operation):
        writes.append(request.kind)
        return original_write(request, operation)

    monkeypatch.setattr(StorageLaneRequest, 'run_write', write)
    state_writes = _watch_state_commits(monkeypatch, home)
    result = run_with_transition(home, time.time(), transition)
    assert result.ran and result.apply_outcome == 'applied'
    assert transition.commits.count('state') == 1
    assert len(state_writes) == 1
    assert writes == []
    marker, kinds = _observe_state(home, coordinator=True)
    assert kinds == set(LANE_BINDINGS) and marker['maintenance_epoch'] == 1
    # 与原缺失通道的保留回退路径对照：辅助锁出生及配额对账成本不误算成新增车道写入。
    baseline = ensure_my_agent_home(tmp_path / 'without-lanes')
    baseline.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    baseline_transition = _RecordingTransition(baseline)
    with monkeypatch.context() as patch:
        patch.setattr(storage_lanes, 'LANE_BINDINGS', dict.fromkeys(LANE_BINDINGS, 'nonexistent_issue_lane_module:x'))
        run_with_transition(baseline, time.time(), baseline_transition)
    assert transition.commits == baseline_transition.commits
    assert writes == []


@pytest.mark.parametrize('source,error,reason', [
    ('state', ValueError, 'original_maintenance_source_changed'),
    ('policy', RetentionPolicyChangedError, 'original_retention_source_changed_before_plan'),
    ('generation', StoreSchemaNewerError, 'STORE_SCHEMA_NEWER'),
    ('epoch', ValueError, 'MAINTENANCE_EPOCH_INVALID'),
])
def test_stale_maintenance_source_is_rejected_before_transition(tmp_path, source, error, reason):
    home = ensure_my_agent_home(tmp_path / source)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    path = home.owner_data_dir / 'maintenance.json'
    marker = {'schema_generation': 1, 'maintenance_epoch': 1}
    if source == 'generation':
        marker['schema_generation'] = 2
    if source == 'epoch':
        marker['maintenance_epoch'] = -1
    path.write_text(json.dumps(marker))
    before = path.read_bytes()
    transition = _RecordingTransition(home)
    options = {'expected_state_fingerprint': hashlib.sha256(before).hexdigest(),
               'expected_policy_fingerprint': file_fingerprint(home.owner_retention_json)}
    if source == 'state':
        options['expected_state_fingerprint'] = 'stale'
    if source == 'policy':
        options['expected_policy_fingerprint'] = 'stale'
    with pytest.raises(error, match='^' + reason + '$'):
        run_with_transition(home, time.time(), transition, **options)
    assert path.read_bytes() == before and transition.commits == []


def test_transition_revocation_propagates_without_late_mutations(lane_world):
    world = lane_world
    # 允许原 runtime 安全清理一笔，随后撤销同一个短门，不能回退到无门执行领域删除。
    transition = _RecordingTransition(world.home, revoke_after=1)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        run_with_transition(world.home, world.now, transition)
    assert caught.value is transition.revoked
    assert transition.commits == ['domain']
    assert not (world.home.owner_data_dir / 'maintenance.json').exists()
    assert world.orphan.exists() and world.output.exists() and world.evolution.exists()
    assert world.log.exists() and world.retired_tree.exists()
    assert world.live.store.session_tasks.require(world.task.task_id).goal == world.task.goal
    _assert_live(world.live)
    _assert_live(world.second)


# LLM: 冷真实入口观察准确候选FD、flock和解析，不预创建锁或替代原领域算法。
# 函数用途: 验证候选锁出生受门而只读等待与解析在门外。
def test_candidate_lock_birth_is_fenced_but_reads_and_flock_stay_outside(tmp_path, monkeypatch):
    """冷 owner 不预创建候选锁；只在准确 FD 出生时持短门，扫描与等待仍在门外。"""
    home = ensure_my_agent_home(tmp_path / 'candidate-cold')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    lock = home.owner_memory_candidates_jsonl.with_suffix('.jsonl.lock')
    assert not lock.exists()
    transition = _RecordingTransition(home)
    births, flocks, reads = [], [], []
    original_open = json_io._open_private_lock_descriptor
    original_flock = json_io._flock_descriptor
    original_read = memory_candidates._borrow_candidate_readonly_rows

    # LLM: 保留原open返回和异常，只记录当前真实短门是否活跃。
    # 函数用途: 观察准确候选锁FD的出生窗口。
    def opened(path):
        if path == lock:
            births.append((path, transition.active))
        return original_open(path)

    # LLM: 以设备及inode匹配真实候选FD，原flock及其阻塞行为保持。
    # 函数用途: 记录候选读锁等待是否占用短门。
    def flocked(descriptor, *, blocking=True):
        if lock.exists():
            actual, target = os.fstat(descriptor), lock.stat()
            if (actual.st_dev, actual.st_ino) == (target.st_dev, target.st_ino):
                flocks.append(transition.active)
        return original_flock(descriptor, blocking=blocking)

    # LLM: 透传原只读借用入口，不伪造缓存或返回行。
    # 函数用途: 观察真实候选解析时的门状态。
    def scanned(service):
        if service.path == home.owner_memory_candidates_jsonl:
            reads.append(transition.active)
        return original_read(service)

    monkeypatch.setattr(json_io, '_open_private_lock_descriptor', opened)
    monkeypatch.setattr(json_io, '_flock_descriptor', flocked)
    monkeypatch.setattr(memory_candidates, '_borrow_candidate_readonly_rows', scanned)
    result = run_with_transition(home, time.time(), transition)
    assert result.ran and result.apply_outcome == 'applied'
    assert births == [(lock, True)], '候选锁 FD 出生必须由本次 transition 授权'
    assert flocks == reads == [False], '候选解析和 flock 等待不能搬入短写门'
    assert lock.is_file() and lock.stat().st_size == 0
    assert transition.commits.count('state') == 1


# LLM: 在原候选读入口准确撤回同一来源，原异常身份及文件出生事实都须核对。
# 函数用途: 防止候选锁在撤权后先创建、后由其他提交报错。
def test_revocation_at_candidate_scan_prevents_lock_birth(tmp_path, monkeypatch):
    """在真实候选规划入口撤权，缺失锁不得先创建再等后续状态提交拒绝。"""
    home = ensure_my_agent_home(tmp_path / 'candidate-revoked')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    lock = home.owner_memory_candidates_jsonl.with_suffix('.jsonl.lock')
    assert not lock.exists()
    transition = _RecordingTransition(home)
    original = memory_candidates.CandidateService.readonly_rows

    # LLM: 仅改变真实录制门的撤权时刻，原候选读链仍完整执行。
    # 函数用途: 在锁出生前撤销本次transition。
    def revoked(service, *args, **kwargs):
        transition.revoke_after = len(transition.commits)
        return original(service, *args, **kwargs)

    monkeypatch.setattr(memory_candidates.CandidateService, 'readonly_rows', revoked)
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        run_with_transition(home, time.time(), transition)
    assert caught.value is transition.revoked
    assert not lock.exists()
    assert not (home.owner_data_dir / 'maintenance.json').exists()


# LLM: 使用真实候选writer与删除CAS；不重入门、准确删除回执和实际清空均须满足。
# 函数用途: 验证新增协调读锁接线未破坏到期候选的嵌套删除。
def test_due_candidate_retirement_reuses_transition_without_reentry(tmp_path, monkeypatch):
    """真实候选删除仍需写门；候选锁/raw写嵌套在已有短窗口中，不能二次取得原门。"""
    home = ensure_my_agent_home(tmp_path / 'candidate-due')
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    candidates = memory_candidates.CandidateService(home.owner_memory_candidates_jsonl)
    candidate = candidates.observe(memory_candidates.CandidateObservation(candidate_type='lesson',
        content='到期候选正文', subject_key='w14.candidate-retire', origin='model_inferred',
        scope=memory_candidates.MemoryScope('personal', 'personal'), promotion_target='lesson'))
    candidates.transition(candidate.candidate_id, 'rejected', reviewer='fixture')
    assert len(candidates.list()) == 1
    transition = _RecordingTransition(home)
    with monkeypatch.context() as patch:
        unfenced, _ = _watch_mutations(patch, transition)
        result = run_with_transition(home, time.time() + 40 * 86400, transition)
    assert result.ran and result.apply_outcome == 'applied'
    assert candidates.list() == []
    assert result.retention.ok
    assert [(action.record_id, action.status) for action in result.retention.actions
            if action.operation == 'candidate_delete'] == [(candidate.candidate_id, 'deleted')]
    assert transition.commits.count('state') == 1
    assert unfenced == []
