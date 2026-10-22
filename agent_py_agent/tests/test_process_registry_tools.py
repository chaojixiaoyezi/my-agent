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


# LLM: 只计数临时proc根，返回真实读取结果；不替换进程关系或完整性结论。
# 函数用途: 记录目录枚举次数及stat/status读取，验证没有隐藏的第二遍扫描。
def _count_proc_io(monkeypatch, proc):
    calls = {"scans": 0, "reads": []}
    original_iterdir, original_read = Path.iterdir, Path.read_text

    # LLM: 完整消费真实目录迭代器，不改变条目顺序或异常。
    # 函数用途: 记录每次proc根枚举。
    def iterdir(path):
        if path == proc:
            calls["scans"] += 1
        return original_iterdir(path)

    # LLM: 只记录临时proc中的文件，不拦截pytest或其它模块读取。
    # 函数用途: 检查有效stat不会额外读取status。
    def read(path, *args, **kwargs):
        if proc in path.parents:
            calls["reads"].append(path.relative_to(proc).as_posix())
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(Path, "read_text", read)
    return calls


# LLM: comm含嵌套括号和空格，stat和status内容故意冲突以证明stat优先；缺失/坏stat须回退。
# 函数用途: 验证同一次扫描产出父表和组表，并保持仅status样本兼容。
def test_linux_proc_combines_maps_single_pass_and_falls_back(proc_group, monkeypatch):
    (proc_group / "200/stat").write_text("200 (my (weird) cmd) S 100 200 0 0")
    (proc_group / "200/status").write_text("Name: ignored\nPPid:\t999\n")
    for pid, stat in ((300, None), (301, "malformed")):
        entry = proc_group / str(pid)
        entry.mkdir()
        if stat is not None:
            (entry / "stat").write_text(stat)
        (entry / "status").write_text("Name: fallback\nPPid:\t201\n")
    calls = _count_proc_io(monkeypatch, proc_group)

    parents, groups = registry._linux_proc_parent_and_group_maps(proc_group)

    assert parents == {200: 100, 201: 1, 202: 1, 300: 201, 301: 201}
    assert groups == {200: 200, 201: 200, 202: 900}
    assert calls["scans"] == 1
    assert sorted(calls["reads"]) == ["200/stat", "201/stat", "202/stat", "300/stat", "300/status", "301/stat", "301/status"]


# LLM: 畸形数字、短stat和拒读不允许产出半份映射；父表可由status修复，组表仍不完整。
# 函数用途: 覆盖stat失败回退及无法证明组范围时的保守处理。
@pytest.mark.parametrize("stat", ["bad", "200 (w) S", "200 (w) S nope 200", "200 (w) S 1 nope", "denied"])
def test_linux_proc_bad_stat_keeps_group_scan_incomplete(proc_group, monkeypatch, stat):
    path = proc_group / "201/stat"
    path.write_text(stat)
    (proc_group / "201/status").write_text("Name: child\nPPid: 200\n")
    original = Path.read_text

    # LLM: 只给当前临时stat注入权限错误；status依旧真实可读。
    # 函数用途: 模拟内核拒绝一项stat但父关系仍可从status恢复。
    def read(target, *args, **kwargs):
        if target == path and stat == "denied":
            raise PermissionError("denied")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    parents, groups = registry._linux_proc_parent_and_group_maps(proc_group)
    assert parents == {200: 1, 201: 200, 202: 1}
    assert groups == {200: 200, 202: 900}
    snapshot, complete = registry._process_tree_snapshot(200)
    assert set(snapshot) == {200, 201}
    assert complete is False


# LLM: 当前Linux父表入口应使用stat优先，与单遍helper同源；status不可覆盖有效stat。
# 函数用途: 验证独立父表调用也不再读取昂贵status。
def test_linux_parent_map_prefers_stat(proc_group, monkeypatch):
    (proc_group / "200/status").write_text("PPid: 999\n")
    calls = _count_proc_io(monkeypatch, proc_group)
    assert registry._process_parent_map() == ({200: 1, 201: 1, 202: 1}, True)
    assert calls["scans"] == 1
    assert sorted(calls["reads"]) == ["200/stat", "201/stat", "202/stat"]


# LLM: 新会话后代不在root进程组中，必须依赖同次扫描的父关系而不能只收原组。
# 函数用途: 验证树快照单遍扫描，既收同组孤儿也收跨会话后代。
def test_linux_tree_snapshot_scans_once_and_keeps_new_sessions(proc_group, monkeypatch):
    entry = proc_group / "203"
    entry.mkdir()
    (entry / "stat").write_text("203 (nested (session)) S 201 203 0")
    calls = _count_proc_io(monkeypatch, proc_group)
    snapshot, complete = registry._process_tree_snapshot(200)
    assert snapshot == {200: "birth", 201: "birth", 203: "birth"}
    assert complete is True
    assert calls["scans"] == 1
    assert sorted(calls["reads"]) == ["200/stat", "201/stat", "202/stat", "203/stat"]


# LLM: 任意旧map入口被替换都必须尊重注入值，不能走默认单遍分支绕过测试和调用契约。
# 函数用途: 分别验证父表/组表monkeypatch仍参与原进程树和完整性判断。
@pytest.mark.parametrize("which", ["parent", "group"])
def test_tree_snapshot_respects_either_map_monkeypatch(proc_group, monkeypatch, which):
    if which == "parent":
        mapping = Mock(return_value=({333: 200}, False))
        monkeypatch.setattr(registry, "_process_parent_map", mapping)
    else:
        mapping = Mock(return_value=({200: 200, 333: 200}, False))
        monkeypatch.setattr(registry, "_process_group_map", mapping)
    snapshot, complete = registry._process_tree_snapshot(200)
    mapping.assert_called_once_with()
    assert snapshot[333] == "birth" and complete is False


# LLM: 默认共享扫描不能把枚举后才读到的新出生身份，反套给旧进程组的孤儿成员。
# 函数用途: 模拟扫描期间root PID复用，验证组范围仍受扫描前后出生身份约束。
def test_prefetched_groups_reject_leader_reused_during_scan(proc_group, monkeypatch):
    reused = False
    original = Path.read_text

    # LLM: 只在临时root stat已读出后切换出生身份，不修改实际进程或发信号。
    # 函数用途: 在组映射采集与后续身份读取之间制造可重复的PID复用竞态。
    def read(path, *args, **kwargs):
        nonlocal reused
        text = original(path, *args, **kwargs)
        if path == proc_group / "200/stat":
            reused = True
        return text

    # LLM: 非root身份恒定，root只在扫描后变为另一个实例。
    # 函数用途: 返回受控的出生标识，避免依赖真实PID复用时机。
    def birth(pid):
        return "new" if pid == 200 and reused else "birth"

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(registry, "capture_process_birth_token", birth)
    monkeypatch.setattr(registry, "_same_process", lambda pid, token: birth(pid) == token)
    snapshot, complete = registry._process_tree_snapshot(200)
    assert 201 not in snapshot
    assert complete is False


# LLM: stat缺失而status拒读/坏文本不能按“进程已消失”处理；预取组表也须传播此缺口。
# 函数用途: 核验组合读取异常不会给不完整的组表贴完整标签，也不会重新扫描。
@pytest.mark.parametrize("case", ["bad", "denied"])
def test_missing_stat_with_bad_status_keeps_prefetched_groups_incomplete(proc_group, monkeypatch, case):
    (proc_group / "201/stat").unlink()
    path = proc_group / "201/status"
    path.write_text("PPid: invalid\n")
    original = Path.read_text

    # LLM: 拒读只作用于临时status，stat缺失仍由真实文件系统抛出。
    # 函数用途: 注入stat缺失叠加status权限错误的组合场景。
    def read(target, *args, **kwargs):
        if target == path and case == "denied":
            raise PermissionError("denied")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    parents, groups = registry._linux_proc_parent_and_group_maps(proc_group)
    assert parents == {200: 1, 202: 1}
    scan = Mock(side_effect=AssertionError("prefetched groups must not rescan"))
    monkeypatch.setattr(registry, "_process_group_map", scan)
    assert registry._exclusive_group_members(200, groups=groups) == ([200], False)
    scan.assert_not_called()
