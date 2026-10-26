from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.models import AgentRunResult
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import (
    AdapterPaths,
    GatewayAskParams,
    _process_gateway_requests,
    gateway_paths,
    gateway_stale_processing,
    process_file_adapter_once,
    read_json_file,
    recover_gateway_processing_requests,
    submit_gateway_ask,
    write_json_file,
)
from agent_py_agent.agent.gateway_parts.io import gateway_response_path
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_request_path
from agent_py_agent.agent.io import append_jsonl
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import JsonlMemory
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.cli.local_doctor import build_local_doctor_report, rebuild_local_store


def test_local_store_records_events_and_searches():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")

        record = store.upsert_record(
            source_type="memory",
            source_id="demo-1",
            title="偏好记录",
            content="用户喜欢清晰的表格和可追溯测试证据。",
            metadata={"kind": "preference"},
        )

        assert record.id.startswith("rec-")
        assert (root / "files" / f"{record.id}.txt").exists()
        hits = store.search("表格", source_type="memory")
        assert len(hits) == 1
        assert hits[0].metadata["kind"] == "preference"
        assert "测试证据" in hits[0].content

        stats = store.stats()
        assert stats["record_count"] == 1
        assert stats["event_count"] == 1

        events = (root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(events) == 1
        assert json.loads(events[0])["event_type"] == "record_upserted"


def test_locked_jsonl_append_preserves_complete_lines_under_threads():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        path = root / "events.jsonl"
        total_threads = 8
        per_thread = 25

        def writer(worker: int) -> None:
            for index in range(per_thread):
                append_jsonl(path, {"worker": worker, "index": index})

        threads = [
            threading.Thread(target=writer, args=(worker,)) for worker in range(total_threads)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        lines = path.read_text(encoding="utf-8").splitlines()
        records = [json.loads(line) for line in lines]

        assert len(records) == total_threads * per_thread
        assert (
            len({(item["worker"], item["index"]) for item in records}) == total_threads * per_thread
        )


def test_local_store_like_search_when_fts_disabled():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db", enable_fts=False)
        store.upsert_record(
            source_type="note",
            source_id="like-source",
            title="like search demo",
            content="FTS5 关闭时也应该能用 LIKE 搜到关键内容。",
        )

        stats = store.stats()
        assert stats["fts5_enabled"] is False
        hits = store.search("关键内容", source_type="note")
        assert len(hits) == 1
        assert hits[0].source_id == "like-source"


def test_local_store_timeline_filters_events():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        store.log_record(
            source_type="gateway_request",
            source_id="gw-1",
            title="Gateway demo",
            content="gateway timeline demo",
            metadata={"status": "done"},
            event_type="gateway_request_completed",
        )
        store.log_record(
            source_type="subagent_run",
            source_id="sub-1",
            title="Subagent demo",
            content="subagent timeline demo",
            metadata={"status": "PLANNING"},
            event_type="subagent_run_saved",
        )

        all_items = store.timeline(limit=5)
        gateway_items = store.timeline(limit=5, source_type="gateway_request")
        event_items = store.timeline(limit=5, event_type="subagent_run_saved")

        assert len(all_items) >= 4
        assert len(gateway_items) == 2
        assert {item.source_id for item in gateway_items} == {"gw-1"}
        assert len(event_items) == 1
        assert event_items[0].source_type == "subagent_run"


def test_jsonl_memory_indexes_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        memory = JsonlMemory(root / "memory.jsonl", local_store=store)

        memory.add("user", "我希望本地查询先用 SQLite 和 FTS5。", kind="preference")
        hits = memory.search("SQLite", top_k=3)

        assert len(hits) == 1
        assert hits[0].role == "user"
        assert hits[0].kind == "preference"
        assert "FTS5" in hits[0].content


def test_jsonl_memory_local_store_search_is_scoped_by_memory_path():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        store = LocalStore(root / "local.db")
        first = JsonlMemory(root / "one" / "memory.jsonl", local_store=store)
        second = JsonlMemory(root / "two" / "memory.jsonl", local_store=store)

        first.add("user", "排序算法任务应该留在第一个记忆文件。", kind="dialogue")
        second_hits = second.search("排序算法", top_k=3)

        assert second_hits == []


def test_jsonl_memory_can_backfill_existing_records():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        memory_path = root / "memory.jsonl"
        JsonlMemory(memory_path).add("assistant", "旧 JSONL 记录也能补建索引。", kind="note")

        store = LocalStore(root / "local.db")
        memory = JsonlMemory(memory_path, local_store=store)
        assert memory.index_all() == 1
        assert store.stats()["record_count"] == 1
        assert memory.search("补建", top_k=1)[0].role == "assistant"


def test_subagent_flow_indexes_logs_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        task = agent.subagents.create_run(
            goal="统一日志测试：让子代理记录可以搜索",
            thought="验证 subagent_run 和 runner_result 会进入 LocalStore。",
            plan=["生成工单", "执行 dry-run runner", "搜索日志"],
        )

        run_hits = agent.local_store.search("统一日志测试", source_type="subagent_run")
        assert len(run_hits) == 1
        assert run_hits[0].source_id == task.id

        result = agent.run_subagent(task.id, dry_run=True)
        runner_hits = agent.local_store.search(task.id, source_type="subagent_runner_result")
        context_hits = agent.local_store.search(task.id, source_type="subagent_execution_context")

        assert result.run_id == task.id
        assert runner_hits
        assert context_hits


def test_gateway_request_indexes_logs_to_local_store():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="gateway 日志测试：这条请求应该能被搜索到",
                save=False,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))
        hits = agent.local_store.search("gateway 日志测试", source_type="gateway_request")

        assert response["ok"] is True
        assert request_id == response["id"]
        assert hits
        assert hits[0].source_id == request_id


def test_gateway_request_writes_runtime_fact_when_saved():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        paths = gateway_paths(agent)
        request_id, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="gateway runtime fact 测试",
                save=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        fact_path = (
            agent.home_paths.owner_home_dir
            / "memory_archive"
            / "runtime_facts"
            / request_id
            / "task.json"
        )
        assert fact_path.exists()
        payload = json.loads(fact_path.read_text(encoding="utf-8"))
        assert payload["request_id"] == request_id
        assert payload["runtime_progress"]["source"] == "gateway"


def test_gateway_request_can_override_resume_context():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            gateway_workspace="gateway",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        agent.run("gateway README 恢复开关任务", save=True)
        paths = gateway_paths(agent)
        _, request_path, _ = submit_gateway_ask(
            paths,
            params=GatewayAskParams(
                prompt="继续 README",
                save=False,
                include_prompt=True,
                resume_context=True,
                agent=agent,
            ),
        )

        ensure_gateway_folders(paths)
        assert _process_gateway_request_path(agent, paths, request_path, "local-store-test")
        response = read_json_file(gateway_response_path(paths, request_path.stem))

        assert response["ok"] is True
        assert response["memory_resume_context_injected"] is True
        assert response["memory_resume_context_token_estimate"] > 0
        assert "### Auto Recovery Context" in response["prompt"]


def test_local_rebuild_indexes_memory_gateway_and_subagents():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        cfg = AgentConfig(
            model_backend="echo",
            my_agent_home=str(root / "home"),
            memory_path="memory.jsonl",
            gateway_workspace="gateway",
            subagent_workspace="subs",
            local_store_path="local_store/local.db",
            local_store_files_dir="local_store/files",
            local_store_events_path="local_store/events.jsonl",
        )
        agent = SimpleAgent(cfg, root)
        agent.remember("重建测试记忆：local-rebuild 应该重新索引。", kind="fact")
        task = agent.subagents.create_run(
            goal="local-rebuild 子代理索引测试",
            thought="生成一个可被重建扫描到的工单。",
            plan=["创建工单", "重建索引"],
        )
        gpaths = gateway_paths(agent)
        submit_gateway_ask(
            gpaths,
            params=GatewayAskParams(
                prompt="local-rebuild gateway 请求索引测试",
                save=False,
                agent=agent,
            ),
        )
        assert _process_gateway_requests(agent, gpaths) == 1

        agent.local_store.reset()
        result = rebuild_local_store(
            agent, sources={"memory", "gateway", "subagent", "fts"}, reset=False
        )

        counts = result["source_counts"]
        assert counts["memory"] == 1
        assert counts["gateway_request"] >= 1
        assert counts["subagent_run"] == 1
        assert (
            agent.local_store.search("local-rebuild 子代理", source_type="subagent_run")[
                0
            ].source_id
            == task.id
        )

        doctor = build_local_doctor_report(agent)
        assert doctor["memory_count"] == 1
        assert any(item["name"] == "memory_index" for item in doctor["checks"])


def _perf_e_event_store(tmp_path, coordinator):
    from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
    from agent_py_agent.tests.test_local_store_coordinator_io import make_store

    store = LocalStore(tmp_path / "index.db")
    events = [store.record_event("retention", payload={"index": index}) for index in range(6)]
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany("UPDATE events SET created_at=? WHERE event_id=?",
            [(created, event.event_id) for created, event in zip((1, 2, 3, 50, 50, 50), events, strict=True)])
    if coordinator:
        store, door = make_store(tmp_path)
        return store, events, lambda: global_write_commit_scope(door)
    from contextlib import nullcontext
    return store, events, nullcontext


@pytest.mark.parametrize("coordinator", [False, True])
def test_perf_e_prune_events_is_bounded_and_preserves_jsonl_only_bytes(tmp_path, coordinator):
    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    prune = getattr(store, "prune_events", None)
    assert callable(prune), "missing bounded event retention"
    original = store.events_path.read_bytes().splitlines(keepends=True)
    extra = b' { "event_id" : "jsonl-only", "created_at" : 0 }\r\n[1]\nnot-json\xff\n{"unfinished"'
    with store.events_path.open("ab") as handle:
        handle.write(extra)
    with scope():
        dry = prune(before=100, keep_last=2, max_delete=2, dry_run=True)
        assert dry == {"ok": True, "dry_run": True, "selected": 2, "deleted_db": 0, "deleted_jsonl": 0}
        assert store.stats()["event_count"] == 6
        assert store.events_path.read_bytes() == b"".join(original) + extra
        first = prune(before=50, keep_last=2, max_delete=2)
        assert first == {"ok": True, "dry_run": False, "selected": 2, "deleted_db": 2, "deleted_jsonl": 2}
        assert store.events_path.read_bytes() == b"".join(original[2:]) + extra
        assert prune(before=50, keep_last=2, max_delete=2)["deleted_db"] == 1  # cutoff严格小于。
        assert prune(before=100, keep_last=2)["deleted_db"] == 1
        assert {item.event_id for item in store.timeline()} == {event.event_id for event in events[-2:]}
        assert store.events_path.read_bytes() == b"".join(original[-2:]) + extra
        assert prune(before=100, keep_last=2)["selected"] == 0


@pytest.mark.parametrize("coordinator", [False, True])
def test_perf_e_prune_events_only_filters_rows_actually_deleted_by_sqlite(tmp_path, coordinator):
    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    prune = getattr(store, "prune_events", None)
    assert callable(prune), "missing bounded event retention"
    original = store.events_path.read_bytes().splitlines(keepends=True)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(f"CREATE TRIGGER keep_first BEFORE DELETE ON events WHEN OLD.event_id='{events[0].event_id}' "
                     "BEGIN SELECT RAISE(IGNORE); END")
    with scope():
        result = prune(before=50, keep_last=0, max_delete=2)
        assert result == {"ok": True, "dry_run": False, "selected": 2, "deleted_db": 1, "deleted_jsonl": 1}
        assert store.stats()["event_count"] == 5
    assert store.events_path.read_bytes() == b"".join([original[0], *original[2:]])


def test_perf_e_prune_events_normalizes_limits_and_chunks_sql_parameters(tmp_path):
    store = LocalStore(tmp_path / "index.db")
    prune = getattr(store, "prune_events", None)
    assert callable(prune), "missing bounded event retention"
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,payload_json,created_at) VALUES(?, 'old', '{}', 1)",
            [(f"id-{index}",) for index in range(1005)])
    assert prune(before=2, keep_last=-1, max_delete=0)["deleted_db"] == 1
    assert prune(before=2, keep_last=2, max_delete=1005)["deleted_db"] == 1002
    assert store.stats()["event_count"] == 2
    assert not store.events_path.exists()


@pytest.mark.parametrize("coordinator", [False, True])
def test_perf_e_prune_events_rewrite_failure_reports_partial_without_dropping_jsonl(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    prune = getattr(store, "prune_events", None)
    assert callable(prune), "missing bounded event retention"
    before = store.events_path.read_bytes()
    error = OSError("atomic rewrite unavailable")

    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", refuse, raising=False)
    with scope(), pytest.raises(OSError) as caught:
        prune(before=50, keep_last=2, max_delete=1)
    assert caught.value is error
    assert store.events_path.read_bytes() == before
    with scope():
        assert store.stats()["event_count"] == 5


@pytest.mark.parametrize("coordinator", [False, True])
def test_perf_e_prune_events_keeps_json_beyond_parser_recursion_limit(tmp_path, coordinator):
    import sys

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    lines = store.events_path.read_bytes().splitlines(keepends=True)
    nested = b"[" * (sys.getrecursionlimit() * 2) + b"0" + b"]" * (sys.getrecursionlimit() * 2) + b"\r\n"
    with store.events_path.open("ab") as handle:
        handle.write(nested)
    with scope():
        assert store.prune_events(before=50, keep_last=2, max_delete=1)["deleted_jsonl"] == 1
    assert store.events_path.read_bytes() == b"".join(lines[1:]) + nested


@pytest.mark.parametrize("coordinator", [False, True])
def test_perf_e_prune_events_keeps_row_when_decoder_reports_recursion_limit(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    lines = store.events_path.read_bytes().splitlines(keepends=True)
    unmatched = "[[[[0]]]]\n"
    with store.events_path.open("a") as handle:
        handle.write(unmatched)
    actual = maintenance.json.loads

    def decode(line, *args, **kwargs):
        if line == unmatched:
            raise RecursionError("JSON nesting exceeds this decoder's limit")
        return actual(line, *args, **kwargs)

    monkeypatch.setattr(maintenance.json, "loads", decode)
    with scope():
        assert store.prune_events(before=50, keep_last=2, max_delete=1)["deleted_jsonl"] == 1
    assert store.events_path.read_bytes() == b"".join(lines[1:]) + unmatched.encode()


# LLM: 只读真实metadata，不用进程缓存或待实现helper推测恢复责任。
# 函数用途: 核对测试数据库里仍待完成的事件删除ID。
def _b2_pending_ids(store):
    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key='events_prune_pending_ids'").fetchone()
    return [] if row is None else json.loads(row[0])


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_failed_rewrite_recovers_without_new_db_deletions(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    writer = maintenance.write_private_text_file_atomic_unlocked

    def fail(*args, **kwargs):
        raise OSError("injected rewrite failure")

    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", fail)
    with scope(), pytest.raises(OSError, match="injected"):
        store.prune_events(before=50, keep_last=2, max_delete=3)
    assert _b2_pending_ids(store) == sorted(event.event_id for event in events[:3])
    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", writer)
    # 新实例模拟重启，不依赖旧进程缓存；DB 无符合 before 的新行。
    with scope():
        store = LocalStore(store.db_path, coordinator_io=coordinator)
        result = store.prune_events(before=50, keep_last=2, max_delete=3)
    assert result == {"ok": True, "dry_run": False, "selected": 0, "deleted_db": 0, "deleted_jsonl": 3}
    assert store.events_path.read_bytes() == b"".join(original[3:])
    assert _b2_pending_ids(store) == []
    assert list(tmp_path.glob(".events.jsonl.*.tmp")) == []


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_dry_run_keeps_pending_and_never_rewrites(tmp_path, coordinator):
    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (events[0].event_id,))
        conn.execute("INSERT INTO metadata(key,value) VALUES('events_prune_pending_ids',?)",
                     (json.dumps([events[0].event_id]),))
    original = store.events_path.read_bytes()
    with scope():
        result = store.prune_events(before=0, dry_run=True)
    assert result == {"ok": True, "dry_run": True, "selected": 0, "deleted_db": 0, "deleted_jsonl": 0}
    assert _b2_pending_ids(store) == [events[0].event_id]
    assert store.events_path.read_bytes() == original
    with scope():
        assert store.prune_events(before=0)["deleted_jsonl"] == 1
    assert _b2_pending_ids(store) == []


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_default_rewrite_streams_with_bounded_memory_and_private_temp(tmp_path, monkeypatch, coordinator):
    import tracemalloc

    from agent_py_agent.agent.local_storage import maintenance

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    retained = b'{"event_id":"jsonl-only","body":"' + b"x" * 256 + b'"}\r\n'
    bad = b'bad-utf8\xff\r\n{"unfinished"'
    with store.events_path.open("ab") as handle:
        for _ in range(20000):
            handle.write(retained)
        handle.write(bad)
    actual_replace = maintenance.os.replace if hasattr(maintenance, "os") else None
    modes = []

    def replace(source, destination):
        modes.append(Path(source).stat().st_mode & 0o777)
        return actual_replace(source, destination)

    if actual_replace is not None:
        monkeypatch.setattr(maintenance.os, "replace", replace)
    tracemalloc.start()
    try:
        with scope():
            result = store.prune_events(before=50, keep_last=2, max_delete=1)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result["deleted_jsonl"] == 1
    assert peak < 2 * 1024 * 1024, f"whole-file buffering: peak={peak}"
    assert modes == [0o600]
    assert store.events_path.read_bytes() == b"".join(original[1:]) + retained * 20000 + bad
    assert list(tmp_path.glob(".events.jsonl.*.tmp")) == []


# LLM: 仅构造临时测试库的大样本；显式同步SQL与JSONL以避免逐条fsync耗时干扰稳态断言。
# 函数用途: 快速准备带固定排序的事件流水，不读写真实用户数据。
def _b2_seed_events(store, count):
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany("INSERT INTO events(event_id,event_type,payload_json,created_at) VALUES(?, 'old', '{}', 1)",
                         ((f"b2-{index:06d}",) for index in range(count)))
    with store.events_path.open("w") as handle:
        for index in range(count):
            handle.write(json.dumps({"event_id": f"b2-{index:06d}"}) + "\n")


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_steady_state_defaults_keep_latest_twenty_thousand_in_bounded_batches(tmp_path, coordinator):
    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    with scope():
        store.reset()
    _b2_seed_events(store, 25005)
    prune = getattr(store, "prune_events_steady_state", None)
    assert callable(prune), "missing steady-state maintenance entry"
    with scope():
        dry = prune(dry_run=True)
        assert dry["selected"] == 5000 and dry["deleted_db"] == 0
        assert prune()["deleted_db"] == 5000
        assert prune()["deleted_db"] == 5
        assert prune()["deleted_db"] == 0
    with sqlite3.connect(store.db_path) as conn:
        ids = [row[0] for row in conn.execute("SELECT event_id FROM events ORDER BY seq")]
    assert ids == [f"b2-{index:06d}" for index in range(5005, 25005)]
    assert [json.loads(line)["event_id"] for line in store.events_path.read_text().splitlines()] == ids


def test_b2_event_maintenance_is_explicit_and_bounded_not_record_event_hot_path(tmp_path, monkeypatch):
    store = LocalStore(tmp_path / "index.db")
    _b2_seed_events(store, 25005)
    maintain = getattr(store, "maintain_events", None)
    assert callable(maintain), "missing bounded event maintenance cycle"
    result = maintain(max_batches=1)
    assert result["deleted_db"] == 5000
    assert store.stats()["event_count"] == 20005

    def refuse(*args, **kwargs):
        pytest.fail("single event hot path must not scan/prune")

    monkeypatch.setattr(store, "prune_events_steady_state", refuse)
    store.record_event("still-lightweight")


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_clear_ledger_releases_file_lock_before_waiting_for_append_writer(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import events as event_module

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    writer = LocalStore(store.db_path)
    inserted, failures = threading.Event(), []
    append = event_module.append_private_jsonl_records
    prune_jsonl = store._prune_event_jsonl

    def blocked_append(*args, **kwargs):
        inserted.set()  # INSERT 已成功且持DB写锁，随后会等裁剪者的文件锁。
        return append(*args, **kwargs)

    def write():
        try:
            writer.record_event("concurrent-append")
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=write, daemon=True)

    def prune(targets):
        removed = prune_jsonl(targets)
        thread.start()
        assert inserted.wait(2), "append writer never reached file lock"
        return removed

    monkeypatch.setattr(event_module, "append_private_jsonl_records", blocked_append)
    monkeypatch.setattr(store, "_prune_event_jsonl", prune)
    if coordinator:
        transaction = store._coordinator_transaction

        def clear_after_writer(operation):
            if inserted.is_set():
                # C的SQLite零等待合同不改；在开始清账事务前证实writer已能取得文件锁。
                thread.join(2)
                assert not thread.is_alive(), "cleanup still owns the event file lock"
            return transaction(operation)

        monkeypatch.setattr(store, "_coordinator_transaction", clear_after_writer)
    try:
        with scope():
            assert store.prune_events(before=50, keep_last=2, max_delete=1)["deleted_jsonl"] == 1
    finally:
        thread.join(3)
    assert not thread.is_alive() and failures == []
    assert b"concurrent-append" in store.events_path.read_bytes()
    assert _b2_pending_ids(store) == []


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_retry_syncs_already_replaced_file_before_clearing_pending(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    sync = maintenance._sync_parent_directory
    synced = []

    def fail(path):
        raise OSError("directory sync failed after replace")

    monkeypatch.setattr(maintenance, "_sync_parent_directory", fail)
    with scope(), pytest.raises(OSError, match="directory sync"):
        store.prune_events(before=50, keep_last=2, max_delete=3)
    assert store.events_path.read_bytes() == b"".join(original[3:])
    assert set(_b2_pending_ids(store)) == {event.event_id for event in events[:3]}

    def track(path):
        synced.append(path)
        return sync(path)

    monkeypatch.setattr(maintenance, "_sync_parent_directory", track)
    with scope():
        assert store.prune_events(before=50, keep_last=2)["deleted_jsonl"] == 0
    assert synced == [store.events_path]
    assert _b2_pending_ids(store) == []


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_ledger_write_failure_rolls_back_db_and_never_touches_jsonl(tmp_path, coordinator):
    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("CREATE TRIGGER fail_ledger BEFORE INSERT ON metadata "
                     "WHEN NEW.key='events_prune_pending_ids' BEGIN SELECT RAISE(ABORT,'ledger unavailable'); END")
    with scope(), pytest.raises(sqlite3.IntegrityError, match="ledger unavailable"):
        store.prune_events(before=50, keep_last=2, max_delete=3)
    assert store.events_path.read_bytes() == original
    assert _b2_pending_ids(store) == []
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 6


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_pending_merges_only_actually_deleted_trigger_survivors_stay(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("DELETE FROM events WHERE event_id=?", (events[0].event_id,))
        conn.execute("INSERT INTO metadata(key,value) VALUES('events_prune_pending_ids',?)",
                     (json.dumps([events[0].event_id]),))
        conn.execute(f"CREATE TRIGGER keep_event BEFORE DELETE ON events WHEN OLD.event_id='{events[1].event_id}' "
                     "BEGIN SELECT RAISE(IGNORE); END")

    def fail(*args, **kwargs):
        raise OSError("pending retained")

    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", fail)
    with scope(), pytest.raises(OSError, match="pending retained"):
        store.prune_events(before=50, keep_last=0, max_delete=2)
    assert _b2_pending_ids(store) == [events[0].event_id, events[2].event_id]


def test_b2_cleanup_preserves_concurrently_registered_new_pending_ids(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, False)
    clear = maintenance._clear_pending_events_transaction

    def register_then_clear(store, targets):
        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM events WHERE event_id=?", (events[1].event_id,))
            conn.execute("UPDATE metadata SET value=? WHERE key='events_prune_pending_ids'",
                         (json.dumps([*targets, events[1].event_id]),))
        return clear(store, targets)

    monkeypatch.setattr(maintenance, "_clear_pending_events_transaction", register_then_clear)
    with scope():
        assert store.prune_events(before=50, keep_last=2, max_delete=1)["deleted_jsonl"] == 1
    assert _b2_pending_ids(store) == [events[1].event_id]


@pytest.mark.parametrize("payload", ['{}', '[1]', '[""]', 'not-json'])
def test_b2_invalid_pending_fails_closed_before_any_deletion(tmp_path, payload):
    store, _events, _scope = _perf_e_event_store(tmp_path, False)
    original = store.events_path.read_bytes()
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("INSERT INTO metadata(key,value) VALUES('events_prune_pending_ids',?)", (payload,))
    with pytest.raises(ValueError):
        store.prune_events(before=50, keep_last=2)
    assert store.events_path.read_bytes() == original
    assert store.stats()["event_count"] == 6


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_bare_carriage_return_inside_bad_jsonl_record_is_preserved(tmp_path, coordinator):
    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    bad = json.dumps({"event_id": events[0].event_id}).encode() + b"\rBROKEN\n"
    with store.events_path.open("ab") as handle:
        handle.write(bad)
    with scope():
        result = store.prune_events(before=50, keep_last=2, max_delete=1)
    assert result["deleted_jsonl"] == 1
    assert store.events_path.read_bytes() == b"".join(original[1:]) + bad


@pytest.mark.parametrize("coordinator", [False, True])
def test_b2_temp_cleanup_failure_does_not_mask_primary_publish_error(tmp_path, monkeypatch, coordinator):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes()
    primary = OSError("original publish error")
    unlink = Path.unlink

    def fail_publish(*args):
        raise primary

    def fail_cleanup(path, *args, **kwargs):
        if path.name.startswith(".events.jsonl.") and path.suffix == ".tmp":
            raise PermissionError("cleanup failed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "replace", fail_publish)
    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    with scope(), pytest.raises(OSError) as caught:
        store.prune_events(before=50, keep_last=2, max_delete=1)
    assert caught.value is primary
    assert _b2_pending_ids(store) == [events[0].event_id]
    assert store.events_path.read_bytes() == original


# LLM: 坏UTF-8位于JSON字符串内仍须原字节保留；替身writer只在过滤后接收surrogateescape文本。
# 函数用途: 复现surrogateescape让坏字节行冒充可删除JSON事件的问题，覆盖普通/C及兼容发表路径。
@pytest.mark.parametrize("coordinator", [False, True])
@pytest.mark.parametrize("patched_writer", [False, True])
@pytest.mark.parametrize("ending", [b"\n", b""])
def test_prune_events_preserves_invalid_utf8_inside_json_string_even_when_event_id_matches(
    tmp_path, monkeypatch, coordinator, patched_writer, ending,
):
    from agent_py_agent.agent.local_storage import maintenance

    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    bad = b'{"event_id":"' + events[0].event_id.encode() + b'","payload":{"text":"\xff"}}' + ending
    with store.events_path.open("ab") as handle:
        handle.write(bad)
    captured = []
    if patched_writer:
        writer = maintenance.write_private_text_file_atomic_unlocked

        # LLM: 包装原writer保留其真实写入与C参数，不替代被测的过滤过程。
        # 函数用途: 核对兼容text writer收到的内容仍含完整坏字节。
        def capture(path, content, **kwargs):
            captured.append(content.encode("utf-8", errors="surrogateescape"))
            return writer(path, content, **kwargs)

        monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", capture)
    with scope():
        result = store.prune_events(before=50, keep_last=2, max_delete=1)
        assert events[0].event_id not in {row.event_id for row in store.timeline()}
    expected = b"".join(original[1:]) + bad
    assert result == {"ok": True, "dry_run": False, "selected": 1, "deleted_db": 1, "deleted_jsonl": 1}
    assert store.events_path.read_bytes() == expected
    assert captured == ([expected] if patched_writer else [])
    assert _b2_pending_ids(store) == []


# LLM: LF才是二进制JSONL边界；含原始CR的行即便json.loads接受为合法空白，也不得删除。
# 函数用途: 验证裸CR、CRLF及无末尾LF的CR记录按任务约定原字节保留。
@pytest.mark.parametrize("coordinator", [False, True])
@pytest.mark.parametrize("ending", [b"\r\n", b"\r \n", b"\r"])
def test_b2_r2_preserves_raw_carriage_return_even_when_json_parser_accepts_it(tmp_path, coordinator, ending):
    store, events, scope = _perf_e_event_store(tmp_path, coordinator)
    original = store.events_path.read_bytes().splitlines(keepends=True)
    bad = json.dumps({"event_id": events[0].event_id}).encode() + ending
    assert json.loads(bad)["event_id"] == events[0].event_id
    with store.events_path.open("ab") as handle:
        handle.write(bad)
    with scope():
        result = store.prune_events(before=50, keep_last=2, max_delete=1)
    assert result["deleted_jsonl"] == 1
    assert store.events_path.read_bytes() == b"".join(original[1:]) + bad


# LLM: 用真实失败发表建立pending，重复失败不能追加删除；恢复后本轮只清旧账，下轮才继续新批。
# 函数用途: 覆盖普通/C的当前批次、默认维护5000阈值及大max_delete不能绕过5000恢复闸。
@pytest.mark.parametrize("coordinator", [False, True])
@pytest.mark.parametrize("entry", ["small_prune", "maintain", "wide_prune"])
def test_b2_r2_full_pending_recovers_before_new_delete_batches(tmp_path, monkeypatch, coordinator, entry):
    from agent_py_agent.agent.local_storage import maintenance

    store, _events, scope = _perf_e_event_store(tmp_path, coordinator)
    if entry != "small_prune":
        with scope():
            store.reset()
        _b2_seed_events(store, 25005 if entry == "maintain" else 5010)
    writer = maintenance.write_private_text_file_atomic_unlocked
    original = store.events_path.read_bytes()

    # LLM: 只在最后发表注入失败，SQL删除与pending仍走真实事务。
    # 函数用途: 保留待恢复账本及未改写的JSONL供下一次重试。
    def fail(*args, **kwargs):
        raise OSError("r2 publish unavailable")

    # LLM: 首次裁剪与重试沿原公开入口，wide重试显式增大max_delete以检查独立5000阈值。
    # 函数用途: 统一运行三种公开维护调用，不改被测批次逻辑。
    def prune(*, retry=False):
        if entry == "maintain":
            return store.maintain_events(max_batches=2, before=50)
        maximum = (10000 if retry else 5000) if entry == "wide_prune" else 2
        return store.prune_events(before=50, keep_last=2, max_delete=maximum)

    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", fail)
    with scope(), pytest.raises(OSError, match="r2 publish"):
        prune()
    pending = _b2_pending_ids(store)
    assert len(pending) == (2 if entry == "small_prune" else 5000)
    with sqlite3.connect(store.db_path) as conn:
        remaining = list(conn.execute("SELECT event_id FROM events ORDER BY seq"))
    for _ in range(2):
        with scope(), pytest.raises(OSError, match="r2 publish"):
            prune(retry=True)
        assert _b2_pending_ids(store) == pending
        with sqlite3.connect(store.db_path) as conn:
            assert list(conn.execute("SELECT event_id FROM events ORDER BY seq")) == remaining
        assert store.events_path.read_bytes() == original
    monkeypatch.setattr(maintenance, "write_private_text_file_atomic_unlocked", writer)
    with scope():
        recovered = prune(retry=True)
    assert recovered == {"ok": True, "dry_run": False, "selected": 0, "deleted_db": 0, "deleted_jsonl": len(pending)}
    assert _b2_pending_ids(store) == []
    assert [json.loads(line)["event_id"] for line in store.events_path.read_bytes().splitlines()] == [row[0] for row in remaining]
    with scope():
        assert prune(retry=True)["deleted_db"] > 0
