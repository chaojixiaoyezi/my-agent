"""E2：经真实 claim/worker/归档入口验证常开复用、关闭语义与 owner 拒绝路径。"""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.settings.config import AgentConfig, load_config


def _save_record(repo, index):
    with repo._runtime_connection() as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (str(index), "saved"))
        conn.commit()
    return conn


# LLM: 原配置不再选择慢路径；真实claim/worker在四次借用中只打开一条物理连接。
# 函数用途: 验证无复用开关配置也始终复用本请求的连接。
def test_claimed_worker_always_reuses_connections(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model_backend: echo\n", encoding="utf-8")
    config = load_config(config_path)
    agent = SimpleAgent(config, tmp_path)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    repo = RuntimeRepository(tmp_path / "test-runtime.db")
    request_id = "sqlite-config-request"
    (paths.inbox / f"{request_id}.json").write_text(json.dumps({
        "id": request_id, "kind": "ask", "prompt": "本地回合", "created_at": 1,
    }), encoding="utf-8")
    connections, events = [], []
    active = [False]

    def audit(event, args):
        if active[0] and event == "sqlite3.connect":
            events.append(args[0])

    sys.addaudithook(audit)

    def handle(actual_agent, path, **_kwargs):
        assert actual_agent is agent
        assert path.parent == paths.processing
        active[0] = True
        try:
            for index in range(4):
                connections.append(_save_record(repo, index))
        finally:
            active[0] = False
        return {"id": request_id, "kind": "ask", "status": "ok", "response": "记录已保存"}

    monkeypatch.setattr(request_worker, "_handle_gateway_request", handle)
    assert request_worker._process_gateway_requests(agent, paths) == 1
    assert len(events) == 1
    assert (paths.terminal / f"{request_id}.json").is_file()
    assert not (paths.processing / f"{request_id}.json").exists()
    for conn in connections:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM metadata WHERE value='saved'").fetchone()[0] == 4


def test_reuse_does_not_bypass_owner_resolution_failure(tmp_path, monkeypatch):
    config = AgentConfig(model_backend="echo")
    agent = SimpleAgent(config, tmp_path)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    request_id = "owner-rejected"
    (paths.inbox / f"{request_id}.json").write_text(json.dumps({
        "id": request_id, "kind": "ask", "prompt": "隔离拒绝", "created_at": 1,
    }), encoding="utf-8")

    def reject(*_args):
        raise request_worker.OwnerScopeUnavailableError("身份不可用")

    def forbidden(*_args, **_kwargs):
        pytest.fail("owner 解析拒绝后不能执行回合")

    monkeypatch.setattr(request_worker, "_resolve_request_agent", reject)
    monkeypatch.setattr(request_worker, "_handle_gateway_request", forbidden)
    assert request_worker._process_gateway_requests(agent, paths) == 1
    response = json.loads((paths.terminal / f"{request_id}.json").read_text(encoding="utf-8"))
    assert response["error_code"] == "OWNER_SCOPE_UNAVAILABLE"


@pytest.mark.parametrize("status", ["ok", "error", "cancelled"])
def test_worker_terminal_paths_release_reused_connections_without_delaying_commits(tmp_path, monkeypatch, status):
    config = AgentConfig(model_backend="echo")
    agent = SimpleAgent(config, tmp_path)
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    repo = RuntimeRepository(tmp_path / "worker-probe.db")
    opened, borrowed = [], []
    connect = repo._runtime_connect

    def observe():
        conn = connect()
        opened.append(conn)
        return conn

    request_id = f"terminal-{status}"
    (paths.inbox / f"{request_id}.json").write_text(json.dumps({
        "id": request_id, "kind": "ask", "prompt": "保存当前事实", "created_at": 1,
    }), encoding="utf-8")

    def handle(_actual_agent, _path, **_kwargs):
        for index in range(2):
            borrowed.append(_save_record(repo, index))
            with sqlite3.connect(repo.db_path) as observer:
                assert observer.execute("SELECT count(*) FROM metadata WHERE value='saved'").fetchone()[0] == index + 1
        with repo._runtime_connection() as conn:
            borrowed.append(conn)
            conn.execute("INSERT INTO metadata VALUES('uncommitted', 'discard')")
        return {"id": request_id, "kind": "ask", "status": status, "response": "当前回合事实"}

    monkeypatch.setattr(repo, "_runtime_connect", observe)
    monkeypatch.setattr(request_worker, "_handle_gateway_request", handle)
    assert request_worker._process_gateway_requests(agent, paths) == 1
    assert len(opened) == 1
    terminal = json.loads((paths.terminal / f"{request_id}.json").read_text(encoding="utf-8"))
    assert terminal["status"] == status
    assert not (paths.processing / f"{request_id}.json").exists()
    for conn in borrowed:
        with pytest.raises(sqlite3.ProgrammingError) as caught:
            conn.execute("SELECT 1")
        assert caught.value.code == "SQLITE_LEASE_EXPIRED"
    with sqlite3.connect(repo.db_path) as observer:
        assert observer.execute("SELECT count(*) FROM metadata WHERE value='saved'").fetchone()[0] == 2
        assert observer.execute("SELECT 1 FROM metadata WHERE key='uncommitted'").fetchone() is None
