# LLM: 只在临时Owner树构造真实沙箱参数，不把argv模拟等同于Linux内核隔离验证。
# 模块用途: 守住多用户沙箱构参性能、挂载体积和原安全覆盖。
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.path_access_policy import HOST_MANAGED_OWNER_STORE_PARTS
from agent_py_agent.agent.tooling import sandbox, shell


# LLM: 数据目录均在pytest临时根，调用真实宿主路径声明；不以减少Owner或隐藏项满足性能数字。
# 函数用途: 创建25个飞书用户及管理员，返回完整宿主只读与隐藏路径规格。
@pytest.fixture
def multi_owner_spec(tmp_path):
    root = tmp_path.resolve() / 'app/.my-agent/data'
    main = root / 'owners/local/main'
    homes = [main, *(root / f'owners/providers/feishu/users/user_{i:02d}' for i in range(25))]
    for home in homes:
        (home / 'persona').mkdir(parents=True)
        (home / 'AGENTS.md').write_text('# rules\n')
        for parts in HOST_MANAGED_OWNER_STORE_PARTS:
            home.joinpath(*parts).mkdir(parents=True, exist_ok=True)
    return sandbox.SandboxSpec(owner_home=main, workspace=main, protected_persona_root=main,
        full_access=True, bwrap_path='/fake/bwrap', read_only_paths=shell.host_readonly_paths_for('', str(main)),
        hidden_paths=shell._host_managed_hidden_paths('', str(main)))


# LLM: 性能时钟只覆盖真实build函数；完整安全断言在计时之后，阈值按任务规定不放宽。
# 函数用途: 测量25用户构参并核对所有人格与宿主存储仍由最终覆盖保护。
def test_multi_owner_build_is_fast_and_compact(multi_owner_spec):
    spec = multi_owner_spec
    start = time.perf_counter()
    argv = sandbox.build_bwrap_argv(spec)
    elapsed = time.perf_counter() - start
    print(json.dumps({'build_seconds': elapsed, 'argv_length': len(argv), 'owners': 26,
                      'readonly_candidates': len(spec.read_only_paths), 'hidden_paths': len(spec.hidden_paths)}))
    mounts = sandbox._mount_snapshot(argv)
    for hidden in spec.hidden_paths:
        mount = sandbox._effective_mount(hidden, mounts)
        assert mount.source is None and mount.arguments == ('--tmpfs', str(hidden), '--remount-ro', str(hidden))
    for protected in spec.read_only_paths:
        if protected.exists():
            assert not sandbox._effective_mount(protected, mounts).writable
    homes = {path.parent for path in spec.hidden_paths if path.name == 'plugins'}
    assert len(homes) == 26
    for data in homes:
        for name in ('AGENTS.md', 'persona'):
            path = data.parent / name
            assert sandbox._effective_mount(path, mounts).arguments == ('--ro-bind', str(path), str(path))
        assert sandbox._effective_mount(data.parent / 'business.txt', mounts).writable
    assert argv[-2:] == ['--chdir', str(spec.workspace)] and '--share-net' in argv
    assert elapsed < .25, f'build_bwrap_argv took {elapsed:.6f}s'
    # 任务2026-10-21修订：26 Owner完整保护最小765项，不删保护项去迎合旧700界限。
    assert len(argv) < 800, f'{len(argv)} arguments'


# LLM: 已被父挂载重盖的重复项可以重放，未重盖的同源ro-bind则不应反复发给内核。
# 函数用途: 验证最终构参没有缺乏遮盖理由的重复只读挂载。
def test_multi_owner_has_no_uncovered_duplicate_readonly_mounts(multi_owner_spec):
    mounts = sandbox._mount_snapshot(sandbox.build_bwrap_argv(multi_owner_spec))
    previous = {}
    for index, mount in enumerate(mounts):
        if mount.arguments[:1] not in {('--ro-bind',), ('--ro-bind-try',)}:
            continue
        key = mount.arguments
        if key in previous:
            intervening = mounts[previous[key] + 1:index]
            assert any(mount.target == item.target or item.target in mount.target.parents for item in intervening), key
        previous[key] = index


# LLM: 词法包含不解析软链接，不能把/a-b当/a的子目录，也不允许热路径用异常控制分支。
# 函数用途: 覆盖绝对、相对、根与双斜杠边界，并拦截relative_to调用。
@pytest.mark.parametrize(('path', 'root', 'expected'), [('/a/b', '/a', True), ('/ab', '/a', False),
    ('/a', '/a/', True), ('/a', '/', True), ('a/b', '.', True), ('../a', '..', True),
    ('a', '/', False), ('//a', '/', False), ('//a/b', '//a', True), ('/a', '.', False)])
def test_lexical_contains_does_not_raise_for_normal_misses(monkeypatch, path, root, expected):
    # LLM: 只替换此次纯词法调用依赖，避免让异常成本重新混入实现。
    # 函数用途: 确认包含判定没有调用旧的异常型API。
    def forbidden(*args, **kwargs):
        pytest.fail('relative_to must not be used for lexical membership')
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, 'relative_to', forbidden)
        actual = sandbox._lexically_within(Path(path), Path(root))
    assert actual is expected


# LLM: 参照原完整重放语义比较最终覆盖，优化只能删除影子副本，不能改变隐藏、异名或嵌套写根。
# 函数用途: 核对一次父挂载压缩前后的有效子挂载及非挂载参数。
@pytest.mark.parametrize('readonly_flag', ['--ro-bind', '--ro-bind-try'])
def test_parent_remount_compacts_without_changing_effective_children(readonly_flag):
    original = ['bwrap', '--bind', '/', '/', '--setenv', 'NAME', 'literal', '--ro-bind', '/src', '/tree',
        readonly_flag, '/old', '/tree/secret', '--tmpfs', '/tree/secret', '--remount-ro', '/tree/secret',
        '--bind', '/business', '/tree/open', '--symlink', '/business', '/tree/alias']
    mounts = sandbox._mount_snapshot(original)
    descendants = [m for m in mounts if m.target != Path('/tree') and Path('/tree') in m.target.parents
                   and sandbox._effective_mount(m.target, mounts) is m]
    expected = [*original, '--ro-bind', '/replacement', '/tree', *(arg for m in descendants for arg in m.arguments)]
    actual = original.copy()
    sandbox._append_mount_preserving_children(actual, ('--ro-bind', '/replacement', '/tree'))
    for path in ('/tree', '/tree/secret/child', '/tree/open/file', '/tree/alias', '/unrelated'):
        assert sandbox._effective_mount(Path(path), sandbox._mount_snapshot(actual)) == sandbox._effective_mount(Path(path), sandbox._mount_snapshot(expected))
    assert actual[4:7] == ['--setenv', 'NAME', 'literal']
    assert actual.count('/old') == 0 and len(actual) < len(original) + 4


# LLM: 只读去重必须考虑中间父覆盖；后续父RW覆盖后仍需重新发精确保护。
# 函数用途: 验证连续重复只读挂载被省略，但父覆盖后的保护不会被缓存吞掉。
@pytest.mark.parametrize(('name', 'flag'), [('protected', '--ro-bind'),
    ('runtime.db-wal', '--ro-bind-try'), ('runtime.db-shm', '--ro-bind-try'),
    ('runtime.db-journal', '--ro-bind-try'), ('state.json.lock', '--ro-bind-try')])
def test_readonly_mount_dedup_rechecks_later_parent(tmp_path, name, flag):
    leaf = tmp_path / name
    leaf.write_text('state')
    argv = ['bwrap', '--bind', str(tmp_path), str(tmp_path)]
    sandbox._append_readonly_mounts(argv, (leaf,))
    once = argv.copy()
    sandbox._append_readonly_mounts(argv, (leaf,))
    assert argv == once
    argv += ['--bind', str(tmp_path), str(tmp_path)]
    sandbox._append_readonly_mounts(argv, (leaf,))
    assert sandbox._effective_mount(leaf, sandbox._mount_snapshot(argv)).arguments == (flag, str(leaf), str(leaf))


# LLM: 只替换最终Attempt构参出口以观察真实shell收集结果，不启动平台沙箱；缺失路径仍须保留保护声明。
# 函数用途: 确认缺失宿主路径不触发resolve，现存路径与悬空链接仍解析，结构化任务保护始终解析。
def test_shell_only_resolves_existing_host_paths(tmp_path, monkeypatch):
    from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
    from agent_py_agent.agent.tooling.write_boundary import SandboxWriteProtection
    existing, missing, dangling = tmp_path / 'present', tmp_path / 'missing', tmp_path / 'alias'
    existing.write_text('state')
    dangling.symlink_to(missing)
    resolved, captured = [], []
    real_resolve = Path.resolve
    # LLM: 观察调用实参且保留真实路径解析，不让spy改变路径结果。
    # 函数用途: 统计本次shell路径收集的realpath调用。
    def resolve(path, *args, **kwargs):
        resolved.append(path)
        return real_resolve(path, *args, **kwargs)
    # LLM: 不执行命令，只读本次真实收集的Attempt规格。
    # 函数用途: 截获最终保护路径以核对缺失项没有被丢弃。
    def build(instance, command):
        captured.append(instance.spec)
        return command
    monkeypatch.setattr(shell, 'host_readonly_paths_for', lambda *args: (existing, missing, dangling))
    monkeypatch.setattr(AttemptExecutionSandbox, 'build_argv', build)
    monkeypatch.setattr(Path, 'resolve', resolve)
    shell._sandbox_exec('true', tmp_path, None, write_protection=SandboxWriteProtection(absolute_paths=(tmp_path / 'task-missing',)))
    assert missing not in resolved and existing in resolved and dangling in resolved
    assert tmp_path / 'task-missing' in resolved
    assert {existing, missing, tmp_path / 'task-missing'} <= set(captured[0].protected_write_paths)


# LLM: Linux只挂现存路径；缺失项提前跳过不改变任何挂载，却必须避免对整份宿主声明反复realpath。
# 函数用途: 在bwrap构参入口锁住缺失路径快跳，仍核验现存软链接的真实目标只读。
def test_readonly_mounts_skip_realpath_for_missing_entries(tmp_path, monkeypatch):
    missing, actual, alias = tmp_path / 'missing', tmp_path / 'actual', tmp_path / 'alias'
    actual.write_text('state')
    alias.symlink_to(actual)
    real_resolve, calls = Path.resolve, []
    # LLM: 观察真实解析，不改返回路径，不给沙箱增加例外。
    # 函数用途: 收集本次只读覆盖的realpath调用。
    def resolve(path, *args, **kwargs):
        calls.append(path)
        return real_resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    argv = ['bwrap']
    sandbox._append_readonly_mounts(argv, (missing, alias))
    assert missing not in calls and alias in calls
    assert argv == ['bwrap', '--ro-bind', str(actual), str(actual)]


# LLM: 独立复核反例：目录本身只读不代表后来子挂载未开写，绝对保护必须覆盖整个子树。
# 函数用途: 验证只读去重不保留后发子写根或异源只读子挂载。
@pytest.mark.parametrize('flag', ['--bind', '--ro-bind', '--ro-bind-try'])
def test_readonly_parent_reapplied_after_later_child_mount(tmp_path, flag):
    child = tmp_path / 'child'
    child.mkdir()
    argv = ['bwrap', '--ro-bind', str(tmp_path), str(tmp_path), flag, '/other-source', str(child)]
    sandbox._append_readonly_mounts(argv, (tmp_path,))
    assert sandbox._effective_mount(child, sandbox._mount_snapshot(argv)).arguments == ('--ro-bind', str(tmp_path), str(tmp_path))


# LLM: 真实临时文件驱动生产构参；仅瞬时侧车允许源缺失，持久文件/目录必须仍是硬只读绑定。
# 函数用途: 锁住SQLite侧车、锁文件和持久路径的精确挂载参数。
@pytest.mark.parametrize(('name', 'flag'), [('runtime.db-wal', '--ro-bind-try'),
    ('runtime.db-shm', '--ro-bind-try'), ('runtime.db-journal', '--ro-bind-try'),
    ('state.json.lock', '--ro-bind-try'), ('runtime.db', '--ro-bind'), ('AGENTS.md', '--ro-bind'),
    ('config', '--ro-bind'), ('runtime.db-wal.backup', '--ro-bind'), ('file.locked', '--ro-bind')])
def test_readonly_sidecar_flags_preserve_persistent_mounts(tmp_path, name, flag):
    path = tmp_path / name
    if name == 'config':
        path.mkdir()
    else:
        path.write_text('original')
    argv = ['bwrap']
    sandbox._append_readonly_mounts(argv, (path,))
    assert argv == ['bwrap', flag, str(path), str(path)]
    mount, = sandbox._mount_snapshot(argv)
    assert mount.source == path and mount.target == path and mount.writable is False


# LLM: 只读投影必须认try-bind；普通bind/dev-bind仍可写，不因新分支改变旧标志的语义。
# 函数用途: 单独核验四类bind的源、目标、写位及原始参数。
@pytest.mark.parametrize(('flag', 'writable'), [('--bind', True), ('--dev-bind', True),
                                               ('--ro-bind', False), ('--ro-bind-try', False)])
def test_mount_snapshot_recognizes_readonly_try(flag, writable):
    argv = ['bwrap', flag, '/source', '/target']
    mounts = sandbox._mount_snapshot(argv)
    assert len(mounts) == 1
    mount = sandbox._effective_mount(Path('/target/child'), mounts)
    assert mount.source == Path('/source') and mount.target == Path('/target')
    assert mount.arguments == (flag, '/source', '/target') and mount.writable is writable


# LLM: 压缩必须成组三参数移除try-bind，不能让已失效的源在祖先重挂前残留并触发bwrap失败。
# 函数用途: 验证只删除指定影子挂载，保留非挂载选项。
def test_remove_shadowed_readonly_try_arguments():
    arguments = ('--ro-bind-try', '/gone-wal', '/tree/runtime.db-wal')
    argv = ['bwrap', '--setenv', 'KEY', 'literal', *arguments, '--bind', '/src', '/other']
    sandbox._remove_shadowed_mount_arguments(argv, {arguments})
    assert argv == ['bwrap', '--setenv', 'KEY', 'literal', '--bind', '/src', '/other']


# LLM: 父挂载后重放的try-bind必须保持只读，且旧副本只能留下一个，避免参数与安全覆盖回归。
# 函数用途: 核验try-bind子覆盖随父挂载压缩和恢复。
@pytest.mark.parametrize('parent_flag', ['--bind', '--ro-bind', '--ro-bind-try'])
def test_parent_remount_preserves_readonly_try_child(parent_flag):
    child = ('--ro-bind-try', '/sidecar-wal', '/tree/runtime.db-wal')
    argv = ['bwrap', '--bind', '/old-tree', '/tree', *child, '--setenv', 'KEY', 'value']
    sandbox._append_mount_preserving_children(argv, (parent_flag, '/new-tree', '/tree'))
    assert argv[-3:] == list(child) and argv.count('--ro-bind-try') == (2 if parent_flag == '--ro-bind-try' else 1)
    mounts = sandbox._mount_snapshot(argv)
    mount = sandbox._effective_mount(Path(child[2]), mounts)
    assert mount.arguments == child and mount.writable is False
    assert argv[1:4] == ['--bind', '/old-tree', '/tree']
    assert argv[4:7] == ['--setenv', 'KEY', 'value']


# LLM: 仅Linux真实bwrap验证源在exec前删除的窗口；其他平台明确跳过，不用模拟器宣称内核保护通过。
# 函数用途: 对照侧车存在时拒写、消失时启动成功，并确认父目录实际可写。
@pytest.mark.skipif(not sys.platform.startswith('linux') or shutil.which('bwrap') is None,
                    reason='requires Linux and real bubblewrap')
@pytest.mark.parametrize('name', ['runtime.db-wal', 'runtime.db-shm', 'runtime.db-journal', 'state.json.lock'])
@pytest.mark.parametrize('disappear', [False, True])
def test_real_bwrap_readonly_sidecar_survives_exec_race(tmp_path, name, disappear):
    leaf = tmp_path / name
    leaf.write_text('original')
    argv = [shutil.which('bwrap'), '--ro-bind', '/', '/', '--bind', str(tmp_path), str(tmp_path)]
    sandbox._append_readonly_mounts(argv, (leaf,))
    assert argv[-3:] == ['--ro-bind-try', str(leaf), str(leaf)]
    if disappear:
        leaf.unlink()
    code = '''import errno, sys
from pathlib import Path
leaf, disappear = Path(sys.argv[1]), sys.argv[2] == 'True'
(leaf.parent / 'parent-write-control').write_text('writable')
if disappear:
    assert not leaf.exists()
else:
    try:
        leaf.write_text('must not write')
    except OSError as error:
        assert error.errno == errno.EROFS, error
    else:
        raise AssertionError('sidecar unexpectedly writable')
'''
    result = subprocess.run([*argv, sys.executable, '-B', '-c', code, str(leaf), str(disappear)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (tmp_path / 'parent-write-control').read_text() == 'writable'
    assert not leaf.exists() if disappear else leaf.read_text() == 'original'


# LLM: 仅提供任务列出的三类bwrap错误数据，不把模拟当Linux实测。
# 函数用途: 构造源缺失、挂载表缺项和只读重挂失败的回归输入。
def _sidecar_error(path, kind=0):
    if kind == 0:
        return f"bwrap: Can't find source path {path}: No such file or directory"
    reason = (f'Unable to find "{path}" in mount table' if kind == 1 else
              f'Unable to remount destination "{path}" with correct flags: Invalid argument')
    return f"bwrap: Can't bind mount /oldroot{path} on /newroot{path}: {reason}"


# LLM: 同时匹配stderr和瞬时try-bind，只扫描最后分隔符以前，输入argv不原地改变。
# 函数用途: 核验三种竞态裁剪，以及持久挂载、存活侧车和用户命令的保留。
@pytest.mark.parametrize('kind', range(3))
@pytest.mark.parametrize('name', ['runtime.db-wal', 'runtime.db-shm', 'runtime.db-journal', 'policy.lock'])
@pytest.mark.parametrize('wrapped', [False, True])
def test_prune_transient_sidecar_mounts(tmp_path, kind, name, wrapped):
    leaf, live, missing = (tmp_path / name, tmp_path / 'live-wal', tmp_path / 'gone-shm')
    leaf.write_text('present')
    live.write_text('present')
    prefix = ['python', '-I', 'landlock_launcher.py', '--deny', '8420', '--'] if wrapped else []
    base = [*prefix, 'bwrap', '--ro-bind', '/config', '/config']
    tail = ['--', 'echo', '--ro-bind-try', str(leaf), str(leaf)]
    argv = [*base, '--ro-bind-try', str(leaf), str(leaf), '--ro-bind-try', str(live), str(live),
            '--ro-bind-try', str(missing), str(missing), *tail]
    before = argv.copy()
    assert sandbox.prune_transient_bwrap_sidecar_mounts(argv, _sidecar_error(leaf, kind)) == [
        *base, '--ro-bind-try', str(live), str(live), *tail]
    assert argv == before


# LLM: 有缺失侧车不能把其它错误变成重试，不能扫描用户命令内的挂载形状参数。
# 函数用途: 锁住普通失败、持久文件、命令参数及不完整三元组的拒绝边界。
@pytest.mark.parametrize('case', ['ordinary', 'persistent', 'hard-sidecar', 'unmatched', 'command', 'command-double-dash', 'command-bwrap-echo', 'incomplete'])
def test_prune_transient_sidecar_rejects_unrelated_failure(case):
    argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal']
    error = _sidecar_error('/absent-wal')
    if case == 'ordinary':
        error = 'ordinary command failed'
    elif case == 'persistent':
        argv += ['--ro-bind', '/runtime.db', '/runtime.db']
        error = _sidecar_error('/runtime.db')
    elif case == 'hard-sidecar':
        argv[1] = '--ro-bind'
    elif case == 'unmatched':
        error = _sidecar_error('/another-wal')
    elif case == 'command':
        argv = ['bwrap', '--', 'echo', *argv[1:]]
    elif case == 'command-double-dash':
        argv = ['bwrap', '--', 'echo', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'end']
    elif case == 'command-bwrap-echo':
        argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'bash', '-c', f"echo '{error}' >&2; exit 1"]
    else:
        argv.pop()
    assert sandbox.prune_transient_bwrap_sidecar_mounts(argv, error) is None


# LLM: 目的路径可以异名；无分隔符按约定扫描整份argv，仍只删除瞬时try-bind。
# 函数用途: 验证异名目的路径匹配和公开导出。
def test_prune_transient_sidecar_matches_destination():
    argv = ['bwrap', '--ro-bind-try', '/source-wal', '/alias']
    assert sandbox.prune_transient_bwrap_sidecar_mounts(argv, _sidecar_error('/alias', 2)) == ['bwrap']
    assert 'prune_transient_bwrap_sidecar_mounts' in sandbox.__all__


# LLM: 仅替换平台构参及Popen/等待出口；调用真实Shell重试控制，不启动平台沙箱。
# 函数用途: 提供真实裁剪输入和可核对的启动、等待记录。
@pytest.fixture
def sidecar_shell(monkeypatch, tmp_path):
    argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'true']
    tool = SimpleNamespace(path_access_policy=SimpleNamespace(owner_scope_root=None), protected_persona_root=None,
                           host_private_roots=(), host_credential_env_names=frozenset())
    popen = Mock()
    communicate = Mock()
    monkeypatch.setattr(shell, '_sandbox_exec', lambda *a, **k: (argv.copy(), False))
    monkeypatch.setattr(shell.subprocess, 'Popen', popen)
    monkeypatch.setattr(shell, '_communicate_process', communicate)
    return tool, tmp_path, argv, popen, communicate


# LLM: 成功对象必须保留捕获/回收事实；每次启动仍关闭stdin、独立会话及擦洗环境。
# 函数用途: 复现瞬时失败后Shell透明恢复。
def test_shell_retries_transient_sidecar_failure(sidecar_shell):
    tool, target, argv, popen, communicate = sidecar_shell
    success = subprocess.CompletedProcess('true', 0, 'done', '')
    success.capture = {'complete': True}
    success.termination = object()
    communicate.side_effect = [subprocess.CompletedProcess('true', 1, '', _sidecar_error('/absent-wal')), success]
    assert shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None) is success
    assert [call.args[0] for call in popen.call_args_list] == [argv, ['bwrap', '--', 'true']]
    assert all(call.kwargs['stdin'] == subprocess.DEVNULL and call.kwargs['start_new_session'] for call in popen.call_args_list)


# LLM: 有stdout的结果、普通失败和成功均不能重放，避免重复业务副作用。
# 函数用途: 保留Shell非重试分支的原结果。
@pytest.mark.parametrize(('code', 'out', 'err'), [(0, '', _sidecar_error('/absent-wal')),
    (1, 'already ran', _sidecar_error('/absent-wal')), (1, '', 'ordinary failure')])
def test_shell_does_not_replay_command_results(sidecar_shell, code, out, err):
    tool, target, _argv, popen, communicate = sidecar_shell
    result = subprocess.CompletedProcess('true', code, out, err)
    communicate.return_value = result
    assert shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None) is result
    assert popen.call_count == 1


# LLM: 假裁剪出口只核验三次重试上限，不作为真实bwrap启动证据。
# 函数用途: 保证Shell最多四次启动且共用剩余时间。
def test_shell_sidecar_retries_share_deadline(sidecar_shell, monkeypatch):
    tool, target, _argv, popen, communicate = sidecar_shell
    now = [100.0]
    monkeypatch.setattr(shell.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(shell, 'prune_transient_bwrap_sidecar_mounts', lambda argv, err: argv[:-1], raising=False)
    # LLM: 每次等待仅推进测试时钟两秒，不执行或清理宿主进程。
    # 函数用途: 让重试预算递减可确定地被断言捕获。
    def completed(*args, **kwargs):
        now[0] += 2
        return subprocess.CompletedProcess('true', 1, '', 'transient')
    communicate.side_effect = completed
    shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None)
    assert popen.call_count == 4
    assert [call.kwargs['timeout'] for call in communicate.call_args_list] == [10, 8, 6, 4]


# LLM: 保留真实每次finally回收，只替换Popen、管道采集和终态等待，不启动宿主进程。
# 函数用途: 复现同步入口重试，核对剩余期限、原args及每次管道清理。
def test_attempt_sidecar_retry_keeps_budget_and_cleanup(monkeypatch, tmp_path):
    from agent_py_agent.agent.attempt import process_run as run
    now = [100.0]
    waits = []
    processes = []
    argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'true']
    monkeypatch.setattr(run.time, 'monotonic', lambda: now[0])
    capture = Mock()
    monkeypatch.setattr(run, 'ProcessOutputCapture', lambda proc: capture)
    # LLM: 假Popen只持有可观察close的管道，避免测试信号落到宿主PID。
    # 函数用途: 留存每次同步启动的参数与独立清理对象。
    def spawn(args, **kwargs):
        proc = Mock(stdout=Mock(), stderr=Mock())
        processes.append((args.copy(), proc, kwargs))
        return proc
    # LLM: 真实控制函数负责重试，替身仅交回预exec失败和成功并推进两秒测试时钟。
    # 函数用途: 观察每次同步等待的剩余预算。
    def wait(state, args, timeout):
        waits.append(timeout)
        now[0] += 2
        return subprocess.CompletedProcess(args, 1 if len(waits) == 1 else 0, '', _sidecar_error('/absent-wal'))
    monkeypatch.setattr(run.subprocess, 'Popen', spawn)
    monkeypatch.setattr(run, '_wait_for_process', wait)
    result = run.run_sandbox_process(argv, run.SandboxProcessOptions(10, .1, True, tmp_path))
    assert result.returncode == 0 and result.args == argv and waits == [10, 8]
    assert [args for args, _proc, _kwargs in processes] == [argv, ['bwrap', '--', 'true']]
    for _args, proc, kwargs in processes:
        proc.stdout.close.assert_called_once()
        proc.stderr.close.assert_called_once()
        assert kwargs['cwd'] == tmp_path and kwargs['stdin'] == subprocess.DEVNULL
    assert capture.finish.call_count == 2


# LLM: 超时143、已有stdout和未捕获输出均不能确认为预exec失败，不能重放。
# 函数用途: 核验同步入口拒绝重试的四类结果。
@pytest.mark.parametrize(('code', 'out', 'captured'), [(0, '', True), (143, '', True), (1, 'ran', True), (1, '', False)])
def test_attempt_does_not_retry_non_preexec_results(monkeypatch, tmp_path, code, out, captured):
    from agent_py_agent.agent.attempt import process_run as run
    popen = Mock(return_value=Mock(stdout=None, stderr=None))
    monkeypatch.setattr(run.subprocess, 'Popen', popen)
    monkeypatch.setattr(run, 'ProcessOutputCapture', lambda proc: Mock())
    monkeypatch.setattr(run, '_wait_for_process', lambda state, argv, timeout:
                        subprocess.CompletedProcess(argv, code, out, _sidecar_error('/absent-wal')))
    argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'false']
    result = run.run_sandbox_process(argv, run.SandboxProcessOptions(10, .1, captured, tmp_path))
    assert result.returncode == code and popen.call_count == 1


# LLM: 用真实取消令牌复现审查指出的预算提前返回窗口；假单次执行不创建任何宿主进程。
# 函数用途: 保证裁剪期间取消且预算耗尽时仍抛ToolCancelled，不交回旧bwrap错误。
def test_attempt_cancel_wins_expired_retry_budget(monkeypatch, tmp_path):
    from agent_py_agent.agent.attempt import process_run as run
    from agent_py_agent.agent.common.cancellation import (
        CancellationToken,
        ToolCancelled,
        bind_cancellation_token,
    )
    now = [100.0]
    token = CancellationToken()
    argv = ['bwrap', '--ro-bind-try', '/absent-wal', '/absent-wal', '--', 'true']
    once = Mock(return_value=subprocess.CompletedProcess(argv, 1, '', _sidecar_error('/absent-wal')))
    monkeypatch.setattr(run, '_run_sandbox_process_once', once)
    monkeypatch.setattr(run.time, 'monotonic', lambda: now[0])
    # LLM: 模拟首次命令已清理、裁剪期间收到取消并越过总期限；仍调用真实裁剪helper。
    # 函数用途: 稳定触发取消检查与提前返回的竞争窗口。
    def prune(args, error):
        token.cancel('cancel-during-prune')
        now[0] += 20
        return sandbox.prune_transient_bwrap_sidecar_mounts(args, error)
    monkeypatch.setattr(run, 'prune_transient_bwrap_sidecar_mounts', prune)
    with bind_cancellation_token(token), pytest.raises(ToolCancelled, match='cancel-during-prune'):
        run.run_sandbox_process(argv, run.SandboxProcessOptions(10, .1, True, tmp_path))
    assert once.call_count == 1


# LLM: 四个真实存活侧车逐次各失败一项，裁剪不能依赖缺失路径才能生效；单次等待仅推进测试时钟。
# 函数用途: 核验同步入口四次启动上限及预算耗尽不再启动。
@pytest.mark.parametrize(('budget', 'expected'), [(10, [10, 8, 6, 4]), (1, [1])])
def test_attempt_sidecar_retry_limit_and_budget(monkeypatch, tmp_path, budget, expected):
    from agent_py_agent.agent.attempt import process_run as run
    argv = ['bwrap']
    for number in range(4):
        sidecar = tmp_path / f'state{number}-wal'
        sidecar.write_text('present')
        argv += ['--ro-bind-try', str(sidecar), str(sidecar)]
    argv += ['--', 'true']
    now = [100.0]
    waits = []
    monkeypatch.setattr(run.time, 'monotonic', lambda: now[0])
    # LLM: 外层调用真实helper；仅替换已由其它用例覆盖的单次进程执行。
    # 函数用途: 在每次启动模拟当前首个侧车报错并记录剩余时间。
    def once(args, options):
        waits.append(options.timeout)
        now[0] += 2
        source = args[args.index('--ro-bind-try') + 1]
        return subprocess.CompletedProcess(args, 1, '', _sidecar_error(source))
    monkeypatch.setattr(run, '_run_sandbox_process_once', once)
    result = run.run_sandbox_process(argv, run.SandboxProcessOptions(budget, .1, True, tmp_path))
    assert result.returncode == 1 and result.args == argv and waits == expected


# LLM: 原shell或字符串执行形态不在侧车重试范围，不能因为stderr看似bwrap错误而改写执行方式。
# 函数用途: 锁住Shell两种明确排除的argv形态。
@pytest.mark.parametrize('use_shell', [False, True])
def test_shell_does_not_retry_shell_or_string_argv(sidecar_shell, monkeypatch, use_shell):
    tool, target, argv, popen, communicate = sidecar_shell
    argument = argv if use_shell else 'bwrap command text'
    monkeypatch.setattr(shell, '_sandbox_exec', lambda *a, **k: (argument, use_shell))
    communicate.return_value = subprocess.CompletedProcess('true', 1, '', _sidecar_error('/absent-wal'))
    shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None)
    assert popen.call_count == 1


# LLM: 只截获真实Attempt规格，不执行平台沙箱；非macOS不应生成Seatbelt正则，路径保护仍完整保留。
# 函数用途: 核验平台分支只影响正则，不丢现存、缺失或悬空链接保护。
@pytest.mark.parametrize('platform_name', ['darwin', 'linux', 'freebsd'])
def test_shell_seatbelt_patterns_are_darwin_only(tmp_path, monkeypatch, platform_name):
    from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox
    existing, missing, dangling = tmp_path / 'present', tmp_path / 'missing', tmp_path / 'alias'
    existing.write_text('state')
    dangling.symlink_to(missing)
    patterns, captured = Mock(return_value=('seatbelt-pattern',)), []
    monkeypatch.setattr(shell.sys, 'platform', platform_name)
    monkeypatch.setattr(shell, 'host_readonly_paths_for', lambda *args: (existing, missing, dangling))
    monkeypatch.setattr(shell, 'host_readonly_patterns_for', patterns)
    monkeypatch.setattr(AttemptExecutionSandbox, 'build_argv', lambda instance, command:
                        captured.append(instance.spec) or command)
    shell._sandbox_exec('true', tmp_path, None)
    spec, = captured
    expected = ('seatbelt-pattern',) if platform_name == 'darwin' else ()
    assert spec.protected_write_patterns == expected
    assert spec.protected_directory_write_patterns == expected
    assert spec.protected_directory_unlink_patterns == expected
    assert {existing.resolve(), missing} <= set(spec.protected_write_paths)
    assert patterns.call_count == (3 if platform_name == 'darwin' else 0)
    if platform_name == 'darwin':
        assert [call.kwargs for call in patterns.call_args_list] == [
            {}, {'directory_operation': 'write'}, {'directory_operation': 'unlink'}]


# LLM: 只有硬RO同源祖先允许省略子项；瞬时try-bind侧车的精确语义不能被新去重吞掉。
# 函数用途: 核验父目录覆盖下的持久文件和目录去重，侧车仍保留try-bind。
@pytest.mark.parametrize(('name', 'append'), [('runtime.db', False), ('AGENTS.md', False),
    ('directory', False), ('runtime.db-wal', True), ('state.lock', True)])
def test_readonly_child_uses_unbroken_same_source_parent(tmp_path, name, append):
    parent = tmp_path.resolve()
    child = parent / name
    child.mkdir() if name == 'directory' else child.write_text('state')
    argv = ['bwrap', '--ro-bind', str(parent), str(parent)]
    before = argv.copy()
    sandbox._append_readonly_mounts(argv, (child,))
    assert argv == (before + ['--ro-bind-try', str(child), str(child)] if append else before)


# LLM: RW、异源RO、try-bind、symlink和空覆盖均不能冒充未改变的同源硬RO祖先。
# 函数用途: 保证祖先去重不跨过改变目标子树的挂载，并忽略真正无关的兄弟挂载。
@pytest.mark.parametrize('intervening', ['parent-rw', 'child-rw', 'parent-alias', 'child-alias',
    'parent-try', 'child-symlink', 'child-tmpfs', 'unrelated'])
def test_readonly_child_rechecks_intervening_mount(tmp_path, intervening):
    parent = tmp_path.resolve()
    child = parent / 'protected'
    child.mkdir()
    argv = ['bwrap', '--ro-bind', str(parent), str(parent)]
    if intervening == 'child-tmpfs':
        argv += ['--tmpfs', str(child)]
    else:
        flag = {'parent-rw': '--bind', 'child-rw': '--bind', 'parent-alias': '--ro-bind',
                'child-alias': '--ro-bind', 'parent-try': '--ro-bind-try',
                'child-symlink': '--symlink', 'unrelated': '--bind'}[intervening]
        target = parent if intervening.startswith('parent-') else child
        if intervening == 'unrelated':
            target = parent / 'sibling'
        source = '/different-source' if 'alias' in intervening or 'symlink' in intervening else str(target)
        argv += [flag, source, str(target)]
    before = argv.copy()
    sandbox._append_readonly_mounts(argv, (child,))
    assert argv == (before if intervening == 'unrelated' else before + ['--ro-bind', str(child), str(child)])


# LLM: 真实存活路径配合三种预exec错误；只要求原参数副本，不删除或改写任何保护。
# 函数用途: 验证四类bind和绝对bwrap路径均能识别挂载竞态。
@pytest.mark.parametrize('kind', range(3))
@pytest.mark.parametrize('flag', ['--ro-bind', '--ro-bind-try', '--bind', '--bind-try'])
@pytest.mark.parametrize('executable', ['bwrap', '/usr/bin/bwrap'])
def test_mount_race_returns_same_argv_copy(tmp_path, kind, flag, executable):
    leaf = tmp_path / 'runtime.db'
    leaf.write_text('replacement')
    argv = [executable, flag, str(leaf), str(leaf), '--', 'true']
    before = argv.copy()
    retry = sandbox.retry_bwrap_mount_race_argv(argv, _sidecar_error(leaf, kind))
    assert retry == before and retry is not argv and argv == before


# LLM: 源存活判定包括目录和悬空链接；别名目的地可匹配错误，但不能因此改写源目标。
# 函数用途: 覆盖目标匹配及exists/is_symlink的两条存在性分支。
@pytest.mark.parametrize('source_kind', ['directory', 'dangling'])
def test_mount_race_matches_alias_target_and_existing_source(tmp_path, source_kind):
    source, target = tmp_path / 'source', tmp_path / 'target'
    source.mkdir() if source_kind == 'directory' else source.symlink_to(tmp_path / 'absent')
    argv = ['bwrap', '--ro-bind', str(source), str(target), '--', 'true']
    retry = sandbox.retry_bwrap_mount_race_argv(argv, _sidecar_error(target, 2))
    assert retry == argv and retry is not argv


# LLM: 不扫描用户命令的挂载形状；载荷回显bwrap错误、非bwrap入口和缺失源均不得重试。
# 函数用途: 锁住竞态helper的反伪造、挂载区边界及不完整三元组拒绝。
@pytest.mark.parametrize('case', ['empty', 'other-executable', 'wrapper', 'ordinary', 'unmatched',
    'missing', 'payload-spoof', 'payload-mount', 'incomplete', 'cross-separator', 'unsupported'])
def test_mount_race_rejects_unconfirmed_failure(tmp_path, case):
    leaf = tmp_path / 'runtime.db'
    leaf.write_text('present')
    argv = ['bwrap', '--ro-bind', str(leaf), str(leaf), '--', 'true']
    error = _sidecar_error(leaf, 2)
    if case == 'empty':
        argv = []
    elif case == 'other-executable':
        argv[0] = 'not-bwrap'
    elif case == 'wrapper':
        argv = ['python', 'landlock_launcher.py', '--', *argv]
    elif case == 'ordinary':
        error = f'ordinary failure reading {leaf}'
    elif case == 'unmatched':
        error = _sidecar_error(tmp_path / 'another-file', 2)
    elif case == 'missing':
        leaf.unlink()
    elif case == 'payload-spoof':
        argv[-1:] = ['bash', '-c', f"echo '{error}' >&2; exit 1"]
    elif case == 'payload-mount':
        argv = ['bwrap', '--', 'echo', '--ro-bind', str(leaf), str(leaf), '--', 'end']
    elif case == 'incomplete':
        argv = ['bwrap', '--ro-bind', str(leaf)]
    elif case == 'cross-separator':
        argv = ['bwrap', '--ro-bind', str(leaf), '--', str(leaf)]
    else:
        argv[1] = '--dev-bind'
    before = argv.copy()
    assert sandbox.retry_bwrap_mount_race_argv(argv, error) is None
    assert argv == before


# LLM: 不启动沙箱或宿主业务进程；提供真实主文件和真实两执行入口共享的Popen/等待替身。
# 函数用途: 把已有侧车执行夹具改为持久文件竞态输入，供集成控制流测试。
@pytest.fixture
def mount_race_shell(sidecar_shell, monkeypatch):
    tool, target, _argv, popen, communicate = sidecar_shell
    leaf = target / 'runtime.db'
    leaf.write_text('original')
    argv = ['bwrap', '--ro-bind', str(leaf), str(leaf), '--', 'true']
    monkeypatch.setattr(shell, '_sandbox_exec', lambda *a, **k: (argv.copy(), False))
    return tool, target, leaf, argv, popen, communicate


# LLM: 第一次等待实际os.replace主文件，再返回模拟bwrap错误；两入口仍走真实外层重试和原清理。
# 函数用途: 验证主文件替换后原argv重试成功，预算、结果事实和管道关闭均保留。
@pytest.mark.parametrize('entry', ['shell', 'attempt'])
@pytest.mark.parametrize('kind', range(3))
def test_mount_race_retries_after_atomic_replace(mount_race_shell, monkeypatch, entry, kind):
    from agent_py_agent.agent.attempt import process_run as run
    tool, target, leaf, argv, popen, communicate = mount_race_shell
    now, waits = [100.0], []
    original_inode = leaf.stat().st_ino
    success = subprocess.CompletedProcess(argv, 0, 'done', '')
    success.capture, success.termination = {'complete': True}, object()
    capture = Mock()
    monkeypatch.setattr(shell.time, 'monotonic', lambda: now[0])
    # LLM: 模拟第一次bwrap预exec失败，真实替换只作用于pytest临时文件，不执行用户载荷。
    # 函数用途: 记录两个入口的剩余期限，在首次错误后交回同一个成功对象。
    def finish(*args, **kwargs):
        waits.append(kwargs['timeout'] if entry == 'shell' else args[2])
        now[0] += 2
        if len(waits) == 1:
            replacement = leaf.with_suffix('.new')
            replacement.write_text('replacement')
            replacement.replace(leaf)
            return subprocess.CompletedProcess(argv, 1, '', _sidecar_error(leaf, kind))
        return success
    if entry == 'shell':
        communicate.side_effect = finish
        result = shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None)
    else:
        monkeypatch.setattr(run, 'ProcessOutputCapture', lambda proc: capture)
        monkeypatch.setattr(run, '_wait_for_process', finish)
        result = run.run_sandbox_process(argv, run.SandboxProcessOptions(10, .1, True, target))
        assert capture.finish.call_count == 2
        assert popen.return_value.stdout.close.call_count == 2
        assert popen.return_value.stderr.close.call_count == 2
    assert result is success and result.args == argv and result.capture == {'complete': True}
    assert result.termination is success.termination and waits == [10, 8]
    assert [call.args[0] for call in popen.call_args_list] == [argv, argv]
    assert leaf.stat().st_ino != original_inode and leaf.read_text() == 'replacement'
    assert all(call.kwargs['stdin'] == subprocess.DEVNULL and call.kwargs['start_new_session']
               for call in popen.call_args_list)


# LLM: 用真实竞态helper验证两入口的四次启动及总期限，不依赖假裁剪器让循环继续。
# 函数用途: 确保持久文件一直存活且一直挂载失败时，也不能无限重试或重置预算。
@pytest.mark.parametrize('entry', ['shell', 'attempt'])
@pytest.mark.parametrize(('budget', 'expected'), [(10, [10, 8, 6, 4]), (1, [1])])
def test_mount_race_retry_limit_and_budget(mount_race_shell, monkeypatch, entry, budget, expected):
    from agent_py_agent.agent.attempt import process_run as run
    tool, target, leaf, argv, popen, communicate = mount_race_shell
    now, waits = [100.0], []
    monkeypatch.setattr(shell.time, 'monotonic', lambda: now[0])
    # LLM: 仅模拟已清理的预exec错误并推进两秒；生产外层控制预算和启动次数。
    # 函数用途: 为两入口提供可确定的重复挂载失败。
    def finish(*args, **kwargs):
        waits.append(kwargs['timeout'] if entry == 'shell' else args[2])
        now[0] += 2
        return subprocess.CompletedProcess(argv, 1, '', _sidecar_error(leaf, 2))
    if entry == 'shell':
        communicate.side_effect = finish
        result = shell._run_attempt_sandboxed_shell_command(tool, 'true', target, budget, None, None, None)
    else:
        monkeypatch.setattr(run, 'ProcessOutputCapture', lambda proc: Mock())
        monkeypatch.setattr(run, '_wait_for_process', finish)
        result = run.run_sandbox_process(argv, run.SandboxProcessOptions(budget, .1, True, target))
    assert result.returncode == 1 and waits == expected
    assert [call.args[0] for call in popen.call_args_list] == [argv] * len(expected)


# LLM: 主文件存在不等于命令没执行；有stdout、成功、普通错误或payload伪造均不重放业务。
# 函数用途: 验证新fallback不能绕过两入口原本的执行结果拒重试边界。
@pytest.mark.parametrize('entry', ['shell', 'attempt'])
@pytest.mark.parametrize('case', ['success', 'stdout', 'ordinary', 'spoof'])
def test_mount_race_entry_rejects_command_results(mount_race_shell, monkeypatch, entry, case):
    from agent_py_agent.agent.attempt import process_run as run
    tool, target, leaf, argv, popen, communicate = mount_race_shell
    error = _sidecar_error(leaf, 2)
    if case == 'spoof':
        argv[-1:] = ['bash', '-c', f"echo '{error}' >&2; exit 1"]
    result = subprocess.CompletedProcess(argv, 0 if case == 'success' else 1,
        'already ran' if case == 'stdout' else '', 'ordinary failure' if case == 'ordinary' else error)
    if entry == 'shell':
        communicate.return_value = result
        actual = shell._run_attempt_sandboxed_shell_command(tool, 'true', target, 10, None, None, None)
    else:
        monkeypatch.setattr(run, 'ProcessOutputCapture', lambda proc: Mock())
        monkeypatch.setattr(run, '_wait_for_process', lambda *args: result)
        actual = run.run_sandbox_process(argv, run.SandboxProcessOptions(10, .1, True, target))
    assert actual is result and popen.call_count == 1
