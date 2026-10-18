"""原私有writer的纯deadline与None默认对照；只操作临时本人文件。"""
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.common import json_io


@pytest.mark.parametrize('bounded', [False, True])
def test_replace_keeps_original_permission_error_and_default_eight_attempts(tmp_path, monkeypatch, bounded):
    source, target = tmp_path/'own.tmp', tmp_path/'own.json'
    source.write_text('candidate')
    target.write_text('original')
    error = PermissionError('synthetic own path')
    attempts, sleeps = [], []

    def replace(actual, destination):
        assert actual == source and destination == target
        attempts.append(1)
        raise error

    monkeypatch.setattr(Path, 'replace', replace)
    monkeypatch.setattr(json_io.time, 'sleep', sleeps.append)
    options = {'deadline': time.monotonic()+1} if bounded else {}
    with pytest.raises(PermissionError) as caught:
        json_io._replace_with_retry(source, target, keep_mode=False, **options)
    assert caught.value is error and target.read_text() == 'original' and source.read_text() == 'candidate'
    assert len(attempts) == (1 if bounded else 8)
    assert sleeps == ([] if bounded else [0.01*(i+1) for i in range(8)])


def test_none_deadline_does_not_consult_clock(monkeypatch):
    def forbidden():
        raise AssertionError('None默认不新增时钟/资源行为')
    monkeypatch.setattr(json_io.time, 'monotonic', forbidden)
    json_io.check_io_deadline(None)


def test_expired_final_replace_keeps_original_canonical_and_cleans_own_temp(tmp_path, monkeypatch):
    target = tmp_path/'own.json'
    target.write_text('original')
    original = json_io._replace_with_retry
    entered = []

    def delayed(source, destination, **options):
        entered.append(source)
        time.sleep(0.06)
        return original(source, destination, **options)

    monkeypatch.setattr(json_io, '_replace_with_retry', delayed)
    with json_io.locked_json_path(target), pytest.raises(BlockingIOError):
        json_io.write_private_json_file_atomic_no_newline_unlocked(target, {'value': 1}, deadline=time.monotonic()+0.02)
    assert entered and target.read_text() == 'original'
    assert not list(tmp_path.glob('.*.tmp'))
