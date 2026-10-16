"""仅供测试观察物理连接身份；生产调用方不得解包租期层。"""

from agent_py_agent.agent.io.sqlite_lease import SQLiteConnectionLease


def physical_connection(conn):
    return conn._target if isinstance(conn, SQLiteConnectionLease) else conn


def assert_reused_lease(current, previous):
    assert isinstance(current, SQLiteConnectionLease)
    assert current is not previous
    assert physical_connection(current) is physical_connection(previous)
