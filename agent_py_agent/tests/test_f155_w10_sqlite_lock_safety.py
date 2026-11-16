"""真实跨进程 record lock 与 WAL 生命周期，禁止裸数据库 FD 破坏 SQLite 锁。"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.recovery_pending_index import private_projection_connection
from agent_py_agent.agent.io.readonly_sqlite import readonly_sqlite_connection

pytestmark = pytest.mark.skipif(sys.platform == 'win32', reason='验证 POSIX record lock 关闭语义')


def _child(db, script):
    return subprocess.run([sys.executable, '-c', script, str(db)], check=True,
                          capture_output=True, text=True, timeout=10).stdout.strip()


def _locked(db, *, sqlite=False):
    if sqlite:
        script = '''import sqlite3,sys
c=sqlite3.connect(sys.argv[1],timeout=0)
try:
 c.execute('BEGIN IMMEDIATE')
 print('acquired')
except sqlite3.OperationalError as e:
 print('blocked' if getattr(e,'sqlite_errorcode',None)==sqlite3.SQLITE_BUSY else 'unexpected:'+str(e))
finally: c.close()
'''
    else:
        script = '''import errno,fcntl,os,sys
f=os.open(sys.argv[1],os.O_RDWR)
try:
 fcntl.lockf(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
 print('acquired')
except OSError as e:
 print('blocked' if e.errno in (errno.EACCES,errno.EAGAIN) else 'unexpected:'+str(e))
finally: os.close(f)
'''
    return _child(db, script)


@pytest.mark.parametrize('mode', ['DELETE', 'WAL'])
@pytest.mark.parametrize('adapter', ['readonly', 'projection_writer'])
def test_adapter_preserves_other_live_connection_record_locks(tmp_path, mode, adapter):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'proof.sqlite3'
    with closing(sqlite3.connect(path)) as live:
        live.execute('PRAGMA journal_mode=' + mode)
        live.execute('CREATE TABLE facts(value)')
        live.execute('INSERT INTO facts VALUES(1)')
        live.commit()
        live.execute('BEGIN IMMEDIATE')
        live.execute('INSERT INTO facts VALUES(2)')
        assert _locked(path) == _locked(path, sqlite=True) == 'blocked'
        manager = readonly_sqlite_connection(path) if adapter == 'readonly' else private_projection_connection(path)
        with manager as observer:
            assert observer.execute('SELECT count(*) FROM facts').fetchone() == (1,)
        # 锁状态由独立进程实际申请，不依赖 mock、FD清单或实现形态。
        assert _locked(path) == 'blocked'
        assert _locked(path, sqlite=True) == 'blocked'
        live.rollback()


@pytest.mark.parametrize('adapter', ['readonly', 'projection_writer'])
def test_adapter_keeps_wal_identity_and_commits_when_foreign_connection_closes(tmp_path, adapter):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    path = root / 'proof.sqlite3'
    wal = Path(str(path) + '-wal')
    with closing(sqlite3.connect(path)) as live:
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('CREATE TABLE facts(value)')
        live.execute('INSERT INTO facts VALUES(1)')
        live.commit()
        live.execute('SELECT * FROM facts').fetchall()
        identity = wal.stat().st_dev, wal.stat().st_ino
        manager = readonly_sqlite_connection(path) if adapter == 'readonly' else private_projection_connection(path)
        with manager as observer:
            assert observer.execute('SELECT count(*) FROM facts').fetchone() == (1,)
        _child(path, "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
                     "c.execute('INSERT INTO facts VALUES(2)');c.commit();c.close()")
        assert (wal.stat().st_dev, wal.stat().st_ino) == identity
        live.execute('INSERT INTO facts VALUES(3)')
        live.commit()
        assert live.execute('SELECT count(*) FROM facts').fetchone() == (3,)
        result = _child(path, "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);"
                       "print(c.execute('SELECT count(*) FROM facts').fetchone()[0]);"
                       "print(c.execute('PRAGMA integrity_check').fetchone()[0]);c.close()")
        assert result == '3\nok'
