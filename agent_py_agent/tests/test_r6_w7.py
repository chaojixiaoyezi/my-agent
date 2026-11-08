"""真实生产连接必须加入 w6 代次门；本回归不宣称自动换库已验收。"""
import pytest

from agent_py_agent.agent.runtime_db import recovery_guard as guard
from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_rdb_w3_runtime_db_salvage import _source


def _blocked(path):
    with pytest.raises(guard.RuntimeDatabaseDeferred):
        with guard.runtime_db_generation_guard(path, exclusive=True):
            pytest.fail("物理连接仍存活，不能放行换库")


@pytest.mark.parametrize("mode", ["existing_only", "transaction", "read_only", "source"])
def test_real_runtime_connections_block_ex_until_physical_close(tmp_path, monkeypatch, mode):
    source = _source(tmp_path / "runtime.db")
    if mode == "source":
        original = salvage._copy_table

        def copy(conn, table):
            _blocked(source)
            return original(conn, table)

        monkeypatch.setattr(salvage, "_copy_table", copy)
        assert salvage.salvage_runtime_db(source, tmp_path / "candidate.db").ok
    else:
        repo = RuntimeRepository(source, read_only=mode == "read_only", existing_only=mode == "existing_only")
        context = repo.transaction() if mode == "transaction" else repo._runtime_connection()
        with context:
            _blocked(source)
    with guard.runtime_db_generation_guard(source, exclusive=True):
        pass
