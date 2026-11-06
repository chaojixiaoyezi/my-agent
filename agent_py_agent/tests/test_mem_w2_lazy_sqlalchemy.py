from __future__ import annotations

# LLM: MEM-W2 只把 SQLAlchemy 的导入推迟到真正构造 StorageBackend / 访问 PgVectorStore 时；默认 SQLite 路径不再常驻它。
# 模块用途: 回归保护 Gateway、TUI 与工具子进程不因 retrieval/scheduler 导入链加载 SQLAlchemy（实测每进程约 10–13 MB）。
import subprocess
import sys

import pytest

from agent_py_agent.agent import storage_backend


# 函数用途: 在全新解释器里执行一段脚本并返回标准输出（隔离 HOME，避免写真实目录）。
def _fresh(tmp_path, script: str) -> str:
    env = {"HOME": str(tmp_path), "MY_AGENT_HOME": str(tmp_path / ".my-agent"), "PATH": "/usr/bin:/bin"}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=120, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


# LLM: 默认链（工具层、调度器、检索包、存储后端模块本身）都不得把 SQLAlchemy 带进进程。
# 函数用途: 确认导入默认运行链后 sys.modules 里没有 sqlalchemy。
def test_default_import_chain_does_not_load_sqlalchemy(tmp_path):
    out = _fresh(tmp_path, (
        "import sys\n"
        "import agent_py_agent.agent.tooling, agent_py_agent.agent.scheduler, agent_py_agent.agent.retrieval\n"
        "from agent_py_agent.agent.storage_backend import ensure_sqlite_wal\n"
        "from agent_py_agent.agent.retrieval import HybridRetriever, cosine\n"
        "print('SQLA=' + str(any(m == 'sqlalchemy' or m.startswith('sqlalchemy.') for m in sys.modules)))\n"
    ))
    assert "SQLA=False" in out, out


# LLM: 原公开入口保持可用：包级 PgVectorStore 首次访问时才加载，得到的是同一个类。
# 函数用途: 确认 retrieval.PgVectorStore 的惰性入口与直接导入等价。
def test_pgvector_store_package_attribute_still_resolves(tmp_path):
    pytest.importorskip("sqlalchemy")
    out = _fresh(tmp_path, (
        "from agent_py_agent.agent.retrieval import PgVectorStore\n"
        "from agent_py_agent.agent.retrieval.pgvector_store import PgVectorStore as Direct\n"
        "import agent_py_agent.agent.retrieval as pkg\n"
        "print('SAME=' + str(PgVectorStore is Direct and 'PgVectorStore' in pkg.__all__))\n"
    ))
    assert "SAME=True" in out, out


# 函数用途: 确认未知属性仍抛 AttributeError（PEP 562 入口不能吞掉拼写错误）。
def test_retrieval_unknown_attribute_still_raises():
    import agent_py_agent.agent.retrieval as retrieval

    with pytest.raises(AttributeError):
        retrieval.NoSuchRetriever  # noqa: B018


# LLM: 真正构造 SQLite StorageBackend 时才导入 SQLAlchemy，连接/事务/表名/标量查询行为不变。
# 函数用途: 端到端核对 SQLite 后端在惰性导入后的读写与查询。
def test_storage_backend_sqlite_round_trip_after_lazy_import(tmp_path):
    pytest.importorskip("sqlalchemy")
    db = storage_backend.StorageBackend.for_path(tmp_path / "store.db")
    try:
        from sqlalchemy import text

        with db.begin() as conn:
            conn.execute(text("CREATE TABLE t (k INTEGER PRIMARY KEY, v TEXT)"))
            conn.execute(text("INSERT INTO t (v) VALUES ('x')"))
        assert db.table_names() == ["t"]
        assert db.scalar("SELECT count(*) FROM t") == 1
    finally:
        db.dispose()


# 函数用途: 确认没装 SQLAlchemy 时仍是构造时报原错误，而不是导入模块时失败。
def test_storage_backend_without_sqlalchemy_raises_at_construction(monkeypatch, tmp_path):
    monkeypatch.setattr(storage_backend, "_HAS_SQLALCHEMY", False)
    with pytest.raises(RuntimeError, match="SQLAlchemy"):
        storage_backend.StorageBackend.for_path(tmp_path / "x.db")
