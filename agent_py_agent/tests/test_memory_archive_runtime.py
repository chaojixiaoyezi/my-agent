from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.memory_archive import archive_run_turn


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_archive_run_turn_writes_user_and_assistant_events():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = archive_run_turn(
            root,
            session_id="session-1",
            request_id="req-1",
            run_id="run-1",
            task_id="task-1",
            user_prompt="请记住这轮用户消息。",
            response_text="我已经完成这一轮回复。",
            backend="echo",
            created_at="2026-04-30T10:00:00+08:00",
        )

        assert result.event_count == 2
        assert result.token_estimate > 0
        assert result.write_paths == (root / "memory" / "raw" / "2026-04-30.jsonl",)

        records = _read_jsonl(result.write_paths[0])
        assert [record["speaker"] for record in records] == ["user", "assistant"]
        assert records[0]["action"] == "message"
        assert records[0]["target"] == "assistant"
        assert records[0]["content_preview"] == "请记住这轮用户消息。"
        assert records[0]["content_path"] == ""
        assert records[0]["request_id"] == "req-1"
        assert records[1]["action"] == "response"
        assert records[1]["target"] == "user"
        assert records[1]["content_preview"] == "我已经完成这一轮回复。"
        assert records[1]["run_id"] == "run-1"
        assert records[1]["task_id"] == "task-1"


def test_archive_run_turn_archive_level_changes_preview_length():
    long_prompt = "用户长消息-" + ("0123456789" * 220)

    with tempfile.TemporaryDirectory() as td0, tempfile.TemporaryDirectory() as td3:
        level0 = archive_run_turn(
            Path(td0),
            session_id="session-1",
            user_prompt=long_prompt,
            response_text="短回复",
            backend="echo",
            archive_level=0,
            created_at="2026-04-30T10:00:00+08:00",
        )
        level3 = archive_run_turn(
            Path(td3),
            session_id="session-1",
            user_prompt=long_prompt,
            response_text="短回复",
            backend="echo",
            archive_level=3,
            created_at="2026-04-30T10:00:00+08:00",
        )

        assert len(level0.events[0].content_preview) > len(level3.events[0].content_preview)
        assert len(level3.events[0].content_preview) == 160
        assert level3.events[0].content_preview.endswith("...")


def test_archive_run_turn_event_ids_and_hashes_are_stable():
    kwargs = {
        "session_id": "session-1",
        "request_id": "req-1",
        "run_id": "run-1",
        "task_id": "task-1",
        "user_prompt": "同样的用户正文",
        "response_text": "同样的助手正文",
        "backend": "echo",
        "tool_calls": [{"tool": "read_file", "id": "call-1", "ok": True, "output": "same output"}],
        "created_at": "2026-04-30T10:00:00+08:00",
    }

    with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
        first = archive_run_turn(Path(td1), **kwargs)
        second = archive_run_turn(Path(td2), **kwargs)

        assert first.event_ids == second.event_ids
        assert first.content_hashes == second.content_hashes
        assert all(event_id.startswith("raw:") for event_id in first.event_ids)
        assert all(content_hash.startswith("sha256:") for content_hash in first.content_hashes)


def test_archive_run_turn_writes_tool_event_metadata():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = archive_run_turn(
            root,
            session_id="session-1",
            user_prompt="读取文件",
            response_text="文件不存在。",
            backend="echo",
            tool_calls=[
                {
                    "tool": "read_file",
                    "id": "call-7",
                    "ok": False,
                    "error_code": "ENOENT",
                    "parameters": {"path": "missing.txt"},
                    "output": "missing.txt not found",
                }
            ],
            created_at="2026-04-30T10:00:00+08:00",
        )

        records = _read_jsonl(result.write_paths[0])
        tool_record = records[2]
        assert result.event_count == 3
        assert tool_record["speaker"] == "tool"
        assert tool_record["target"] == "assistant"
        assert tool_record["action"] == "tool_call"
        assert tool_record["tool_name"] == "read_file"
        assert tool_record["tool_call_id"] == "call-7"
        assert tool_record["tool_success"] is False
        assert tool_record["status"] == "error"
        assert tool_record["error_code"] == "ENOENT"
        assert "read_file" in tool_record["content_preview"]
        assert "output_hash" in tool_record["content_preview"]
        assert tool_record["content_path"] == ""
