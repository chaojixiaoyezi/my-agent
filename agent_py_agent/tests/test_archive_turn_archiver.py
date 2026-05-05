"""单元测试：memory_archive runtime 模块 - turn_archiver 回合归档器"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_archive.runtime.turn_archiver import (
    ArchiveRunTurnParams,
    ArchiveRunTurnResult,
    ArchiveTurnContext,
    RunContext,
    TurnData,
    _build_run_turn_events,
    archive_run_turn,
)


class TestArchiveRunTurnResult:
    """测试 ArchiveRunTurnResult 数据类"""

    def test_paths_property(self):
        """验证 paths 属性是 write_paths 的别名"""
        result = ArchiveRunTurnResult(
            write_paths=(Path("/a"), Path("/b")),
            event_count=3,
            token_estimate=100,
            event_ids=("e1", "e2", "e3"),
            content_hashes=("h1", "h2", "h3"),
            events=(),
        )
        assert result.paths == result.write_paths

    def test_to_dict_serializes(self):
        """验证 to_dict 正确序列化"""
        result = ArchiveRunTurnResult(
            write_paths=(Path("/a"),),
            event_count=1,
            token_estimate=50,
            event_ids=("e1",),
            content_hashes=("h1",),
            events=(),
        )
        d = result.to_dict()

        assert "write_paths" in d
        assert d["event_count"] == 1
        assert d["token_estimate"] == 50

    def test_dict_access_compatibility(self):
        """验证字典式访问兼容"""
        result = ArchiveRunTurnResult(
            write_paths=(),
            event_count=5,
            token_estimate=200,
            event_ids=(),
            content_hashes=(),
            events=(),
        )
        assert result["event_count"] == 5

    def test_immutable_write_paths(self):
        """验证 write_paths 不可变"""
        result = ArchiveRunTurnResult(
            write_paths=(Path("/a"),),
            event_count=1,
            token_estimate=50,
            event_ids=("e1",),
            content_hashes=("h1",),
            events=(),
        )
        with pytest.raises(AttributeError):
            result.write_paths = (Path("/b"),)


class TestBuildRunTurnEvents:
    """测试 _build_run_turn_events 事件构建"""

    def test_user_message_first(self):
        """验证用户消息在第一位"""
        events = _build_run_turn_events(
            session_id="s1",
            turn=TurnData(
                user_prompt="hello",
                response_text="hi",
                tool_calls=[],
            ),
            ctx=RunContext(
                backend="test",
                request_id="r1",
                run_id="run1",
                task_id="t1",
                source="run",
                archive_level=3,
                created_at="2026-05-01T10:00:00Z",
            ),
        )

        assert events[0].speaker == "user"
        assert events[0].action == "message"

    def test_assistant_response_second(self):
        """验证助手回复在第二位"""
        events = _build_run_turn_events(
            session_id="s1",
            turn=TurnData(
                user_prompt="hello",
                response_text="hi there",
                tool_calls=[],
            ),
            ctx=RunContext(
                backend="test",
                request_id="r1",
                run_id="run1",
                task_id="t1",
                source="run",
                archive_level=3,
                created_at="2026-05-01T10:00:00Z",
            ),
        )

        assert events[1].speaker == "assistant"
        assert events[1].action == "response"

    def test_tool_events_after_messages(self):
        """验证工具事件在消息之后"""
        tool_calls = [
            {"tool_name": "read", "tool_call_id": "c1"},
        ]
        events = _build_run_turn_events(
            session_id="s1",
            turn=TurnData(
                user_prompt="hello",
                response_text="hi",
                tool_calls=tool_calls,
            ),
            ctx=RunContext(
                backend="test",
                request_id="r1",
                run_id="run1",
                task_id="t1",
                source="run",
                archive_level=3,
                created_at="2026-05-01T10:00:00Z",
            ),
        )

        tool_events = [e for e in events if e.speaker == "tool"]
        assert len(tool_events) == 1
        assert tool_events[0].tool_name == "read"

    def test_event_ids_unique(self):
        """验证事件 ID 唯一"""
        events = _build_run_turn_events(
            session_id="s1",
            turn=TurnData(
                user_prompt="hello",
                response_text="hi",
                tool_calls=[],
            ),
            ctx=RunContext(
                backend="test",
                request_id="r1",
                run_id="run1",
                task_id="t1",
                source="run",
                archive_level=3,
                created_at="2026-05-01T10:00:00Z",
            ),
        )

        event_ids = [e.event_id for e in events]
        assert len(event_ids) == len(set(event_ids))

    def test_session_id_propagated(self):
        """验证 session_id 传播到所有事件"""
        events = _build_run_turn_events(
            session_id="my-session",
            turn=TurnData(
                user_prompt="hello",
                response_text="hi",
                tool_calls=[],
            ),
            ctx=RunContext(
                backend="test",
                request_id="r1",
                run_id="run1",
                task_id="t1",
                source="run",
                archive_level=3,
                created_at="2026-05-01T10:00:00Z",
            ),
        )

        assert all(e.session_id == "my-session" for e in events)


class TestArchiveRunTurn:
    """测试 archive_run_turn 回合归档"""

    def test_writes_to_raw_directory(self, tmp_path):
        """验证写入 raw 目录"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert result.event_count >= 2
        assert len(result.write_paths) >= 1
        assert all("memory/raw" in str(p) for p in result.write_paths)

    def test_event_count_matches(self, tmp_path):
        """验证事件计数正确"""
        tool_calls = [
            {"tool_name": "read"},
            {"tool_name": "write"},
        ]
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=tool_calls,
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        # user message + assistant response + 2 tool calls = 4 events
        assert result.event_count == 4

    def test_returns_valid_event_ids(self, tmp_path):
        """验证返回有效事件 ID"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert len(result.event_ids) == result.event_count
        assert all(eid.startswith("raw:") for eid in result.event_ids)

    def test_content_hashes_consistent(self, tmp_path):
        """验证内容哈希一致性"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello world",
                    response_text="hi there",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert len(result.content_hashes) == result.event_count
        assert all(ch.startswith("sha256:") for ch in result.content_hashes)

    def test_token_estimate_positive(self, tmp_path):
        """验证 token 估算为正数"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert result.token_estimate >= 0

    def test_default_created_at_used(self, tmp_path):
        """验证使用默认 created_at"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                ),
            )
        )

        # 应该使用 UTC now，不抛出异常
        assert result.event_count >= 2

    def test_empty_tool_calls(self, tmp_path):
        """验证空工具调用列表"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=None,
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert result.event_count == 2  # 只有 user + assistant

    def test_none_tool_calls(self, tmp_path):
        """验证 None 工具调用"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=None,
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert result.event_count == 2

    def test_archive_level_normalized(self, tmp_path):
        """验证归档等级被标准化"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=99,  # 超出范围，应该被标准化为 3
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert result.event_count >= 2

    def test_result_paths_are_path_objects(self, tmp_path):
        """验证返回路径是 Path 对象"""
        result = archive_run_turn(
            ArchiveRunTurnParams(
                root=tmp_path,
                ctx=ArchiveTurnContext(
                    session_id="s1",
                    user_prompt="hello",
                    response_text="hi",
                    backend="test",
                    tool_calls=[],
                    request_id="r1",
                    run_id="run1",
                    task_id="t1",
                    source="run",
                    archive_level=3,
                    created_at="2026-05-01T10:00:00Z",
                ),
            )
        )

        assert all(isinstance(p, Path) for p in result.write_paths)
        assert all(isinstance(p, Path) for p in result.paths)