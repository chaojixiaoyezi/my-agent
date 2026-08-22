"""启动时恢复检测功能测试。

测试 detect_active_work() 函数、status 命令显示和配置项。
"""
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_py_agent.agent.startup_recovery import (
    ActiveWorkSummary,
    detect_active_work,
    format_active_work_summary,
    has_active_work,
)


@dataclass
class _BoardTimestamps:
    created_at: float
    updated_at: float


@dataclass
class _BoardItem:
    id: str
    goal: str
    status: str
    verification_status: str
    timestamps: _BoardTimestamps
    risk_flags: list = field(default_factory=list)

    @property
    def created_at(self):
        return self.timestamps.created_at

    @property
    def updated_at(self):
        return self.timestamps.updated_at


def _board_item(row: tuple) -> _BoardItem:
    run_id, goal, status, verification_status, created_at, updated_at, risk_flags = row
    timestamps = _BoardTimestamps(created_at, updated_at)
    return _BoardItem(run_id, goal, status, verification_status, timestamps, risk_flags)


@pytest.fixture
def mock_agent():
    """创建一个模拟的 SimpleAgent。"""
    agent = Mock()
    agent.config = Mock()
    agent.config.gateway_stale_seconds = 120
    agent.root = Path("/tmp/test_agent")
    agent.subagents = Mock()
    agent.local_store = Mock()
    return agent


@pytest.fixture
def mock_paths(tmp_path):
    """创建模拟的 gateway 路径结构。"""
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
    return GatewayPaths(
        root=tmp_path,
        pid=tmp_path / "gateway.pid",
        adapter_pid=tmp_path / "adapter.pid",
        state=tmp_path / "gateway_state.json",
        heartbeat=tmp_path / "gateway_heartbeat.json",
        stop_request=tmp_path / "gateway_stop.request",
        log=tmp_path / "gateway.log",
        inbox=tmp_path / "pending",
        processing=tmp_path / "processing",
        done=tmp_path / "done",
        failed=tmp_path / "failed",
        responses=tmp_path / "responses",
        history=tmp_path / "gateway_requests.jsonl",
    )


@pytest.fixture
def sample_board():
    board = Mock()
    now = time.time()

    # 模拟活跃任务
    board.hot_list = [
        _board_item(("sub-001", "测试任务1", "RUNNING", "UNVERIFIED", now - 20.0, now - 10.0, ["timeout"])),
        _board_item(("sub-002", "测试任务2", "BLOCKED", "FAILED", now - 18.0, now - 8.0, [])),
    ]

    # 模拟最近任务
    board.recent = [
        _board_item(("sub-003", "测试任务3", "DONE", "VERIFIED", now - 30.0, now - 7.0, [])),
        _board_item(("sub-004", "测试任务4", "FAILED", "FAILED", now - 25.0, now - 5.0, [])),
        _board_item(("sub-005", "测试任务5", "PLANNING", "UNVERIFIED", now - 20.0, now - 3.0, ["new"])),
    ]

    board.summary = {
        "total": 5,
        "by_status": {
            "RUNNING": 1,
            "BLOCKED": 1,
            "DONE": 1,
            "FAILED": 2,
        }
    }

    return board


class TestDetectActiveWork:
    """测试 detect_active_work() 函数。"""

    def test_gateway_not_exists(self, mock_agent, mock_paths):
        """测试 gateway 不存在时的返回值。"""
        mock_agent.subagents.board.build_board.return_value = Mock(
            hot_list=[], recent=[], summary={}
        )

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(0, False)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 0, "done": 0, "failed": 0}):
            summary = detect_active_work(mock_agent)

        assert summary.gateway_alive is False
        assert summary.gateway_pid == 0
        assert summary.active_task_count == 0
        assert summary.stale_request_count == 0
        assert len(summary.recent_tasks) == 0
        assert len(summary.processing_requests) == 0

    def test_gateway_alive_no_active_tasks(self, mock_agent, mock_paths):
        """测试 gateway 存活但没有活跃任务。"""
        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')

        mock_agent.subagents.board.build_board.return_value = Mock(
            hot_list=[], recent=[], summary={}
        )

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 0, "done": 5, "failed": 0}):
            summary = detect_active_work(mock_agent)

        assert summary.gateway_alive is True
        assert summary.gateway_pid == 12345
        assert summary.active_task_count == 0
        assert summary.stale_request_count == 0

    def test_has_active_tasks(self, mock_agent, mock_paths, sample_board):
        """测试有活跃任务时的返回值。"""
        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')

        mock_agent.subagents.board.build_board.return_value = sample_board

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 1, "processing": 0, "done": 2, "failed": 0}):
            summary = detect_active_work(mock_agent)

        assert summary.gateway_alive is True
        assert summary.active_task_count == 2
        assert len(summary.recent_tasks) == 3
        assert summary.recent_tasks[0]["id"] == "sub-003"
        assert summary.recent_tasks[2]["id"] == "sub-005"

    def test_stale_processing_requests(self, mock_agent, mock_paths):
        """测试有遗留 processing 请求时的返回值。"""
        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')
        # 创建 processing 目录和文件
        mock_paths.processing.mkdir(parents=True)
        (mock_paths.processing / "req-001.json").write_text("{}")
        (mock_paths.processing / "req-002.json").write_text("{}")
        (mock_paths.processing / "req-003.json").write_text("{}")

        mock_agent.subagents.board.build_board.return_value = Mock(
            hot_list=[], recent=[], summary={}
        )

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 3, "done": 0, "failed": 0}):
            summary = detect_active_work(mock_agent)

        assert summary.gateway_alive is True
        assert summary.stale_request_count == 3
        assert len(summary.processing_requests) == 3
        assert set(summary.processing_requests) == {"req-001", "req-002", "req-003"}

    def test_gateway_stale_requests_combined(self, mock_agent, mock_paths, sample_board):
        """测试 gateway 状态、活跃任务和遗留请求的组合。"""
        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')
        # 创建 processing 目录和文件
        mock_paths.processing.mkdir(parents=True)
        (mock_paths.processing / "req-001.json").write_text("{}")

        mock_agent.subagents.board.build_board.return_value = sample_board

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 1, "done": 0, "failed": 0}):
            summary = detect_active_work(mock_agent)

        assert summary.gateway_alive is True
        assert summary.active_task_count == 2
        assert summary.stale_request_count == 1
        assert len(summary.processing_requests) == 1


class TestFormatActiveWorkSummary:
    """测试 format_active_work_summary() 函数。"""

    def test_format_empty_summary(self):
        """测试空摘要格式化。"""
        summary = ActiveWorkSummary(
            gateway_alive=False,
            active_task_count=0,
            stale_request_count=0,
            recent_tasks=[],
            processing_requests=[],
        )

        formatted = format_active_work_summary(summary)
        assert "✗ Gateway 未运行" in formatted
        assert "✓ 没有近期未收口任务" in formatted

    def test_format_with_active_tasks(self):
        """测试有活跃任务的摘要格式化。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            gateway_pid=12345,
            active_task_count=2,
            stale_request_count=0,
            recent_tasks=[
                {
                    "id": "sub-001",
                    "goal": "测试任务1",
                    "status": "RUNNING",
                    "verification_status": "UNVERIFIED",
                    "created_at": 1234567890.0,
                    "updated_at": 1234567891.0,
                }
            ],
            processing_requests=[],
        )

        formatted = format_active_work_summary(summary)
        assert "✓ Gateway 运行中" in formatted
        assert "✓ 发现 2 个近期未收口任务" in formatted
        assert "sub-001" in formatted
        assert "RUNNING" in formatted

    def test_format_with_stale_requests(self):
        """测试有遗留请求的摘要格式化。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            gateway_pid=12345,
            active_task_count=0,
            stale_request_count=2,
            recent_tasks=[],
            processing_requests=["req-001", "req-002"],
        )

        formatted = format_active_work_summary(summary)
        assert "⚠ 发现 2 个遗留的 processing 请求" in formatted
        assert "req-001" in formatted

    def test_format_truncated_goal(self):
        """测试长目标字符串的截断。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            gateway_pid=12345,
            active_task_count=0,
            stale_request_count=0,
            recent_tasks=[
                {
                    "id": "sub-001",
                    "goal": "这是一个非常长的任务描述，应该被截断显示" + "x" * 100,
                    "status": "RUNNING",
                    "verification_status": "UNVERIFIED",
                    "created_at": 1234567890.0,
                    "updated_at": 1234567891.0,
                }
            ],
            processing_requests=[],
        )

        formatted = format_active_work_summary(summary)
        assert "..." in formatted
        # 目标应该被截断，包含原始字符串的一部分和省略号
        assert "这是一个非常长的任务描述，应该被截断显示" in formatted
        assert "..." in formatted
        # 验证输出中目标被截断（包含原始目标的一部分但不是全部）
        goal_in_output = [line for line in formatted.split('\n') if 'sub-001' in line][0]
        assert len(goal_in_output) < len(summary.recent_tasks[0]["goal"])


class TestHasActiveWork:
    """测试 has_active_work() 函数。"""

    def test_no_active_work(self):
        """测试没有进行中任务。"""
        summary = ActiveWorkSummary(
            gateway_alive=False,
            active_task_count=0,
            stale_request_count=0,
            recent_tasks=[],
            processing_requests=[],
        )

        assert has_active_work(summary) is False

    def test_has_active_tasks(self):
        """测试有活跃任务。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            active_task_count=1,
            stale_request_count=0,
            recent_tasks=[],
            processing_requests=[],
        )

        assert has_active_work(summary) is True

    def test_has_stale_requests(self):
        """测试有遗留请求。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            active_task_count=0,
            stale_request_count=1,
            recent_tasks=[],
            processing_requests=[],
        )

        assert has_active_work(summary) is True

    def test_has_both(self):
        """测试同时有活跃任务和遗留请求。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            active_task_count=2,
            stale_request_count=3,
            recent_tasks=[],
            processing_requests=[],
        )

        assert has_active_work(summary) is True


class TestActiveWorkSummaryModel:
    """测试 ActiveWorkSummary 数据类。"""

    def test_default_values(self):
        """测试默认值。"""
        summary = ActiveWorkSummary()

        assert summary.gateway_alive is False
        assert summary.gateway_pid == 0
        assert summary.active_task_count == 0
        assert summary.stale_request_count == 0
        assert summary.recent_tasks == []
        assert summary.processing_requests == []

    def test_field_assignment(self):
        """测试字段赋值。"""
        summary = ActiveWorkSummary(
            gateway_alive=True,
            gateway_pid=9999,
            active_task_count=5,
            stale_request_count=2,
            recent_tasks=[{"id": "test"}],
            processing_requests=["req-001"],
        )

        assert summary.gateway_alive is True
        assert summary.gateway_pid == 9999
        assert summary.active_task_count == 5
        assert summary.stale_request_count == 2
        assert summary.recent_tasks == [{"id": "test"}]
        assert summary.processing_requests == ["req-001"]


class TestStatusCommand:
    """测试 status 命令显示。"""

    def test_status_shows_active_work_block(self):
        """测试 status 命令显示进行中任务区块。"""
        # 这个测试需要完整的命令行集成测试
        # 这里只测试数据模型部分
        from agent_py_agent.agent.startup_recovery import ActiveWorkSummary
        from agent_py_agent.cli.local_commands import cmd_status

        # 验证导入成功
        assert cmd_status is not None
        assert ActiveWorkSummary is not None


class TestIntegrationScenarios:
    """集成测试场景。"""

    def test_full_detection_cycle(self, mock_agent, mock_paths, sample_board):
        """测试完整检测周期。"""
        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')
        # 创建 processing 目录和文件
        mock_paths.processing.mkdir(parents=True)
        (mock_paths.processing / "req-001.json").write_text("{}")

        mock_agent.subagents.board.build_board.return_value = sample_board

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 1, "done": 0, "failed": 0}):

            # 1. 检测
            summary = detect_active_work(mock_agent)

            # 2. 验证结果
            assert summary.gateway_alive is True
            assert summary.active_task_count == 2
            assert summary.stale_request_count == 1

            # 3. 格式化
            formatted = format_active_work_summary(summary)
            assert formatted
            assert "✓ Gateway 运行中" in formatted
            assert "发现 2 个近期未收口任务" in formatted
            assert "发现 1 个遗留的 processing 请求" in formatted

            # 4. 判断
            assert has_active_work(summary) is True

    def test_retired_startup_toggle_cannot_hide_explicit_status(
        self,
        mock_agent,
        mock_paths,
        sample_board,
    ):
        """旧配置残留不能关闭用户明确执行的 status。"""
        mock_agent.config.auto_detect_work_on_startup = False
        mock_agent.subagents.board.build_board.return_value = sample_board

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(0, False)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 0, "done": 0, "failed": 0}):

            summary = detect_active_work(mock_agent)

            assert summary.active_task_count == 2

    def test_malformed_board_handling(self, mock_agent, mock_paths):
        """测试异常 board 处理。"""
        # 模拟 build_board 返回无效对象
        mock_agent.subagents.board.build_board.return_value = None

        # 创建 pid 和 state 文件
        mock_paths.pid.write_text("12345")
        mock_paths.state.write_text('{"status": "running"}')

        with patch('agent_py_agent.agent.gateway_parts.gateway_paths', return_value=mock_paths), \
             patch('agent_py_agent.agent.gateway_parts.gateway_running', return_value=(12345, True)), \
             patch('agent_py_agent.agent.gateway_parts.gateway_request_counts', return_value={"pending": 0, "processing": 0, "done": 0, "failed": 0}):

            # 应该优雅处理 None
            summary = detect_active_work(mock_agent)

            assert summary is not None
            assert summary.gateway_alive is True
            assert summary.active_task_count == 0
            assert summary.recent_tasks == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
