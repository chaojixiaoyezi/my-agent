"""构建身份来自本进程安装树的VCS字段，不能追活动指针或把包hash当commit。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent.common import build_identity as build


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    prefix = tmp_path / 'runtime-a'
    module = prefix / 'lib/site-packages/agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    monkeypatch.setattr(build, '_RUNTIME_PREFIX', prefix)
    monkeypatch.setattr(build.sys, 'prefix', str(prefix))
    for name in ('MY_AGENT_GIT_SHA', 'GIT_SHA', 'BUILD_GIT_SHA'):
        monkeypatch.delenv(name, raising=False)
    build.get_build_identity.cache_clear()
    yield prefix
    build.get_build_identity.cache_clear()


@pytest.mark.parametrize('commit,expected', [('A'*40, 'a'*40), ('b'*64, 'b'*64), ('release-123',''), (None,'')])
def test_installed_commit_is_vcs_field_only(runtime, commit, expected):
    (runtime / 'my-agent-release.json').write_text(json.dumps({'commit': commit, 'wheel_sha256': 'c'*64}))
    assert build.get_build_identity()['git_sha'] == expected


def test_other_install_cwd_and_current_pointer_cannot_change_identity(runtime, tmp_path, monkeypatch):
    other = tmp_path / 'runtime-b'
    other.mkdir()
    (runtime / 'my-agent-release.json').write_text(json.dumps({'commit': 'a'*40}))
    (other / 'my-agent-release.json').write_text(json.dumps({'commit': 'b'*40}))
    current = tmp_path / 'current'
    current.symlink_to(runtime, target_is_directory=True)
    first = build.get_build_identity()
    current.unlink()
    current.symlink_to(other, target_is_directory=True)
    monkeypatch.chdir(other)
    assert build.get_build_identity() is first
    assert first['git_sha'] == 'a'*40
    assert first['runtime_prefix'] == str(runtime)


def test_source_module_cannot_claim_interpreter_install_metadata(runtime, tmp_path, monkeypatch):
    (runtime / 'my-agent-release.json').write_text(json.dumps({'commit': 'a'*40}))
    module = tmp_path / 'export/agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    assert build.get_build_identity()['git_sha'] == ''


def test_old_invalid_and_symlink_manifest_remain_unknown(runtime, tmp_path):
    path = runtime / 'my-agent-release.json'
    for payload in ('{broken', json.dumps({'wheel_sha256': 'c'*64}), ' '*65537):
        path.write_text(payload)
        build.get_build_identity.cache_clear()
        assert build.get_build_identity()['git_sha'] == ''
    path.unlink()
    external = tmp_path / 'foreign.json'
    external.write_text(json.dumps({'commit': 'd'*40}))
    path.symlink_to(external)
    assert build.get_build_identity()['git_sha'] == ''


def test_portable_base_commit_is_not_exact_build_commit(tmp_path, monkeypatch):
    prefix = tmp_path / 'release/runtime'
    module = prefix / 'lib/site-packages/agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    (prefix.parent / 'release.json').write_text(json.dumps({
        'schema': 'my-agent.private-release.v1', 'base_commit': 'a'*40, 'source_sha256': 'b'*64}))
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    monkeypatch.setattr(build, '_RUNTIME_PREFIX', prefix)
    for name in ('MY_AGENT_GIT_SHA', 'GIT_SHA', 'BUILD_GIT_SHA'):
        monkeypatch.delenv(name, raising=False)
    build.get_build_identity.cache_clear()
    try:
        assert build.get_build_identity()['git_sha'] == ''
    finally:
        build.get_build_identity.cache_clear()


def test_pip_vcs_metadata_is_bound_to_this_install(runtime, monkeypatch):
    site = runtime / 'lib/site-packages'
    info = site / 'my_agent-0.3.0.dist-info'
    info.mkdir(parents=True)
    (info / 'METADATA').write_text('Name: my-agent\nVersion: 0.3.0\n')
    (info / 'direct_url.json').write_text(json.dumps({
        'url': 'file:///synthetic-source', 'vcs_info': {'vcs': 'git', 'commit_id': 'D'*40}}))
    monkeypatch.syspath_prepend(str(site))
    assert build.get_build_identity()['git_sha'] == 'd'*40


@pytest.mark.parametrize('mode', ['current', 'legacy', 'wrong_pid', 'http_error'])
def test_status_cli_reads_original_gateway_route_over_http(tmp_path, mode):
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from types import SimpleNamespace

    from agent_py_agent.agent.gateway_parts.http_handlers import handle_status
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
    from agent_py_agent.cli.local_commands import _read_gateway_build_identity

    paths = gateway_paths_from_root(tmp_path / 'gateway')
    paths.root.mkdir(parents=True)
    paths.state.write_text(json.dumps({'pid': os.getpid(), 'status': 'running'}))
    context = SimpleNamespace(paths=paths)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == '/status'
            handle_status(self, context)

        def _send_json(self, status, body):
            # 原server响应产生后才做旧版/错进程运输刺激，客户端不得拿本地版本补齐。
            if mode == 'legacy':
                body.pop('build_identity', None)
            elif mode == 'wrong_pid':
                body['pid'] += 1
            if mode == 'http_error':
                status = 403
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever)
    worker.start()
    try:
        agent = SimpleNamespace(config=SimpleNamespace(gateway_port=server.server_port))
        result = _read_gateway_build_identity(agent, os.getpid(), True)
        if mode == 'current':
            assert result['source'] == 'gateway_status'
            assert result['version'] == build.get_build_identity()['version']
            assert result['runtime_prefix'] == build.get_build_identity()['runtime_prefix']
        else:
            assert result['git_sha'] == 'unknown'
            assert result['reason'] == {'legacy': 'server_build_identity_unavailable',
                'wrong_pid': 'gateway_pid_mismatch', 'http_error': 'gateway_status_http_error'}[mode]
            if mode == 'http_error':
                assert result['http_status'] == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def _source_checkout(tmp_path, name):
    import os
    import subprocess

    root = tmp_path / name
    root.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(['git', 'init', str(root)], check=True, capture_output=True, env=env)
    subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                    'commit', '--allow-empty', '-m', name], cwd=root, check=True, capture_output=True, env=env)
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True, env=env).strip()
    module = root / 'agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    return root, module, sha


@pytest.mark.parametrize('variant', ['git_dir', 'work_tree', 'common_dir', 'config_count', 'config_parameters',
                                     'global_config', 'system_config'])
def test_source_identity_cannot_borrow_foreign_git_environment(runtime, tmp_path, monkeypatch, variant):
    source, module, sha = _source_checkout(tmp_path, 'source')
    foreign, _, other = _source_checkout(tmp_path, 'foreign')
    assert sha != other
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    config = tmp_path / 'foreign.config'
    config.write_text(f'[core]\n\tworktree = {foreign.as_posix()}\n\tbare = true\n')
    changes = {
        'git_dir': {'GIT_DIR': str(foreign / '.git')},
        'work_tree': {'GIT_WORK_TREE': str(foreign)},
        'common_dir': {'GIT_COMMON_DIR': str(foreign / '.git')},
        'config_count': {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true'},
        'config_parameters': {'GIT_CONFIG_PARAMETERS': "'core.bare=true'"},
        'global_config': {'GIT_CONFIG_GLOBAL': str(config)},
        'system_config': {'GIT_CONFIG_SYSTEM': str(config), 'GIT_CONFIG_NOSYSTEM': '0'},
    }[variant]
    for key, value in changes.items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(foreign)
    assert build.get_build_identity()['git_sha'] == sha


def test_nested_source_without_own_git_never_claims_ancestor(runtime, tmp_path, monkeypatch):
    outer, _, _ = _source_checkout(tmp_path, 'outer')
    module = outer / 'nested/site-packages/agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    monkeypatch.chdir(outer)
    assert build.get_build_identity()['git_sha'] == ''


def test_source_git_symlink_and_non_commit_head_remain_unknown(runtime, tmp_path, monkeypatch):
    import shutil

    source, module, _ = _source_checkout(tmp_path, 'source')
    foreign, _, _ = _source_checkout(tmp_path, 'foreign')
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    (source / '.git/HEAD').write_text('a' * 40 + '\n')
    assert build.get_build_identity()['git_sha'] == ''
    shutil.rmtree(source / '.git')
    try:
        (source / '.git').symlink_to(foreign / '.git', target_is_directory=True)
    except OSError:
        pytest.skip('当前平台无目录软链权限')
    build.get_build_identity.cache_clear()
    assert build.get_build_identity()['git_sha'] == ''


def test_explicit_build_identity_contract_keeps_precedence(runtime, tmp_path, monkeypatch):
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'foreign-git'))
    monkeypatch.setenv('MY_AGENT_GIT_SHA', 'A' * 40)
    assert build.get_build_identity()['git_sha'] == 'a' * 40


def test_legitimate_linked_source_worktree_keeps_its_own_commit(runtime, tmp_path, monkeypatch):
    import os
    import subprocess

    source, _, sha = _source_checkout(tmp_path, 'source')
    worktree = tmp_path / 'linked'
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    subprocess.run(['git', 'worktree', 'add', '--detach', str(worktree), sha], cwd=source,
                   check=True, capture_output=True, env=env)
    assert (worktree / '.git').is_file()
    module = worktree / 'agent_py_agent/agent/common/build_identity.py'
    module.parent.mkdir(parents=True)
    module.write_text('')
    monkeypatch.setattr(build, '_MODULE_ORIGIN', module)
    monkeypatch.setenv('GIT_DIR', str(source / '.git'))
    assert build.get_build_identity()['git_sha'] == sha
