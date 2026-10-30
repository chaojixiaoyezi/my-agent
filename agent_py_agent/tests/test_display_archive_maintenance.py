"""显式维护的扫描预算：跳过、错误和非UUID目录均不可无限枚举。"""
import json
import os
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import display_archive as archive
from agent_py_agent.agent.conversation import display_archive_maintenance as maintenance


@pytest.mark.parametrize('operation', ['compact', 'prune', 'pinned', 'errors'])
def test_skipped_and_failed_archives_obey_scan_budget(tmp_path, operation):
    root = tmp_path / 'display_archives'
    root.mkdir()
    pins = set()
    for number in range(100):
        archive_id = f'{number:032x}'
        directory = root / archive_id
        directory.mkdir()
        pins.add(archive_id)
        if operation == 'errors':
            (directory / 'manifest.json').write_text('{broken')
        else:
            manifest = dict(schema=archive.DISPLAY_ARCHIVE_SCHEMA, archive_id=archive_id,
                            thread_id='thread-test', page_count=1, row_count=1, char_count=1)
            (directory / 'manifest.json').write_text(json.dumps(manifest))
    now = 2_000_000_000.0
    if operation == 'compact':
        result = maintenance.compact_display_archives(tmp_path, now=now, min_age_days=100000, max_archives=1)
    else:
        result = maintenance.prune_expired_display_archives(
            tmp_path, now=now, ttl_days=100000, max_archives=1,
            pinned_archive_ids=pins if operation == 'pinned' else None,
        )
    assert result['scanned'] == 64, result
    assert result['errors' if operation == 'errors' else 'skipped'] == 64, result
    assert result['compacted'] == result['expired'] == result['removed'] == 0
    assert len(list(root.iterdir())) == 100


# 用真实目录迭代器旁听yield数，结果scanned只统计UUID仍保持原合同。
def test_non_archive_entries_also_consume_enumeration_budget(tmp_path, monkeypatch):
    root = tmp_path / 'display_archives'
    root.mkdir()
    for number in range(100):
        (root / f'not-an-archive-{number}').mkdir()
    original, visited = os.scandir, []

    class Observed:
        def __enter__(self):
            self.entries = original(root)
            self.entries.__enter__()
            return self

        def __iter__(self):
            for entry in self.entries:
                visited.append(entry.name)
                yield entry

        def __exit__(self, *args):
            return self.entries.__exit__(*args)

    monkeypatch.setattr(maintenance.os, 'scandir', lambda _: Observed())
    result = maintenance.compact_display_archives(tmp_path, max_archives=1)
    assert len(visited) == 64
    assert result == dict(scanned=0, compacted=0, expired=0, removed=0, skipped=0, errors=0)


def test_success_limit_and_zero_budget_keep_archive_contract(tmp_path):
    store = SimpleNamespace(storage=SimpleNamespace(root=tmp_path), threads=SimpleNamespace(load=lambda _: True))
    refs = [archive.archive_display_rows(SimpleNamespace(conversation_store=store),
            thread_id='thread-test', rows=[{'text': '中🙂' * 10000}]) for _ in range(3)]
    before = [[archive.read_display_archive_page(store, ref, i) for i in range(ref['page_count'])] for ref in refs]
    assert maintenance.compact_display_archives(store, max_archives=0)['scanned'] == 0
    result = maintenance.compact_display_archives(store, min_age_days=0, max_archives=1)
    assert result['compacted'] == result['scanned'] == 1
    assert [[archive.read_display_archive_page(store, ref, i) for i in range(ref['page_count'])] for ref in refs] == before


@pytest.mark.parametrize('maximum', [-1, True, 1.5])
def test_invalid_batch_limit_is_rejected(tmp_path, maximum):
    with pytest.raises(ValueError):
        maintenance.compact_display_archives(tmp_path, max_archives=maximum)
