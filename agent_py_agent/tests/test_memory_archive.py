from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import pytest

import agent_py_agent.agent.memory_archive.storage as archive_storage
from agent_py_agent.agent.memory_archive import (
    CompressionSnapshot,
    RawMemoryEvent,
    append_raw_event,
    append_snapshot,
    enforce_retention,
    estimate_tokens,
)


def _demo_snapshot(created_at: str = "2026-04-30T10:00:00+08:00") -> CompressionSnapshot:
    return CompressionSnapshot(
        snapshot_id="snap-1",
        session_id="session-1",
        compression_id="compact-1",
        turn_range={"start": 1, "end": 12},
        participants=["user", "assistant"],
        user_intents=["继续记忆系统开发"],
        assistant_actions=["写入压缩前快照"],
        tool_calls=[{"tool_name": "read_file", "success": True}],
        dispatch_events=[{"worker": "memory-c", "status": "done"}],
        task_refs=["memory-archive"],
        decisions=["hook 和 raw 分目录存储"],
        open_questions=["真实压缩流程接入点待定"],
        next_actions=["接入压缩前 hook"],
        token_usage={"estimate": 1234},
        archive_level=3,
        content_paths=["audit/2026-04-30.jsonl"],
        created_at=created_at,
    )


def _demo_raw_event(created_at: str = "2026-04-30T10:01:00+08:00") -> RawMemoryEvent:
    return RawMemoryEvent(
        event_id="evt-1",
        session_id="session-1",
        request_id="req-1",
        run_id="run-1",
        speaker="user",
        target="assistant",
        action="message",
        created_at=created_at,
        status="ok",
        error_code="",
        is_dispatch=False,
        task_id="task-1",
        tool_name="",
        tool_call_id="",
        tool_success=None,
        content_preview="宝宝，记忆压缩前要先保存现场。",
        content_path="audit/blob-1.txt",
        content_hash="sha256:demo",
        visibility="private",
        source="chat",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_snapshot_and_raw_event_use_daily_paths():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        snapshot_path = append_snapshot(root, _demo_snapshot())
        raw_path = append_raw_event(root, _demo_raw_event())

        assert snapshot_path == root / "memory" / "hooks" / "2026-04-30.jsonl"
        assert raw_path == root / "audit" / "2026-04-30.jsonl"
        assert snapshot_path.exists()
        assert raw_path.exists()


def test_append_snapshot_readback_writes_expected_payload():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        snapshot = _demo_snapshot()

        path = append_snapshot(root, snapshot)
        records = _read_jsonl(path)

        assert len(records) == 1
        assert records[0]["snapshot_id"] == "snap-1"
        assert records[0]["token_usage"]["estimate"] == 1234
        assert records[0]["next_actions"] == ["接入压缩前 hook"]


def test_append_raw_event_writes_archive_fields():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        event = _demo_raw_event()

        path = append_raw_event(root, event)
        records = _read_jsonl(path)

        assert len(records) == 1
        assert records[0]["event_id"] == "evt-1"
        assert records[0]["speaker"] == "user"
        assert records[0]["tool_success"] is None
        assert records[0]["content_hash"] == "sha256:demo"


def test_append_raw_event_readback_failure_is_reported(monkeypatch, tmp_path):
    def append_incomplete_record(path: Path, payload: dict, **kwargs) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"event_id": payload["event_id"]}, ensure_ascii=False) + "\n", encoding="utf-8")

    monkeypatch.setattr(archive_storage, "append_jsonl", append_incomplete_record)

    with pytest.raises(archive_storage.MemoryArchiveError, match="readback payload mismatch"):
        archive_storage.append_raw_event(tmp_path, _demo_raw_event())


def test_retention_seven_days_deletes_old_hook_files_only():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hook_dir = root / "memory" / "hooks"
        raw_dir = root / "audit"
        hook_dir.mkdir(parents=True)
        raw_dir.mkdir(parents=True)
        old_hook = hook_dir / "2026-04-20.jsonl"
        kept_boundary_hook = hook_dir / "2026-04-24.jsonl"
        kept_today_hook = hook_dir / "2026-04-30.jsonl"
        raw_file = raw_dir / "2026-04-20.jsonl"
        for path in [old_hook, kept_boundary_hook, kept_today_hook, raw_file]:
            path.write_text("{}\n", encoding="utf-8")

        deleted = enforce_retention(root, 7, today=date(2026, 4, 30))

        assert deleted == [old_hook]
        assert not old_hook.exists()
        assert kept_boundary_hook.exists()
        assert kept_today_hook.exists()
        assert raw_file.exists()


def test_retention_zero_keeps_all_hook_files():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hook_dir = root / "memory" / "hooks"
        hook_dir.mkdir(parents=True)
        old_hook = hook_dir / "2026-04-01.jsonl"
        old_hook.write_text("{}\n", encoding="utf-8")

        deleted = enforce_retention(root, 0, today="2026-04-30")

        assert deleted == []
        assert old_hook.exists()


def test_invalid_retention_is_safe_and_does_not_delete():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        hook_dir = root / "memory" / "hooks"
        hook_dir.mkdir(parents=True)
        old_hook = hook_dir / "2026-04-01.jsonl"
        old_hook.write_text("{}\n", encoding="utf-8")

        assert enforce_retention(root, "abcd", today="2026-04-30") == []
        assert enforce_retention(root, -7, today="2026-04-30") == []
        assert enforce_retention(root, None, today="2026-04-30") == []
        assert old_hook.exists()


def test_token_estimate_handles_mixed_language_and_tool_payload():
    payload = {
        "message": "中文任务说明 mixed with English words",
        "tool_output": "line one\nline two\n" + ("x" * 300),
        "success": True,
    }

    assert estimate_tokens(payload) > 0
    assert estimate_tokens("纯中文内容也要保守估算") >= 1
