# LLM: 本文件钉住第三批宿主私有写（pw3）：改动过的写入点新文件必须 0600、新目录 0700、
#   原子写不留半截、旧数据照常可读。任何一个写入点退回普通写时，这些用例必须变红。
#   optimistic_lock 的版本锁记录在子代理工作区（用户可见产出区），按 ds10/ds1 裁定不收私、
#   恢复原样，不在本批覆盖（17j sclk 段已登记"非私有状态目录，未改"）。
# 模块用途: 验证 pw3 各写入点的权限与原子性契约，不测业务语义。

from __future__ import annotations

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextmanager
def _umask(value: int):
    """临时设置进程 umask；本文件用它证明 0700 来自显式 mode，而不是继承调用方的 umask。"""
    old = os.umask(value)
    try:
        yield
    finally:
        os.umask(old)


def _assert_private_file(path: Path) -> None:
    assert path.exists(), f"missing file: {path}"
    assert _mode(path) == 0o600, f"{path} mode={oct(_mode(path))}, expected 0o600"


def _assert_private_dir(path: Path) -> None:
    assert path.is_dir(), f"missing dir: {path}"
    assert _mode(path) == 0o700, f"{path} mode={oct(_mode(path))}, expected 0o700"


def _adapter_paths(root: Path):
    from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )


class TestChunkStream:
    def test_write_chunk_event_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

        chunk = tmp_path / "chunks" / "req.jsonl"
        write_chunk_event(chunk, {"kind": "model_delta", "text": "hi"})
        _assert_private_dir(chunk.parent)
        _assert_private_file(chunk)
        row = json.loads(chunk.read_text(encoding="utf-8").splitlines()[0])
        assert row["kind"] == "model_delta"
        assert row["text"] == "hi"

    def test_write_chunk_event_tightens_existing_and_keeps_old_rows(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import write_chunk_event

        chunk = tmp_path / "chunks" / "req.jsonl"
        chunk.parent.mkdir(parents=True, exist_ok=True)
        chunk.write_text('{"old": 1}\n', encoding="utf-8")
        os.chmod(chunk, 0o644)
        write_chunk_event(chunk, {"kind": "runtime_progress", "text": "x"})
        _assert_private_file(chunk)
        lines = chunk.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"old": 1}

    def test_open_chunk_stream_creates_private_dir(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.stream_writer import open_chunk_stream

        chunk = tmp_path / "chunks" / "req.jsonl"
        path, started = open_chunk_stream(chunk)
        assert path == chunk
        assert started > 0
        _assert_private_dir(chunk.parent)


class TestAdapterLate:
    def test_record_late_pending_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter_late import _record_late_pending

        paths = _adapter_paths(tmp_path)
        _record_late_pending(paths, "req_1", 30.0)
        late = tmp_path / "late_pending.jsonl"
        _assert_private_file(late)
        entry = json.loads(late.read_text(encoding="utf-8").splitlines()[0])
        assert entry["request_id"] == "req_1"
        assert entry["original_timeout"] == 30.0

    def test_rewrite_unchecked_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter_late import _rewrite_unchecked_late_entries

        late = tmp_path / "late_pending.jsonl"
        late.write_text('{"request_id": "old", "checked": false}\n', encoding="utf-8")
        os.chmod(late, 0o644)
        _rewrite_unchecked_late_entries(late, [{"request_id": "old", "checked": False}], None)
        _assert_private_file(late)
        assert json.loads(late.read_text(encoding="utf-8").splitlines()[0])["request_id"] == "old"

    def test_adapter_dirs_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.adapter import _ensure_adapter_dirs

        paths = _adapter_paths(tmp_path)
        _ensure_adapter_dirs(paths)
        for directory in (paths.inbox, paths.processing, paths.done, paths.failed, paths.outbox):
            _assert_private_dir(directory)


class TestTaskProgress:
    def test_write_task_progress_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.task_progress import (
            progress_path,
            read_task_progress,
            write_task_progress,
        )

        merged = write_task_progress(
            tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "pending"}]}
        )
        assert merged["items"][0]["id"] == "p1"
        path = progress_path(tmp_path, "run-1")
        _assert_private_file(path)
        _assert_private_dir(path.parent)
        _assert_private_dir(path.parent.parent)
        again = read_task_progress(tmp_path, "run-1")
        assert again["items"][0]["id"] == "p1"

    def test_atomic_replace_failure_keeps_previous_content(self, tmp_path: Path, monkeypatch) -> None:
        from agent_py_agent.agent import task_progress
        from agent_py_agent.agent.common import json_io

        task_progress.write_task_progress(
            tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "pending"}]}
        )
        path = task_progress.progress_path(tmp_path, "run-1")
        before = path.read_text(encoding="utf-8")

        def _fail_replace(tmp, target, *, keep_mode: bool = True) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(json_io, "_replace_with_retry", _fail_replace)
        with pytest.raises(OSError):
            task_progress.write_task_progress(
                tmp_path, "run-1", {"items": [{"id": "p1", "title": "t", "status": "done"}]}
            )
        assert path.read_text(encoding="utf-8") == before
        leftovers = [item.name for item in path.parent.iterdir() if item.name.startswith(".")]
        assert leftovers == []


class TestSessionManager:
    def test_create_session_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        manager = SessionManager(
            SimpleNamespace(session_workspace=str(tmp_path / "sessions"), user_id="u1")
        )
        session = manager.create_session()
        session_path = tmp_path / "sessions" / session.session_id / "session.json"
        _assert_private_file(session_path)
        _assert_private_dir(session_path.parent)
        loaded = manager.load_session(session.session_id)
        assert loaded is not None and loaded.session_id == session.session_id

    def test_session_root_created_private_under_umask_022(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        # umask 022 下仍必须是 0700：证明权限来自显式 mode，而不是跟着调用方的 umask 走。
        root = tmp_path / "sessions"
        with _umask(0o022):
            SessionManager(SimpleNamespace(session_workspace=str(root), user_id="u1"))
        _assert_private_dir(root)

    def test_session_root_existing_permissions_unchanged(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.manager import SessionManager

        # 已存在的目录一位都不动：只动自己新建的东西。
        root = tmp_path / "sessions"
        root.mkdir()
        os.chmod(root, 0o755)
        SessionManager(SimpleNamespace(session_workspace=str(root), user_id="u1"))
        assert _mode(root) == 0o755, f"existing dir mode changed to {oct(_mode(root))}"


class TestCrossChannel:
    def test_save_channels_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        store = CrossChannelSession(SimpleNamespace(session_workspace=str(tmp_path / "sessions")))
        store._save_channels("sess1", {"telegram": {"active": True}})
        path = tmp_path / "sessions" / "sess1" / "channels.json"
        _assert_private_file(path)
        _assert_private_dir(path.parent)
        assert json.loads(path.read_text(encoding="utf-8")) == {"telegram": {"active": True}}

    def test_channel_root_created_private_under_umask_022(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        root = tmp_path / "sessions"
        with _umask(0o022):
            CrossChannelSession(SimpleNamespace(session_workspace=str(root)))
        _assert_private_dir(root)

    def test_channel_root_existing_permissions_unchanged(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        root = tmp_path / "sessions"
        root.mkdir()
        os.chmod(root, 0o755)
        CrossChannelSession(SimpleNamespace(session_workspace=str(root)))
        assert _mode(root) == 0o755, f"existing dir mode changed to {oct(_mode(root))}"


class TestStoreTasksSummary:
    def test_summary_status_sync_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.conversation.store_tasks import (
            _sync_task_workspace_summary_status,
        )

        task_root = tmp_path / "task"
        task_root.mkdir()
        summary = task_root / "current_summary.md"
        summary.write_text("# T\n- status: PENDING\n- current_step: PENDING\n", encoding="utf-8")
        os.chmod(summary, 0o644)
        _sync_task_workspace_summary_status(task_root, summary, {"status": "running"})
        _assert_private_file(summary)
        text = summary.read_text(encoding="utf-8")
        assert "- status: RUNNING" in text
        assert "- current_step: RUNNING" in text


class TestRotatingLog:
    def test_append_with_rotation_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.common.rotating_log import (
            RotatePolicy,
            append_with_rotation,
            total_line_count,
        )

        log = tmp_path / "logs" / "app.log"
        append_with_rotation(log, "line1\n", RotatePolicy())
        _assert_private_dir(log.parent)
        _assert_private_file(log)
        _assert_private_file(tmp_path / "logs" / "app.log.rotmeta.json")
        assert total_line_count(log) == 1
        assert log.read_text(encoding="utf-8") == "line1\n"


class TestMessageRepairs:
    def test_queue_gateway_conversation_repair_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.gateway_parts.request_history import (
            _queue_gateway_conversation_repair,
        )

        # message_repairs 含消息正文；落盘必须私有：目录缺失按 0700 建、文件出生 0600。
        # 刻意不预建 message_repairs 目录，验证缺失目录也由私有写建出。
        root = tmp_path / "storage"
        root.mkdir()
        agent = SimpleNamespace(
            conversation_store=SimpleNamespace(storage=SimpleNamespace(root=str(root))),
            _conversation_indexed_threads=set(),
        )
        conversation = SimpleNamespace(thread_id="thread-1")
        _queue_gateway_conversation_repair(
            agent, {}, conversation, request_id="gw-1", role="assistant", content="补交正文",
        )
        path = root / "message_repairs" / "gw-1-assistant.json"
        _assert_private_dir(path.parent)
        _assert_private_file(path)
        assert json.loads(path.read_text(encoding="utf-8"))["content"] == "补交正文"


class TestHomeBackup:
    def test_manifest_private(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.user_space.home_backup import create_home_backup_manifest
        from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home

        home = ensure_my_agent_home(tmp_path)
        manifest = create_home_backup_manifest(home, reason="pw3 test")
        _assert_private_file(manifest.manifest_path)
        payload = json.loads(manifest.manifest_path.read_text(encoding="utf-8"))
        assert payload["reason"] == "pw3 test"


class TestLocalStorageRecords:
    def test_upsert_content_file_private_and_readable(self, tmp_path: Path) -> None:
        from agent_py_agent.agent.local_storage import LocalStore
        from agent_py_agent.agent.local_storage.records import LocalRecordInput

        store = LocalStore(tmp_path / "data" / "local.db", enable_fts=False)
        store.upsert_record(
            LocalRecordInput(source_type="note", source_id="n1", title="t", content="正文内容", record_id="n1")
        )
        content_path = store.files_dir / "n1.txt"
        _assert_private_file(content_path)
        assert content_path.read_text(encoding="utf-8") == "正文内容"
