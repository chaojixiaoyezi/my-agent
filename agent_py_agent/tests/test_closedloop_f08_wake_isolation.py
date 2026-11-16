"""外部审计 F08：坏单条保留，不能截断同页健康唤醒发现。"""
import json
from pathlib import Path

import pytest

from agent_py_agent.agent import owner_wake_discovery as discovery


@pytest.mark.parametrize('bad', ['{', 'null', '[]'])
def test_bad_wake_does_not_hide_healthy_same_page(tmp_path, monkeypatch, caplog, bad):
    directory = tmp_path / 'store/wake_queue/normal'
    directory.mkdir(parents=True)
    broken, healthy = directory / 'bad.json', directory / 'healthy.json'
    broken.write_text(bad)
    healthy.write_text(json.dumps({'task_id': 'healthy'}))
    original = discovery._recovery_source_page
    def ordered(directory_arg, deadline_out):
        page = original(directory_arg, deadline_out)
        return tuple(sorted(page, key=lambda path: path.name))
    monkeypatch.setattr(discovery, '_recovery_source_page', ordered)
    assert discovery._has_pending_wake_signal(tmp_path / 'store', owner_home=tmp_path)
    assert broken.read_text() == bad
    assert 'OWNER_WAKE_RECORD_UNREADABLE' in caplog.text
    assert 'side_effects=not_started' in caplog.text
    assert healthy.exists()


def test_one_unreadable_wake_does_not_hide_healthy(tmp_path, monkeypatch):
    directory = tmp_path / 'store/wake_queue/normal'
    directory.mkdir(parents=True)
    broken, healthy = directory / 'bad.json', directory / 'healthy.json'
    broken.write_text('{}')
    healthy.write_text('{}')
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path == broken:
            raise PermissionError('injected unreadable wake')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert discovery._has_pending_wake_signal(tmp_path / 'store', owner_home=tmp_path)
    assert broken.exists()


def test_bad_wake_backlog_retains_existing_64_entry_budget_and_continuation(tmp_path, monkeypatch):
    directory = tmp_path / 'store/wake_queue/normal'
    directory.mkdir(parents=True)
    for index in range(65):
        (directory / f'bad-{index:03}.json').write_text('{')
    (directory / 'healthy.json').write_text('{}')
    original = Path.read_text
    reads = 0
    def count(path, *args, **kwargs):
        nonlocal reads
        reads += 1
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', count)
    found = False
    for _ in range(3):
        reads = 0
        deadlines = []
        found |= discovery._has_pending_wake_signal(tmp_path / 'store', owner_home=tmp_path, deadline_out=deadlines)
        assert reads <= 64
        if found:
            break
        assert deadlines
    assert found
    assert len(list(directory.glob('*.json'))) == 66
