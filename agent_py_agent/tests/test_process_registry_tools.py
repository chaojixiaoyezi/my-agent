# LLM: 只读临时procfs样本与假信号验证进程枚举/退出优化，不以模拟代替真实Linux进程树验收。
# 模块用途: 守住无需fork的组枚举、出生身份边界及僵尸免发信号语义。
from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.tooling import process_registry as registry


# LLM: 仅向pytest临时目录写proc stat；comm含空格和右括号，不能用首个右括号切分。
# 函数用途: 构造最小真实文本的PID/PGID表并替换proc根，不访问真实宿主进程。
@pytest.fixture
def proc_group(monkeypatch, tmp_path):
    proc = tmp_path / 'proc'
    proc.mkdir()
    for pid, group in [(200, 200), (201, 200), (202, 900)]:
        entry = proc / str(pid)
        entry.mkdir()
        (entry / 'stat').write_text(f'{pid} (worker ) name) S 1 {group} 0 0 0')
    (proc / 'uptime').write_text('not a process')
    monkeypatch.setattr(registry, 'Path', lambda value: proc if str(value) == '/proc' else Path(value))
    monkeypatch.setattr(registry, 'capture_process_birth_token', lambda pid: 'birth')
    monkeypatch.setattr(registry, '_same_process', lambda *args: True)
    monkeypatch.setattr(registry.os, 'getpgrp', lambda: 900)
    return proc


# LLM: Linux分支不得fork庞大的Gateway；仅stat中的pgrp可以决定组归属。
# 函数用途: 证明存在proc时不调用ps，且只返回原独立组成员。
def test_exclusive_group_reads_proc_without_subprocess(proc_group, monkeypatch):
    run = Mock(side_effect=AssertionError('must not fork ps on Linux'))
    monkeypatch.setattr(registry.subprocess, 'run', run)
    members, complete = registry._exclusive_group_members(200)
    assert sorted(members) == [200, 201] and complete is True
    run.assert_not_called()


# LLM: 进程消失可跳过，坏文本或权限失败须保留枚举不完整，不能用空表证明退出。
# 函数用途: 核验消失、坏stat和拒读三种边界。
@pytest.mark.parametrize(('case', 'complete'), [('gone', True), ('bad', False), ('denied', False)])
def test_proc_group_read_failures_are_not_confirmed(proc_group, monkeypatch, case, complete):
    path = proc_group / '201/stat'
    if case == 'gone':
        path.unlink()
    elif case == 'bad':
        path.write_text('not stat')
    else:
        original = Path.read_text
        # LLM: 仅目标stat模拟内核拒读，其余样本保持真实读取。
        # 函数用途: 注入组成员读取缺口。
        def read(self, *args, **kwargs):
            if self == path:
                raise PermissionError('denied')
            return original(self, *args, **kwargs)
        monkeypatch.setattr(Path, 'read_text', read)
    monkeypatch.setattr(registry.subprocess, 'run', Mock(side_effect=AssertionError('unexpected ps')))
    assert registry._exclusive_group_members(200) == ([200], complete)


# LLM: 组枚举前后必须是同一出生实例，不能凭旧PID扩大范围；宿主组绝不能被接管。
# 函数用途: 核验PID复用、非独立组长、宿主组和组长缺失。
@pytest.mark.parametrize('case', ['reused', 'not-leader', 'own-group', 'missing'])
def test_proc_group_keeps_leader_identity_guards(proc_group, monkeypatch, case):
    expected = True
    if case == 'reused':
        monkeypatch.setattr(registry, '_same_process', lambda *args: False)
        expected = False
    elif case == 'not-leader':
        (proc_group / '200/stat').write_text('200 (root) S 1 900 0')
    elif case == 'own-group':
        monkeypatch.setattr(registry.os, 'getpgrp', lambda: 200)
    else:
        (proc_group / '200/stat').unlink()
        expected = False
    monkeypatch.setattr(registry.subprocess, 'run', Mock(side_effect=AssertionError('unexpected ps')))
    assert registry._exclusive_group_members(200) == ([], expected)


# LLM: 无proc平台保留原ps参数、关闭stdin及一秒预算，坏行不能称枚举完整。
# 函数用途: 锁住macOS等平台的原分支与完整性事实。
@pytest.mark.parametrize(('rows', 'expected'), [('200 200\n201 200\n202 900\n', True), ('200 200\nbad\n201 200\n', False)])
def test_group_without_proc_keeps_ps(proc_group, monkeypatch, rows, expected):
    monkeypatch.setattr(registry, 'Path', lambda value: proc_group / 'absent')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, rows, ''))
    monkeypatch.setattr(registry.subprocess, 'run', run)
    assert registry._exclusive_group_members(200) == ([200, 201], expected)
    assert run.call_args.args[0] == ['ps', '-axo', 'pid=,pgid=']
    assert run.call_args.kwargs['stdin'] == subprocess.DEVNULL and run.call_args.kwargs['timeout'] == 1


# LLM: 仅被确认的同出生活进程可进入发信号集合，僵尸和调用者自身不参与。
# 函数用途: 核验僵尸退出不发信号，仍存活后代保持TERM语义。
@pytest.mark.parametrize('with_live', [False, True])
def test_signal_snapshot_skips_zombies(monkeypatch, with_live):
    monkeypatch.setattr(registry.os, 'getpid', lambda: 900)
    monkeypatch.setattr(registry.os, 'getpgrp', lambda: 900)
    monkeypatch.setattr(registry.os, 'getpgid', lambda pid: 200)
    monkeypatch.setattr(registry, '_same_process', lambda *args: True)
    monkeypatch.setattr(registry, '_process_is_zombie', lambda pid: pid == 200)
    kill, killpg = Mock(), Mock()
    monkeypatch.setattr(registry.os, 'kill', kill)
    monkeypatch.setattr(registry.os, 'killpg', killpg)
    snapshot = {900: 'own', 200: 'zombie'}
    if with_live:
        snapshot[201] = 'live'
    assert registry._signal_process_snapshot(snapshot, signal.SIGTERM) is with_live
    if with_live:
        kill.assert_called_once_with(201, signal.SIGTERM)
    else:
        kill.assert_not_called()
    assert killpg.call_count == int(with_live)


# LLM: 僵尸根已退出，不应再进入TERM宽限轮询；最后确认和Popen回收仍走原逻辑。
# 函数用途: 锁住自然退出免50ms等待且终止回执保持确认。
def test_zombie_foreground_has_no_signal_wait(monkeypatch):
    monkeypatch.setattr(registry, '_IS_WINDOWS', False)
    monkeypatch.setattr(registry, '_process_tree_snapshot', lambda pid: ({200: 'birth'}, True))
    monkeypatch.setattr(registry, '_same_process', lambda *args: True)
    monkeypatch.setattr(registry, '_process_is_zombie', lambda pid: True)
    monkeypatch.setattr(registry, '_process_instance_terminated', lambda *args: True)
    monkeypatch.setattr(registry.os, 'getpid', lambda: 900)
    monkeypatch.setattr(registry.os, 'getpgrp', lambda: 900)
    monkeypatch.setattr(registry.os, 'getpgid', lambda pid: 200)
    monkeypatch.setattr(registry.os, 'killpg', Mock())
    monkeypatch.setattr(registry.os, 'kill', Mock())
    monkeypatch.setattr(registry.time, 'sleep', lambda seconds: pytest.fail('zombie must not sleep'))
    proc = Mock()
    proc.poll.return_value = 0
    receipt = registry.terminate_process_tree(200, proc, expected_birth_token='birth')
    assert receipt.confirmed is True and receipt.method == 'already_gone'
