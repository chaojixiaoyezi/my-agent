"""真实插话读写不应为缺失队列保留来源投影。"""

import sqlite3
from contextlib import closing

import pytest

from agent_py_agent.agent.conversation import guidance_pending_index
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig


@pytest.fixture
def store(tmp_path):
    agent = SimpleAgent(
        AgentConfig(
            model_backend="echo",
            my_agent_home=str(tmp_path / "home"),
            auto_save_memory=False,
        ),
        tmp_path,
    )
    return agent.conversation_store


def _projection_path(tmp_path):
    paths = list(tmp_path.rglob(".guidance-pending.sqlite3"))
    assert len(paths) == 1
    return paths[0]


def _rows(path, query, parameters=()):
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        return conn.execute(query, parameters).fetchall()


def test_missing_targets_do_not_create_null_sources(store, tmp_path):
    for i in range(3):
        assert store.guidance.pending_report("agent_run", f"gwreq-msg-missing{i}", limit=5) == (
            [], []
        )

    path = _projection_path(tmp_path)
    assert _rows(path, "SELECT COUNT(*) FROM sources WHERE stamp='null'")[0][0] == 0


def test_first_install_prunes_legacy_null_sources(store, tmp_path):
    entry = store.guidance.append(
        {"target_type": "thread", "target_id": "live", "message": "保留真实来源"}
    )
    assert store.guidance.pending_report("thread", "live", limit=5) == ([entry], [])
    path = _projection_path(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO sources VALUES (?,?,?)", ("/legacy/ghost.jsonl", "null", 0)
        )
        conn.commit()
    assert _rows(path, "SELECT COUNT(*) FROM sources WHERE stamp='null'")[0][0] == 1

    # 清空安装缓存模拟进程重启；不改变磁盘代次或真实队列。
    with guidance_pending_index._INSTALLED_LOCK:
        guidance_pending_index._INSTALLED.clear()
    assert store.guidance.pending_report("thread", "live", limit=5) == ([entry], [])

    assert _rows(path, "SELECT COUNT(*) FROM sources WHERE stamp='null'")[0][0] == 0
    source = str(store.storage.guidance_path("thread", "live"))
    assert _rows(path, "SELECT path FROM sources") == [(source,)]


def test_vanished_queue_removes_source_entries_and_pending(store, tmp_path):
    entry = store.guidance.append(
        {"target_type": "thread", "target_id": "vanished", "message": "待删除队列"}
    )
    assert store.guidance.pending_report("thread", "vanished", limit=5) == ([entry], [])
    path = _projection_path(tmp_path)
    source = store.storage.guidance_path("thread", "vanished")
    assert _rows(path, "SELECT path FROM sources WHERE path=?", (str(source),))
    assert _rows(path, "SELECT id FROM entries") == [(entry.guidance_id,)]
    assert _rows(path, "SELECT id FROM pending") == [(entry.guidance_id,)]

    source.unlink()
    assert store.guidance.ledger.pending_index().refresh("thread", "vanished")

    assert _rows(path, "SELECT path FROM sources WHERE path=?", (str(source),)) == []
    assert _rows(path, "SELECT id FROM entries") == []
    assert _rows(path, "SELECT id FROM pending") == []
