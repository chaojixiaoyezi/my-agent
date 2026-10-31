"""已回填但非空的旧WAL也不能在主库替换后重放；截断不能继承业务忙等。"""

import sqlite3

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import RuntimeWriteFence
from agent_py_agent.tests.support import sqlite_lease_assertions


# RuntimeDB 写借用外包 RuntimeWriteFence；租期与物理连接身份观察取栅栏内层借用。
def _lease_layer(conn):
    return conn._connection if isinstance(conn, RuntimeWriteFence) else conn


def assert_reused_lease(current, previous):
    sqlite_lease_assertions.assert_reused_lease(_lease_layer(current), _lease_layer(previous))


def test_return_clears_wal_frames_and_restores_business_busy_timeout(tmp_path):
    path = tmp_path / 'reset.db'
    with sqlite_connection_scope(True):
        repo = RuntimeRepository(path)
        statements = []
        with repo._runtime_connection() as writer:
            writer.set_trace_callback(statements.append)
            writer.execute("INSERT INTO metadata VALUES('saved', 'new')")
            writer.commit()
        assert path.with_name('reset.db-wal').stat().st_size == 0
        assert 'PRAGMA busy_timeout=0' in statements
        assert 'PRAGMA wal_checkpoint(TRUNCATE)' in statements
        with repo._runtime_connection() as next_borrow:
            assert_reused_lease(next_borrow, writer)
            assert next_borrow.execute('PRAGMA busy_timeout').fetchone()[0] == 30000
            assert next_borrow.execute("SELECT value FROM metadata WHERE key='saved'").fetchone()[0] == 'new'


def test_reader_at_latest_frame_retires_writer_without_busy_wait_or_data_loss(tmp_path):
    path = tmp_path / 'reader.db'
    repo = RuntimeRepository(path)
    reader = sqlite3.connect(path)
    try:
        _latest_reader_return(repo, reader)
    finally:
        reader.close()
    with repo._runtime_connection() as final:
        assert [row[0] for row in final.execute('SELECT value FROM latest_rows ORDER BY value')] == [1, 2, 3]


def _latest_reader_return(repo, reader):
    with sqlite_connection_scope(True):
        with repo._runtime_connection() as writer:
            # RuntimeDB 借用拒绝隐式提交脚本；建表与写入经代际栅栏后显式提交，读者仍停在最新帧。
            writer.execute('CREATE TABLE latest_rows(value)')
            writer.execute('INSERT INTO latest_rows VALUES(1),(2),(3)')
            writer.commit()
            statements = []
            writer.set_trace_callback(statements.append)
            cursor = reader.execute('SELECT value FROM latest_rows ORDER BY value')
            assert cursor.fetchone()[0] == 1
            physical = writer._target
        assert 'PRAGMA busy_timeout=0' in statements
        assert 'PRAGMA wal_checkpoint(TRUNCATE)' in statements
        assert cursor.fetchone()[0] == 2
        cursor.close()
        try:
            physical.execute('SELECT 1')
        except sqlite3.ProgrammingError:
            return
        raise AssertionError('writer with a live latest-frame reader must be retired')
