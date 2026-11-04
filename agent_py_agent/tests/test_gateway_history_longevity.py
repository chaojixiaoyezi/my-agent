"""按请求寻址的历史投影：规模、重放、交错和迁移证据。"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import io
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root


# LLM: 观察真实解析和历史正文读取，不替代产品查重或存储逻辑。
# 函数用途: 固定规模历史超过缓存边界后，每次追加仍保持预算。
@pytest.mark.parametrize("rows", [1000, 20000])
def test_history_append_has_fixed_read_decode_and_lock_budget(tmp_path, monkeypatch, rows):
    paths = gateway_paths_from_root(tmp_path / str(rows))
    paths.root.mkdir()
    with paths.history.open("w", encoding="utf-8") as handle:
        for number in range(rows):
            handle.write(json.dumps({"id": f"old-{number}", "content": "x" * 512}) + "\n")
    decoded, read_bytes, lock_seconds = [], [], []
    original_loads, original_open = json.loads, Path.open
    original_transition = io.locked_file_transition

    def loads(value, *args, **kwargs):
        decoded.append(1)
        return original_loads(value, *args, **kwargs)

    class ObservedRead:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args):
            value = self.handle.read(*args)
            read_bytes.append(len(value.encode("utf-8") if isinstance(value, str) else value))
            return value

        def readline(self, *args):
            value = self.handle.readline(*args)
            read_bytes.append(len(value.encode("utf-8") if isinstance(value, str) else value))
            return value

    def observed_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return ObservedRead(handle) if path == paths.history and "r" in mode else handle

    @contextmanager
    def transition(path, **kwargs):
        with original_transition(path, **kwargs):
            start = time.monotonic()
            yield
            lock_seconds.append(time.monotonic() - start)

    monkeypatch.setattr(json, "loads", loads)
    monkeypatch.setattr(Path, "open", observed_open)
    monkeypatch.setattr(io, "locked_file_transition", transition)
    payload = {"id": "fresh", "status": "done", "content": "finished"}
    assert io.append_gateway_history_once(paths, payload) is True
    assert io.append_gateway_history_once(paths, payload) is False
    assert sum(read_bytes) <= 256 * 1024
    assert len(decoded) <= 260
    assert max(lock_seconds) < 1.0


def _index(paths):
    name = "agent_py_agent.agent.gateway_parts.gateway_history_index"
    assert importlib.util.find_spec(name) is not None, "request-keyed history projection is missing"
    return importlib.import_module(name).GatewayHistoryIndex(paths)


# LLM: 正文大小变化与原生SQLite点查共同证明只修复目标行，旧JSONL不是新完成权威。
# 函数用途: 重启后幂等、冲突单行修正及旧历史保持不变。
def test_history_replay_conflict_and_restart_are_point_updates(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    first = {"id": "a", "status": "done", "content": "short"}
    second = {"id": "b", "status": "done", "content": "keep"}
    assert io.append_gateway_history_once(paths, first)
    assert io.append_gateway_history_once(paths, second)
    index = _index(paths)
    before = index.page(after="", limit=128)
    assert io.append_gateway_history_once(paths, first) is False
    corrected = {**first, "content": "corrected" * 32}
    assert io.append_gateway_history_once(paths, corrected) is True
    assert _index(paths).get("a") == corrected
    assert _index(paths).get("b") == second
    assert len(index.page(after="", limit=128)) == len(before) == 2
    assert io.append_gateway_history_once(paths, corrected) is False


def test_history_concurrent_completions_and_replay_commit_once(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    barrier = threading.Barrier(3)
    results, errors = [], []

    def append(request_id):
        try:
            barrier.wait(timeout=5)
            results.append(io.append_gateway_history_once(paths, {"id": request_id, "status": "done"}))
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=append, args=(key,)) for key in ("one", "two", "one")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert errors == []
    assert sorted(results) == [False, True, True]
    assert {row["id"] for row in _index(paths).page(after="", limit=128)} == {"one", "two"}


# LLM: 每批同时限制行数与字节，字节游标与错误随SQLite事务提交；新写者覆盖优先于旧行。
# 函数用途: 验证坏行、超长行、并发冲突及重建对象后的迁移收敛。
def test_legacy_migration_is_resumable_and_live_row_wins(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    paths.history.write_text("\n".join([
        json.dumps({"id": f"old-{number}", "content": "x" * 3000}) for number in range(300)
    ]) + '\n{bad row\n' + 'x' * 300000 + '\n' + json.dumps({"id": "last", "status": "done"}), encoding="utf-8")
    index = _index(paths)
    report = index.migrate_batch()
    assert 0 < report["bytes_read"] <= 256 * 1024
    assert report["lines_read"] <= 128
    assert report["complete"] is False
    live = {"id": "old-299", "content": "current canonical"}
    assert io.append_gateway_history_once(paths, live)
    last_cursor = report["cursor"]
    for _ in range(30):
        report = _index(paths).migrate_batch()
        assert report["cursor"] >= last_cursor
        assert report["bytes_read"] <= 256 * 1024
        assert report["lines_read"] <= 128
        last_cursor = report["cursor"]
        if report["complete"]:
            break
    assert report["complete"] is True
    assert index.get("old-299") == live
    assert index.get("last")["status"] == "done"
    assert len(index.migration_errors()) == 2


def test_history_rows_are_isolated_between_roots(tmp_path):
    left, right = [gateway_paths_from_root(tmp_path / name) for name in ("left", "right")]
    io.append_gateway_history_once(left, {"id": "same", "content": "left"})
    io.append_gateway_history_once(right, {"id": "same", "content": "right"})
    assert _index(left).get("same")["content"] == "left"
    assert _index(right).get("same")["content"] == "right"


# LLM: 语法守卫只拒绝真实热入口全历史读取；反例同规则必须被拦。
# 函数用途: 防止全历史快照重新进入正常append路径。
def _history_guard(source):
    module = ast.parse(source)
    problems = []
    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef) and node.name == "append_gateway_history_once":
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    name = getattr(call.func, "id", "") or getattr(call.func, "attr", "")
                    if name in {"_gateway_history_snapshot", "read_text", "read_bytes"}:
                        problems.append((call.lineno, name))
    return problems


def test_history_hot_path_ast_guard_and_invalid_fixture():
    assert _history_guard(Path(io.__file__).read_text()) == []
    assert _history_guard("def append_gateway_history_once(paths, payload):\n    return _gateway_history_snapshot(paths.history)\n")


def _descriptor_count():
    if os.name == "nt":
        import ctypes
        count = ctypes.c_ulong()
        kernel = ctypes.windll.kernel32
        assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(), ctypes.byref(count))
        return count.value
    directory = "/proc/self/fd" if Path("/proc/self/fd").exists() else "/dev/fd"
    return len(os.listdir(directory))


def test_history_append_closes_file_descriptors(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    io.append_gateway_history_once(paths, {"id": "warm"})
    before = _descriptor_count()
    for number in range(20):
        assert io.append_gateway_history_once(paths, {"id": str(number)})
    after = _descriptor_count()
    assert after - before == 0


def test_history_projection_files_are_private(tmp_path):
    import stat
    paths = gateway_paths_from_root(tmp_path / "new")
    io.append_gateway_history_once(paths, {"id": "private"})
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700
    index = _index(paths)
    assert stat.S_IMODE(index.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.history.stat().st_mode) == 0o600
    with sqlite3.connect(index.path) as connection:
        assert connection.execute("SELECT count(*) FROM history").fetchone()[0] == 1


# LLM: 同一载荷的重放必须连投影事务也不提交变化，不能只把返回值写成False。
# 函数用途: 通过另一条真实SQLite连接证明重放不改已持久数据。
def test_identical_replay_does_not_commit_database_changes(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    payload = {"id": "unchanged", "content": "same"}
    assert io.append_gateway_history_once(paths, payload)
    with sqlite3.connect(_index(paths).path) as connection:
        before = connection.execute("PRAGMA data_version").fetchone()[0]
        assert io.append_gateway_history_once(paths, payload) is False
        assert connection.execute("PRAGMA data_version").fetchone()[0] == before


# LLM: 模拟单次导出写失败，不伪造canonical或索引读回；持久意图须在重开后自行恢复。
# 函数用途: 验证SQLite提交与JSONL导出之间的中断不会导致重复导出或丢正文。
@pytest.mark.parametrize("published", [False, True])
def test_interrupted_export_replays_durable_intent(tmp_path, monkeypatch, published):
    paths = gateway_paths_from_root(tmp_path)
    index = _index(paths)
    module = importlib.import_module(type(index).__module__)
    original = module.append_private_jsonl_records

    def interrupted(*args, **kwargs):
        if published:
            original(*args, **kwargs)
        raise OSError("simulated I/O error")

    payload = {"id": "interrupted", "content": "keep canonical summary"}
    with monkeypatch.context() as patch:
        patch.setattr(module, "append_private_jsonl_records", interrupted)
        with pytest.raises(OSError, match="simulated I/O error"):
            io.append_gateway_history_once(paths, payload)
    assert _index(paths).get(payload["id"]) == payload
    assert io.append_gateway_history_once(paths, payload) is False
    assert [json.loads(row) for row in paths.history.read_text().splitlines()] == [payload]


def test_new_exports_are_segmented_without_rewriting_old_history(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    for number in range(128):
        assert io.append_gateway_history_once(paths, {"id": str(number), "content": "x" * 256})
    old = paths.history.read_bytes()
    assert io.append_gateway_history_once(paths, {"id": "next", "content": "new"})
    assert paths.history.read_bytes() == old
    assert list((paths.root / "history_segments").glob("*.jsonl"))


def test_partial_export_is_repaired_before_next_request(tmp_path, monkeypatch):
    paths = gateway_paths_from_root(tmp_path)
    module = importlib.import_module(type(_index(paths)).__module__)
    payload = {"id": "partial", "content": "not lost"}

    def partial(path, rows, **_kwargs):
        path.write_bytes((json.dumps(rows[0], ensure_ascii=False, sort_keys=True) + "\n").encode()[:12])
        raise OSError("partial write")

    with monkeypatch.context() as patch:
        patch.setattr(module, "append_private_jsonl_records", partial)
        with pytest.raises(OSError, match="partial write"):
            io.append_gateway_history_once(paths, payload)
    assert io.append_gateway_history_once(paths, {"id": "after-partial"})
    assert [json.loads(line)["id"] for line in paths.history.read_text().splitlines()] == ["partial", "after-partial"]
    assert _index(paths).get("partial") == payload


def test_legacy_source_replacement_and_initially_empty_source_are_ingested(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    index = _index(paths)
    assert index.migrate_batch()["complete"]
    paths.history.write_text('{"id":"from-old-writer"}\n')
    assert index.migrate_batch()["complete"]
    assert index.get("from-old-writer") == {"id": "from-old-writer"}
    assert io.append_gateway_history_once(paths, {"id": "live", "content": "canonical"})
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text('{"id":"replacement-old"}\n{"id":"live","content":"stale"}\n')
    replacement.replace(paths.history)
    assert io.append_gateway_history_once(paths, {"id": "after-replace"})
    assert index.get("replacement-old") == {"id": "replacement-old"}
    assert index.get("live")["content"] == "canonical"


def test_valid_large_legacy_row_is_not_discarded_as_a_byte_budget_error(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    payload = {"id": "large-legacy", "response": "x" * 300000}
    paths.history.write_text(json.dumps(payload) + "\n")
    for _ in range(10):
        report = _index(paths).migrate_batch()
        assert report["bytes_read"] <= 256 * 1024
        assert report["lines_read"] <= 128
        if report["complete"]:
            break
    assert report["complete"]
    assert _index(paths).get(payload["id"]) == payload
    assert _index(paths).migration_errors() == []


# LLM: 损坏的是派生数据库，doctor必须保留准确来源并结构化报错，不能中止其它重建。
# 函数用途: 检查历史投影损坏时的诊断路径，不删除坏文件伪造成功。
def test_doctor_reports_projection_database_corruption_at_database_path(tmp_path):
    from agent_py_agent.agent.gateway_parts.queue_service import (
        _rebuild_gateway_history_index_report,
    )

    paths = gateway_paths_from_root(tmp_path)
    index = _index(paths)
    index.path.write_bytes(b"not sqlite")
    report = _rebuild_gateway_history_index_report(object(), paths)
    assert report.indexed_count == 0
    assert len(report.load_errors) == 1
    assert report.load_errors[0]["path"] == str(index.path)


# LLM: 原HTTP监听/认证/worker/终态写账均真实；唯一离线边界是正式echo后端，不联外部模型。
# 函数用途: 大旧历史下走/ask到终态，重开监听后同消息重放仍只有一个终态和历史键。
def test_real_http_ask_terminal_restart_and_history_lookup(tmp_path):
    import urllib.request

    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import _process_gateway_requests, gateway_paths
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.settings import AgentConfig

    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), gateway_workspace="gateway")
    agent = SimpleAgent(config, tmp_path)
    agent.backend = get_backend("echo", config)
    paths = gateway_paths(agent)
    paths.root.mkdir(parents=True, exist_ok=True)
    with paths.history.open("w") as handle:
        for number in range(1000):
            handle.write(json.dumps({"id": f"legacy-{number}", "content": "x" * 5120}) + "\n")
    assert paths.history.stat().st_size > 4 * 1024 * 1024

    def ask(server):
        body = {"goal": "真实历史规模验证", "delivery_intent": "queue", "metadata": {"message_id": "w8-history-wire"}}
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server.server_address[1]}/ask", json.dumps(body).encode(),
            {"Content-Type": "application/json", "X-Gateway-Token": server._local_client_credential},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            return json.loads(response.read())["request_id"]

    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        request_id = ask(server)
        assert _process_gateway_requests(agent, paths, worker_id="w8-http-wire") == 1
        canonical = io.read_json_file(paths.terminal / f"{request_id}.json")
        expected = canonical["terminal_response"]
        assert expected["ok"] is True
        assert _index(paths).get(request_id) == expected
    finally:
        server.stop()
    restarted = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    restarted.start()
    try:
        assert ask(restarted) == request_id
        assert _process_gateway_requests(agent, paths, worker_id="w8-http-replay") == 0
        assert io.append_gateway_history_once(paths, expected) is False
        assert io.read_json_file(paths.terminal / f"{request_id}.json") == canonical
        assert _index(paths).get(request_id) == expected
        with sqlite3.connect(_index(paths).path) as connection:
            assert connection.execute("SELECT count(*) FROM history WHERE request_id=?", (request_id,)).fetchone()[0] == 1
        from agent_py_agent.agent.gateway_parts.queue_service import rebuild_gateway_index_report

        rebuilt = rebuild_gateway_index_report(agent, paths)
        assert rebuilt.load_errors == []
        assert rebuilt.indexed_count >= 1001
    finally:
        restarted.stop()
