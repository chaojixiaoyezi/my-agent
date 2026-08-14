"""status_commands CLI 命令测试。

测试 status/timeline/memory-list/memory-search/remember 命令。
"""
from __future__ import annotations

import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def _status_args(tmp_path: Path, *, json_mode: bool = False) -> MagicMock:
    args = MagicMock()
    args.config = str(tmp_path / "config.yaml")
    args.limit = 5
    args.recent = False
    args.json = json_mode
    return args


def _status_mock_agent(tmp_path: Path) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.config.agent_name = "test_agent"
    mock_agent.root = tmp_path
    mock_agent.config.gateway_stale_seconds = 300
    mock_agent.config.auto_detect_work_on_startup = False
    mock_agent.config.subagent_board_limit = 5
    mock_agent.local_store.stats.return_value = {
        "record_count": 100,
        "event_count": 50,
        "fts5_enabled": True,
        "db_path": str(tmp_path / "store.db"),
    }
    mock_agent.subagents.board.build_board.return_value = MagicMock(summary={"total": 0}, hot_list=[], recent=[])
    mock_agent.local_store.timeline.return_value = []
    return mock_agent


class TestCmdStatus:
    """测试 cmd_status 命令。"""

    def test_status_basic(self, tmp_path: Path):
        """正常显示状态。"""
        from agent_py_agent.cli.local_commands import cmd_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 5
        args.recent = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.config.agent_name = "test_agent"
        mock_agent.root = tmp_path
        mock_agent.config.gateway_stale_seconds = 300
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.local_store.stats.return_value = {"record_count": 100, "event_count": 50, "fts5_enabled": True, "db_path": str(tmp_path / "store.db")}
        mock_agent.subagents.board.build_board.return_value = MagicMock(summary={"total": 0}, hot_list=[], recent=[])
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.gateway_paths", return_value=MagicMock(root=tmp_path, state=tmp_path / "state.json", heartbeat=tmp_path / "heartbeat.json")), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]):
            result = cmd_status(args)
            assert result == 0

    def test_status_json_does_not_report_running_when_gateway_process_is_dead(self, tmp_path: Path):
        """旧 state 写着 running，但进程已不在时，状态应以进程存活为准。"""
        from agent_py_agent.cli.local_commands import cmd_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 5
        args.recent = False
        args.json = True

        mock_agent = MagicMock()
        mock_agent.config.agent_name = "test_agent"
        mock_agent.root = tmp_path
        mock_agent.config.gateway_stale_seconds = 300
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.config.subagent_board_limit = 5
        mock_agent.local_store.stats.return_value = {
            "record_count": 100,
            "event_count": 50,
            "fts5_enabled": True,
            "db_path": str(tmp_path / "store.db"),
        }
        mock_agent.subagents.board.build_board.return_value = MagicMock(
            summary={"total": 0},
            hot_list=[],
            recent=[],
        )
        mock_agent.local_store.timeline.return_value = []

        state_path = tmp_path / "state.json"
        heartbeat_path = tmp_path / "heartbeat.json"
        state_path.write_text(json.dumps({"status": "running"}), encoding="utf-8")
        heartbeat_path.write_text("{}", encoding="utf-8")

        stdout = StringIO()
        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.gateway_paths", return_value=MagicMock(root=tmp_path, state=state_path, heartbeat=heartbeat_path)), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]), \
             redirect_stdout(stdout):
            result = cmd_status(args)

        assert result == 0
        payload = json.loads(stdout.getvalue())
        assert payload["gateway"]["alive"] is False
        assert payload["gateway"]["status"] == "stopped"

    def test_status_json_reports_bad_gateway_state_files(self, tmp_path: Path):
        from agent_py_agent.cli.local_commands import cmd_status

        state_path = tmp_path / "state.json"
        heartbeat_path = tmp_path / "heartbeat.json"
        state_path.write_text("{bad state", encoding="utf-8")
        heartbeat_path.write_text("{bad heartbeat", encoding="utf-8")

        stdout = StringIO()
        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=_status_mock_agent(tmp_path)), \
             patch(
                 "agent_py_agent.cli.local_commands.gateway_paths",
                 return_value=MagicMock(root=tmp_path, state=state_path, heartbeat=heartbeat_path),
             ), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]), \
             redirect_stdout(stdout):
            result = cmd_status(_status_args(tmp_path, json_mode=True))

        assert result == 0
        payload = json.loads(stdout.getvalue())
        assert payload["gateway"]["state_load_error"]["context"] == "cli.status.gateway_state.read"
        assert payload["gateway"]["heartbeat_load_error"]["context"] == "cli.status.gateway_heartbeat.read"

    def test_status_json_truncates_subagent_goals(self, tmp_path: Path):
        from agent_py_agent.cli.local_commands import cmd_status

        long_goal = "读取很多项目并输出详细报告。" * 80
        mock_agent = _status_mock_agent(tmp_path)
        mock_agent.subagents.board.build_board.return_value = MagicMock(
            summary={"total": 1},
            hot_list=[],
            recent=[
                SimpleNamespace(
                    id="sub-long",
                    status="DONE",
                    verification_status="VERIFIED",
                    goal=long_goal,
                    artifact_registry_refs=[{"path": "/tmp/full-registry-entry.json"}],
                    artifact_refs=["/tmp/a.md", "/tmp/b.md", "/tmp/c.md", "/tmp/d.md"],
                    updated_at=1.0,
                    seconds_since_progress=1.0,
                )
            ],
        )
        stdout = StringIO()
        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch(
                 "agent_py_agent.cli.local_commands.gateway_paths",
                 return_value=MagicMock(root=tmp_path, state=tmp_path / "state.json", heartbeat=tmp_path / "heartbeat.json"),
             ), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]), \
             redirect_stdout(stdout):
            result = cmd_status(_status_args(tmp_path, json_mode=True))

        assert result == 0
        item = json.loads(stdout.getvalue())["subagents"]["recent"][0]
        assert item["goal_truncated"] is True
        assert len(item["goal"]) < len(long_goal)
        assert item["goal"].endswith("...")
        assert "artifact_registry_refs" not in item
        assert item["artifact_refs"] == ["/tmp/a.md", "/tmp/b.md", "/tmp/c.md"]

    def test_status_with_recent_flag(self, tmp_path: Path):
        """带 --recent 标志显示最近项。"""
        from agent_py_agent.cli.local_commands import cmd_status

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 5
        args.recent = True
        args.json = False

        mock_item = MagicMock()
        mock_item.id = "run_001"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "pending"
        mock_item.risk_flags = []
        mock_item.goal = "测试任务"

        mock_agent = MagicMock()
        mock_agent.config.agent_name = "test_agent"
        mock_agent.root = tmp_path
        mock_agent.config.gateway_stale_seconds = 300
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.local_store.stats.return_value = {"record_count": 100, "event_count": 50, "fts5_enabled": True, "db_path": str(tmp_path / "store.db")}
        mock_agent.subagents.board.build_board.return_value = MagicMock(summary={"total": 1}, hot_list=[], recent=[mock_item])
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.gateway_paths", return_value=MagicMock(root=tmp_path, state=tmp_path / "state.json", heartbeat=tmp_path / "heartbeat.json")), \
             patch("agent_py_agent.cli.local_commands.gateway_running", return_value=(None, False)), \
             patch("agent_py_agent.cli.local_commands.gateway_request_counts", return_value={}), \
             patch("agent_py_agent.cli.local_commands.build_status_suggestions", return_value=[]):
            result = cmd_status(args)
            assert result == 0


class TestCmdTimeline:
    """测试 cmd_timeline 命令。"""

    def test_timeline_basic(self, tmp_path: Path):
        """正常显示时间线。"""
        from agent_py_agent.cli.local_commands import cmd_timeline

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.source_type = None
        args.event_type = None
        args.details = False
        args.json = False

        mock_timeline_item = MagicMock()
        mock_timeline_item.source_type = "gateway_request"
        mock_timeline_item.source_id = "req_001"
        mock_timeline_item.event_type = "completed"
        mock_timeline_item.created_at = 1704067200.0
        mock_timeline_item.title = "请求完成"

        mock_agent = MagicMock()
        mock_agent.local_store.timeline.return_value = [mock_timeline_item]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_timeline(args)
            assert result == 0

    def test_timeline_empty(self, tmp_path: Path):
        """空时间线显示提示。"""
        from agent_py_agent.cli.local_commands import cmd_timeline

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20
        args.source_type = None
        args.event_type = None
        args.details = False
        args.json = False

        mock_agent = MagicMock()
        mock_agent.local_store.timeline.return_value = []

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_timeline(args)
            assert result == 0


class TestCmdMemoryList:
    """测试 cmd_memory_list 命令。"""

    def test_memory_list_basic(self, tmp_path: Path):
        """正常列出记忆。"""
        from agent_py_agent.cli.local_commands import cmd_memory_list

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.limit = 20

        mock_record = MagicMock()
        mock_record.__dict__ = {"id": "mem_001", "content": "测试记忆", "kind": "note"}

        mock_agent = MagicMock()
        mock_agent.memory.all.return_value = [mock_record]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_memory_list(args)
            assert result == 0


class TestCmdMemorySearch:
    """测试 cmd_memory_search 命令。"""

    def test_memory_search_basic(self, tmp_path: Path):
        """正常搜索记忆。"""
        from agent_py_agent.cli.local_commands import cmd_memory_search

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.query = "测试"
        args.limit = 5

        mock_record = MagicMock()
        mock_record.__dict__ = {"id": "mem_001", "content": "测试记忆", "kind": "note"}

        mock_agent = MagicMock()
        mock_agent.recall.return_value = [mock_record]

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_memory_search(args)
            assert result == 0


class TestCmdRemember:
    """测试 cmd_remember 命令。"""

    def test_remember_basic(self, tmp_path: Path):
        """正常记住内容。"""
        from agent_py_agent.cli.local_commands import cmd_remember

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.content = "这是一条测试记忆"
        args.kind = "note"

        # 创建一个简单的 dict-based mock 对象
        class MockRecord:
            def __init__(self):
                self.id = "mem_001"
                self.content = "这是一条测试记忆"
                self.kind = "note"

        mock_agent = MagicMock()
        mock_agent.remember.return_value = MockRecord()

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent):
            result = cmd_remember(args)
            assert result == 0


class TestCmdRun:
    """测试 cmd_run 命令。"""

    def test_run_basic(self, tmp_path: Path):
        """正常执行单轮请求。"""
        from agent_py_agent.cli.local_commands import cmd_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.prompt = "测试 prompt"
        args.inject = None
        args.prompt_file = None
        args.save = True
        args.show_prompt = False
        args.resume_context = None

        mock_result = MagicMock()
        mock_result.response = "测试响应"
        mock_result.prompt = "最终的 prompt"
        mock_result.backend = "test"
        mock_result.used_memories = 0
        mock_result.tool_rounds = 1
        mock_result.memory_route_matches = 0
        mock_result.prompt_token_estimate = 100
        mock_result.runtime_injection_token_estimate = 0
        mock_result.archive_events = 0
        mock_result.recovery_snapshot_path = None
        mock_result.recovery_snapshot_error = None
        mock_result.memory_resume_context_injected = False
        mock_result.memory_resume_context_token_estimate = 0
        # 2026-08-14 cmd_run 改走 run_with_resume: mock 需提供 ok 收口
        # (unresumable → 单轮结束返回 0) + conversation_store(会话绑定)。
        mock_result.runtime_status = "ok"
        mock_result.runtime_reason = ""
        mock_result.runtime_source = ""

        from agent_py_agent.agent.conversation.store import ConversationStore

        mock_agent = MagicMock()
        mock_agent.run.return_value = mock_result
        mock_agent.conversation_store = ConversationStore(tmp_path / "conv")
        mock_agent.home_paths = MagicMock(owner_id="local/main", owner_home_dir=str(tmp_path))

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.ThinkingSpinner"):
            result = cmd_run(args)
            assert result == 0

    def test_run_streaming_response_not_printed_twice(self, tmp_path: Path, capsys):
        """流式输出已经写到 stdout 时，cmd_run 不应再重复打印完整 response。"""
        from agent_py_agent.cli.local_commands import cmd_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.prompt = "测试 prompt"
        args.inject = None
        args.prompt_file = None
        args.save = True
        args.show_prompt = False
        args.resume_context = None

        result_payload = SimpleNamespace(
            response="流式最终响应",
            prompt="最终的 prompt",
            backend="test",
            used_memories=0,
            tool_rounds=1,
            memory_route_matches=0,
            prompt_token_estimate=100,
            runtime_injection_token_estimate=0,
            archive_events=0,
            recovery_snapshot_path=None,
            recovery_snapshot_error=None,
            memory_resume_context_injected=False,
            memory_resume_context_token_estimate=0,
            memory_compact_suggested=False,
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
        )

        from agent_py_agent.agent.conversation.store import ConversationStore

        class StreamingAgent:
            def __init__(self):
                self.conversation_store = ConversationStore(tmp_path / "conv")
                self.home_paths = SimpleNamespace(owner_id="local/main", owner_home_dir=str(tmp_path))
                self.config = SimpleNamespace(request_timeout=23)

            def run(self, *args, **kwargs):
                kwargs["on_chunk"]("流式最终响应")
                return result_payload

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=StreamingAgent()), \
             patch("agent_py_agent.cli.local_commands.ThinkingSpinner"):
            result = cmd_run(args)

        assert result == 0
        assert capsys.readouterr().out.count("流式最终响应") == 1

    def test_run_streaming_response_does_not_repeat_model_final(
        self, tmp_path: Path, capsys
    ):
        """模型最终正文已流式送达时，不应整体重印一遍。"""
        from agent_py_agent.cli.local_commands import cmd_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.prompt = "测试 prompt"
        args.inject = None
        args.prompt_file = None
        args.save = True
        args.show_prompt = False
        args.resume_context = None

        result_payload = SimpleNamespace(
            response="流式最终响应",
            model_response="流式最终响应",
            prompt="最终的 prompt",
            backend="test",
            used_memories=0,
            tool_rounds=1,
            memory_route_matches=0,
            prompt_token_estimate=100,
            runtime_injection_token_estimate=0,
            archive_events=0,
            recovery_snapshot_path=None,
            recovery_snapshot_error=None,
            memory_resume_context_injected=False,
            memory_resume_context_token_estimate=0,
            memory_compact_suggested=False,
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
        )

        from agent_py_agent.agent.conversation.store import ConversationStore

        class StreamingAgent:
            def __init__(self):
                self.conversation_store = ConversationStore(tmp_path / "conv")
                self.home_paths = SimpleNamespace(owner_id="local/main", owner_home_dir=str(tmp_path))
                self.config = SimpleNamespace(request_timeout=23)

            def run(self, *args, **kwargs):
                kwargs["on_chunk"]("流式最终响应")
                return result_payload

        with patch(
            "agent_py_agent.cli.local_commands.make_agent",
            return_value=StreamingAgent(),
        ), patch("agent_py_agent.cli.local_commands.ThinkingSpinner"):
            result = cmd_run(args)

        output = capsys.readouterr().out
        assert result == 0
        assert output.count("流式最终响应") == 1

    def test_run_hides_tentative_tool_protocol_and_prints_committed_final(
        self, tmp_path: Path, capsys
    ):
        """未提交的模型工具协议不得先于最终答复打印到用户终端。"""
        from agent_py_agent.cli.local_commands import cmd_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.prompt = "测试 prompt"
        args.inject = None
        args.prompt_file = None
        args.save = True
        args.show_prompt = False
        args.resume_context = None

        result_payload = SimpleNamespace(
            response="最终已经完成，文件在当前目录。",
            prompt="最终的 prompt",
            backend="test",
            used_memories=0,
            tool_rounds=1,
            memory_route_matches=0,
            prompt_token_estimate=100,
            runtime_injection_token_estimate=0,
            archive_events=0,
            recovery_snapshot_path=None,
            recovery_snapshot_error=None,
            memory_resume_context_injected=False,
            memory_resume_context_token_estimate=0,
            memory_compact_suggested=False,
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
        )

        from agent_py_agent.agent.conversation.store import ConversationStore

        class StreamingToolAgent:
            def __init__(self):
                self.conversation_store = ConversationStore(tmp_path / "conv")
                self.home_paths = SimpleNamespace(owner_id="local/main", owner_home_dir=str(tmp_path))
                self.config = SimpleNamespace(request_timeout=23)

            def run(self, *args, **kwargs):
                kwargs["on_chunk"]('[TOOL_CALL]\n{"tool":"run_command"}\n[/TOOL_CALL]')
                return result_payload

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=StreamingToolAgent()), \
             patch("agent_py_agent.cli.local_commands.ThinkingSpinner"):
            result = cmd_run(args)

        output = capsys.readouterr().out

        assert result == 0
        assert "[TOOL_CALL]" not in output
        assert "run_command" not in output
        assert "最终已经完成" in output

    def test_run_returns_nonzero_for_structured_blocked_status(self, tmp_path: Path):
        """系统级阻断要通过机器状态变成非零退出码，不能靠读取最终中文文本。"""
        from agent_py_agent.cli.local_commands import cmd_run

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")
        args.prompt = "测试 prompt"
        args.inject = None
        args.prompt_file = None
        args.save = True
        args.show_prompt = False
        args.resume_context = None

        result_payload = SimpleNamespace(
            response="[RUNTIME_BLOCKED] blocked",
            prompt="最终的 prompt",
            backend="test",
            used_memories=0,
            tool_rounds=1,
            memory_route_matches=0,
            prompt_token_estimate=100,
            runtime_injection_token_estimate=0,
            archive_events=0,
            recovery_snapshot_path=None,
            recovery_snapshot_error=None,
            memory_resume_context_injected=False,
            memory_resume_context_token_estimate=0,
            memory_compact_suggested=False,
            runtime_status="blocked",
            runtime_reason="RUNTIME_BLOCKED",
        )

        mock_agent = MagicMock()
        mock_agent.run.return_value = result_payload

        with patch("agent_py_agent.cli.local_commands.make_agent", return_value=mock_agent), \
             patch("agent_py_agent.cli.local_commands.ThinkingSpinner"):
            result = cmd_run(args)

        assert result == 2
