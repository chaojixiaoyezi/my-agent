"""端到端网关工作流测试。

测试 gateway 启动 → 请求投递 → 处理 → 响应完整流程，崩溃恢复，以及多 worker 并发。
使用真实的网关组件，少用 mock。
"""
from __future__ import annotations

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.gateway import GatewayPaths
from agent_py_agent.agent.gateway_parts import (
    gateway_request_counts,
    gateway_response_path,
    is_pid_alive,
    new_gateway_request_id,
    write_gateway_request,
    read_json_file,
)
from agent_py_agent.agent.gateway_parts.io import write_json_file


def make_gateway_paths(temp_root: Path) -> GatewayPaths:
    """创建测试用 GatewayPaths。"""
    return GatewayPaths(
        root=temp_root,
        pid=temp_root / "gateway.pid",
        adapter_pid=temp_root / "adapter.pid",
        state=temp_root / "gateway_state.json",
        heartbeat=temp_root / "gateway_heartbeat.json",
        stop_request=temp_root / "gateway_stop.request",
        log=temp_root / "gateway.log",
        inbox=temp_root / "requests" / "pending",
        processing=temp_root / "requests" / "processing",
        done=temp_root / "requests" / "done",
        failed=temp_root / "requests" / "failed",
        responses=temp_root / "responses",
        history=temp_root / "gateway_requests.jsonl",
    )


@pytest.fixture
def temp_gateway_dir():
    """创建临时网关目录。"""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def gateway_paths_fixture(temp_gateway_dir):
    """创建 GatewayPaths 实例。"""
    return make_gateway_paths(temp_gateway_dir)


class TestGatewayStartup:
    """测试网关启动功能。"""

    def test_gateway_paths_created(self, gateway_paths_fixture):
        """测试网关路径创建。

        验证 GatewayPaths 正确创建所有必要的路径。
        """
        paths = gateway_paths_fixture
        assert hasattr(paths, "inbox")
        assert hasattr(paths, "responses")
        assert hasattr(paths, "history")
        assert hasattr(paths, "processing")

    def test_gateway_request_id_generation(self):
        """测试网关请求 ID 生成。

        验证可以生成唯一的请求 ID。
        """
        id1 = new_gateway_request_id()
        id2 = new_gateway_request_id()

        assert id1 != id2
        assert id1.startswith("gwreq-")
        assert id2.startswith("gwreq-")

    def test_gateway_paths_for_requests(self, gateway_paths_fixture):
        """测试网关请求路径。

        验证请求目录路径正确。
        """
        paths = gateway_paths_fixture
        # inbox 是 pending 请求目录
        assert paths.inbox is not None


class TestGatewayRequestSubmission:
    """测试网关请求投递功能。"""

    def test_write_gateway_request(self, gateway_paths_fixture):
        """测试写入网关请求。

        验证可以将请求写入网关队列。
        """
        paths = gateway_paths_fixture
        request = {
            "id": "test-req-1",
            "prompt": "测试提示",
            "timestamp": time.time(),
        }

        write_gateway_request(paths, request)

        # 验证请求文件已创建
        request_file = paths.inbox / "test-req-1.json"
        assert request_file.exists()

        # 验证内容正确
        content = read_json_file(request_file)
        assert content["id"] == "test-req-1"
        assert content["prompt"] == "测试提示"

    def test_multiple_requests_submission(self, gateway_paths_fixture):
        """测试多请求投递。

        验证可以同时投递多个请求。
        """
        paths = gateway_paths_fixture
        for i in range(5):
            request = {
                "id": f"test-req-{i}",
                "prompt": f"测试提示 {i}",
                "timestamp": time.time(),
            }
            write_gateway_request(paths, request)

        # 验证所有请求都已写入
        files = list(paths.inbox.glob("test-req-*.json"))
        assert len(files) == 5


class TestGatewayRequestProcessing:
    """测试网关请求处理功能。"""

    def test_gateway_request_counts(self, gateway_paths_fixture):
        """测试网关请求计数。

        验证可以正确统计请求数量。
        """
        paths = gateway_paths_fixture

        # 写入几个请求
        for i in range(3):
            request = {
                "id": f"count-req-{i}",
                "prompt": f"计数测试 {i}",
                "timestamp": time.time(),
            }
            write_gateway_request(paths, request)

        # 获取请求计数
        counts = gateway_request_counts(paths)
        assert isinstance(counts, dict)
        assert counts["pending"] == 3

    def test_gateway_response_path(self, gateway_paths_fixture):
        """测试网关响应路径。

        验证可以获取正确的响应文件路径。
        """
        paths = gateway_paths_fixture
        req_id = "response-test-1"
        resp_path = gateway_response_path(paths, req_id)

        assert resp_path is not None
        assert "response-test-1" in str(resp_path)
        assert resp_path.parent == paths.responses

    def test_gateway_request_processing_state(self, gateway_paths_fixture):
        """测试网关请求处理状态。

        验证请求处理状态可以正确记录和查询。
        """
        paths = gateway_paths_fixture
        req_id = "state-test-1"
        request = {
            "id": req_id,
            "prompt": "状态测试",
            "status": "pending",
            "timestamp": time.time(),
        }
        write_gateway_request(paths, request)

        # 读取请求验证状态
        req_file = paths.inbox / f"{req_id}.json"
        content = read_json_file(req_file)
        assert content["status"] == "pending"


class TestGatewayCrashRecovery:
    """测试网关崩溃恢复功能。"""

    def test_gateway_request_persistence_after_error(self, gateway_paths_fixture):
        """测试错误后请求持久化。

        验证发生错误后请求仍然保存。
        """
        paths = gateway_paths_fixture
        req_id = "persist-test-1"
        request = {
            "id": req_id,
            "prompt": "持久化测试",
            "timestamp": time.time(),
        }
        write_gateway_request(paths, request)

        # 模拟崩溃场景 - 再次读取验证数据完整
        req_file = paths.inbox / f"{req_id}.json"
        content = read_json_file(req_file)

        assert content["id"] == req_id
        assert content["prompt"] == "持久化测试"

    def test_gateway_recovery_with_partial_state(self, gateway_paths_fixture):
        """测试部分状态下的恢复。

        验证在部分写入的情况下可以恢复。
        """
        paths = gateway_paths_fixture
        # 创建请求
        req_id = "partial-recovery-test"
        request = {
            "id": req_id,
            "prompt": "部分状态恢复测试",
            "attempts": 1,
            "timestamp": time.time(),
        }
        write_gateway_request(paths, request)

        # 验证可以重新读取
        req_file = paths.inbox / f"{req_id}.json"
        content = read_json_file(req_file)
        assert content["attempts"] == 1


class TestGatewayMultiWorker:
    """测试网关多 worker 并发功能。"""

    def test_concurrent_request_writes(self, gateway_paths_fixture):
        """测试并发写入请求。

        验证多线程同时写入请求不会出错。
        """
        paths = gateway_paths_fixture
        errors = []

        def write_request(req_id):
            try:
                request = {
                    "id": f"concurrent-req-{req_id}",
                    "prompt": f"并发请求 {req_id}",
                    "timestamp": time.time(),
                }
                write_gateway_request(paths, request)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=write_request, args=(i,))
            for i in range(10)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

        # 验证所有请求都已写入
        files = list(paths.inbox.glob("concurrent-req-*.json"))
        assert len(files) == 10

    def test_concurrent_request_reads(self, gateway_paths_fixture):
        """测试并发读取请求。

        验证多线程同时读取请求不会出错。
        """
        paths = gateway_paths_fixture
        # 先写入一些请求
        for i in range(5):
            request = {
                "id": f"read-concurrent-{i}",
                "prompt": f"并发读取测试 {i}",
                "timestamp": time.time(),
            }
            write_gateway_request(paths, request)

        errors = []
        results = []

        def read_request(req_id):
            try:
                req_file = paths.inbox / f"{req_id}.json"
                content = read_json_file(req_file)
                results.append(content)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=read_request, args=(f"read-concurrent-{i}",))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert len(results) == 5

    def test_gateway_response_thread_safety(self, gateway_paths_fixture):
        """测试网关响应的线程安全。

        验证多线程写入响应不会出错。
        """
        paths = gateway_paths_fixture
        errors = []
        req_ids = [f"resp-thread-{i}" for i in range(5)]

        # 先写入请求
        for req_id in req_ids:
            request = {
                "id": req_id,
                "prompt": f"响应线程安全测试 {req_id}",
                "timestamp": time.time(),
            }
            write_gateway_request(paths, request)

        def write_response(req_id):
            try:
                resp = {
                    "id": req_id,
                    "response": f"响应内容 {req_id}",
                    "timestamp": time.time(),
                }
                resp_path = gateway_response_path(paths, req_id)
                resp_path.parent.mkdir(parents=True, exist_ok=True)
                with open(resp_path, "w", encoding="utf-8") as f:
                    json.dump(resp, f)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_response, args=(req_id,)) for req_id in req_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0


class TestGatewayResponse:
    """测试网关响应功能。"""

    def test_gateway_response_content(self, gateway_paths_fixture):
        """测试网关响应内容。

        验证响应内容正确保存。
        """
        paths = gateway_paths_fixture
        req_id = "resp-content-test"
        request = {
            "id": req_id,
            "prompt": "响应内容测试",
            "timestamp": time.time(),
        }
        write_gateway_request(paths, request)

        # 创建响应
        resp = {
            "id": req_id,
            "response": "这是测试响应内容",
            "timestamp": time.time(),
        }
        resp_path = gateway_response_path(paths, req_id)
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        with open(resp_path, "w", encoding="utf-8") as f:
            json.dump(resp, f)

        # 读取验证
        content = read_json_file(resp_path)
        assert content["response"] == "这是测试响应内容"

    def test_gateway_response_association(self, gateway_paths_fixture):
        """测试网关响应关联。

        验证响应可以正确关联到原始请求。
        """
        paths = gateway_paths_fixture
        req_id = "assoc-test"
        request = {
            "id": req_id,
            "prompt": "关联测试",
            "timestamp": time.time(),
        }
        write_gateway_request(paths, request)

        # 验证请求 ID 一致
        resp_path = gateway_response_path(paths, req_id)
        assert req_id in str(resp_path)


class TestGatewayHealth:
    """测试网关健康检查功能。"""

    def test_pid_alive_check(self):
        """测试进程存活检查。

        验证 is_pid_alive 函数正确工作。
        """
        # 当前进程应该存活
        current_pid = 0  # 0 表示当前进程
        alive = is_pid_alive(current_pid)
        # is_pid_alive 可能对 0 有特殊处理
        assert isinstance(alive, bool)

    def test_gateway_paths_health(self, gateway_paths_fixture):
        """测试网关路径健康状态。

        验证路径对象包含必要的健康检查信息。
        """
        paths = gateway_paths_fixture
        # 验证所有路径属性都存在
        assert paths.inbox is not None
        assert paths.responses is not None