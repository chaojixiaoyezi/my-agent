# LLM: 611只在原受控C/P4/W fixture与本地临时目录确定性重现第三次role读取窗口；不启动真实服务、模型或native。
# 模块用途: 验closed分支显式复用原strict snapshot，默认CountNone调用形状与危险路径拒绝保持。
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common import nofollow_fs as fs
from agent_py_agent.agent.gateway_parts import multiprocess_takeover_sources as sources
from agent_py_agent.tests.test_gateway_prepared_route_immutability import context as context
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import (
    publish_heartbeat,
)
from agent_py_agent.tests.test_gateway_role_heartbeat_stability import (
    running as running,
)
from agent_py_agent.tests.test_gateway_worker_runtime import physical as physical


# LLM: 钩子仅在原_check_role_marker的w0读取内调用原producer，真实os.open/完整reader仍执行，不模拟安全异常。
# 函数用途: 把原正常heartbeat原子发表精确放在第三次role读取stat/open窗口，记录真实inode和原parent FD。
def third_role_publication(monkeypatch, workers, *, every=False):
    supported = fs._supports_dir_fd()
    monkeypatch.setattr(fs, '_supports_dir_fd', lambda: supported)
    original_open, original_check = os.open, sources._check_role_marker
    phase, attempts, parents, replacements = [], [], [], []
    marker = workers.control.paths.root / 'workers/w0.json'

    # LLM: 只标实验中的准确读取阶段，转发全部原参数；不改变原方法的身份或生命周期检查。
    # 函数用途: 确保竞态发生在closed第三次读，而非首末完整snapshot。
    def checking(root, parts, current, **kwargs):
        phase.append(parts)
        try:
            return original_check(root, parts, current, **kwargs)
        finally:
            phase.pop()

    # LLM: 原os.open始终真正执行；原producer只改本测试fixture的heartbeat，支持常态一次和三次有界竞态。
    # 函数用途: 记录stat/open间真实原子替换前后inode，不依赖线程调度或sleep。
    def opening(name, flags, mode=0o777, **kwargs):
        if phase and phase[-1] == ('workers', 'w0.json') and name == 'w0.json':
            attempts.append(name)
            parents.append(kwargs.get('dir_fd'))
            if every or len(attempts) == 1:
                before = marker.stat().st_ino
                publish_heartbeat(workers, monkeypatch, stamp=2.0 + len(attempts), healthy=False)
                replacements.append((before, marker.stat().st_ino))
        return original_open(name, flags, mode, **kwargs)

    monkeypatch.setattr(sources, '_check_role_marker', checking)
    monkeypatch.setattr(os, 'open', opening)
    return attempts, parents, replacements


def test_closed_current_role_check_accepts_original_atomic_publication(running, monkeypatch):
    attempts, parents, replacements = third_role_publication(monkeypatch, running)
    assert sources.require_role_coverage(running.control.paths, running.control.c_run_id, count=running.control.worker_count) == ()
    assert len(attempts) == 2 and len(set(parents)) == 1
    assert len(replacements) == 1 and all(before != after for before, after in replacements)


def test_closed_third_role_churn_stops_after_original_three_attempts(running, monkeypatch):
    attempts, parents, replacements = third_role_publication(monkeypatch, running, every=True)
    with pytest.raises(fs._SnapshotLeafChanged):
        sources.require_role_coverage(running.control.paths, running.control.c_run_id, count=running.control.worker_count)
    assert len(attempts) == len(replacements) == 3
    assert len(set(parents)) == 1
    assert all(before != after for before, after in replacements)


def test_default_CountNone_keeps_original_read_kwargs(tmp_path, monkeypatch):
    (tmp_path / 'workers').mkdir()
    (tmp_path / 'gateway_state.json').write_text(json.dumps({'c_run_id': 'current', 'pid': os.getpid()}))
    original, calls = sources.read_bytes_beneath, []

    # LLM: 原读取继续执行，spy只记录可观察的调用kwargs，不能通过省略真实读来伪造默认行为。
    # 函数用途: 核对默认CountNone仍没有向原reader添加snapshot参数。
    def reading(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(sources, 'read_bytes_beneath', reading)
    assert sources.require_role_coverage(SimpleNamespace(root=tmp_path), 'current') is None
    assert calls == [{'max_bytes': 16384, 'require_dir_fd': True}]


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'directory'])
def test_opted_in_role_marker_still_refuses_unsafe_leaf(tmp_path, kind):
    (tmp_path / 'workers').mkdir()
    leaf, other = tmp_path / 'workers/w0.json', tmp_path / 'untouched'
    other.write_bytes(b'untouched')
    if kind == 'symlink':
        leaf.symlink_to(other)
    elif kind == 'hardlink':
        os.link(other, leaf)
    elif kind == 'fifo':
        os.mkfifo(leaf)
    else:
        leaf.mkdir()
    with pytest.raises(OSError):
        sources._check_role_marker(tmp_path, ('workers', 'w0.json'), 'current', snapshot_retries=2)
    assert other.read_bytes() == b'untouched'
