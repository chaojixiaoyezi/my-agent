"""模拟portable平台分支；不将本机回归称为Windows native验收。"""
import sqlite3
import stat
from contextlib import closing
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.io import readonly_sqlite as reader
from agent_py_agent.agent.runtime_db.recovery_guard import retire_idle_runtime_connections
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def portable(monkeypatch):
    monkeypatch.setattr(reader, '_windows_sqlite_platform', lambda: True)


def test_portable_runtime_readonly_query_uses_no_raw_fd_or_application_locks(tmp_path, portable, monkeypatch):
    directory = tmp_path / 'canonical'
    directory.mkdir()
    path = directory / 'runtime.db'
    repo = RuntimeRepository(path)
    task = repo.create_task(owner_id='owner', conversation_task_id='task')
    before = path.read_bytes()
    def forbidden(*_args, **_kwargs):
        raise AssertionError('portable readonly must not open raw file/directory descriptors')
    with monkeypatch.context() as context:
        context.setattr(reader.os, 'open', forbidden)
        context.setattr(reader.os, 'chmod', forbidden)
        context.setattr(reader.os, 'geteuid', forbidden, raising=False)
        context.setattr(reader.os, 'getegid', forbidden, raising=False)
        assert RuntimeRepository(path, read_only=True).get_task(task['task_id'])['owner_id'] == 'owner'
    assert path.read_bytes() == before
    # 写端句柄确认退役后删除原应用锁，只读不得重新创建。
    retire_idle_runtime_connections(path)
    path.with_name('runtime.db.generation.lock').unlink(missing_ok=True)
    assert RuntimeRepository(path, read_only=True).get_task(task['task_id'])
    assert not path.with_name('runtime.db.generation.lock').exists()


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_portable_leaf_and_sidefiles_reject_reparse(tmp_path, portable, monkeypatch, suffix):
    path = tmp_path / 'runtime.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE sample(value)')
    if suffix:
        path.with_name(path.name + suffix).touch()
    original = reader.Path.lstat
    def observe(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if target == path.with_name(path.name + suffix):
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                   st_dev=info.st_dev, st_ino=info.st_ino, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(reader.Path, 'lstat', observe)
    with pytest.raises(reader.ProjectionReadUnavailableError, match='reparse'):
        with reader.readonly_sqlite_connection(path):
            pytest.fail('unsafe source reached SQLite reader')


def test_portable_parent_reparse_is_rejected(tmp_path, portable, monkeypatch):
    path = tmp_path / 'runtime.db'
    path.touch()
    original = reader.Path.lstat
    def observe(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if target == tmp_path:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_nlink=1,
                                   st_dev=info.st_dev, st_ino=info.st_ino, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(reader.Path, 'lstat', observe)
    with pytest.raises(reader.ProjectionReadUnavailableError, match='reparse'):
        with reader.readonly_sqlite_connection(path):
            pytest.fail('unsafe parent reached SQLite reader')


def test_portable_parent_replacement_is_detected_before_return(tmp_path, portable, monkeypatch):
    directory = tmp_path / 'parent'
    directory.mkdir()
    path = directory / 'runtime.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE sample(value)')
    original = reader.Path.lstat
    changed = False
    def observe(target, *args, **kwargs):
        info = original(target, *args, **kwargs)
        if changed and target == directory:
            return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)
        return info
    monkeypatch.setattr(reader.Path, 'lstat', observe)
    with pytest.raises(reader.ProjectionReadUnavailableError, match='identity changed'):
        with reader.readonly_sqlite_connection(path) as conn:
            assert conn.execute('SELECT count(*) FROM sample').fetchone()[0] == 0
            changed = True
