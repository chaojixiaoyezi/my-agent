"""上线历史索引批次与多进程请求级 SQLite 作用域合并后的真实存储入口。"""
import pytest

from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage import LocalStore


# LLM: 合并后两个连接优化必须可叠加，不能把每次操作未提交的数据带到下一次提交；只访问临时测试库。
# 函数用途: 同时开启请求作用域与历史索引批次，核对物理复用及独立提交。
@pytest.mark.parametrize("scope_enabled", [False, True])
def test_batch_inside_request_scope_preserves_operation_boundaries(tmp_path, monkeypatch, scope_enabled):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    with store._connection() as conn:
        conn.execute("CREATE TABLE merge_probe (value TEXT)")
        conn.commit()
    opened = []
    original = store._connect

    def connect():
        conn = original()
        opened.append(conn)
        return conn

    monkeypatch.setattr(store, "_connect", connect)
    with sqlite_connection_scope(scope_enabled), store.connection_batch():
        with store._connection() as conn:
            conn.execute("INSERT INTO merge_probe VALUES ('uncommitted')")
        with store.connection_batch(), store._connection() as conn:
            assert conn.execute("SELECT value FROM merge_probe").fetchall() == []
            conn.execute("INSERT INTO merge_probe VALUES ('committed')")
            conn.commit()
        with store._connection() as conn:
            assert [row[0] for row in conn.execute("SELECT value FROM merge_probe")] == ["committed"]
    assert len(opened) == 1
    reopened = LocalStore(tmp_path / "local.db", enable_fts=False)
    with reopened._connection() as conn:
        assert [row[0] for row in conn.execute("SELECT value FROM merge_probe")] == ["committed"]
