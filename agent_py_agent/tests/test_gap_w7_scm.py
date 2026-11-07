"""SCM 使用真实临时 Git 仓库和确定性边界替身；不访问网络和真实 home。"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.business_spaces import gap158_w7_scm as scm
from agent_py_agent.agent.gateway_parts.business_api import bind_business_context
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError


# LLM: 测试 Git 只在临时根执行，环境不读取用户全局配置。
# 函数用途: 初始化和检查隔离仓库。
def git(root, *args):
    """初始化仅使用测试根，不读取真实全局 Git 配置。"""
    return subprocess.run(['git', '-C', str(root), *args], check=True,
        env={'PATH': os.defpath, 'HOME': str(root.parent), 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': os.devnull, 'LC_ALL': 'C'}, capture_output=True).stdout


# LLM: 保留真实 Git，仅在原单元场景注入可调上界和 FS 接缝；集成场景恢复生产接口。
# 函数用途: 为每个测试建立独立仓库、身份、时钟和状态。
@pytest.fixture
def fixture(tmp_path, monkeypatch):
    """W0 未接入时只注入测试上界和两个宿主接缝，不伪造 limits/fs 模块。"""
    owner = tmp_path / 'owner'
    workspace = owner / 'workspace'
    root = workspace / 'repo'
    root.mkdir(parents=True)
    git(root, 'init', '-b', 'main')
    git(root, 'config', 'user.name', '测试作者')
    git(root, 'config', 'user.email', 'test@example.invalid')
    (root / 'a.txt').write_text('original\n')
    git(root, 'add', '--', 'a.txt')
    git(root, 'commit', '-m', 'first')
    bounds = SimpleNamespace(SCM_MAX_SUBSCRIPTIONS_PER_OWNER=3, SCM_MAX_STATUS_ENTRIES=32,
        SCM_MAX_LOG_ENTRIES=5, SCM_MAX_PATHS_PER_ACTION=8, SCM_GIT_TIMEOUT_SECONDS=2,
        SCM_MIN_REFRESH_INTERVAL_SECONDS=1)
    production_limits, production_fs = scm._limits, scm._fs
    monkeypatch.setattr(scm, '_limits', lambda: bounds)
    monkeypatch.setattr(scm, '_subscriptions', {})
    monkeypatch.setattr(scm, '_owner_states', {}, raising=False)
    now, epoch = [100.0], [1]
    fs = SimpleNamespace(workspace_root=lambda call: Path(call.context.host.config.workspace_root),
                         current_epoch=lambda call: epoch[0])
    monkeypatch.setattr(scm, '_fs', lambda: fs)
    paths = SimpleNamespace(owner_id='local/main', owner_home_dir=owner,
                            owner_workspace_dir=workspace, root=tmp_path)
    host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(
        business_spaces_enabled=True, workspace_root=str(workspace), path_access_mode='normal',
        path_dangerous_roots=[]))
    context = bind_business_context(host, authorize=lambda action, facts: True)
    services = SimpleNamespace(owner_id='local/main')
    counter = [0]

    # LLM: 每次调用由可信上下文创建身份，序号键不会与其他测试交叉。
    # 函数用途: 组装本测试的领域请求。
    def call(action, params, *, key=None):
        counter[0] += 1
        return V13Call('scm.' + action, params, 'test-request', 'local/main', 'host',
            context, services, owner, key or 'scm_test_key_' + str(counter[0]).zfill(8),
            clock=lambda: now[0])

    return SimpleNamespace(root=root, workspace=workspace, owner=owner, call=call,
        now=now, epoch=epoch, bounds=bounds, host=host, fs=fs,
        production_limits=production_limits, production_fs=production_fs)


# LLM: 使用真实发行的调用上下文，不绕过领域授权。
# 函数用途: 创建标准测试仓库的订阅。
def subscription(fixture):
    return scm.subscribe(fixture.call('subscribe', {'repo_root': 'repo'}))


# LLM: 实际 Git 状态变化、节流与 DTO 深拷贝共同限定序号语义。
# 函数用途: 核验真实仓库投影只在摘要变化时递增。
def test_real_projection_seq_throttle_and_dto(fixture):
    created = subscription(fixture)
    assert set(created) == {'subscription_id', 'repo_seq', 'repository'}
    repo = created['repository']
    assert repo['branch'] == 'main'
    assert repo['committed'][0]['subject'] == 'first'
    assert not repo['working'] and not repo['staged']
    sid, seq = created['subscription_id'], created['repo_seq']
    (fixture.root / 'new space\nfile.txt').write_text('new')
    params = {'subscription_id': sid, 'after_seq': seq}
    assert scm.events(fixture.call('events', params))['events'] == []
    fixture.now[0] += 2
    changed = scm.events(fixture.call('events', params))
    assert changed['events'][0]['repo_seq'] == seq + 1
    assert changed['events'][0]['repository']['working'][0]['path'] == 'new space\nfile.txt'
    fixture.now[0] += 2
    assert scm.events(fixture.call('events', {'subscription_id': sid,
        'after_seq': changed['next_seq']}))['events'] == []
    # 只读 DTO 不能让调用方通过修改返回对象污染内部投影。
    changed['events'][0]['repository']['working'].clear()
    assert scm._subscriptions[sid].repository['working']


# LLM: 恶意钩子存在但不得执行，不能只断言命令行外观。
# 函数用途: 运行实际暂存、取消暂存和丢弃全链路。
def test_stage_unstage_discard_real_and_no_hooks(fixture):
    marker = fixture.owner / 'HOOK_RAN'
    hookdir = fixture.owner / 'untrusted-hooks'
    hookdir.mkdir()
    for name in ('post-checkout', 'pre-commit', 'post-index-change'):
        hook = hookdir / name
        hook.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 99\n')
        hook.chmod(0o700)
    git(fixture.root, 'config', 'core.hooksPath', str(hookdir))
    git(fixture.root, 'config', 'core.fsmonitor', str(hookdir / 'post-checkout'))
    sub = subscription(fixture)
    sid = sub['subscription_id']
    (fixture.root / 'a.txt').write_text('edited\n')
    staged = scm.stage(fixture.call('stage', {'subscription_id': sid, 'paths': ['a.txt']}))
    assert staged['succeeded'] == ['a.txt'] and not staged['failed']
    assert scm._subscriptions[sid].repository['staged'][0]['path'] == 'a.txt'
    unstaged = scm.unstage(fixture.call('unstage', {'subscription_id': sid, 'paths': ['a.txt']}))
    assert unstaged['succeeded'] == ['a.txt']
    assert not scm._subscriptions[sid].repository['staged']
    discarded = scm.discard(fixture.call('discard', {'subscription_id': sid,
        'paths': ['a.txt'], 'expected_repo_seq': unstaged['repo_seq']}))
    assert discarded['succeeded'] == ['a.txt']
    assert (fixture.root / 'a.txt').read_text() == 'original\n'
    assert not marker.exists()


# LLM: 不合格字面路径必须在任何 Git 调用前失败。
# 函数用途: 覆盖跨平台路径注入和内部目录边界。
@pytest.mark.parametrize('path', ['..', '../else', '/tmp/outside', '-danger', '.git/HEAD',
    'd/.Git/config', 'd/../a.txt', 'a\x00b', 'a//b', 'a/./b', 'd/-option', 'C:\\outside', 'C:/outside'])
def test_lexical_rejection_before_git(fixture, monkeypatch, path):
    sub = subscription(fixture)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('坏路径不能启动 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    result = scm.stage(fixture.call('stage', {'subscription_id': sub['subscription_id'], 'paths': [path]}))
    assert result['succeeded'] == []
    assert result['failed'] == [{'path': path, 'code': 'PATH_OUTSIDE_WORKSPACE'}]
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        scm.subscribe(fixture.call('subscribe', {'repo_root': path}))


# LLM: 仓库、父目录和叶链接都不得扩权，批次内安全文件仍可成功。
# 函数用途: 核验真实软链接拒绝与逐路径结果。
def test_symlink_leaf_parent_repo_and_partial_failure(fixture, monkeypatch):
    outside = fixture.owner / 'outside'
    outside.mkdir()
    (outside / 'secret').write_text('secret')
    (fixture.root / 'link').symlink_to(outside / 'secret')
    (fixture.root / 'parent-link').symlink_to(outside, target_is_directory=True)
    (fixture.workspace / 'repo-link').symlink_to(fixture.root, target_is_directory=True)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        scm.subscribe(fixture.call('subscribe', {'repo_root': 'repo-link'}))
    sub = subscription(fixture)
    actual, seen = scm._git, []
    # LLM: 只观察真实 Git 参数，不替代授权、路径检查或执行结果。
    # 函数用途: 记录本测试实际启动的 Git 命令。
    def observe(fd, args, hooks):
        seen.append(args)
        return actual(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', observe)
    result = scm.stage(fixture.call('stage', {'subscription_id': sub['subscription_id'],
        'paths': ['link', 'parent-link/secret', '../bad', 'a.txt', 'missing.txt']}))
    assert result['succeeded'] == ['a.txt']
    assert {row['path'] for row in result['failed']} == {'link', 'parent-link/secret', '../bad', 'missing.txt'}
    assert all('link' not in args and 'parent-link/secret' not in args and '../bad' not in args for args in seen)
    assert (outside / 'secret').read_text() == 'secret'


# LLM: 外来 owner、伪造上下文和过期代次不能借已知编号访问资源。
# 函数用途: 检查订阅隔离和撤销行为。
def test_owner_isolation_epoch_revocation_and_forgery(fixture):
    sub = subscription(fixture)
    call = fixture.call('events', {'subscription_id': sub['subscription_id'], 'after_seq': 0})
    with pytest.raises(HostAPIError):
        scm.events(replace(call, owner_id='local/foreign'))
    foreign_owner = fixture.owner.parent / 'other'
    foreign_owner.mkdir()
    paths = SimpleNamespace(owner_id='local/other', owner_home_dir=foreign_owner,
        owner_workspace_dir=foreign_owner, root=fixture.owner.parent)
    foreign_host = SimpleNamespace(home_paths=paths, config=fixture.host.config)
    ctx = bind_business_context(foreign_host, authorize=lambda action, facts: True)
    foreign = replace(call, owner_id='local/other', owner_home=foreign_owner,
        context=ctx, services=SimpleNamespace(owner_id='local/other'))
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        scm.events(foreign)
    assert scm.unsubscribe(replace(foreign, action='scm.unsubscribe')) == {'removed': False}
    with pytest.raises(HostAPIError):
        scm.events(replace(call, context=SimpleNamespace(host=fixture.host)))
    fixture.epoch[0] += 1
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        scm.events(call)
    assert scm.unsubscribe(fixture.call('unsubscribe', {'subscription_id': sub['subscription_id']})) == {'removed': False}


# LLM: 丢弃不能使用节流缓存作 CAS，冲突时不得发生写入。
# 函数用途: 核验强制刷新与过期序号拒绝。
def test_discard_force_cas_refresh_even_when_throttled(fixture, monkeypatch):
    sub = subscription(fixture)
    (fixture.root / 'a.txt').write_text('changed\n')
    actual, writes = scm._git, []
    # LLM: 只观察真实 Git 参数，不替代授权、路径检查或执行结果。
    # 函数用途: 记录本测试实际启动的 Git 命令。
    def observe(fd, args, hooks):
        if args[0] == 'checkout':
            writes.append(args)
        return actual(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', observe)
    with pytest.raises(HostAPIError) as caught:
        scm.discard(fixture.call('discard', {'subscription_id': sub['subscription_id'],
            'paths': ['a.txt'], 'expected_repo_seq': sub['repo_seq']}))
    assert caught.value.code == 'REVISION_CONFLICT'
    assert caught.value.current_repo_seq > sub['repo_seq']
    assert not writes
    assert (fixture.root / 'a.txt').read_text() == 'changed\n'


# LLM: 写超时后幂等重放不能再次执行，变更参数必须冲突。
# 函数用途: 检验未知结果封存与重放。
def test_timeout_unknown_replay_and_idempotency_conflict(fixture, monkeypatch):
    sub = subscription(fixture)
    actual, writes = scm._git, []
    # LLM: 只在写命令边界制造超时，保留真实状态读取路径。
    # 函数用途: 确定性记录一次未知写结果。
    def timeout(fd, args, hooks):
        if args[0] == 'checkout':
            writes.append(args)
            raise subprocess.TimeoutExpired(args, 2)
        return actual(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', timeout)
    params = {'subscription_id': sub['subscription_id'], 'paths': ['a.txt'],
              'expected_repo_seq': sub['repo_seq']}
    call = fixture.call('discard', params, key='stable_discard_timeout_key')
    for _ in range(2):
        with pytest.raises(HostAPIError) as caught:
            scm.discard(call)
        assert caught.value.code == 'OUTCOME_UNKNOWN'
        assert caught.value.outcome == 'unknown'
    assert len(writes) == 1
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        scm.discard(replace(call, params={**params, 'paths': ['other']}))


# LLM: 观察真实启动入口，固定 argv、空钩子与环境隔离都必须成立。
# 函数用途: 检查进程约束和成功结果幂等重放。
def test_fixed_argv_empty_hooks_sanitized_env_and_replay(fixture, monkeypatch):
    original, seen = scm.subprocess.Popen, []
    # LLM: 观察真实 Popen 参数后仍使用原启动器，不用替身冒充端到端结果。
    # 函数用途: 检查进程环境、空钩子与固定命令行。
    def spawn(command, **kwargs):
        assert 'shell' not in kwargs or kwargs['shell'] is False
        assert kwargs['env']['GIT_CONFIG_GLOBAL'] == os.devnull
        assert kwargs['env']['GIT_OPTIONAL_LOCKS'] == '0'
        assert 'HOME' not in kwargs['env'] and 'GIT_CONFIG_COUNT' not in kwargs['env']
        gitargs = command[4:]
        assert gitargs[0] == 'git'
        hookarg = next(arg for arg in gitargs if arg.startswith('core.hooksPath='))
        hookroot = Path(hookarg.split('=', 1)[1])
        assert hookroot.is_dir() and list(hookroot.iterdir()) == []
        assert 'core.fsmonitor=false' in gitargs
        assert '--literal-pathspecs' in gitargs
        assert '--no-pager' in gitargs
        seen.append(gitargs)
        return original(command, **kwargs)
    monkeypatch.setattr(scm.subprocess, 'Popen', spawn)
    subscribe_call = fixture.call('subscribe', {'repo_root': 'repo'}, key='replay_subscribe_123')
    sub = scm.subscribe(subscribe_call)
    before = len(seen)
    assert scm.subscribe(subscribe_call) == sub
    assert len(seen) == before
    (fixture.root / 'a.txt').write_text('edited\n')
    params = {'subscription_id': sub['subscription_id'], 'paths': ['a.txt']}
    stage_call = fixture.call('stage', params, key='replay_stage_key_123')
    staged = scm.stage(stage_call)
    before = len(seen)
    assert scm.stage(stage_call) == staged
    assert len(seen) == before
    unstaged = scm.unstage(fixture.call('unstage', params))
    scm.discard(fixture.call('discard', {**params, 'expected_repo_seq': unstaged['repo_seq']}))
    assert any(args[-3:] == ['add', '--', 'a.txt'] for args in seen)
    assert any(args[-4:] == ['restore', '--staged', '--', 'a.txt'] for args in seen)
    assert any(args[-3:] == ['checkout', '--', 'a.txt'] for args in seen)
    assert any(args[-6:-2] == ['status', '--porcelain=v2', '-z', '--branch'] for args in seen)
    assert any('log' in args and args[args.index('log') + 1:args.index('log') + 3] == ['-n', '5'] for args in seen)


# LLM: 可执行仓库配置必须在进程启动前拒绝，不能靠钩子开关兜底。
# 函数用途: 覆盖恶意配置节的失败关闭。
@pytest.mark.parametrize('entry', ['filter', 'include', 'diff', 'extensions'])
def test_executable_repository_config_rejected_before_git(fixture, monkeypatch, entry):
    marker = fixture.owner / 'EXECUTED'
    with (fixture.root / '.git' / 'config').open('a') as stream:
        stream.write('\n[' + entry + ' "untrusted_filter"]\n command = touch ' + str(marker) + '\n')
    (fixture.root / '.gitattributes').write_text('* filter=untrusted_filter diff=untrusted_filter\n')
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('可执行仓库配置不允许进入 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        subscription(fixture)
    assert not marker.exists()


# LLM: 上界截断需显式暴露，订阅与路径配额不得被绕过。
# 函数用途: 检查状态、事件和写入批次的资源限制。
def test_status_bounds_subscription_cap_paths_cap_and_overflow(fixture):
    fixture.bounds.SCM_MAX_STATUS_ENTRIES = 2
    for name in ('b', 'c', 'd'):
        (fixture.root / name).write_text('x')
    sub = subscription(fixture)
    assert sub['repository']['truncated'] is True
    assert len(sub['repository']['working']) == 2
    sid = sub['subscription_id']
    for name in ('b', 'c', 'd'):
        (fixture.root / name).unlink()
        fixture.now[0] += 2
        scm.events(fixture.call('events', {'subscription_id': sid, 'after_seq': 0}))
    result = scm.events(fixture.call('events', {'subscription_id': sid, 'after_seq': 0}))
    assert result['overflow'] is True and result['events'][0]['kind'] == 'overflow'
    assert 'repository' in result['events'][0]
    # 事件容量与控制回执共用 W0 上界；独立提高回执容量后验证订阅配额。
    fixture.bounds.SCM_MAX_STATUS_ENTRIES = 32
    subscription(fixture)
    subscription(fixture)
    with pytest.raises(HostAPIError, match='LIMIT_EXCEEDED'):
        subscription(fixture)
    with pytest.raises(HostAPIError, match='INVALID_SCHEMA'):
        scm.stage(fixture.call('stage', {'subscription_id': sid, 'paths': ['a.txt'] * 9}))


# LLM: 日志项数和字符串宽度同时有界，超额需要 truncated 标记。
# 函数用途: 验证提交投影不会收集无界内容。
def test_log_parser_has_entry_and_string_bounds(fixture, monkeypatch):
    sub = subscription(fixture)
    actual = scm._git
    # LLM: 仅对日志注入超量输出，其他命令保留真实执行。
    # 函数用途: 产生确定性的项数和字符串宽度边界。
    def log_output(fd, args, hooks):
        if args[0] == 'log':
            record = (b'a' * 40 + b'\x00' + b'x' * 600 + b'\x00' + b'y' * 300 + b'\x00100\x00')
            return record * 20, False, 0
        return actual(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', log_output)
    fixture.now[0] += 2
    result = scm.events(fixture.call('events', {'subscription_id': sub['subscription_id'], 'after_seq': 0}))
    repo = result['events'][-1]['repository']
    assert len(repo['committed']) == 5
    assert len(repo['committed'][0]['subject']) == 500
    assert len(repo['committed'][0]['author']) == 256
    assert repo['truncated'] is True


# LLM: 用真实进程验证双管道预算和截止时间，而非只测替身返回值。
# 函数用途: 核验过量输出终止与超时终止。
def test_real_pipe_budget_and_real_timeout(fixture, monkeypatch):
    original = scm.subprocess.Popen
    budget = scm._output_budget()
    # LLM: 真实子进程输出超过预算，父进程必须截断并终止它。
    # 函数用途: 生成实际管道过量输出。
    def noisy(command, **kwargs):
        return original([sys.executable, '-c',
            'import os; os.write(1, b"x" * ' + str(budget * 2) + ')'], **kwargs)
    descriptor = os.open(fixture.root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(scm.subprocess, 'Popen', noisy)
        raw, truncated, _ = scm._git(descriptor, ['status', '--porcelain=v2', '-z', '--branch'], str(fixture.owner))
        assert len(raw) == budget and truncated is True
        # LLM: 子进程真实阻塞，让生产截止时间和清理逻辑负责终止。
        # 函数用途: 产生可重复的管道超时。
        def blocked(command, **kwargs):
            return original([sys.executable, '-c', 'import signal; signal.pause()'], **kwargs)
        monkeypatch.setattr(scm.subprocess, 'Popen', blocked)
        fixture.bounds.SCM_GIT_TIMEOUT_SECONDS = 0.05
        with pytest.raises(subprocess.TimeoutExpired):
            scm._git(descriptor, ['status', '--porcelain=v2', '-z', '--branch'], str(fixture.owner))
    finally:
        os.close(descriptor)


# LLM: 策略拒绝、目录与多链接叶都不得进入 Git 写命令。
# 函数用途: 覆盖逐叶写入安全门。
def test_write_policy_deny_directory_and_hardlink_no_git(fixture, monkeypatch):
    sub = subscription(fixture)
    (fixture.root / 'dir').mkdir()
    # 多链接元数据用确定性替身核验；隔离运行器不授予真实 link(2) 创建权限。
    (fixture.root / 'hardlink').write_text('simulated multi-link leaf')
    original_stat = scm._leaf_info
    # LLM: 只注入或观察局部叶元数据，不替换平台级 nofollow 能力检测。
    # 函数用途: 提供本测试需要的确定性文件类型或覆盖证据。
    def metadata(parent, name):
        info = original_stat(parent, name)
        if name == 'hardlink':
            fields = list(info)
            fields[3] = 2
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(scm, '_leaf_info', metadata)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('不合格叶不能启动 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    result = scm.stage(fixture.call('stage', {'subscription_id': sub['subscription_id'],
        'paths': ['dir', 'hardlink']}))
    assert len(result['failed']) == 2 and not result['succeeded']
    original = scm._policy
    # LLM: 只改变写裁决，不绕过真实路径读取安全门。
    # 函数用途: 模拟当前策略拒绝写入。
    def policy(call):
        value = original(call)
        return SimpleNamespace(check=value.check,
            check_write=lambda path: SimpleNamespace(allowed=False))
    monkeypatch.setattr(scm, '_policy', policy)
    result = scm.stage(fixture.call('stage', {'subscription_id': sub['subscription_id'], 'paths': ['blocked']}))
    assert result['failed'] == [{'path': 'blocked', 'code': 'PATH_OUTSIDE_WORKSPACE'}]


# LLM: 已移除订阅的原创建回执仍存在，不可重放创建第二个资源。
# 函数用途: 检查控制回执在删除后的生命周期。
def test_unsubscribe_and_subscribe_key_do_not_recreate_removed_subscription(fixture, monkeypatch):
    call = fixture.call('subscribe', {'repo_root': 'repo'}, key='subscribe_once_key_123')
    sub = scm.subscribe(call)
    remove_call = fixture.call('unsubscribe', {'subscription_id': sub['subscription_id']},
                               key='unsubscribe_once_key_123')
    assert scm.unsubscribe(remove_call) == {'removed': True}
    assert scm.unsubscribe(remove_call) == {'removed': True}
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('重放控制回执不能重新创建订阅')
    monkeypatch.setattr(scm, '_git', forbidden)
    assert scm.subscribe(call) == sub
    assert sub['subscription_id'] not in scm._subscriptions
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        scm.subscribe(replace(call, params={'repo_root': 'other'}))


# LLM: 每次重建真实发行的上下文，禁止以复用同一 host 掩盖请求级缓存丢失。
# 函数用途: 模拟同一 owner 的下一次独立请求宿主。
def fresh_request(call):
    host = SimpleNamespace(home_paths=call.context.host.home_paths, config=call.context.host.config)
    context = bind_business_context(host, authorize=lambda action, facts: True)
    return replace(call, context=context)


# LLM: 删除订阅后，控制回执仍必须跨请求保存；重放不授予新的资源。
# 函数用途: 检查独立 host 上订阅和取消订阅的幂等性。
def test_regression_control_replay_across_request_hosts(fixture, monkeypatch):
    create = fixture.call('subscribe', {'repo_root': 'repo'}, key='regression_create_123')
    result = scm.subscribe(create)
    remove = fixture.call('unsubscribe', {'subscription_id': result['subscription_id']},
                          key='regression_remove_123')
    assert scm.unsubscribe(fresh_request(remove)) == {'removed': True}
    assert scm.unsubscribe(fresh_request(remove)) == {'removed': True}
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('控制重放不允许再次启动 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    assert scm.subscribe(fresh_request(create)) == result
    assert result['subscription_id'] not in scm._subscriptions
    with pytest.raises(HostAPIError, match='IDEMPOTENCY_CONFLICT'):
        scm.subscribe(fresh_request(replace(create, params={'repo_root': 'different'})))


# LLM: CAS 当前值必须穿过真实的错误详情投影，不靠自定义异常属性自证。
# 函数用途: 验证过期 discard 的可运输结构化当前序号。
def test_regression_cas_error_details_transport_mapping(fixture):
    from agent_py_agent.agent.gateway_parts.business_api import _error_details
    result = subscription(fixture)
    (fixture.root / 'a.txt').write_text('stale state\n')
    with pytest.raises(HostAPIError) as caught:
        scm.discard(fixture.call('discard', {'subscription_id': result['subscription_id'],
            'paths': ['a.txt'], 'expected_repo_seq': result['repo_seq']}))
    assert caught.value.code == 'REVISION_CONFLICT'
    current = scm._subscriptions[result['subscription_id']].repo_seq
    assert current > result['repo_seq']
    assert _error_details(caught.value) == [{'key': 'current_repo_seq', 'value': current}]
    assert (fixture.root / 'a.txt').read_text() == 'stale state\n'


# LLM: refs 和 objects 更深层的链接与直接链接同样不能交给 Git 跟随。
# 函数用途: 在启动前拒绝嵌套元数据软链接。
@pytest.mark.parametrize(('relative', 'directory'), [
    ('refs/heads/nested/main', False), ('objects/ab/nested/object', False),
    ('refs/heads/foreign', True), ('objects/ab/foreign', True)])
def test_regression_nested_metadata_symlink_before_git(fixture, monkeypatch, relative, directory):
    target = fixture.owner / 'external-metadata'
    if directory:
        target.mkdir()
        (target / 'secret').write_text('external metadata')
    else:
        target.write_text('external metadata')
    entry = fixture.root / '.git' / relative
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.symlink_to(target, target_is_directory=directory)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('嵌套元数据链接必须在启动 Git 前拒绝')
    monkeypatch.setattr(scm, '_git', forbidden)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        subscription(fixture)
    assert (target / 'secret' if directory else target).read_text() == 'external metadata'


# LLM: 顶层 owner 注册表有界且不得用淘汰现有回执给新 owner 腾位。
# 函数用途: 检查跨 owner 容量拒绝不会破坏已保存控制结果。
def test_regression_owner_registry_capacity_does_not_evict(fixture):
    call = fixture.call('subscribe', {'repo_root': 'repo'}, key='regression_owner_cap_create')
    created = scm.subscribe(call)
    fixture.bounds.SCM_MAX_STATUS_ENTRIES = 1
    home = fixture.owner.parent / 'another-owner'
    home.mkdir()
    paths = SimpleNamespace(owner_id='local/next', owner_home_dir=home,
        owner_workspace_dir=home, root=fixture.owner.parent)
    host = SimpleNamespace(home_paths=paths, config=fixture.host.config)
    context = bind_business_context(host, authorize=lambda action, facts: True)
    foreign = replace(call, context=context, owner_id='local/next', owner_home=home,
        services=SimpleNamespace(owner_id='local/next'))
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        scm.subscribe(foreign)
    assert len(scm._owner_states) == 1
    assert scm.subscribe(fresh_request(call)) == created


# LLM: 全局字典仅作已知编号镜像，任何扫描都会使本测试立即失败。
# 函数用途: 验证授权、订阅配额和撤销只访问本 owner 索引。
def test_regression_owner_index_never_scans_global_subscriptions(fixture, monkeypatch):
    class NoScan(dict):
        # LLM: 测试接缝不允许隐式全局遍历。
        # 函数用途: 拦住 items 全表扫描。
        def items(self):
            pytest.fail('不能全局扫描其他 owner 的订阅')

        # LLM: 配额统计也不得全表扫描。
        # 函数用途: 拦住 values 全表扫描。
        def values(self):
            pytest.fail('不能全局计算其他 owner 的订阅配额')

        # LLM: 禁止用迭代键换写法绕过同一约束。
        # 函数用途: 拦住隐式全表扫描。
        def __iter__(self):
            pytest.fail('不能隐式扫描所有订阅编号')

    monkeypatch.setattr(scm, '_subscriptions', NoScan())
    created = subscription(fixture)
    sid = created['subscription_id']
    assert scm.events(fixture.call('events', {'subscription_id': sid, 'after_seq': 0}))['events']
    fixture.epoch[0] += 1
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        scm.events(fixture.call('events', {'subscription_id': sid, 'after_seq': 0}))
    assert sid not in scm._subscriptions
    assert subscription(fixture)['subscription_id'] != sid


# LLM: 容量满后原回执仍可重放；不能以 LRU 淘汰让副作用再次发生。
# 函数用途: 验证稳定控制回执的容量与代次切换。
def test_regression_control_capacity_preserves_replay_and_epoch(fixture, monkeypatch):
    fixture.bounds.SCM_MAX_STATUS_ENTRIES = 2
    create = fixture.call('subscribe', {'repo_root': 'repo'}, key='regression_capacity_create')
    result = scm.subscribe(create)
    remove = fixture.call('unsubscribe', {'subscription_id': result['subscription_id']},
                          key='regression_capacity_remove')
    assert scm.unsubscribe(remove) == {'removed': True}
    actual, seen = scm._git, []
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        seen.append(args)
        pytest.fail('容量拒绝或重放不能进入 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    assert scm.subscribe(fresh_request(create)) == result
    assert scm.unsubscribe(fresh_request(remove)) == {'removed': True}
    with pytest.raises(HostAPIError, match='LIMIT_EXCEEDED'):
        subscription(fixture)
    with pytest.raises(HostAPIError, match='OUTCOME_UNKNOWN'):
        scm.unsubscribe(fixture.call('unsubscribe', {'subscription_id': 'missing'}))
    assert not seen
    monkeypatch.setattr(scm, '_git', actual)
    fixture.epoch[0] += 1
    replacement = scm.subscribe(fresh_request(create))
    assert replacement['subscription_id'] != result['subscription_id']


# LLM: 写入未知后持续占用回执槽，跨请求重放必须停在领域缓存而非重新执行。
# 函数用途: 验证未知写回执不因容量或新 host 丢失。
def test_regression_unknown_write_cross_host_capacity_no_retry(fixture, monkeypatch):
    result = subscription(fixture)
    fixture.bounds.SCM_MAX_STATUS_ENTRIES = 2
    actual, writes = scm._git, []
    # LLM: 只在写命令边界制造超时，保留真实状态读取路径。
    # 函数用途: 确定性记录一次未知写结果。
    def timeout(fd, args, hooks):
        if args[0] == 'checkout':
            writes.append(args)
            raise subprocess.TimeoutExpired(args, 2)
        return actual(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', timeout)
    call = fixture.call('discard', {'subscription_id': result['subscription_id'],
        'paths': ['a.txt'], 'expected_repo_seq': result['repo_seq']},
        key='regression_unknown_write')
    for current in (call, fresh_request(call), fresh_request(call)):
        with pytest.raises(HostAPIError) as caught:
            scm.discard(current)
        assert caught.value.code == 'OUTCOME_UNKNOWN'
        assert caught.value.outcome == 'unknown'
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        scm.stage(fixture.call('stage', {'subscription_id': result['subscription_id'], 'paths': ['a.txt']}))
    assert len(writes) == 1


# LLM: 文件系统不授真实 link/mkfifo 时，使用局部 stat 接缝核验同一拒绝分支。
# 函数用途: 检查嵌套多链接、特殊文件和跨设备元数据都在 Git 前拒绝。
@pytest.mark.parametrize('kind', ['hardlink', 'special', 'external_device'])
def test_regression_nested_metadata_unsafe_inode_before_git(fixture, monkeypatch, kind):
    entry = fixture.root / '.git' / 'objects' / 'ab' / 'nested' / 'unsafe'
    entry.parent.mkdir(parents=True)
    entry.write_text('metadata')
    original = scm._leaf_info
    # LLM: 只注入或观察局部叶元数据，不替换平台级 nofollow 能力检测。
    # 函数用途: 提供本测试需要的确定性文件类型或覆盖证据。
    def metadata(parent, name):
        info = original(parent, name)
        if name == 'unsafe':
            fields = list(info)
            if kind == 'hardlink':
                fields[3] = 2
            elif kind == 'special':
                fields[0] = stat.S_IFIFO | 0o600
            else:
                fields[2] += 1
            return os.stat_result(fields)
        return info
    monkeypatch.setattr(scm, '_leaf_info', metadata)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('不安全元数据 inode 不能进入 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        subscription(fixture)


# LLM: 枚举只允许预算加一次探测，不把未知树伪装成已检查完。
# 函数用途: 验证 metadata 超预算失败关闭且不会启动 Git。
def test_regression_metadata_scan_budget_fail_closed(fixture, monkeypatch):
    fixture.bounds.FS_WATCH_MAX_ENTRIES_PER_SCAN = 2
    actual, inspected = scm._leaf_info, []
    # LLM: 只注入或观察局部叶元数据，不替换平台级 nofollow 能力检测。
    # 函数用途: 提供本测试需要的确定性文件类型或覆盖证据。
    def metadata(parent, name):
        inspected.append(name)
        return actual(parent, name)
    monkeypatch.setattr(scm, '_leaf_info', metadata)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('元数据覆盖不完整不能启动 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    with pytest.raises(HostAPIError, match='HOST_NOT_READY'):
        subscription(fixture)
    assert len(inspected) == fixture.bounds.FS_WATCH_MAX_ENTRIES_PER_SCAN


# LLM: lstat 后替换为链接不能靠缓存 stat 结果漏过 nofollow 打开。
# 函数用途: 用确定性交错检验嵌套元数据 FD 的重新核验。
def test_regression_metadata_stat_open_replacement(fixture, monkeypatch):
    entry = fixture.root / '.git' / 'refs' / 'heads' / 'safe'
    entry.write_text('a' * 40 + '\n')
    target = fixture.owner / 'external-ref'
    target.write_text('b' * 40 + '\n')
    actual = scm._leaf_info
    # LLM: 替换发生在 stat 与 open 之间，必须由 O_NOFOLLOW 捕获。
    # 函数用途: 制造安全叶被换成外部链接的确定性交错。
    def replace_after_stat(parent, name):
        info = actual(parent, name)
        if name == 'safe':
            entry.unlink()
            entry.symlink_to(target)
        return info
    monkeypatch.setattr(scm, '_leaf_info', replace_after_stat)
    # LLM: 安全门拒绝或回执重放时不得产生新的进程副作用。
    # 函数用途: 在不允许执行的分支捕获意外 Git 启动。
    def forbidden(*args, **kwargs):
        pytest.fail('核验后替换不能进入 Git')
    monkeypatch.setattr(scm, '_git', forbidden)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        subscription(fixture)
    assert target.read_text() == 'b' * 40 + '\n'


# LLM: 缺 W0 时直接读取生产函数，禁止通过 fixture 替身冒充可用依赖。
# 函数用途: 逐项核对计划中冻结的 SCM 和扫描上界。
def test_regression_frozen_limits_fallback_exact(fixture, monkeypatch):
    from agent_py_agent.agent import business_spaces
    monkeypatch.delattr(business_spaces, 'gap158_limits', raising=False)
    monkeypatch.delitem(sys.modules, business_spaces.__name__ + '.gap158_limits', raising=False)
    bounds = fixture.production_limits()
    assert {name: getattr(bounds, name) for name in (
        'SCM_MAX_SUBSCRIPTIONS_PER_OWNER', 'SCM_MAX_STATUS_ENTRIES', 'SCM_MAX_LOG_ENTRIES',
        'SCM_MAX_PATHS_PER_ACTION', 'SCM_GIT_TIMEOUT_SECONDS', 'SCM_MIN_REFRESH_INTERVAL_SECONDS',
        'FS_WATCH_MAX_ENTRIES_PER_SCAN')} == {
            'SCM_MAX_SUBSCRIPTIONS_PER_OWNER': 8, 'SCM_MAX_STATUS_ENTRIES': 2000,
            'SCM_MAX_LOG_ENTRIES': 50, 'SCM_MAX_PATHS_PER_ACTION': 200,
            'SCM_GIT_TIMEOUT_SECONDS': 15, 'SCM_MIN_REFRESH_INTERVAL_SECONDS': 1.0,
            'FS_WATCH_MAX_ENTRIES_PER_SCAN': 5000}
    supplied = SimpleNamespace(SCM_MAX_STATUS_ENTRIES=7)
    monkeypatch.setattr(business_spaces, 'gap158_limits', supplied, raising=False)
    assert fixture.production_limits() is supplied


# LLM: 不替换 FS/epoch/limits；冷请求真实读规范 RuntimeDB，并经过真实 Git 写链路。
# 函数用途: 联测 SCM 对 FS 工作根、代次撤销与 W0 缺失兼容的实际依赖。
def test_regression_real_fs_epoch_and_limits_integration(fixture, monkeypatch):
    from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
    from agent_py_agent.agent.runtime_db.schema import runtime_db_path
    from agent_py_agent.agent.user_space.owner_epoch import activate_owner_epoch
    monkeypatch.setattr(scm, '_fs', fixture.production_fs)
    monkeypatch.setattr(scm, '_limits', fixture.production_limits)
    repository = RuntimeRepository(runtime_db_path(fixture.owner))
    epoch = activate_owner_epoch(repository)
    call = fixture.call('subscribe', {'repo_root': 'repo'}, key='regression_real_fs_create')
    assert scm._fs().workspace_root(call) == fixture.workspace
    assert scm._fs().current_epoch(call) == epoch
    result = scm.subscribe(call)
    sid = result['subscription_id']
    assert result['repository']['branch'] == 'main'
    (fixture.root / 'a.txt').write_text('integration write\n')
    staged = scm.stage(fresh_request(fixture.call('stage', {'subscription_id': sid, 'paths': ['a.txt']})))
    assert staged['succeeded'] == ['a.txt'] and not staged['failed']
    unstaged = scm.unstage(fresh_request(fixture.call('unstage', {'subscription_id': sid, 'paths': ['a.txt']})))
    assert unstaged['succeeded'] == ['a.txt'] and not unstaged['failed']
    discarded = scm.discard(fresh_request(fixture.call('discard', {'subscription_id': sid,
        'paths': ['a.txt'], 'expected_repo_seq': unstaged['repo_seq']})))
    assert discarded['succeeded'] == ['a.txt'] and not discarded['failed']
    assert (fixture.root / 'a.txt').read_text() == 'original\n'
    assert activate_owner_epoch(repository) > epoch
    with pytest.raises(HostAPIError, match='RESOURCE_NOT_FOUND'):
        scm.events(fresh_request(fixture.call('events', {'subscription_id': sid, 'after_seq': 0})))
    recreated = scm.subscribe(fresh_request(call))
    assert recreated['subscription_id'] != sid


# LLM: 每次 Git 启动前再核验，覆盖上一投影命令之后被替换的配置或属性。
# 函数用途: 检查 status 到 log 之间恶意替换会在第二个进程前失败关闭。
@pytest.mark.parametrize('target', ['config', 'info/attributes'])
def test_regression_metadata_rechecked_between_git_commands(fixture, monkeypatch, target):
    original, spawn, started = scm._git, scm.subprocess.Popen, []
    # LLM: 只记录已实际启动的进程，未执行命令不能算验证成功。
    # 函数用途: 观察 status 与 log 的启动边界。
    def record(command, **kwargs):
        started.append(command)
        return spawn(command, **kwargs)
    monkeypatch.setattr(scm.subprocess, 'Popen', record)
    # LLM: 第一条命令成功后修改元数据，第二条命令必须重新核验。
    # 函数用途: 确定性注入两次 Git 调用之间的配置或属性变化。
    def replace_before_log(fd, args, hooks):
        if args[0] == 'log':
            entry = fixture.root / '.git' / target
            if target == 'config':
                entry.write_text('[include]\n path = external\n')
            else:
                entry.write_text('* filter=external\n')
        return original(fd, args, hooks)
    monkeypatch.setattr(scm, '_git', replace_before_log)
    with pytest.raises(HostAPIError, match='PATH_OUTSIDE_WORKSPACE'):
        subscription(fixture)
    assert len(started) == 1
    assert 'status' in started[0] and 'log' not in started[0]
