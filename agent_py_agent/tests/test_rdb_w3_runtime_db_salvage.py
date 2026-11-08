"""在离线临时数据库上验证真实页损坏恢复、只读源、规范 schema 和 CLI 回执。"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from agent_py_agent.agent.runtime_db import salvage
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_cat3_runtime_history_retention import _run, _tool


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _source(path, *, events=11):
    path.parent.mkdir(parents=True, exist_ok=True)
    RuntimeRepository(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO tasks(task_id,owner_id,title,goal,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     ("task", "local/main", "恢复样本", "保留完整状态", 100, 101))
        _run(conn)
        _tool(conn)
        conn.execute("INSERT INTO resource_locks(lock_id,canonical_scope,holder_instance,attempt_id,"
                     "attempt_generation,workspace_epoch,lease_expires_at,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", ("lock", "scope", "fixture", "attempt-run", 1, 1, 9999, 100, 101))
        conn.execute("INSERT INTO metadata(key,value) VALUES('fixture-marker','原始游标')")
        conn.executemany("INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,"
                         "task_run_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (
            (f"event-{number:08d}", "fixture.event", "attempt-run", "run", "task",
             json.dumps({"number": number, "text": "需要完整恢复的材料" * 20}, ensure_ascii=False), 100 + number)
            for number in range(events)
        ))
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    return path


def _snapshot(path):
    info = path.stat()
    return hashlib.sha256(path.read_bytes()).hexdigest(), info.st_size, info.st_mtime_ns


def _counts(path):
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'",
        )]
        return {table: conn.execute(f"SELECT count(*) FROM {_quote(table)}").fetchone()[0] for table in tables}


def _damaged_copy(source, target, *, table, tail=False):
    """先关闭 SQLite 连接，再只修改离线副本的一页；不依赖可选 dbstat 扩展。"""
    with closing(sqlite3.connect(source.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute("SELECT rootpage FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    page = root
    with target.open("r+b") as stream:
        if tail:
            # SQLite fileformat.html#b_tree_pages：表内页 0x05 的最右子页号位于页头 +8，大端。
            visited = set()
            while page not in visited:
                visited.add(page)
                stream.seek((page - 1) * page_size + (100 if page == 1 else 0))
                header = stream.read(12)
                if header[0] == 0x0D:
                    break
                assert header[0] == 0x05
                page = int.from_bytes(header[8:12], "big")
            assert page != root and header[0] == 0x0D
        stream.seek((page - 1) * page_size)
        stream.write(bytes(page_size))
    return target


def _cli(*args, cwd):
    script = Path(__file__).resolve().parents[2] / "scripts/runtime_db_salvage.py"
    return subprocess.run([sys.executable, str(script), *map(str, args)], cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def test_salvage_copies_all_tables_and_events(tmp_path):
    source = _source(tmp_path / "source/runtime.db")
    target = tmp_path / "target/runtime.db"
    expected = _counts(source)
    result = salvage.salvage_runtime_db(source, target, batch_rows=3)
    assert result.integrity == "ok" and result.ok is True
    assert result.events_copied == result.events_last_seq == 11 and result.events_error == ""
    assert _counts(target) == expected
    assert set(result.tables) == set(expected) - {"runtime_events"}
    for table, receipt in result.tables.items():
        # 指纹由目标安装器生成；回执只计入实际参与复制的 metadata 行。
        count = expected[table] - int(table == "metadata")
        assert receipt == {"source_rows": count, "copied": count, "error": ""}
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        new = conn.execute("INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,created_at) "
                           "VALUES('after-salvage','fixture','attempt-run','run',1000)")
        assert new.lastrowid > result.events_last_seq
        conn.commit()
    with pytest.raises(FrozenInstanceError):
        result.ok = False
    copied = result.to_dict()
    copied["tables"]["tasks"]["copied"] = -1
    assert result.tables["tasks"]["copied"] == 1
    assert json.loads(json.dumps(result.to_dict(), ensure_ascii=False))["source"] == str(source)


def test_salvage_stops_at_damaged_events_tail(tmp_path):
    source = _source(tmp_path / "source/runtime.db", events=1024)
    damaged = _damaged_copy(source, tmp_path / "damaged/runtime.db", table="runtime_events", tail=True)
    before = _snapshot(damaged)
    result = salvage.salvage_runtime_db(damaged, tmp_path / "recovered/runtime.db", batch_rows=17)
    assert result.ok is True and result.integrity == "ok"
    assert result.events_error and 0 < result.events_copied < 1024
    assert result.events_last_seq == result.events_copied
    assert all(not item["error"] and item["source_rows"] == item["copied"] for item in result.tables.values())
    assert result.tables["tasks"]["copied"] == result.tables["resource_locks"]["copied"] == 1
    assert _snapshot(damaged) == before
    with closing(sqlite3.connect(result.target)) as conn:
        rows = conn.execute("SELECT seq,event_id FROM runtime_events ORDER BY seq").fetchall()
        assert rows == [(number + 1, f"event-{number:08d}") for number in range(result.events_copied)]
        new = conn.execute("INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,created_at) "
                           "VALUES('after-damage','fixture','attempt-run','run',2000)")
        assert new.lastrowid > result.events_last_seq
        conn.commit()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_salvage_refuses_existing_target_and_never_writes_source(tmp_path, suffix):
    source = _source(tmp_path / "source/runtime.db")
    target = tmp_path / "target/runtime.db"
    target.parent.mkdir()
    artifact = Path(str(target) + suffix)
    artifact.write_bytes(b"must remain unchanged")
    before = _snapshot(source)
    with pytest.raises(FileExistsError):
        salvage.salvage_runtime_db(source, target)
    assert artifact.read_bytes() == b"must remain unchanged" and _snapshot(source) == before
    result = salvage.salvage_runtime_db(source, tmp_path / "clean-target/runtime.db")
    assert result.ok and _snapshot(source) == before


def test_source_attach_is_enforced_read_only(tmp_path, monkeypatch):
    source = _source(tmp_path / "source/runtime.db")
    before = _snapshot(source)
    original = salvage._copy_table
    checked = []

    def copy_table(conn, table):
        if not checked:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute("INSERT INTO src.metadata(key,value) VALUES('forbidden-write','no')")
            conn.rollback()
            checked.append(True)
        return original(conn, table)

    monkeypatch.setattr(salvage, "_copy_table", copy_table)
    assert salvage.salvage_runtime_db(source, tmp_path / "target/runtime.db").ok
    assert checked and _snapshot(source) == before


def test_salvage_uses_column_intersection_and_only_shared_tables(tmp_path):
    source = _source(tmp_path / "source/runtime.db")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("ALTER TABLE tasks DROP COLUMN goal")
        conn.execute("ALTER TABLE tasks ADD COLUMN legacy_context TEXT NOT NULL DEFAULT 'legacy'")
        conn.execute("DROP TABLE artifact_records")
        conn.execute("UPDATE metadata SET value='untrusted-source-fingerprint' WHERE key='sqlite_schema_fingerprint'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = _snapshot(source)
    result = salvage.salvage_runtime_db(source, tmp_path / "target/runtime.db")
    assert result.ok and "artifact_records" not in result.tables
    with closing(sqlite3.connect(result.target)) as conn:
        assert conn.execute("SELECT title,goal FROM tasks").fetchone() == ("恢复样本", "")
        assert "legacy_context" not in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
        fingerprint = conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0]
        assert fingerprint != "untrusted-source-fingerprint"
        assert conn.execute("SELECT value FROM metadata WHERE key='fixture-marker'").fetchone()[0] == "原始游标"
        assert conn.execute("SELECT count(*) FROM artifact_records").fetchone()[0] == 0
    assert _snapshot(source) == before


def test_cli_exit_codes(tmp_path):
    source = _source(tmp_path / "source/runtime.db")
    good = _cli("--source", source, "--target", tmp_path / "good/runtime.db", cwd=tmp_path)
    assert good.returncode == 0, good.stderr
    receipt = json.loads(good.stdout)
    assert receipt["ok"] is True and receipt["integrity"] == "ok" and receipt["source_wal_bytes"] == 0
    damaged = _damaged_copy(source, tmp_path / "damaged/runtime.db", table="tasks")
    before = _snapshot(damaged)
    bad = _cli("--source", damaged, "--target", tmp_path / "bad/runtime.db", cwd=tmp_path)
    assert bad.returncode == 2, bad.stderr
    receipt = json.loads(bad.stdout)
    assert receipt["ok"] is False and receipt["tables"]["tasks"]["error"]
    assert receipt["tables"]["tasks"]["copied"] == 0
    assert receipt["tables"]["agent_runs"]["copied"] == 1
    assert _snapshot(damaged) == before
    missing = _cli(cwd=tmp_path)
    assert missing.returncode == 1 and json.loads(missing.stdout)["error"]
    invalid = _cli("--source", source, "--target", tmp_path / "unused", "--batch-rows", "0", cwd=tmp_path)
    assert invalid.returncode == 1 and not (tmp_path / "unused").exists()


def test_cold_wal_rows_are_copied_and_cli_reports_original_wal_bytes(tmp_path):
    source = tmp_path / "cold WAL 中文/runtime.db"
    source.parent.mkdir()
    writer = """
import os, sqlite3, sys
from pathlib import Path
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
repo = RuntimeRepository(Path(sys.argv[1]))
conn = sqlite3.connect(repo.db_path)
conn.execute('PRAGMA wal_autocheckpoint=0')
conn.execute("INSERT INTO metadata(key,value) VALUES('wal-only','WAL 中的状态')")
conn.commit()
os._exit(0)
"""
    child = subprocess.run([sys.executable, "-c", writer, str(source)], capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr
    wal_bytes = Path(str(source) + "-wal").stat().st_size
    assert wal_bytes > 0
    before = _snapshot(source)
    target = tmp_path / "target with % and # 中文/runtime.db"
    result = _cli("--source", source, "--target", target, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source_wal_bytes"] == wal_bytes
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='wal-only'").fetchone()[0] == "WAL 中的状态"
    assert _snapshot(source) == before


def test_event_watermark_does_not_reuse_previously_allocated_sequences(tmp_path):
    source = _source(tmp_path / "source/runtime.db")
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("UPDATE sqlite_sequence SET seq=1000 WHERE name='runtime_events'")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    result = salvage.salvage_runtime_db(source, tmp_path / "target/runtime.db")
    assert result.events_last_seq == 11
    with closing(sqlite3.connect(result.target)) as conn:
        appended = conn.execute("INSERT INTO runtime_events(event_id,event_type,attempt_id,agent_run_id,created_at) "
                                "VALUES('new-sequence','fixture','attempt-run','run',2000)")
        assert appended.lastrowid > 1000
        conn.commit()


def test_missing_source_does_not_create_target(tmp_path):
    target = tmp_path / "not-created/runtime.db"
    with pytest.raises(FileNotFoundError):
        salvage.salvage_runtime_db(tmp_path / "missing.db", target)
    assert not target.parent.exists()


def test_unrelated_empty_sqlite_database_is_not_verified_as_runtime_history(tmp_path):
    source = tmp_path / "empty.db"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA user_version=1")
        conn.commit()
    before = _snapshot(source)
    result = salvage.salvage_runtime_db(source, tmp_path / "target.db")
    assert result.integrity == "ok" and result.ok is False
    assert not result.tables and result.events_copied == 0
    assert _snapshot(source) == before


def test_unreadable_source_does_not_count_installer_rows_as_copied(tmp_path):
    source = tmp_path / "not-a-database.db"
    source.write_bytes(b"not a SQLite database")
    before = _snapshot(source)
    result = salvage.salvage_runtime_db(source, tmp_path / "target.db")
    assert result.ok is False and result.integrity == "ok"
    assert result.tables and all(item == {
        "source_rows": None, "copied": 0, "error": result.events_error,
    } for item in result.tables.values())
    assert result.events_error and _snapshot(source) == before


@pytest.mark.parametrize("batch_rows", [0, -1, True, False, 1.5, "3"])
def test_invalid_batch_budget_does_not_create_target(tmp_path, batch_rows):
    source = _source(tmp_path / "source/runtime.db")
    target = tmp_path / "not-created/runtime.db"
    with pytest.raises(ValueError):
        salvage.salvage_runtime_db(source, target, batch_rows=batch_rows)
    assert not target.parent.exists()
