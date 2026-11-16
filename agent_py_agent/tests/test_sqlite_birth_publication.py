"""主库出生FD只能在未发表名字下关闭；跨进程锁证明覆盖三个真实入口。"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.io.sqlite_scope import ensure_private_sqlite_birth
from agent_py_agent.agent.local_storage.schema import _private_sqlite_files
from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
from agent_py_agent.agent.scheduler.history_index import SchedulerHistoryIndex


def _invoke(kind, path):
    if kind == 'local':
        _private_sqlite_files(path, create=True)
    elif kind == 'scheduler':
        index = SchedulerHistoryIndex.__new__(SchedulerHistoryIndex)
        index.path, index.source = path, path.with_suffix('.jsonl')
        index._install()
    else:
        with OwnerSnapshotIndex.open(path):
            pass


def _probe(path):
    script = '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0)
try:
 c.execute('BEGIN EXCLUSIVE')
 print('acquired')
 c.rollback()
except sqlite3.OperationalError:
 print('blocked')
finally:c.close()
'''
    return subprocess.check_output([sys.executable, '-c', script, str(path)], text=True).strip()


@pytest.mark.parametrize('kind', ['local', 'scheduler', 'snapshot'])
def test_real_creation_closes_only_unpublished_inode_and_existing_lock_survives(tmp_path, monkeypatch, kind):
    path = tmp_path / 'main.sqlite3'
    original_close = os.close
    closed_births = []

    def close(descriptor):
        inode = os.fstat(descriptor)
        births = [p for p in tmp_path.glob('.main.sqlite3.birth-*')
                  if p.stat().st_ino == inode.st_ino and p.stat().st_dev == inode.st_dev]
        if births:
            assert not path.exists(), 'a raw DB fd must close before canonical publication'
            closed_births.append(inode.st_ino)
        return original_close(descriptor)

    monkeypatch.setattr(os, 'close', close)
    _invoke(kind, path)
    assert len(closed_births) == 1
    assert not list(tmp_path.glob('.main.sqlite3.birth-*'))

    # DELETE journal的主库record lock由另一SQLite连接持有；裸close缺陷会让子进程夺锁。
    keeper = sqlite3.connect(path, timeout=0)
    keeper.execute('PRAGMA journal_mode=DELETE')
    keeper.execute('CREATE TABLE IF NOT EXISTS keeper(value INTEGER)')
    keeper.commit()
    keeper.execute('BEGIN EXCLUSIVE')
    native_connect = sqlite3.connect

    def immediate(*args, **kwargs):
        kwargs['timeout'] = 0
        return native_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, 'connect', immediate)
    try:
        assert _probe(path) == 'blocked'
        try:
            _invoke(kind, path)
        except sqlite3.OperationalError as exc:
            assert 'locked' in str(exc).lower()
        assert keeper.in_transaction
        assert _probe(path) == 'blocked'
        assert len(closed_births) == 1
    finally:
        keeper.rollback()
        keeper.close()


def test_link_denied_does_not_fallback_to_published_raw_descriptor(tmp_path, monkeypatch):
    path = tmp_path / 'main.sqlite3'

    def denied(*args, **kwargs):
        raise PermissionError('synthetic file-link denied')

    monkeypatch.setattr(os, 'link', denied)
    with pytest.raises(PermissionError, match='SQLITE_BIRTH_PUBLICATION_DENIED.*not_started'):
        _private_sqlite_files(path, create=True)
    assert not path.exists()
    assert not list(tmp_path.glob('.main.sqlite3.birth-*'))


def test_racing_publication_preserves_winning_inode(tmp_path, monkeypatch):
    path = tmp_path / 'main.sqlite3'
    native_link = os.link

    def winner(source, target, **kwargs):
        Path(target).write_bytes(b'winning canonical bytes')
        return native_link(source, target, **kwargs)

    monkeypatch.setattr(os, 'link', winner)
    assert ensure_private_sqlite_birth(path) is False
    assert path.read_bytes() == b'winning canonical bytes'


@pytest.mark.skipif(os.open not in os.supports_dir_fd or os.link not in os.supports_dir_fd,
                    reason='platform has no descriptor-relative directory publication API')
def test_birth_publication_keeps_supplied_parent_descriptor_anchor(tmp_path):
    parent = tmp_path / 'parent'
    parent.mkdir()
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    moved = tmp_path / 'moved'
    try:
        parent.rename(moved)
        parent.mkdir()
        assert ensure_private_sqlite_birth(parent / 'main.sqlite3', parent_fd=descriptor)
        assert (moved / 'main.sqlite3').is_file()
        assert not (parent / 'main.sqlite3').exists()
        assert not list(moved.glob('.main.sqlite3.birth-*'))
        assert ensure_private_sqlite_birth(parent / 'main.sqlite3', parent_fd=descriptor) is False
    finally:
        os.close(descriptor)
