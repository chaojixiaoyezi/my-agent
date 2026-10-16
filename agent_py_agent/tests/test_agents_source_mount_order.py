"""人格祖先mount与main精确写/读保护组合；只构造argv，不执行平台隔离。"""
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling import sandbox
from agent_py_agent.tests.test_agents_source_protection import protected as protected


# LLM: 只按bwrap顺序检查目标上最终可见挂载，不执行挂载、不以存在性代替授权；测试用精确词法namespace。
# 函数用途: 从完整argv判断指定测试路径最后落在空视图、只读源还是可写源。
def view(argv, path):
    selected = None
    for index, flag in enumerate(argv):
        if flag in {'--bind', '--ro-bind', '--dev-bind'}:
            source, target = Path(argv[index + 1]), Path(argv[index + 2])
        elif flag in {'--tmpfs', '--dev', '--proc'}:
            source, target = None, Path(argv[index + 1])
        else:
            continue
        if path == target or target in path.parents:
            selected = (flag, source, target)
    return selected


@pytest.fixture(autouse=True)
def no_platform_probe(monkeypatch):
    monkeypatch.setattr(sandbox, '_proc_mount_args', lambda: ['--dir', '/proc'])


@pytest.mark.parametrize('mode', ['owner', 'full', 'allowlist'])
def test_h2_hidden_is_last_after_persona_parent_bind(protected, mode):
    p = protected
    store, secret = p.home / 'data/plugins', p.home / 'secrets'
    store.mkdir(parents=True)
    secret.mkdir()
    (store / 'retired-package.txt').write_text('不可读旧包')
    (secret / 'test-ref.txt').write_text('临时不可读样本')
    system = p.root / 'fake-system'
    system.mkdir()
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home,),
        hidden_paths=(store, secret), protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    if mode == 'full':
        spec = replace(spec, full_access=True)
    if mode == 'allowlist':
        spec = replace(spec, read_only_root=True, read_mode='allowlist', system_read_roots=(system,))
    argv = sandbox.build_bwrap_argv(spec)
    for path in (store, secret):
        assert view(argv, path / 'child')[1] is None
        index = next(i for i, flag in enumerate(argv) if flag == '--tmpfs' and argv[i + 1] == str(path))
        assert index > max(i for i, flag in enumerate(argv) if flag in {'--bind', '--ro-bind'})
    assert argv[-2:] == ['--chdir', str(p.home)]
    assert view(argv, p.source)[0] == '--ro-bind'
    assert view(argv, p.home / 'docs/normal-output')[0] == '--bind'


@pytest.mark.parametrize('full_access', [False, True])
def test_denied_nested_allow_and_absolute_readonly_survive_parent_bind(protected, full_access):
    p = protected
    denied = p.home / 'docs/denied'
    reopened = denied / 'explicit-output'
    reopened.mkdir(parents=True)
    absolute = p.home / 'docs/absolute.json'
    absolute.write_text('{}')
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home, reopened),
        write_scope_denied_roots=(denied,), read_only_paths=(absolute,), full_access=full_access,
        protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    argv = sandbox.build_bwrap_argv(spec)
    assert view(argv, denied / 'private')[0] == '--ro-bind'
    assert view(argv, reopened / 'accepted-output')[0] == '--bind'
    assert view(argv, absolute)[0] == '--ro-bind'
    assert view(argv, p.source)[0] == '--ro-bind'
    assert view(argv, p.source.parent / 'ordinary.txt')[0] == '--bind'
    assert any(argv[i:i + 3] == ['--bind', str(p.source.parent), str(p.source.parent)]
               for i in range(len(argv) - 2))
    assert argv[-2:] == ['--chdir', str(p.home)]


def test_hide_home_preserves_explicit_public_workspace_and_write_roots(protected):
    p = protected
    public = p.home / 'public'
    public.mkdir()
    secret = p.home / 'private-secret'
    secret.mkdir()
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.source.parent, write_roots=(p.source.parent,),
        public_ro_roots=(public,), hidden_paths=(p.home,), read_only_root=True, read_mode='hide_home',
        protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    argv = sandbox.build_bwrap_argv(spec)
    assert view(argv, secret / 'child')[1] is None
    assert view(argv, public / 'allowed-reference')[0] == '--ro-bind'
    assert view(argv, p.source.parent / 'normal-output')[0] == '--bind'
    assert view(argv, p.source)[0] == '--ro-bind'
    assert argv[-2:] == ['--chdir', str(p.source.parent)]


def test_tmp_namespace_parent_preserves_source_denial_and_business_write(protected):
    p = protected
    source = p.home / '.sandbox-tmp/branch/rules.bin'
    source.parent.mkdir(parents=True)
    source.write_bytes(p.before)
    chain = [(node.stat().st_dev, node.stat().st_ino) for node in (*reversed(source.parents), source)]
    identity = sha256(json.dumps(chain, separators=(',', ':')).encode()).hexdigest()
    p.control.write_text(json.dumps(dict(p.record, value=str(source), identity=identity)))
    denied = source.parent / 'denied'
    denied.mkdir()
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home,),
        write_scope_denied_roots=(denied,), protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    argv = sandbox.build_bwrap_argv(spec)
    assert view(argv, Path('/tmp/branch/rules.bin'))[0] == '--ro-bind'
    assert view(argv, Path('/tmp/branch/output.txt'))[0] == '--bind'
    assert view(argv, denied / 'private')[0] == '--ro-bind'
    assert view(argv, source)[0] == '--ro-bind'
    assert argv[-2:] == ['--chdir', str(p.home)]


@pytest.mark.parametrize('full_access', [False, True])
def test_hidden_source_never_becomes_visible_while_hiding_is_deferred(protected, full_access):
    p = protected
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home,),
        hidden_paths=(p.source.parent,), full_access=full_access,
        protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    argv = sandbox.build_bwrap_argv(spec)
    assert view(argv, p.source)[1] is None
    assert not any(argv[i:i + 3] == ['--ro-bind', str(p.source), str(p.source)]
                   for i in range(len(argv) - 2))


def test_absolute_readonly_directory_overrides_declared_nested_write(protected):
    p = protected
    absolute = p.home / 'docs/absolute-dir'
    nested = absolute / 'declared-output'
    nested.mkdir(parents=True)
    spec = sandbox.SandboxSpec(owner_home=p.home, workspace=p.home, write_roots=(p.home, nested),
        read_only_paths=(absolute,), protected_persona_root=p.home, bwrap_path='/fake/bwrap')
    argv = sandbox.build_bwrap_argv(spec)
    assert view(argv, nested / 'must-remain-readonly')[0] == '--ro-bind'
    assert view(argv, p.source.parent / 'ordinary.txt')[0] == '--bind'
