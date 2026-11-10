"""限定Darwin树关系直接核内核身份；信号出口仅注入替身，不操作用户进程。"""
from __future__ import annotations

import errno
import threading
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_native_host_observation as native
from agent_py_agent.agent.tooling import process_registry as registry

ROOT, CHILD, ORPHAN = 41001, 41002, 41003


def _info(pid, parent, group, microsecond=1):
    return SimpleNamespace(prefix=[0, 2, 0, pid, parent], scope=[0, group], start_sec=1794249600, start_usec=microsecond)


@pytest.fixture
def tree(monkeypatch):
    state = SimpleNamespace(rows={ROOT: _info(ROOT, 1, ROOT), CHILD: _info(CHILD, ROOT, ROOT),
                                  ORPHAN: _info(ORPHAN, 1, ROOT)}, queries=[], ps=[])
    monkeypatch.setattr(native, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(registry, '_native_posix_probe_enabled', lambda: True)
    monkeypatch.setattr(registry, '_IS_WINDOWS', False)
    monkeypatch.setattr(registry.os, 'getpgrp', lambda: 90000, raising=False)
    monkeypatch.setattr(registry, '_darwin_zombie_root_info', lambda pid: None)
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (state.rows.get(pid), 0 if pid in state.rows else errno.ESRCH))

    def members(kind, identifier):
        state.queries.append((kind, identifier))
        index = 'group' if kind == 2 else 'parent'
        return [pid for pid, row in state.rows.items()
                if (row.scope[1] if index == 'group' else row.prefix[4]) == identifier], True

    def capture(pid):
        row = state.rows.get(pid)
        return f'host:synthetic#kb={row.start_sec}:{row.start_usec:06d}' if row else ''

    def ps(relation):
        state.ps.append(relation)
        return {pid: row.prefix[4] if relation == 'ppid' else row.scope[1] for pid, row in state.rows.items()}, True

    monkeypatch.setattr(native, '_darwin_members', members)
    monkeypatch.setattr(registry, 'capture_process_birth_token', capture)
    monkeypatch.setattr(registry, '_ps_relation_map', ps)
    monkeypatch.setattr(registry, '_same_process', lambda pid, token: capture(pid) == token)
    return state


def test_fresh_tree_uses_only_selected_parent_and_exclusive_group_kernel_queries(tree):
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert complete and set(snapshot) == {ROOT, CHILD, ORPHAN}
    assert tree.ps == []
    assert (2, ROOT) in tree.queries
    assert all(kind in (2, 6) and identifier in snapshot for kind, identifier in tree.queries)


@pytest.mark.parametrize('error', [errno.EPERM, errno.EIO, errno.ESRCH])
def test_unknown_root_never_gains_signal_authority_or_exit_confirmation(tree, monkeypatch, error):
    signals = []
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (None, error))
    monkeypatch.setattr(registry, '_process_is_zombie', lambda pid: False)
    monkeypatch.setattr(registry.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(registry.os, 'killpg', lambda pid, sig: signals.append((pid, sig)), raising=False)
    receipt = registry.terminate_process_tree(ROOT, None, expected_birth_token='original')
    assert not receipt.confirmed and receipt.method == 'identity_unavailable'
    assert signals == [] and tree.ps == []


@pytest.mark.parametrize('phase', ['group', 'children', 'pid_field'])
def test_group_leader_pid_reuse_revokes_entire_snapshot(tree, monkeypatch, phase):
    original = native._darwin_members

    def changed(kind, identifier):
        result = original(kind, identifier)
        if (phase == 'group' and kind == 2) or (phase == 'children' and kind == 6 and identifier == ROOT):
            tree.rows[ROOT] = _info(ROOT, 1, ROOT, microsecond=2)
        if phase == 'pid_field' and kind == 2:
            tree.rows[ROOT].prefix[3] = ROOT + 99
        return result

    monkeypatch.setattr(native, '_darwin_members', changed)
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert snapshot == {} and not complete


def test_listed_member_reused_into_unrelated_group_is_never_adopted(tree, monkeypatch):
    original = native._darwin_members

    def changed(kind, identifier):
        result = original(kind, identifier)
        if kind == 2:
            tree.rows[ORPHAN] = _info(ORPHAN, 1, 80000, microsecond=2)
        return result

    monkeypatch.setattr(native, '_darwin_members', changed)
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert ORPHAN not in snapshot and ROOT in snapshot and not complete


def test_child_reused_between_relationship_and_birth_is_not_adopted(tree, monkeypatch):
    original = registry.capture_process_birth_token

    def changed(pid):
        if pid == CHILD:
            tree.rows[pid] = _info(pid, 1, 80000, microsecond=2)
        return original(pid)

    monkeypatch.setattr(registry, 'capture_process_birth_token', changed)
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert CHILD not in snapshot and not complete


def test_host_group_does_not_expand_into_unrelated_group_members(tree, monkeypatch):
    for row in tree.rows.values():
        row.scope[1] = 90000
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert complete and set(snapshot) == {ROOT, CHILD}
    assert not any(kind == 2 for kind, identifier in tree.queries)


@pytest.mark.parametrize('missing', [ROOT, CHILD])
def test_permission_and_partial_enumeration_never_sign_complete(tree, monkeypatch, missing):
    original = native._darwin_members

    def partial(kind, identifier):
        members, complete = original(kind, identifier)
        return members, False if kind == 6 and identifier == missing else complete

    monkeypatch.setattr(native, '_darwin_members', partial)
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert ROOT in snapshot and not complete


def test_total_tree_budget_is_shared_across_parent_and_group_pages(tree, monkeypatch):
    # 单页均未饱和，但递归总节点超过原8192容量；不能把各页完整冒充全树完整。
    tree.rows = {ROOT: _info(ROOT, 1, ROOT)}
    tree.rows.update({pid: _info(pid, ROOT, 80000) for pid in range(50000, 58200)})
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert not complete and len(snapshot) == 8192
    assert len(tree.queries) <= 8193 and tree.ps == []


@pytest.mark.parametrize('size,error', [(-1, 0), (32768, 0), (3, 0), (0, errno.EPERM)])
def test_native_member_buffer_error_and_saturation_remain_unknown(monkeypatch, size, error):
    import ctypes

    def query(kind, identifier, buffer, capacity):
        assert kind in (2, 6) and identifier == ROOT and capacity == 32768
        ctypes.set_errno(error)
        return size

    monkeypatch.setattr(native.ctypes, 'CDLL', lambda *args, **kwargs: SimpleNamespace(proc_listpids=query))
    assert native._darwin_members(6, ROOT) == ([], False)


@pytest.mark.parametrize('changed', ['gone', 'live', 'reused', 'permission'])
def test_zombie_root_late_change_cannot_confirm_or_authorize_tree(tree, monkeypatch, changed):
    token = 'host:original zombie'
    monkeypatch.setattr(registry, '_same_process', lambda pid, value: registry.capture_process_birth_token(pid) == value)
    calls = []
    original = native._darwin_info
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (None, errno.ESRCH) if pid == ROOT else original(pid))
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: token if pid == ROOT else
                        f'host:synthetic#kb={tree.rows[pid].start_sec}:{tree.rows[pid].start_usec:06d}')

    def zombie(pid):
        calls.append(pid)
        return (ROOT, token) if len(calls) == 1 else ((ROOT, 'host:new zombie') if changed == 'reused' else None)

    monkeypatch.setattr(registry, '_darwin_zombie_root_info', zombie)
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert not complete and snapshot == {}


def test_zombie_root_uses_original_identity_and_keeps_reparented_group_member(tree, monkeypatch):
    token = 'host:original zombie'
    monkeypatch.setattr(registry, '_same_process', lambda pid, value: registry.capture_process_birth_token(pid) == value)
    tree.rows.pop(CHILD)
    original = native._darwin_info
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (None, errno.ESRCH) if pid == ROOT else original(pid))
    monkeypatch.setattr(registry, '_darwin_zombie_root_info', lambda pid: (ROOT, token))
    capture = registry.capture_process_birth_token
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: token if pid == ROOT else capture(pid))
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert complete and set(snapshot) == {ROOT, ORPHAN} and snapshot[ROOT] == token
    assert tree.ps == []


def test_root_exiting_during_scan_keeps_only_original_zombie_identity(tree, monkeypatch):
    exited = [False]
    original_members, original_info = native._darwin_members, native._darwin_info
    token = registry.capture_process_birth_token(ROOT)

    def members(kind, identifier):
        result = original_members(kind, identifier)
        if kind == 2:
            exited[0] = True
            tree.rows[CHILD].prefix[4] = 1
        return result

    monkeypatch.setattr(native, '_darwin_members', members)
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (None, errno.ESRCH)
                        if pid == ROOT and exited[0] else original_info(pid))
    monkeypatch.setattr(registry, '_darwin_zombie_root_info', lambda pid: (ROOT, token.split('#kb=')[0]))
    snapshot, complete = registry._fresh_process_tree_snapshot(ROOT)
    assert complete and set(snapshot) == {ROOT, CHILD, ORPHAN}
    assert snapshot[ROOT] == token


def test_event_interleaved_root_reuse_cannot_signal_or_confirm(tree, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    original, results, signals = native._darwin_members, [], []
    token = registry.capture_process_birth_token(ROOT)

    def members(kind, identifier):
        result = original(kind, identifier)
        if kind == 2:
            entered.set()
            assert proceed.wait(2)
        return result

    monkeypatch.setattr(native, '_darwin_members', members)
    monkeypatch.setattr(registry.os, 'kill', lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(registry.os, 'killpg', lambda pid, sig: signals.append((pid, sig)), raising=False)
    worker = threading.Thread(target=lambda: results.append(
        registry.terminate_process_tree(ROOT, None, expected_birth_token=token)))
    worker.start()
    try:
        assert entered.wait(2)
        tree.rows[ROOT] = _info(ROOT, 1, ROOT, microsecond=2)
    finally:
        proceed.set()
        worker.join(3)
    assert not worker.is_alive() and len(results) == 1
    assert results[0].method == 'identity_unavailable' and not results[0].confirmed
    assert signals == []


@pytest.mark.parametrize('output,code', [('', 0), ('41001 41001 R Mon Nov  9 11:00:00 2026', 0),
    ('41099 41001 Z Mon Nov  9 11:00:00 2026', 0), ('41001 0 Z Mon Nov  9 11:00:00 2026', 0),
    ('41001 41001 Z', 0), ('41001 41001 Z Mon Nov  9 11:00:00 2026\nextra', 0),
    ('41001 41001 Z Mon Nov  9 11:00:00 2026', 1)])
def test_exact_zombie_probe_never_promotes_unknown_output(monkeypatch, output, code):
    monkeypatch.setattr(registry.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=output))
    assert registry._darwin_zombie_root_info(ROOT) is None


def test_exact_zombie_probe_preserves_original_date_token_and_never_enumerates_host(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs['stdin'] == registry.subprocess.DEVNULL and kwargs['env']['LC_ALL'] == 'C'
        return SimpleNamespace(returncode=0, stdout='41001 41001 Z+ Mon Nov  9 11:00:00 2026\n')

    monkeypatch.setattr(registry.subprocess, 'run', run)
    assert registry._darwin_zombie_root_info(ROOT) == (ROOT, 'host:Mon Nov  9 11:00:00 2026')
    assert calls == [['ps', '-p', str(ROOT), '-o', 'pid=,pgid=,stat=,lstart=']]


def test_cached_zombie_identity_uses_fresh_kernel_birth_without_ps(monkeypatch):
    # 此处模拟Darwin LP64字节，而不是Windows LLP64的long；产品sizeof硬门保持。
    import builtins
    import ctypes
    from pathlib import Path

    from agent_py_agent.agent.common import heartbeat
    from agent_py_agent.agent.gateway_parts import (
        multiprocess_takeover_darwin_types as darwin_types,
    )

    real_import = builtins.__import__
    darwin_ctypes = SimpleNamespace(**{key: getattr(ctypes, key) for key in dir(ctypes) if not key.startswith('__')})
    darwin_ctypes.c_long = ctypes.c_int64
    def import_darwin(name, *args, **kwargs):
        return darwin_ctypes if name == 'ctypes' else real_import(name, *args, **kwargs)
    namespace = {'__builtins__': {**vars(builtins), '__import__': import_darwin}}
    exec(compile(Path(darwin_types.__file__).read_text(encoding='utf-8'), darwin_types.__file__, 'exec'), namespace)
    KinfoProc = namespace['KinfoProc']
    assert ctypes.sizeof(KinfoProc) == 648
    monkeypatch.setattr(darwin_types, 'KinfoProc', KinfoProc)
    value = KinfoProc()
    value.kp_proc.p_pid = ROOT
    value.kp_proc.p_stat = b'\x05'
    value.kp_proc.p_un.p_starttime.tv_sec = 1794249600
    value.kp_proc.p_un.p_starttime.tv_usec = 123
    value.kp_eproc.e_pgid = ROOT
    queries, ps = [], []
    def query(mib, count, output, size, *_unused):
        assert count == 4 and tuple(mib) == (1, 14, 1, ROOT)
        queries.append(ROOT)
        ctypes.memmove(output, ctypes.byref(value), ctypes.sizeof(value))
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = ctypes.sizeof(value)
        return 0
    monkeypatch.setattr(heartbeat, '_darwin_sysctl', lambda: query)
    monkeypatch.setattr(registry, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(native, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(native, '_darwin_info', lambda pid: (None, errno.ESRCH))
    monkeypatch.setattr(registry, '_native_posix_probe_enabled', lambda: True)
    monkeypatch.setattr(native, 'process_birth', lambda pid: None)
    monkeypatch.setattr(registry, '_PID_BIRTH_TOKEN_CACHE', {
        (ROOT, '1794249600:000123'): 'host:original#kb=1794249600:000123'})
    def empty_ps(*args, **kwargs):
        ps.append(args)
        return SimpleNamespace(returncode=1, stdout='')
    monkeypatch.setattr(registry.subprocess, 'run', empty_ps)
    assert registry._darwin_zombie_root_info(ROOT) == (ROOT, 'host:original#kb=1794249600:000123')
    assert registry.capture_process_birth_token(ROOT) == 'host:original#kb=1794249600:000123'
    assert registry._native_kernel_terminated(ROOT) is True
    assert len(queries) >= 3 and ps == []


@pytest.mark.parametrize('failure', ['size', 'pid', 'live', 'group', 'seconds', 'micros', 'errno', 'abi'])
def test_zombie_kernel_unknown_never_borrows_cached_identity(monkeypatch, failure):
    import ctypes

    from agent_py_agent.agent.common import heartbeat
    from agent_py_agent.agent.gateway_parts import multiprocess_takeover_darwin_types as types

    value = types.KinfoProc()
    value.kp_proc.p_pid = ROOT + (failure == 'pid')
    value.kp_proc.p_stat = b'\x02' if failure == 'live' else b'\x05'
    value.kp_proc.p_un.p_starttime.tv_sec = 0 if failure == 'seconds' else 1794249600
    value.kp_proc.p_un.p_starttime.tv_usec = 1000000 if failure == 'micros' else 123
    value.kp_eproc.e_pgid = 0 if failure == 'group' else ROOT
    def query(mib, count, output, size, *_unused):
        ctypes.memmove(output, ctypes.byref(value), ctypes.sizeof(value))
        ctypes.cast(size, ctypes.POINTER(ctypes.c_size_t))[0] = ctypes.sizeof(value) - (failure == 'size')
        return -1 if failure == 'errno' else 0
    monkeypatch.setattr(heartbeat, '_darwin_sysctl', lambda: query)
    monkeypatch.setattr(registry, 'sys', SimpleNamespace(platform='darwin'))
    monkeypatch.setattr(registry, '_native_posix_probe_enabled', lambda: True)
    monkeypatch.setattr(registry, '_PID_BIRTH_TOKEN_CACHE', {
        (ROOT, '1794249600:000123'): 'host:original#kb=1794249600:000123'})
    if failure == 'abi':
        monkeypatch.setattr(types, 'KinfoProc', ctypes.c_int)
    calls = []
    def empty_ps(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(registry.subprocess, 'run', empty_ps)
    assert registry._darwin_zombie_kernel_identity(ROOT) is None
    assert registry._darwin_zombie_root_info(ROOT) is None
    assert len(calls) == 1


@pytest.mark.parametrize('platform', ['linux', 'freebsd', 'win32'])
def test_non_darwin_never_queries_darwin_zombie_abi(monkeypatch, platform):
    from agent_py_agent.agent.common import heartbeat

    monkeypatch.setattr(registry, 'sys', SimpleNamespace(platform=platform))
    def forbidden():
        raise AssertionError('wrong-platform sysctl')
    monkeypatch.setattr(heartbeat, '_darwin_sysctl', forbidden)
    monkeypatch.setattr(registry, '_native_posix_probe_enabled', lambda: True)
    calls = []
    def old_probe(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='41001 41001 Z Mon Nov  9 11:00:00 2026')
    monkeypatch.setattr(registry.subprocess, 'run', old_probe)
    assert registry._darwin_zombie_kernel_identity(ROOT) is None
    assert registry._darwin_zombie_root_info(ROOT) == (ROOT, 'host:Mon Nov  9 11:00:00 2026')
    assert len(calls) == 1
