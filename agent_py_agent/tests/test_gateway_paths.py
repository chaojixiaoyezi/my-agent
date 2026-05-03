"""gateway_parts/paths.py 单元测试。

测试路径生成、目录结构。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestGatewayPaths:
    """测试 GatewayPaths 数据类。"""

    def test_gateway_paths_fields(self):
        """验证 GatewayPaths 包含所有必要字段。"""
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=Path("/tmp/gateway"),
            pid=Path("/tmp/gateway/gateway.pid"),
            adapter_pid=Path("/tmp/gateway/adapter.pid"),
            state=Path("/tmp/gateway/state.json"),
            heartbeat=Path("/tmp/gateway/heartbeat.json"),
            stop_request=Path("/tmp/gateway/stop.request"),
            log=Path("/tmp/gateway/gateway.log"),
            inbox=Path("/tmp/gateway/requests/pending"),
            processing=Path("/tmp/gateway/requests/processing"),
            done=Path("/tmp/gateway/requests/done"),
            failed=Path("/tmp/gateway/requests/failed"),
            responses=Path("/tmp/gateway/responses"),
            history=Path("/tmp/gateway/history.jsonl"),
        )

        assert paths.root == Path("/tmp/gateway")
        assert paths.pid == Path("/tmp/gateway/gateway.pid")
        assert "pending" in str(paths.inbox)
        assert "processing" in str(paths.processing)


class TestAdapterPaths:
    """测试 AdapterPaths 数据类。"""

    def test_adapter_paths_fields(self):
        """验证 AdapterPaths 包含所有必要字段。"""
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        paths = AdapterPaths(
            root=Path("/tmp/adapter"),
            inbox=Path("/tmp/adapter/inbox"),
            processing=Path("/tmp/adapter/processing"),
            done=Path("/tmp/adapter/done"),
            failed=Path("/tmp/adapter/failed"),
            outbox=Path("/tmp/adapter/outbox"),
        )

        assert paths.root == Path("/tmp/adapter")
        assert "inbox" in str(paths.inbox)
        assert "outbox" in str(paths.outbox)


class TestGatewayPathsResolver:
    """测试 gateway_paths() 解析函数。"""

    def test_gateway_paths_from_agent(self):
        """验证从 agent 配置解析路径。"""
        from agent_py_agent.agent.gateway_parts.paths import gateway_paths

        mock_agent = MagicMock()
        mock_agent.root = Path("/tmp/agent")
        mock_agent.config.gateway_workspace = "data/gateway"

        paths = gateway_paths(mock_agent)

        assert "gateway" in str(paths.root)
        assert paths.pid.exists() is False  # 只是路径对象，不创建文件


class TestAdapterPathsResolver:
    """测试 adapter_paths() 解析函数。"""

    def test_adapter_paths_from_agent(self):
        """验证从 agent 配置解析路径。"""
        from agent_py_agent.agent.gateway_parts.paths import adapter_paths

        mock_agent = MagicMock()
        mock_agent.root = Path("/tmp/agent")
        mock_agent.config.adapter_workspace = "data/adapter"

        paths = adapter_paths(mock_agent)

        assert "adapter" in str(paths.root)
        assert "inbox" in str(paths.inbox)
        assert "outbox" in str(paths.outbox)


class TestGatewayChunkPath:
    """测试 gateway_chunk_path() 函数。"""

    def test_chunk_path_format(self, tmp_path: Path):
        """验证流式 chunk 文件路径格式。"""
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths, gateway_chunk_path

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/requests/pending",
            processing=tmp_path / "gateway/requests/processing",
            done=tmp_path / "gateway/requests/done",
            failed=tmp_path / "gateway/requests/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )

        result = gateway_chunk_path(paths, "req_123")
        assert "req_123" in str(result)
        assert ".chunks.jsonl" in str(result)


class TestPathsDirectoryStructure:
    """测试路径目录结构。"""

    def test_gateway_requests_subdirectories(self, tmp_path: Path):
        """验证 gateway requests 目录结构。"""
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths = GatewayPaths(
            root=tmp_path / "gateway",
            pid=tmp_path / "gateway/gateway.pid",
            adapter_pid=tmp_path / "gateway/adapter.pid",
            state=tmp_path / "gateway/state.json",
            heartbeat=tmp_path / "gateway/heartbeat.json",
            stop_request=tmp_path / "gateway/stop.request",
            log=tmp_path / "gateway/gateway.log",
            inbox=tmp_path / "gateway/requests/pending",
            processing=tmp_path / "gateway/requests/processing",
            done=tmp_path / "gateway/requests/done",
            failed=tmp_path / "gateway/requests/failed",
            responses=tmp_path / "gateway/responses",
            history=tmp_path / "gateway/history.jsonl",
        )

        # 验证 requests 子目录
        assert "requests" in str(paths.inbox)
        assert "requests" in str(paths.processing)
        assert "requests" in str(paths.done)
        assert "requests" in str(paths.failed)

    def test_adapter_directory_structure(self, tmp_path: Path):
        """验证 adapter 目录结构。"""
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        paths = AdapterPaths(
            root=tmp_path / "adapter",
            inbox=tmp_path / "adapter/inbox",
            processing=tmp_path / "adapter/processing",
            done=tmp_path / "adapter/done",
            failed=tmp_path / "adapter/failed",
            outbox=tmp_path / "adapter/outbox",
        )

        # 验证完整的目录列表
        assert "inbox" in str(paths.inbox)
        assert "processing" in str(paths.processing)
        assert "done" in str(paths.done)
        assert "failed" in str(paths.failed)
        assert "outbox" in str(paths.outbox)


class TestPathsEquality:
    """测试路径相等性。"""

    def test_same_paths_equal(self):
        """验证相同路径相等。"""
        from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

        paths1 = GatewayPaths(
            root=Path("/tmp/a"),
            pid=Path("/tmp/a/pid"),
            adapter_pid=Path("/tmp/a/apid"),
            state=Path("/tmp/a/state"),
            heartbeat=Path("/tmp/a/hb"),
            stop_request=Path("/tmp/a/stop"),
            log=Path("/tmp/a/log"),
            inbox=Path("/tmp/a/in"),
            processing=Path("/tmp/a/proc"),
            done=Path("/tmp/a/done"),
            failed=Path("/tmp/a/fail"),
            responses=Path("/tmp/a/resp"),
            history=Path("/tmp/a/hist"),
        )

        paths2 = GatewayPaths(
            root=Path("/tmp/a"),
            pid=Path("/tmp/a/pid"),
            adapter_pid=Path("/tmp/a/apid"),
            state=Path("/tmp/a/state"),
            heartbeat=Path("/tmp/a/hb"),
            stop_request=Path("/tmp/a/stop"),
            log=Path("/tmp/a/log"),
            inbox=Path("/tmp/a/in"),
            processing=Path("/tmp/a/proc"),
            done=Path("/tmp/a/done"),
            failed=Path("/tmp/a/fail"),
            responses=Path("/tmp/a/resp"),
            history=Path("/tmp/a/hist"),
        )

        assert paths1.root == paths2.root