"""验证 SessionListIndex 有界目录分页续点在线性复杂度、游标持久化、异常目录与并发变更下的行为。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.session_list_index import (
    _REBUILD_BATCH,
    SessionListIndex,
    _stamp,
)
from agent_py_agent.agent.user_space import directory_paging
from agent_py_agent.agent.user_space.owner_quota import _QuotaDirectoryStream


def _task_reader(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return {}, {"code": "READ_ERROR", "detail": str(exc)}


@pytest.mark.parametrize("count", [1024, 2048, 4096])
def test_rebuild_enumeration_grows_linearly_and_continues_across_fresh_wrappers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    for n in range(count):
        (root / f"t{n:04}.json").write_text(
            json.dumps({"task_id": f"t{n:04}", "created_at": float(n), "target_thread_id": "owner"}),
            encoding="utf-8",
        )

    dirent_visits = 0
    original_next = _QuotaDirectoryStream.__next__

    def counted_next(self):
        nonlocal dirent_visits
        item = original_next(self)
        dirent_visits += 1
        return item

    monkeypatch.setattr(_QuotaDirectoryStream, "__next__", counted_next)
    scandir_calls: list[Path] = []
    original_scandir = directory_paging.os.scandir

    def counted_scandir(target, *args, **kwargs):
        if isinstance(target, (str, Path)) and Path(target) == root:
            scandir_calls.append(Path(target))
        return original_scandir(target, *args, **kwargs)

    monkeypatch.setattr(directory_paging.os, "scandir", counted_scandir)

    max_calls = count // _REBUILD_BATCH + 3
    first_cursor_json = ""
    for step in range(max_calls):
        # 每轮新建 SessionListIndex 包装器，确保续点落在 SQLite meta 而非进程内实例字段。
        wrapper = SessionListIndex(root, kind="session_task", reader=_task_reader)
        page = wrapper.page(limit=5, filter_key="owner")
        if step == 0:
            with wrapper._connection() as conn:
                first_cursor_json = wrapper._meta(conn, "directory_cursor")
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail(f"rebuild did not complete within {max_calls} bounded batches")

    assert first_cursor_json and first_cursor_json != "{}"
    parsed_cursor = json.loads(first_cursor_json)
    assert isinstance(parsed_cursor, dict)
    assert len(first_cursor_json) < 8192

    if directory_paging._native_directory_supported():
        assert scandir_calls == []
        # 原生目录流线性消费：每个目录项仅访问一次，绝无 O(N^2) 前缀重放。
        assert dirent_visits == count

    seen: list[str] = []
    cursor = None
    checker = SessionListIndex(root, kind="session_task", reader=_task_reader)
    while True:
        page = checker.page(limit=100, cursor=cursor, filter_key="owner")
        assert page["coverage"] == "complete"
        assert page["load_errors"] == []
        seen.extend(item["task_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == count


def test_legacy_incomplete_projection_without_directory_cursor_restarts_once_from_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    count = 300
    for n in range(count):
        (root / f"t{n:04}.json").write_text(
            json.dumps({"task_id": f"t{n:04}", "created_at": float(n), "target_thread_id": "owner"}),
            encoding="utf-8",
        )

    index = SessionListIndex(root, kind="session_task", reader=_task_reader)
    current_stamp = _stamp(root)
    with index._connection() as conn:
        index._put_meta(conn, "directory", current_stamp)
        index._put_meta(conn, "complete", 0)
        index._put_meta(conn, "offset", 256)
        index._put_meta(conn, "generation", 4)
        conn.execute("DELETE FROM meta WHERE key='directory_cursor'")

    observed_cursors: list[dict] = []
    import agent_py_agent.agent.conversation.session_list_index as sli_mod

    original_dp = sli_mod.directory_page

    def spy_dp(scan_root, parts, cursor, limit, *, require_dir_fd=True):
        observed_cursors.append(dict(cursor))
        return original_dp(scan_root, parts, cursor, limit, require_dir_fd=require_dir_fd)

    monkeypatch.setattr(sli_mod, "directory_page", spy_dp)

    for _ in range(10):
        page = index.page(limit=5)
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("legacy incomplete projection did not converge")

    # 绝不能把历史数字 offset=256 当作原生目录 cookie/offset。
    assert observed_cursors[0] == {}
    with index._connection() as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == count


def test_cursor_continuation_survives_cursor_cache_reset_and_subprocess_restart(tmp_path: Path) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    count = 300
    for n in range(count):
        (root / f"t{n:04}.json").write_text(
            json.dumps({"task_id": f"t{n:04}", "created_at": float(n), "target_thread_id": "owner"}),
            encoding="utf-8",
        )

    first = SessionListIndex(root, kind="session_task", reader=_task_reader).page(limit=5)
    assert first["coverage"] == "partial"

    with directory_paging._SCAN_LOCK:
        for _key, cached in list(directory_paging._SCAN_STREAMS.items()):
            cached[0].close()
        directory_paging._SCAN_STREAMS.clear()

    repo_root = Path(__file__).resolve().parents[2]
    script = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from agent_py_agent.agent.conversation.session_list_index import SessionListIndex\n"
        "root = Path(sys.argv[1])\n"
        "idx = SessionListIndex(root, kind='session_task', reader=lambda p: (json.loads(p.read_text()), None))\n"
        "res = idx.page(limit=5)\n"
        "print(json.dumps({'coverage': res['coverage'], 'count': len(res['items'])}))\n"
    )
    out = subprocess.check_output(
        [sys.executable, "-c", script, str(root)],
        cwd=repo_root,
        text=True,
        timeout=30,
    )
    second = json.loads(out)
    assert second["coverage"] == "partial"

    final_idx = SessionListIndex(root, kind="session_task", reader=_task_reader)
    for _ in range(5):
        page = final_idx.page(limit=5)
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("rebuild did not complete after subprocess restart")

    with final_idx._connection() as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == count


def test_directory_mutation_mid_rebuild_never_fabricates_complete_and_expires_client_cursor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tasks"
    root.mkdir()
    count = 200
    for n in range(count):
        (root / f"t{n:04}.json").write_text(
            json.dumps({"task_id": f"t{n:04}", "created_at": float(n), "target_thread_id": "owner"}),
            encoding="utf-8",
        )

    index = SessionListIndex(root, kind="session_task", reader=_task_reader)
    first = index.page(limit=5, filter_key="owner")
    assert first["coverage"] == "partial"
    assert first["next_cursor"] is not None

    import agent_py_agent.agent.conversation.session_list_index as sli_mod

    original_dp = sli_mod.directory_page
    mutated = False

    def mutate_during_tail_batch(scan_root, parts, cursor, limit, *, require_dir_fd=True):
        nonlocal mutated
        result = original_dp(scan_root, parts, cursor, limit, require_dir_fd=require_dir_fd)
        if not mutated:
            mutated = True
            # 即使尾批返回 exhausted=True，目录在批间被修改也绝不能伪造 complete。
            (root / "t_late.json").write_text(
                json.dumps({"task_id": "t_late", "created_at": 9999.0, "target_thread_id": "owner"}),
                encoding="utf-8",
            )
        return result

    monkeypatch.setattr(sli_mod, "directory_page", mutate_during_tail_batch)
    second = index.page(limit=5, filter_key="owner")
    assert second["coverage"] == "partial"
    monkeypatch.setattr(sli_mod, "directory_page", original_dp)

    # 旧客户端分页游标在目录变更引发重建后必须显式失效。
    expired = index.page(limit=5, cursor=first["next_cursor"], filter_key="owner")
    assert expired["coverage"] == "expired"

    for _ in range(10):
        page = index.page(limit=5, filter_key="owner")
        if page["coverage"] == "complete":
            break
    else:
        pytest.fail("rebuild did not converge after mutation")

    seen: list[str] = []
    cursor = None
    while True:
        page = index.page(limit=100, cursor=cursor, filter_key="owner")
        assert page["coverage"] == "complete"
        seen.extend(item["task_id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == count + 1
    assert "t_late" in seen


def test_missing_projection_deleted_entries_and_nofollow_symlinks(tmp_path: Path) -> None:
    task_root = tmp_path / "tasks"
    task_root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"task_id": "evil", "created_at": 999.0}), encoding="utf-8")

    for n in range(3):
        (task_root / f"t{n}.json").write_text(
            json.dumps({"task_id": f"t{n}", "created_at": float(n)}),
            encoding="utf-8",
        )
    (task_root / "notes.txt").write_text("ignored", encoding="utf-8")
    (task_root / "evil.json").symlink_to(outside)
    (task_root / "dangling.json").symlink_to(tmp_path / "missing_target.json")

    task_index = SessionListIndex(task_root, kind="session_task", reader=_task_reader)
    page = task_index.page(limit=10)
    assert page["coverage"] == "complete"
    assert [item["task_id"] for item in page["items"]] == ["t2", "t1", "t0"]

    # 删除真实条目后，下次重建会淘汰旧代次记录。
    (task_root / "t1.json").unlink()
    page_after_del = task_index.page(limit=10)
    assert page_after_del["coverage"] == "complete"
    assert [item["task_id"] for item in page_after_del["items"]] == ["t2", "t0"]

    # 丢弃 SQLite 投影文件后能自动从 canonical 重建，并使旧游标失效。
    first = task_index.page(limit=1)
    assert first["next_cursor"] is not None
    task_index.path.unlink()
    assert task_index.page(limit=1, cursor=first["next_cursor"])["coverage"] == "expired"
    rebuilt = task_index.page(limit=10)
    assert rebuilt["coverage"] == "complete"
    assert [item["task_id"] for item in rebuilt["items"]] == ["t2", "t0"]

    # kind="session" 同样严格校验目录项 follow_symlinks=False，仅收真实目录。
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    for name, ts in (("s1", 10.0), ("s2", 20.0)):
        d = session_root / name
        d.mkdir()
        (d / "session.json").write_text(
            json.dumps({"session_id": name, "user_id": "u1", "updated_at": ts}),
            encoding="utf-8",
        )
    (session_root / "regular.json").write_text("{}", encoding="utf-8")
    outside_dir = tmp_path / "outside_session"
    outside_dir.mkdir()
    (outside_dir / "session.json").write_text(
        json.dumps({"session_id": "evil_dir", "user_id": "u1", "updated_at": 999.0}),
        encoding="utf-8",
    )
    (session_root / "link_dir").symlink_to(outside_dir)

    session_index = SessionListIndex(session_root, kind="session", reader=_task_reader)
    s_page = session_index.page(limit=10, filter_key="u1")
    assert s_page["coverage"] == "complete"
    assert [item["session_id"] for item in s_page["items"]] == ["s2", "s1"]
    with session_index._connection() as conn:
        assert conn.execute("SELECT count(*) FROM entries").fetchone()[0] == 2
