# LLM: 只观察原本地原子写叶的重试、权限、原异常和预算；replace_once是资源选择，不提供C来源或授权。
# 模块用途: 验证默认八次重试与显式单次替换，不运行模型、服务或真实外部请求。
import json
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io


@pytest.mark.parametrize('replace_once,attempts', [(False, 8), (True, 1)])
def test_permission_failure_retains_original_error_without_extra_replace(tmp_path, monkeypatch, replace_once, attempts):
    target = tmp_path / 'receipt.json'
    target.write_text('original', encoding='utf-8')
    failures, sleeps = [], []

    def denied(source, destination):
        assert destination == target and stat.S_IMODE(source.stat().st_mode) == 0o600
        error = PermissionError('own temporary replacement ' + str(len(failures)))
        failures.append(error)
        raise error

    monkeypatch.setattr(Path, 'replace', denied)
    monkeypatch.setattr(json_io.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError) as caught:
        json_io.write_private_text_file_atomic_unlocked(target, 'new', replace_once=replace_once)
    assert len(failures) == attempts and caught.value is failures[-1]
    assert sleeps == ([] if replace_once else [0.01 * (i + 1) for i in range(8)])
    assert target.read_text() == 'original' and list(tmp_path.glob('.*.tmp')) == []


def test_omitted_default_retries_then_succeeds(tmp_path, monkeypatch):
    target, error = tmp_path / 'default.txt', PermissionError('temporary')
    original, calls, sleeps = Path.replace, [], []

    def transient(source, destination):
        calls.append(destination)
        if len(calls) == 1:
            raise error
        return original(source, destination)

    monkeypatch.setattr(Path, 'replace', transient)
    monkeypatch.setattr(json_io.time, 'sleep', sleeps.append)
    json_io.write_private_text_file_atomic_unlocked(target, 'exact\n')
    assert calls == [target, target] and sleeps == [0.01]
    assert target.read_bytes() == b'exact\n' and list(tmp_path.glob('.*.tmp')) == []


@pytest.mark.parametrize('explicit_default', [False, True])
def test_default_keeps_old_monkeypatched_replacer_call_shape(tmp_path, monkeypatch, explicit_default):
    calls = []

    def old_replacer(source, target, *, keep_mode=True):
        calls.append(keep_mode)
        source.replace(target)

    monkeypatch.setattr(json_io, '_replace_with_retry', old_replacer)
    options = {'replace_once': False} if explicit_default else {}
    json_io.write_private_text_file_atomic_unlocked(tmp_path / 'default.txt', 'payload', **options)
    assert calls == [False] and (tmp_path / 'default.txt').read_text() == 'payload'


@pytest.mark.parametrize('replace_once', [False, True])
def test_private_exact_raw_atomic_content_mode_and_parent_policy(tmp_path, replace_once):
    parent = tmp_path / 'existing'
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    target = parent / 'receipt.json'
    target.write_text('old')
    target.chmod(0o644)
    raw = '{\n  "中文": [1, true],\n  "opaque": "  unchanged  "\n}'
    json_io.write_private_text_file_atomic_unlocked(target, raw, replace_once=replace_once)
    assert target.read_bytes() == raw.encode('utf-8')
    assert json.loads(target.read_text())['opaque'] == '  unchanged  '
    assert not target.read_bytes().endswith(b'\n')
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(parent.stat().st_mode) == 0o755
    assert list(parent.glob('.*.tmp')) == []


@pytest.mark.parametrize('replace_once', [False, True])
def test_parent_creation_and_explicit_skip_keep_original_contract(tmp_path, replace_once):
    target = tmp_path / 'new' / 'inner' / 'payload.txt'
    json_io.write_private_text_file_atomic_unlocked(target, '', replace_once=replace_once)
    assert target.read_bytes() == b'' and stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    missing = tmp_path / 'not-created' / 'payload.txt'
    with pytest.raises(FileNotFoundError):
        json_io.write_private_text_file_atomic_unlocked(missing, 'x', ensure_parent_private=False, replace_once=replace_once)
    assert not missing.parent.exists()
    target.parent.chmod(0o755)
    json_io.write_private_text_file_atomic_unlocked(target, 'new', ensure_parent_private=False, replace_once=replace_once)
    assert target.read_bytes() == b'new' and stat.S_IMODE(target.parent.stat().st_mode) == 0o755
    assert list(tmp_path.rglob('.*.tmp')) == []


@pytest.mark.parametrize('replace_once', [False, True])
def test_non_permission_failure_same_object_and_cleanup(tmp_path, monkeypatch, replace_once):
    target, error, sleeps = tmp_path / 'receipt.json', OSError('ordinary local IO error'), []

    def failed(_source, _destination):
        raise error

    monkeypatch.setattr(Path, 'replace', failed)
    monkeypatch.setattr(json_io.time, 'sleep', sleeps.append)
    with pytest.raises(OSError) as caught:
        json_io.write_private_text_file_atomic_unlocked(target, 'payload', replace_once=replace_once)
    assert caught.value is error and sleeps == []
    assert not target.exists() and list(tmp_path.glob('.*.tmp')) == []


@pytest.mark.parametrize('replace_once', [False, True])
def test_existing_deadline_is_same_value_and_failure_never_sleeps(tmp_path, monkeypatch, replace_once):
    source, target = tmp_path / 'source.tmp', tmp_path / 'target.txt'
    source.write_text('payload')
    deadline, checked, attempts, sleeps = 12345.6789, [], [], []
    error = PermissionError('bounded original failure')

    def check(value):
        checked.append(value)

    def failed(path, destination):
        attempts.append((path, destination))
        raise error

    monkeypatch.setattr(json_io, 'check_io_deadline', check)
    monkeypatch.setattr(Path, 'replace', failed)
    monkeypatch.setattr(json_io.time, 'sleep', sleeps.append)
    with pytest.raises(PermissionError) as caught:
        json_io._replace_with_retry(source, target, keep_mode=False, deadline=deadline, replace_once=replace_once)
    assert caught.value is error and attempts == [(source, target)] and sleeps == []
    assert len(checked) == 2 and all(value is deadline for value in checked)


@pytest.mark.parametrize('replace_once', [False, True])
def test_expired_original_deadline_blocks_before_mode_and_replace(tmp_path, monkeypatch, replace_once):
    source, target = tmp_path / 'source.tmp', tmp_path / 'target.txt'
    source.write_text('payload')
    seen = []
    monkeypatch.setattr(json_io.time, 'monotonic', lambda: 10.0)
    monkeypatch.setattr(json_io, '_keep_target_mode', lambda *_: seen.append('mode'))
    monkeypatch.setattr(Path, 'replace', lambda *_: seen.append('replace'))
    with pytest.raises(BlockingIOError, match='local IO resource budget expired'):
        json_io._replace_with_retry(source, target, deadline=9.0, replace_once=replace_once)
    assert seen == [] and source.read_text() == 'payload' and not target.exists()


@pytest.mark.parametrize('replace_once,keep_mode,expected', [(False, True, 0o640), (True, True, 0o640), (False, False, 0o600), (True, False, 0o600)])
def test_replace_keep_mode_unchanged(tmp_path, replace_once, keep_mode, expected):
    source, target = tmp_path / 'source.tmp', tmp_path / 'target.txt'
    source.write_text('new')
    source.chmod(0o600)
    target.write_text('old')
    target.chmod(0o640)
    json_io._replace_with_retry(source, target, keep_mode=keep_mode, replace_once=replace_once)
    assert target.read_bytes() == b'new' and stat.S_IMODE(target.stat().st_mode) == expected
    assert not source.exists()
