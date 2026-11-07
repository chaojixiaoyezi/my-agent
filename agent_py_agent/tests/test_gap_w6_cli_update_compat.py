"""固定 anchor 的完整 CLI 文案及 git/pip 调用次序，不依赖当前源码是 git 检出。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.cli import update as U


def test_update_argparse_surface_and_help_snapshot(capsys):
    parser = argparse.ArgumentParser(prog='my-agent')
    sub = parser.add_subparsers(dest='command')
    U.add_update_subcommand(sub)
    command = sub.choices['update']
    assert command.format_help() == (
        'usage: my-agent update [-h] [--check] [--package PACKAGE]\n\n'
        'options:\n'
        '  -h, --help         show this help message and exit\n'
        '  --check            只检查有没有更新,不实际更新\n'
        '  --package PACKAGE  离线更新包 .run；保留当前安装目录、配置和数据\n'
    )
    args = parser.parse_args(['update', '--check', '--package', 'release.run'])
    assert vars(args) == {'command': 'update', 'check': True,
        'package': Path('release.run'), 'func': U.cmd_update}
    with pytest.raises(SystemExit) as error:
        parser.parse_args(['update', '--rollback'])
    assert error.value.code == 2
    assert 'unrecognized arguments: --rollback' in capsys.readouterr().err


@pytest.mark.parametrize('fetch,behind,rc,expected', [
    ((1, 'network diagnostic'), None, 1, '⚠️ 无法 fetch 远端(检查网络/远端配置)。当前: abc123\n'),
    ((0, ''), (128, 'no upstream'), 0, '⚠️ 无法判断更新:当前分支可能未设上游/远端无此分支。当前: abc123\n'),
    ((0, ''), (0, 'not-a-count'), 0, '⚠️ 无法判断更新:当前分支可能未设上游/远端无此分支。当前: abc123\n'),
    ((0, ''), (0, '0'), 0, '✅ 已是最新(abc123),无需更新。\n'),
    ((0, ''), (0, '2'), 0, '有更新可用:落后远端 2 个提交(当前 abc123)。跑 `my-agent update` 升级。\n'),
])
def test_check_anchor_full_output_and_fake_git_order(monkeypatch, capsys, tmp_path, fetch, behind, rc, expected):
    calls = []
    monkeypatch.setattr(U, 'detect_install_source', lambda: U.InstallSource('git', tmp_path))

    def git(repo, args):
        assert repo == tmp_path
        calls.append(list(args))
        if args[0] == 'rev-parse':
            return 0, 'abc123'
        if args[0] == 'fetch':
            return fetch
        assert args == ['rev-list', '--count', 'HEAD..@{u}']
        return behind

    monkeypatch.setattr(U, '_git', git)
    assert U.cmd_update(SimpleNamespace(check=True)) == rc
    assert capsys.readouterr().out == expected
    assert calls == [['rev-parse', '--short', 'HEAD'], ['fetch', '--quiet']] + (
        [['rev-list', '--count', 'HEAD..@{u}']] if behind is not None else [])


@pytest.mark.parametrize('pull,pip,commits,rc,tail', [
    ((1, '本地有改动'), None, ['aaa111'], 1,
        '❌ git pull 失败(本地可能有改动/分叉):\n本地有改动\n请手动处理后重试。\n'),
    ((0, 'Updated'), (1, '依赖装失败'), ['aaa111'], 1,
        '刷新依赖…\n⚠️ 代码已更新但依赖刷新失败:\n依赖装失败\n手动跑 `pip install -e {repo}`。\n'),
    ((0, 'Updated'), (0, 'ok'), ['aaa111', 'bbb222'], 0,
        '刷新依赖…\n✅ 更新完成:aaa111 → bbb222。\n'),
    ((0, 'Already up to date.'), (0, ''), ['aaa111', 'aaa111'], 0,
        '刷新依赖…\n✅ 已是最新(aaa111),无变化。\n'),
])
def test_apply_anchor_full_output_and_fake_git_order(monkeypatch, capsys, tmp_path, pull, pip, commits, rc, tail):
    calls = []
    values = iter(commits)
    monkeypatch.setattr(U, 'detect_install_source', lambda: U.InstallSource('git', tmp_path))

    def git(repo, args):
        calls.append(list(args))
        if args[0] == 'rev-parse':
            return 0, next(values)
        assert args == ['pull', '--ff-only']
        # 开始文案必须在 pull 前出现，不能等整个结构化过程结束后补印。
        return pull

    def install(repo):
        calls.append(['pip', str(repo)])
        return pip

    monkeypatch.setattr(U, '_git', git)
    monkeypatch.setattr(U, '_pip_install_editable', install)
    assert U.cmd_update(SimpleNamespace(check=False)) == rc
    assert capsys.readouterr().out == f'当前: {tmp_path} @ aaa111\n拉取最新代码…\n' + tail.format(repo=tmp_path)
    expected = [['rev-parse', '--short', 'HEAD'], ['pull', '--ff-only']]
    if pip is not None:
        expected.append(['pip', str(tmp_path)])
        if pip[0] == 0:
            expected.append(['rev-parse', '--short', 'HEAD'])
    assert calls == expected


def test_non_git_and_offline_error_outputs_unchanged(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(U, 'detect_install_source', lambda: U.InstallSource('pip', None))
    assert U.cmd_update(SimpleNamespace(check=False)) == 1
    assert capsys.readouterr().out == ('⚠️ my-agent 不是从 git 检出安装的(可能是 pip 包)。'
        '请用 `pip install --upgrade ...` 或重跑安装脚本更新。\n')
    assert U.cmd_update(SimpleNamespace(check=True, package=tmp_path / 'missing.run')) == 1
    assert capsys.readouterr().out == '请指定存在的离线 .run 更新包。\n'
    package = tmp_path / 'bundle.run'
    package.write_text('offline fixture', encoding='utf-8')
    monkeypatch.setattr(U.sys, 'executable', str(tmp_path / 'runtime' / 'bin' / 'python'))
    assert U.cmd_update(SimpleNamespace(check=True, package=package)) == 1
    assert capsys.readouterr().out == '当前程序不是离线安装实例，请用 bash 新包.run --update 识别已有实例。\n'


def test_offline_marker_and_package_precedence_unchanged(monkeypatch, capsys, tmp_path):
    prefix = tmp_path / 'runtime'
    prefix.mkdir()
    executable = prefix / 'bin' / 'python'
    monkeypatch.setattr(U.sys, 'executable', str(executable))
    marker = prefix / '.my-agent-private-install.json'
    marker.write_text(json.dumps({'schema': 'wrong'}), encoding='utf-8')
    package = tmp_path / 'bundle.run'
    package.write_text('offline fixture', encoding='utf-8')
    assert U.cmd_update(SimpleNamespace(check=True, package=package)) == 1
    assert capsys.readouterr().out == '当前离线安装标记不一致，未更新。\n'
    marker.write_text(json.dumps({'schema': 'my-agent.private-release.v1',
        'owner_uid': U.os.getuid(), 'prefix': str(prefix)}), encoding='utf-8')

    class Executed(Exception):
        pass

    def execute(program, argv):
        assert program == 'bash'
        assert argv == ['bash', str(package), '--update', '--prefix', str(prefix)]
        raise Executed()

    monkeypatch.setattr(U.os, 'execvp', execute)
    monkeypatch.setattr(U, 'detect_install_source', lambda: pytest.fail('离线包必须先于来源探测'))
    with pytest.raises(Executed):
        U.cmd_update(SimpleNamespace(check=True, package=package))
    assert capsys.readouterr().out == ''


def test_structured_callbacks_preserve_immediate_cli_timing(monkeypatch, tmp_path, capsys):
    seen = []

    def git(repo, args):
        if args[0] == 'rev-parse':
            return 0, 'abc123'
        assert seen[-1]['step'] == 'start'
        return 0, 'pull output'

    def install(repo):
        assert seen[-1]['step'] == 'pull'
        return 0, 'pip output'

    monkeypatch.setattr(U, '_git', git)
    monkeypatch.setattr(U, '_pip_install_editable', install)
    result = U.apply_update_steps(tmp_path, on_step=seen.append)
    assert result['returncode'] == 0
    assert result['steps'] == seen
    assert [step['step'] for step in seen] == ['start', 'pull', 'install', 'complete']
    assert capsys.readouterr().out == ''


def test_structured_rollback_rejects_options_without_running_git(monkeypatch, tmp_path):
    monkeypatch.setattr(U, '_git', lambda repo, args: pytest.fail('非法提交不能到达 git'))
    result = U.rollback_update_steps(tmp_path, '--hard')
    assert result['returncode'] == 1 and result['reason'] == 'invalid_pre_commit'


def test_install_detection_distinguishes_valid_offline_marker_from_pip_and_worktree(monkeypatch, tmp_path):
    import agent_py_agent

    package = tmp_path / 'site' / 'agent_py_agent'
    package.mkdir(parents=True)
    monkeypatch.setattr(agent_py_agent, '__file__', str(package / '__init__.py'))
    prefix = tmp_path / 'runtime'
    prefix.mkdir()
    monkeypatch.setattr(U.sys, 'executable', str(prefix / 'bin' / 'python'))
    assert U.detect_install_source() == U.InstallSource('pip', None)
    marker = prefix / '.my-agent-private-install.json'
    marker.write_text(json.dumps({'schema': 'my-agent.private-release.v1',
        'owner_uid': U.os.getuid(), 'prefix': str(prefix)}), encoding='utf-8')
    assert U.detect_install_source() == U.InstallSource('offline', None)
    marker.write_text('not-json', encoding='utf-8')
    assert U.detect_install_source() == U.InstallSource('unknown', None)
    (package.parent / '.git').write_text('gitdir: fixture', encoding='utf-8')
    assert U.detect_install_source() == U.InstallSource('git', package.parent)
