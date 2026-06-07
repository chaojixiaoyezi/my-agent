"""gateway_parts/io.py 单元测试。

测试文件读写、队列操作、原子性保证。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestWriteJsonFile:
    """测试 write_json_file() 函数。"""

    def test_creates_parent_directory(self, tmp_path: Path):
        """验证自动创建父目录。"""
        from agent_py_agent.agent.gateway_parts.io import write_json_file

        target = tmp_path / "subdir" / "file.json"
        assert not target.parent.exists()

        write_json_file(target, {"key": "value"})

        assert target.parent.exists()
        assert target.exists()

    def test_json_formatting(self, tmp_path: Path):
        """验证 JSON 格式化。"""
        from agent_py_agent.agent.gateway_parts.io import write_json_file

        target = tmp_path / "test.json"
        write_json_file(target, {"b": 2, "a": 1})

        content = target.read_text(encoding="utf-8")
        assert "a" in content
        assert "b" in content


class TestWriteJsonFileAtomic:
    """测试 write_json_file_atomic() 函数。"""

    def test_atomic_write(self, tmp_path: Path):
        """验证原子写入（先写临时文件再 rename）。"""
        from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

        target = tmp_path / "test.json"
        write_json_file_atomic(target, {"atomic": True})

        assert target.exists()
        content = target.read_text(encoding="utf-8")
        assert "atomic" in content

    def test_creates_parent_directory(self, tmp_path: Path):
        """验证自动创建父目录。"""
        from agent_py_agent.agent.gateway_parts.io import write_json_file_atomic

        target = tmp_path / "subdir" / "file.json"
        write_json_file_atomic(target, {"key": "value"})

        assert target.exists()


class TestReadJsonFile:
    """测试 read_json_file() 函数。"""

    def test_read_valid_json(self, tmp_path: Path):
        """验证读取有效 JSON。"""
        from agent_py_agent.agent.gateway_parts.io import read_json_file

        target = tmp_path / "test.json"
        target.write_text('{"key": "value"}', encoding="utf-8")

        result = read_json_file(target)
        assert result == {"key": "value"}

    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        """文件不存在返回空字典。"""
        from agent_py_agent.agent.gateway_parts.io import read_json_file

        result = read_json_file(tmp_path / "nonexistent.json")
        assert result == {}

    def test_invalid_json_returns_empty_dict(self, tmp_path: Path):
        """无效 JSON 返回空字典。"""
        from agent_py_agent.agent.gateway_parts.io import read_json_file

        target = tmp_path / "invalid.json"
        target.write_text("not json", encoding="utf-8")

        result = read_json_file(target)
        assert result == {}

    def test_non_dict_json_returns_empty_dict(self, tmp_path: Path):
        """非字典 JSON 返回空字典。"""
        from agent_py_agent.agent.gateway_parts.io import read_json_file

        target = tmp_path / "array.json"
        target.write_text('[1, 2, 3]', encoding="utf-8")

        result = read_json_file(target)
        assert result == {}


class TestReadPid:
    """测试 read_pid() 函数。"""

    def test_read_valid_pid(self, tmp_path: Path):
        """验证读取有效 PID。"""
        from agent_py_agent.agent.gateway_parts.io import read_pid

        target = tmp_path / "pid"
        target.write_text("12345", encoding="utf-8")

        result = read_pid(target)
        assert result == 12345

    def test_missing_file_returns_zero(self, tmp_path: Path):
        """文件不存在返回 0。"""
        from agent_py_agent.agent.gateway_parts.io import read_pid

        result = read_pid(tmp_path / "nonexistent")
        assert result == 0

    def test_invalid_pid_returns_zero(self, tmp_path: Path):
        """无效 PID 内容返回 0。"""
        from agent_py_agent.agent.gateway_parts.io import read_pid

        target = tmp_path / "invalid"
        target.write_text("not a number", encoding="utf-8")

        result = read_pid(target)
        assert result == 0


class TestTailLines:
    """测试 tail_lines() 函数。"""

    def test_tail_lines(self, tmp_path: Path):
        """验证返回最后 N 行。"""
        from agent_py_agent.agent.gateway_parts.io import tail_lines

        target = tmp_path / "log.txt"
        target.write_text("line1\nline2\nline3\nline4\nline5", encoding="utf-8")

        result = tail_lines(target, 3)
        assert len(result) == 3
        assert result == ["line3", "line4", "line5"]

    def test_missing_file_returns_empty(self, tmp_path: Path):
        """文件不存在返回空列表。"""
        from agent_py_agent.agent.gateway_parts.io import tail_lines

        result = tail_lines(tmp_path / "nonexistent", 10)
        assert result == []

    def test_zero_line_count(self, tmp_path: Path):
        """line_count 为 0 返回全部。"""
        from agent_py_agent.agent.gateway_parts.io import tail_lines

        target = tmp_path / "log.txt"
        target.write_text("line1\nline2", encoding="utf-8")

        result = tail_lines(target, 0)
        assert len(result) == 2


class TestNewGatewayRequestId:
    """测试 new_gateway_request_id() 函数。"""

    def test_id_format(self):
        """验证 ID 格式包含时间戳和随机字符。"""
        from agent_py_agent.agent.gateway_parts.io import new_gateway_request_id

        request_id = new_gateway_request_id()
        assert request_id.startswith("gwreq-")
        assert "-" in request_id

    def test_id_uniqueness(self):
        """验证 ID 唯一性。"""
        from agent_py_agent.agent.gateway_parts.io import new_gateway_request_id

        ids = [new_gateway_request_id() for _ in range(100)]
        assert len(set(ids)) == 100


class TestGatewayRequestCounts:
    """测试 gateway_request_counts() 函数。"""

    def test_counts_empty_directories(self, tmp_path: Path):
        """验证空目录计数。"""
        from agent_py_agent.agent.gateway_parts.io import gateway_request_counts
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/inbox",
            processing=tmp_path / "gateway/processing",
            done=tmp_path / "gateway/done",
            failed=tmp_path / "gateway/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )

        result = gateway_request_counts(paths)
        assert result["pending"] == 0
        assert result["processing"] == 0

    def test_counts_json_files(self, tmp_path: Path):
        """验证统计 JSON 文件数量。"""
        from agent_py_agent.agent.gateway_parts.io import gateway_request_counts
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/inbox",
            processing=tmp_path / "gateway/processing",
            done=tmp_path / "gateway/done",
            failed=tmp_path / "gateway/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )

        # 创建测试文件
        (tmp_path / "gateway/inbox").mkdir(parents=True, exist_ok=True)
        for i in range(3):
            (tmp_path / "gateway/inbox" / f"req_{i}.json").write_text("{}", encoding="utf-8")

        result = gateway_request_counts(paths)
        assert result["pending"] == 3

    def test_counts_can_skip_archive_directories_for_hot_paths(self, tmp_path: Path):
        """热路径可只统计活跃队列，避免心跳反复扫描历史响应目录。"""
        from agent_py_agent.agent.gateway_parts.io import gateway_request_counts
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/inbox",
            processing=tmp_path / "gateway/processing",
            done=tmp_path / "gateway/done",
            failed=tmp_path / "gateway/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )
        for folder, prefix in ((paths.inbox, "pending"), (paths.processing, "processing"), (paths.done, "done"), (paths.responses, "response")):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{prefix}.json").write_text("{}", encoding="utf-8")

        hot_counts = gateway_request_counts(paths, include_archives=False)
        full_counts = gateway_request_counts(paths)

        assert hot_counts == {"pending": 1, "processing": 1}
        assert full_counts["done"] == 1
        assert full_counts["responses"] == 1


class TestGatewayResponsePath:
    """测试 gateway_response_path() 函数。"""

    def test_response_path_format(self, tmp_path: Path):
        """验证响应路径格式。"""
        from agent_py_agent.agent.gateway_parts.io import gateway_response_path
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/inbox",
            processing=tmp_path / "gateway/processing",
            done=tmp_path / "gateway/done",
            failed=tmp_path / "gateway/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )

        result = gateway_response_path(paths, "req_123")
        assert result == tmp_path / "gateway/responses/req_123.json"
