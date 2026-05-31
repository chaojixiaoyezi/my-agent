from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.runtime_live_archive import (
    archive_assistant_tool_round_if_enabled,
    update_runtime_fact_progress_if_enabled,
)
from agent_py_agent.agent.memory_archive import archive_run_turn
from agent_py_agent.agent.memory_archive.runtime.live_archiver import (
    ArchiveAssistantToolRoundParams,
    ArchiveLiveToolCallParams,
    archive_assistant_tool_round,
    archive_live_tool_call,
)
from agent_py_agent.agent.memory_archive.runtime.turn_archiver import (
    ArchiveRunTurnParams,
    ArchiveTurnContext,
)
from agent_py_agent.agent.settings.config import AgentConfig


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_archive_run_turn_writes_user_and_assistant_events():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=root,
                ctx=ArchiveTurnContext(
                    session_id="session-1",
                    request_id="req-1",
                    run_id="run-1",
                    task_id="task-1",
                    user_prompt="请记住这轮用户消息。",
                    response_text="我已经完成这一轮回复。",
                    backend="echo",
                    created_at="2026-04-30T10:00:00+08:00",
                ),
            )
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
            ArchiveRunTurnParams(
                root=Path(td0),
                ctx=ArchiveTurnContext(
                    session_id="session-1",
                    user_prompt=long_prompt,
                    response_text="短回复",
                    backend="echo",
                    archive_level=0,
                    created_at="2026-04-30T10:00:00+08:00",
                ),
            )
        )
        level3 = archive_run_turn(
            ArchiveRunTurnParams(
                root=Path(td3),
                ctx=ArchiveTurnContext(
                    session_id="session-1",
                    user_prompt=long_prompt,
                    response_text="短回复",
                    backend="echo",
                    archive_level=3,
                    created_at="2026-04-30T10:00:00+08:00",
                ),
            )
        )

        assert len(level0.events[0].content_preview) > len(level3.events[0].content_preview)
        assert len(level3.events[0].content_preview) == 160
        assert level3.events[0].content_preview.endswith("...")


def test_archive_run_turn_event_ids_and_hashes_are_stable():
    params = ArchiveRunTurnParams(
        root=None,  # will be overridden per branch
        ctx=ArchiveTurnContext(
            session_id="session-1",
            request_id="req-1",
            run_id="run-1",
            task_id="task-1",
            user_prompt="同样的用户正文",
            response_text="同样的助手正文",
            backend="echo",
            tool_calls=[{"tool": "read_file", "id": "call-1", "ok": True, "output": "same output"}],
            created_at="2026-04-30T10:00:00+08:00",
        ),
    )

    with tempfile.TemporaryDirectory() as td1, tempfile.TemporaryDirectory() as td2:
        first = archive_run_turn(ArchiveRunTurnParams(root=Path(td1), ctx=params.ctx))
        second = archive_run_turn(ArchiveRunTurnParams(root=Path(td2), ctx=params.ctx))

        assert first.event_ids == second.event_ids
        assert first.content_hashes == second.content_hashes
        assert all(event_id.startswith("raw:") for event_id in first.event_ids)
        assert all(content_hash.startswith("sha256:") for content_hash in first.content_hashes)


def test_archive_run_turn_writes_tool_event_metadata():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=root,
                ctx=ArchiveTurnContext(
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
                ),
            )
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


def test_archive_live_tool_round_writes_before_turn_finalization():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        assistant = archive_assistant_tool_round(
            ArchiveAssistantToolRoundParams(
                root=root,
                session_id="session-live",
                request_id="req-live",
                run_id="run-live",
                task_id="task-live",
                tool_round=7,
                response_text="我先搜索最近一周的数据，再写入表格。",
                tool_calls=[{"tool": "web_search", "query": "weekly stars"}],
                created_at="2026-04-30T10:00:00+08:00",
            )
        )
        tool = archive_live_tool_call(
            ArchiveLiveToolCallParams(
                root=root,
                session_id="session-live",
                request_id="req-live",
                run_id="run-live",
                task_id="task-live",
                tool_round=7,
                tool_index=1,
                tool_record={
                    "tool": "web_search",
                    "id": "7-1",
                    "ok": True,
                    "parameters": {"query": "weekly stars"},
                    "output_preview": "search results",
                },
                created_at="2026-04-30T10:00:01+08:00",
            )
        )

        records = _read_jsonl(root / "memory" / "raw" / "2026-04-30.jsonl")
        assert [record["action"] for record in records] == ["assistant_tool_round", "tool_call"]
        assert records[0]["speaker"] == "assistant"
        assert records[0]["status"] == "ok"
        assert "搜索最近一周" in records[0]["content_preview"]
        assert records[1]["tool_name"] == "web_search"
        assert records[1]["tool_success"] is True
        assert assistant.event_count == 1
        assert tool.event_count == 1


def test_live_archive_respects_archive_level_zero(tmp_path: Path) -> None:
    config = AgentConfig()
    config.memory_archive_level = 0
    config.memory_archive_preview_level_0_chars = 1000
    config.memory_archive_preview_level_3_chars = 20
    agent = SimpleNamespace(root=tmp_path, config=config, session_id="session-live")
    params = SimpleNamespace(
        request_id="req-live",
        run_id="run-live",
        task_id="task-live",
        save=True,
    )

    archive_assistant_tool_round_if_enabled(
        agent,
        params,
        tool_round=1,
        response_text="长内容-" + ("abcdef" * 30),
        tool_calls=[],
    )

    raw_files = list((tmp_path / "memory" / "raw").glob("*.jsonl"))
    assert len(raw_files) == 1
    records = _read_jsonl(raw_files[0])
    assert records[0]["archive_level"] == 0
    assert "abcdefabcdefabcdef" in records[0]["content_preview"]


def test_archive_run_turn_skips_tool_records_already_written_live():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)

        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=root,
                ctx=ArchiveTurnContext(
                    session_id="session-1",
                    request_id="req-1",
                    run_id="run-1",
                    task_id="task-1",
                    user_prompt="执行工具",
                    response_text="工具已经执行完。",
                    backend="echo",
                    tool_calls=[
                        {
                            "tool": "read_file",
                            "id": "1-1",
                            "ok": True,
                            "raw_archive_event_id": "raw-live-tool",
                            "output_preview": "already archived",
                        }
                    ],
                    created_at="2026-04-30T10:00:00+08:00",
                ),
            )
        )

        records = _read_jsonl(result.write_paths[0])
        assert result.event_count == 2
        assert [record["speaker"] for record in records] == ["user", "assistant"]


def test_runtime_fact_progress_updates_without_raw_checkpoint(tmp_path: Path) -> None:
    config = AgentConfig()
    agent = SimpleNamespace(root=tmp_path, config=config, session_id="session-live")
    params = SimpleNamespace(
        request_id="req-live",
        run_id="run-live",
        task_id="task-live",
        user_prompt="继续长期任务",
        executed_tools=["web_search"],
        tool_context=["下一步写入最终报告。"],
        archive_tool_calls=[
            {
                "tool": "web_search",
                "raw_archive_path": str(tmp_path / "memory" / "raw" / "2026-04-30.jsonl"),
                "artifact_path": str(tmp_path / "outputs" / "report.md"),
            }
        ],
        live_archive_state={},
    )

    update_runtime_fact_progress_if_enabled(agent, params, tool_round=7)

    fact_path = tmp_path / "memory_archive" / "runtime_facts" / "req-live" / "task.json"
    payload = json.loads(fact_path.read_text(encoding="utf-8"))
    assert payload["runtime_progress"]["tool_rounds"] == 7
    assert payload["runtime_progress"]["executed_tools"] == ["web_search"]
    assert any(ref.endswith("outputs/report.md") for ref in payload["runtime_progress"]["artifact_refs"])
    assert not (tmp_path / "memory" / "raw").exists()


# LLM: Runtime live archive should follow the resolved owner home when available.
# 函数用途: 验证运行中 raw archive 和 runtime_fact 优先写入 owner 私有目录，不再默认写到 workspace 根。
def test_live_archive_and_runtime_fact_use_owner_home(tmp_path: Path) -> None:
    config = AgentConfig()
    owner_home = tmp_path / "home" / "owners" / "providers" / "feishu" / "users" / "ou_123"
    agent = SimpleNamespace(
        root=tmp_path / "workspace",
        home_paths=SimpleNamespace(owner_home_dir=owner_home),
        config=config,
        session_id="session-live",
    )
    params = SimpleNamespace(
        request_id="req-owner",
        run_id="run-owner",
        task_id="task-owner",
        user_prompt="记录到 owner home",
        executed_tools=["read_file"],
        tool_context=[],
        archive_tool_calls=[],
        live_archive_state={},
        save=True,
    )

    archive_assistant_tool_round_if_enabled(agent, params, tool_round=1, response_text="准备读取资料。", tool_calls=[])
    update_runtime_fact_progress_if_enabled(agent, params, tool_round=1)

    assert list((owner_home / "memory" / "raw").glob("*.jsonl"))
    assert (owner_home / "memory_archive" / "runtime_facts" / "req-owner" / "task.json").exists()
    assert not (agent.root / "memory" / "raw").exists()
    assert not (agent.root / "memory_archive" / "runtime_facts" / "req-owner" / "task.json").exists()


# LLM: Continuation runs should update progress without replacing the original task goal.
# 函数用途: 验证 compact 续跑轮写 runtime_fact 时保留 root_user_prompt，不把“继续执行”提示当用户目标。
def test_runtime_fact_progress_preserves_root_user_prompt(tmp_path: Path) -> None:
    config = AgentConfig()
    agent = SimpleNamespace(root=tmp_path, config=config, session_id="session-live")
    params = SimpleNamespace(
        request_id="req-live-root",
        run_id="run-live-root",
        task_id="task-live-root",
        user_prompt="继续执行 Compact Auto Continuation 包里的 Next Step。",
        root_user_prompt="请整理 all-agent 下面的项目并写中文报告。",
        executed_tools=["read_file"],
        tool_context=["[TOOL_CALL]\n{\"tool\":\"read_file\"}\n[/TOOL_CALL]\ntools: read_file"],
        archive_tool_calls=[],
        live_archive_state={},
    )

    update_runtime_fact_progress_if_enabled(agent, params, tool_round=2)

    fact_path = tmp_path / "memory_archive" / "runtime_facts" / "req-live-root" / "task.json"
    payload = json.loads(fact_path.read_text(encoding="utf-8"))
    assert payload["goal"] == "请整理 all-agent 下面的项目并写中文报告。"
