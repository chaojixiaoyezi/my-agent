"""审计只读分页的来源预算与跨实例游标行为。"""
import json
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest

from agent_py_agent.agent.gateway_parts.gateway_history_index import GatewayHistoryIndex
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_audit_records import (
    OutcomeQuery,
    request_outcome_records,
    request_queue_records,
)


def seed(tmp_path, count=7):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.terminal.mkdir(parents=True)
    index = GatewayHistoryIndex(paths)
    for i in range(count):
        payload = {"id": f"r{i:05}", "status": "done", "owner_id": "local/main", "ended_at": 100,
                   "terminal_response": {"owner_id": "local/main"}, "prompt": "BODY-SENTINEL"}
        (paths.terminal / (payload["id"] + ".json")).write_text(json.dumps(payload))
        index.append(payload)
    index.backfill_audit_step(limit=128)
    return paths


def query(**kwargs):
    return OutcomeQuery(frozenset({"local/main"}), {}, "", 0, 2, **kwargs)


def test_keyset_cross_instance_sparse_filters_and_canonical_wins(tmp_path):
    paths = seed(tmp_path)
    cursor, ids, scanned = "", [], 0
    with patch("os.scandir", side_effect=AssertionError("no history enumeration")):
        while True:
            page = GatewayHistoryIndex(paths).audit_page(query(cursor=cursor))
            ids += [row["request_id"] for row in page["entries"]]
            scanned += page["scanned"]
            assert page["scanned"] <= 2
            assert "BODY-SENTINEL" not in json.dumps(page)
            cursor = page["next_cursor"]
            if not cursor:
                break
    assert ids == [f"r{i:05}" for i in reversed(range(7))] and scanned == 7
    first = GatewayHistoryIndex(paths).audit_page(query(status="failed"))
    assert first["entries"] == [] and first["next_cursor"]
    with pytest.raises(ValueError, match="mismatch"):
        GatewayHistoryIndex(paths).audit_page(query(cursor=first["next_cursor"], channel="other"))
    # 投影的旧状态不能覆盖规范终态。
    path = paths.terminal / "r00000.json"
    payload = json.loads(path.read_text())
    payload["status"] = "failed"
    payload["error_code"] = "TOOL_TIMEOUT"
    path.write_text(json.dumps(payload))
    exact = request_outcome_records(paths, query(request_id="r00000"))
    assert exact["entries"][0]["error_code"] == "TOOL_TIMEOUT"


def test_missing_read_does_not_create_anything(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "missing")
    page = GatewayHistoryIndex(paths).audit_page(query())
    assert page["coverage"]["status"] == "partial" and not paths.root.exists()


def test_read_preserves_database_schema_files_and_mtimes(tmp_path):
    paths = seed(tmp_path)
    before = {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in paths.root.rglob("*") if not str(path).endswith(("-wal", "-shm"))}
    for _ in range(3):
        page = GatewayHistoryIndex(paths).audit_page(query())
        assert page["entries"] or page["coverage"]["status"] == "partial"
    after = {str(path): (path.stat().st_mtime_ns, path.stat().st_size) for path in paths.root.rglob("*") if not str(path).endswith(("-wal", "-shm"))}
    assert before == after
    with sqlite3.connect(paths.root / "history_index.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM audit_keys").fetchone()[0] == 7


def test_exact_pending_is_not_silently_limited_to_24h(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    paths.inbox.mkdir(parents=True)
    (paths.inbox / "old.json").write_text(json.dumps({"id": "old", "owner_id": "local/main", "status": "queued"}))
    page = request_outcome_records(paths, query(request_id="old"))
    assert page["entries"][0]["source"] == "pending"
    active = request_queue_records(paths, query())
    assert active["entries"][0]["orphan_reason"] != "age_expired"


def test_cursor_rejects_projection_replacement_and_paths_before_io(tmp_path):
    paths = seed(tmp_path)
    page = GatewayHistoryIndex(paths).audit_page(query())
    GatewayHistoryIndex(paths).append({"id": "new", "status": "done"})
    with pytest.raises(ValueError, match="generation"):
        GatewayHistoryIndex(paths).audit_page(query(cursor=page["next_cursor"]))
    with pytest.raises(ValueError):
        request_outcome_records(paths, query(request_id="../private"))
    with pytest.raises(ValueError):
        request_outcome_records(paths, replace(query(), since=float("nan")))


def test_pending_processing_overlay_bounded_and_no_duplicates(tmp_path):
    paths = seed(tmp_path, 2)
    paths.inbox.mkdir()
    paths.processing.mkdir()
    pending = {"id": "active", "owner_id": "local/main", "status": "queued"}
    for directory in (paths.inbox, paths.processing):
        (directory / "active.json").write_text(json.dumps(pending))
    cursor, ids = "", []
    for _ in range(10):
        page = request_outcome_records(paths, query(cursor=cursor))
        assert page["scanned"] <= 2
        ids += [row["request_id"] for row in page["entries"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert set(ids) == {"active", "r00000", "r00001"} and len(ids) == 3


def test_public_cursor_freezes_absolute_window(tmp_path):
    from agent_py_agent.agent.tooling.audit_records_tool import _parse
    paths = seed(tmp_path)
    page = request_outcome_records(paths, query())
    with patch("time.time", return_value=999999):
        parsed = _parse({"topic": "requests", "cursor": page["next_cursor"], "limit": 2})
    assert parsed[4] == 0


def test_live_wal_committed_rows_are_visible_and_no_application_files(tmp_path):
    paths = seed(tmp_path, 1)
    with sqlite3.connect(paths.root / "history_index.sqlite3") as keeper:
        keeper.execute("SELECT 1 FROM history").fetchone()
        before = keeper.execute("SELECT request_id,body,digest FROM history ORDER BY request_id").fetchall()
        page = GatewayHistoryIndex(paths).audit_page(query())
        assert page["entries"][0]["request_id"] == "r00000"
        assert keeper.execute("SELECT request_id,body,digest FROM history ORDER BY request_id").fetchall() == before


def test_archived_terminal_point_lookup_does_not_rebuild_missing_locator(tmp_path):
    import base64
    import hashlib
    import os

    paths = seed(tmp_path, 1)
    body = (paths.terminal / "r00000.json").read_bytes()
    relative = "requests/terminal/r00000.json"
    archive = paths.root / "archive"
    archive.mkdir(mode=0o700)
    digest = hashlib.sha256(body).hexdigest()
    encoded = json.dumps({"schema_generation": 1, "path": relative, "request_id": "r00000",
                          "digest": digest, "body": base64.b64encode(body).decode()}).encode() + b"\n"
    (archive / "receipts-seg-000000000001.jsonl").write_bytes(encoded)
    (archive / "receipts-manifest.json").write_text(json.dumps({"schema_generation": 1, "next_seq": 2, "segment_count": 1}))
    locator = archive / "receipts-index.sqlite3"
    with sqlite3.connect(locator) as conn:
        conn.execute("CREATE TABLE receipts(path TEXT PRIMARY KEY,segment TEXT,offset INTEGER,length INTEGER,digest TEXT)")
        conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?)", (relative, "receipts-seg-000000000001.jsonl", 0, len(encoded), digest))
    os.chmod(locator, 0o600)
    (paths.terminal / "r00000.json").unlink()
    with patch("os.scandir", side_effect=AssertionError("no archive discovery")):
        page = request_outcome_records(paths, query(request_id="r00000"))
    assert page["entries"][0]["source"] == "terminal"
    locator.unlink()
    before = {str(path) for path in paths.root.rglob("*")}
    page = request_outcome_records(paths, query(request_id="r00000"))
    assert page["coverage"]["status"] == "partial"
    assert before == {str(path) for path in paths.root.rglob("*")}


def test_backfill_quarantines_oversized_row_and_durably_advances(tmp_path):
    paths = seed(tmp_path, 2)
    index = GatewayHistoryIndex(paths)
    with index.connection() as conn:
        conn.execute("DELETE FROM audit_keys")
        conn.execute("UPDATE history SET body=? WHERE request_id='r00000'", (json.dumps({"id": "r00000", "goal": "x" * 1000000}),))
        index._set_meta(conn, "audit_cursor", 0)
        index._set_meta(conn, "audit_complete", 0)
    first = index.backfill_audit_step(limit=1)
    assert first["items"] == 1 and first["bytes"] <= 262145
    second = GatewayHistoryIndex(paths).backfill_audit_step(limit=1)
    assert second["cursor"] > first["cursor"]
    GatewayHistoryIndex(paths).backfill_audit_step(limit=1)
    page = GatewayHistoryIndex(paths).audit_page(query())
    assert [row["request_id"] for row in page["entries"]] == ["r00001"]
    assert page["coverage"]["status"] == "partial"
