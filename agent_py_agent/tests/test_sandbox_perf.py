# LLM: 只在临时Owner树构造真实沙箱参数，不把argv模拟等同于Linux内核隔离验证。
# 模块用途: 守住多用户沙箱构参性能、挂载体积和原安全覆盖。
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

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
