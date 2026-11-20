"""验证调度原账行范围和网关审计游标窗口。"""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.gateway_history_index import GatewayHistoryIndex
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_audit_records import (
    OutcomeQuery,
    request_outcome_records,
)
from agent_py_agent.agent.scheduler.recovery import existing_terminals
from agent_py_agent.agent.scheduler.repository import SchedulerRepository


def _gateway_terminal(paths, number, *, stamp=100):
    request_id = f"req-{number:03}"
    payload = {"id": request_id, "owner_id": "local/main", "status": "done", "ended_at": stamp}
    (paths.terminal / f"{request_id}.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload


def _gateway_row(paths, index, number, *, stamp=100):
    index.append(_gateway_terminal(paths, number, stamp=stamp))


def test_audit_cursor_survives_appends_and_rejects_source_rotation(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.terminal.mkdir(parents=True)
    index = GatewayHistoryIndex(paths)
    for number in range(4):
        _gateway_row(paths, index, number)
    index.backfill_audit_step(limit=128)
    query = OutcomeQuery(frozenset({"local/main"}), {}, "", 0, 2)
    first = index.audit_page(query)
    assert [row["request_id"] for row in first["entries"]] == ["req-003", "req-002"]
    next_query = replace(query, cursor=first["next_cursor"])
    for number in range(4, 104):
        _gateway_row(paths, index, number, stamp=90)
    second = GatewayHistoryIndex(paths).audit_page(next_query)
    assert [row["request_id"] for row in second["entries"]] == ["req-001", "req-000"]
    with index.connection() as conn:
        assert index._meta(conn, "append_seq") == "104"
        assert index._meta(conn, "source_generation") == "0"
    cursor = second["next_cursor"]
    while cursor:
        remainder = index.audit_page(replace(query, cursor=cursor))
        assert remainder["entries"] == []
        cursor = remainder["next_cursor"]
    replacement = paths.history.with_suffix(".rotated")
    replacement.write_bytes(paths.history.read_bytes())
    replacement.replace(paths.history)
    with pytest.raises(ValueError, match="source generation"):
        index.audit_page(next_query)
    index.migrate_batch()
    with pytest.raises(ValueError, match="source generation"):
        index.audit_page(next_query)


def test_public_cursor_freezes_history_before_draining_active_rows(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.terminal.mkdir(parents=True)
    paths.inbox.mkdir()
    index = GatewayHistoryIndex(paths)
    for number in range(2):
        _gateway_row(paths, index, number)
    (paths.inbox / "active.json").write_text(
        json.dumps({"id": "active", "owner_id": "local/main", "status": "queued"}), encoding="utf-8",
    )
    query = OutcomeQuery(frozenset({"local/main"}), {}, "", 0, 1)
    first = request_outcome_records(paths, query)
    assert [row["request_id"] for row in first["entries"]] == ["active"]
    _gateway_row(paths, index, 2, stamp=101)
    seen = []
    cursor = first["next_cursor"]
    for _ in range(8):
        page = request_outcome_records(paths, replace(query, cursor=cursor))
        seen.extend(row["request_id"] for row in page["entries"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert not cursor
    assert seen == ["req-001", "req-000"]


def _insert_baseline_gateway_row(conn, payload):
    body = json.dumps(payload)
    conn.execute("INSERT INTO history VALUES (?,?,?,'live',0,0,'',0,'')", (payload["id"], body, "baseline"))


def test_gateway_upgrade_preserves_legacy_cursor_and_nine_column_writer(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.terminal.mkdir(parents=True)
    index = GatewayHistoryIndex(paths)
    with closing(sqlite3.connect(index.path)) as conn, conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE history(request_id TEXT PRIMARY KEY, body TEXT NOT NULL, digest TEXT NOT NULL,
                origin TEXT NOT NULL, conflict INTEGER NOT NULL, export_pending INTEGER NOT NULL,
                export_path TEXT NOT NULL, export_offset INTEGER NOT NULL, export_mode TEXT NOT NULL);
            CREATE TABLE audit_keys(request_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, thread_id TEXT NOT NULL,
                recorded_at REAL NOT NULL, status TEXT NOT NULL, error_code TEXT NOT NULL, channel TEXT NOT NULL);
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO meta VALUES ('initialized','1'),('audit_revision','3'),('audit_complete','1'),
                ('audit_cursor','3'),('compat','0');
        """)
        for number in range(3):
            payload = _gateway_terminal(paths, number)
            _insert_baseline_gateway_row(conn, payload)
            conn.execute("INSERT INTO audit_keys VALUES (?,'local/main','',100,'done','','')", (payload["id"],))
    index.path.chmod(0o600)
    paths.root.chmod(0o700)
    query = OutcomeQuery(frozenset({"local/main"}), {}, "", 0, 1)
    legacy = replace(query, cursor=index.audit_page(query)["next_cursor"])
    assert legacy.cursor
    _gateway_row(paths, index, 3, stamp=110)
    pending = index.audit_page(legacy)
    assert pending["coverage"]["reason"] == "projection_publish_backfill_incomplete"
    assert pending["next_cursor"] == legacy.cursor
    assert index.backfill_audit_step(limit=1)["items"] == 1
    assert index.backfill_audit_step(limit=128)["complete"]
    assert index.audit_page(legacy)["entries"][0]["request_id"] == "req-001"
    first = index.audit_page(query)
    assert first["entries"][0]["request_id"] == "req-003"
    with closing(sqlite3.connect(index.path)) as conn, conn:
        assert len(conn.execute("PRAGMA table_info(history)").fetchall()) == 9
        baseline = _gateway_terminal(paths, 4, stamp=120)
        _insert_baseline_gateway_row(conn, baseline)
        assert conn.execute("SELECT value FROM meta WHERE key='publish_backfill_complete'").fetchone()[0] == "0"
        # 旧版维护可以完成白名单键，同时仍然缺发布序号。
        conn.execute("INSERT INTO audit_keys VALUES (?,'local/main','',120,'done','','')", (baseline["id"],))
        conn.execute("UPDATE meta SET value=(SELECT MAX(rowid) FROM history) WHERE key='audit_cursor'")
        conn.execute("UPDATE meta SET value='1' WHERE key='audit_complete'")
    assert index.audit_page(query)["coverage"]["status"] == "partial"
    assert index.backfill_audit_step(limit=128)["complete"]
    page = index.audit_page(query)
    assert page["entries"][0]["request_id"] == "req-004"
    old_page = index.audit_page(replace(query, cursor=first["next_cursor"]))
    assert old_page["entries"][0]["request_id"] == "req-002"
    _gateway_row(paths, index, 5, stamp=130)
    assert index.audit_page(query)["entries"][0]["request_id"] == "req-005"


def _repository(tmp_path):
    return SchedulerRepository(tmp_path / "scheduler", owner_provider="local",
                               owner_kind="main", owner_id="local/main")


def _run(repo, number, *, status="done", trigger="schedule", thread="thread-one"):
    return {"schema_version": "scheduler_run.v1", "run_id": f"srun_{number}", "job_id": "job_history",
            "thread_id": thread, "owner": repo.owner, "status": status, "trigger": trigger,
            "ended_at": number + 1, "summary": "正文" * 256}


def _measure_jsonl_reads(monkeypatch, path):
    original = os.fdopen
    inode = path.stat().st_ino
    counts = {"bytes": 0}

    class Meter:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            data = next(self.handle)
            counts["bytes"] += len(data.encode("utf-8") if isinstance(data, str) else data)
            return data

        def read(self, size=-1):
            data = self.handle.read(size)
            counts["bytes"] += len(data.encode("utf-8") if isinstance(data, str) else data)
            return data

    def open_measured(fd, *args, **kwargs):
        handle = original(fd, *args, **kwargs)
        return Meter(handle) if os.fstat(fd).st_ino == inode else handle

    monkeypatch.setattr(os, "fdopen", open_measured)
    return counts


def test_terminal_locate_reads_only_indexed_lines(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    target = _run(repo, 0, status="failed")
    repo._history_index.append([target, *(_run(repo, i) for i in range(1, 400))])
    reads = _measure_jsonl_reads(monkeypatch, repo.history_path)
    active = {**target, "status": "running"}
    terminals = repo._history_terminal_runs({"runs": {active["run_id"]: active}})
    assert terminals[active["run_id"]]["status"] == "failed"
    assert 0 < reads["bytes"] <= 4 * 8192 + 4096


def test_sparse_scheduler_filter_pages_past_two_hundred_newer_rows(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    target = _run(repo, 0, status="failed", trigger="manual")
    repo._history_index.append([target, *(_run(repo, i) for i in range(1, 400))])
    reads = _measure_jsonl_reads(monkeypatch, repo.history_path)
    rows, errors = repo.history(statuses=("failed",), trigger="manual", limit=1)
    assert errors == []
    assert [row["run_id"] for row in rows] == [target["run_id"]]
    assert 0 < reads["bytes"] <= 2 * 8192 + 4096


def test_scheduler_exact_locator_retains_older_matching_terminal(tmp_path, monkeypatch):
    repo = _repository(tmp_path)
    target = _run(repo, 0)
    later = {**target, "claim_epoch": 2, "status": "failed", "ended_at": 2}
    repo._history_index.append([target, later])
    reads = _measure_jsonl_reads(monkeypatch, repo.history_path)
    assert existing_terminals(repo, [target])[target["run_id"]]["status"] == "done"
    assert existing_terminals(repo, [later])[target["run_id"]]["status"] == "failed"
    assert reads["bytes"]


def _baseline_scheduler_write(path, row):
    body = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(body)
    size = path.stat().st_size
    with closing(sqlite3.connect(path.with_suffix(".index.sqlite3"))) as conn, conn:
        conn.execute("CREATE TABLE IF NOT EXISTS history_rows(generation INTEGER, run_id TEXT, job_id TEXT,"
                     "ended_at REAL,payload TEXT,position INTEGER,PRIMARY KEY(generation,run_id))")
        conn.execute("INSERT INTO history_rows VALUES (?,?,?,?,?,?)",
                     (1, row["run_id"], row["job_id"], row["ended_at"], body, size))


def test_scheduler_upgrade_bounded_replay_and_positional_writer_rollback(tmp_path):
    repo = _repository(tmp_path)
    source, index_path = repo.history_path, repo._history_index.path
    old = _run(repo, 0, status="failed", trigger="manual")
    index_path.unlink()
    _baseline_scheduler_write(source, old)
    repo = _repository(tmp_path)
    new = _run(repo, 1, status="failed", trigger="manual")
    repo._history_index.append([new])
    assert [row["run_id"] for row in repo.history(statuses=("failed",))[0]] == [new["run_id"], old["run_id"]]
    repo._history_index.append([_run(repo, 2)])
    with closing(sqlite3.connect(index_path)) as conn:
        indexed = conn.execute("SELECT line_offset,line_length,status,trigger FROM history_rows WHERE run_id=? "
                               "ORDER BY generation DESC LIMIT 1",
                               (old["run_id"],)).fetchone()
    start, length, status, trigger = indexed
    assert (start, status, trigger) == (0, "failed", "manual")
    assert json.loads(source.read_bytes()[start:start + length]) == old

    lagging = _run(repo, 3, status="skipped")
    with source.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(lagging, ensure_ascii=False) + "\n")
    assert "SCHEDULER_HISTORY_INDEX_INCOMPLETE" in repo.history(statuses=("skipped",))[1]
    first = repo._history_index.backfill(max_rows=1, max_bytes=31)
    assert not first["complete"] and first["bytes"] == 31
    for _ in range(64):
        if repo._history_index.backfill(max_rows=1, max_bytes=256)["complete"]:
            break
    else:
        pytest.fail("bounded scheduler replay did not converge")
    assert [row["run_id"] for row in repo.history(statuses=("skipped",))[0]] == [lagging["run_id"]]

    index_path.unlink()
    last = _run(repo, 4)
    _baseline_scheduler_write(source, last)
    with closing(sqlite3.connect(index_path)) as conn:
        assert len(conn.execute("PRAGMA table_info(history_rows)").fetchall()) == 6
        assert conn.execute("SELECT run_id FROM history_rows").fetchone()[0] == last["run_id"]
    assert [row["run_id"] for row in _repository(tmp_path).history(limit=1)[0]] == [last["run_id"]]
