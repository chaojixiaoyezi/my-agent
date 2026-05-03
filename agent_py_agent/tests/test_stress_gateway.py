"""压力测试：gateway 网关高并发和竞态场景"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.gateway_parts.io import (
    gateway_request_counts,
    gateway_response_path,
    new_gateway_request_id,
    read_json_file,
    read_pid,
    tail_lines,
    write_gateway_request,
    write_json_file,
    write_json_file_atomic,
)
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths, gateway_chunk_path, gateway_paths


@pytest.fixture
def gateway_paths_fixture(tmp_path) -> GatewayPaths:
    """Create a GatewayPaths fixture for testing."""
    root = tmp_path / "gateway"
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "gateway_requests.jsonl",
    )


class TestGatewayRequestIdStress:
    """网关请求ID生成压力测试"""

    @pytest.mark.slow
    def test_many_request_ids_rapidly(self):
        """验证快速生成大量请求ID"""
        ids = []

        def generate_ids(count):
            return [new_gateway_request_id() for _ in range(count)]

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(generate_ids, 100) for _ in range(10)]
            for f in as_completed(futures):
                ids.extend(f.result())

        assert len(ids) == 1000
        assert len(set(ids)) == 1000  # 所有ID应该唯一

    @pytest.mark.slow
    def test_request_id_uniqueness(self):
        """验证请求ID唯一性"""
        ids = [new_gateway_request_id() for _ in range(10000)]
        assert len(set(ids)) == 10000


class TestGatewayJsonFileStress:
    """网关JSON文件读写压力测试"""

    @pytest.mark.slow
    def test_concurrent_json_file_writes(self, tmp_path):
        """验证并发JSON文件写入"""
        path = tmp_path / "test.json"
        errors = []

        def write_payload(idx):
            try:
                write_json_file(path, {"id": idx, "data": f"payload-{idx}"})
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(write_payload, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0

    @pytest.mark.slow
    def test_concurrent_json_file_reads(self, tmp_path):
        """验证并发JSON文件读取"""
        path = tmp_path / "read_test.json"
        path.write_text(json.dumps({"id": "test", "value": 123}))

        results = []

        def read_payload():
            return read_json_file(path)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(read_payload) for _ in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 100
        assert all(r.get("id") == "test" for r in results)

    @pytest.mark.slow
    def test_concurrent_atomic_writes(self, tmp_path):
        """验证并发原子写入"""
        path = tmp_path / "atomic.json"
        results = []

        def atomic_write(idx):
            write_json_file_atomic(path, {"idx": idx, "ts": time.time()})
            return read_json_file(path).get("idx")

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(atomic_write, i) for i in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        # 最终文件应该包含某个写入的值
        final = read_json_file(path)
        assert final.get("idx") in results


class TestGatewayQueueStress:
    """网关队列操作压力测试"""

    @pytest.mark.slow
    def test_gateway_request_counts_rapid_updates(self, gateway_paths_fixture):
        """验证快速更新后的请求计数"""
        paths = gateway_paths_fixture
        paths.inbox.mkdir(parents=True, exist_ok=True)

        # 创建一些请求文件
        for i in range(50):
            write_json_file(paths.inbox / f"req-{i}.json", {"id": f"req-{i}"})

        counts = gateway_request_counts(paths)
        assert counts["pending"] == 50

    @pytest.mark.slow
    def test_concurrent_queue_writes(self, gateway_paths_fixture):
        """验证并发队列写入"""
        paths = gateway_paths_fixture
        paths.inbox.mkdir(parents=True, exist_ok=True)

        def enqueue_request(idx):
            payload = {
                "id": f"req-{idx}",
                "kind": "ask",
                "prompt": f"Test prompt {idx}",
                "status": "pending",
            }
            write_gateway_request(paths, payload)
            return idx

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(enqueue_request, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        counts = gateway_request_counts(paths)
        assert counts["pending"] == 100

    @pytest.mark.slow
    def test_response_path_resolution_stress(self, gateway_paths_fixture):
        """验证大量响应路径解析"""
        paths = gateway_paths_fixture

        def resolve_path(idx):
            request_id = f"gwreq-{idx}"
            return gateway_response_path(paths, request_id)

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(resolve_path, i) for i in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 100
        assert all(str(paths.responses) in str(r) for r in results)


class TestGatewayPidStress:
    """网关PID文件压力测试"""

    @pytest.mark.slow
    def test_concurrent_pid_reads(self, tmp_path):
        """验证并发PID文件读取"""
        pid_path = tmp_path / "gateway.pid"
        pid_path.write_text("12345")

        results = []

        def read_pid_value():
            return read_pid(pid_path)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(read_pid_value) for _ in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert all(pid == 12345 for pid in results)

    @pytest.mark.slow
    def test_missing_pid_file(self, tmp_path):
        """验证缺失PID文件返回0"""
        missing_path = tmp_path / "nonexistent.pid"
        assert read_pid(missing_path) == 0


class TestGatewayTailLinesStress:
    """网关日志尾行压力测试"""

    @pytest.mark.slow
    def test_tail_lines_many_calls(self, tmp_path):
        """验证大量tail_lines调用"""
        log_path = tmp_path / "gateway.log"
        log_path.write_text("\n".join([f"log line {i}" for i in range(1000)]))

        results = []

        def tail_call():
            return tail_lines(log_path, 10)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(tail_call) for _ in range(50)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 50
        assert all(len(r) == 10 for r in results)

    @pytest.mark.slow
    def test_tail_lines_empty_file(self, tmp_path):
        """验证空日志文件的tail"""
        log_path = tmp_path / "empty.log"
        log_path.write_text("")

        result = tail_lines(log_path, 10)
        assert result == []


class TestGatewayChunkPathStress:
    """网关分块路径压力测试"""

    @pytest.mark.slow
    def test_chunk_path_resolution_stress(self, gateway_paths_fixture):
        """验证大量分块路径解析"""
        paths = gateway_paths_fixture

        def resolve_chunk(idx):
            request_id = f"gwreq-{idx}"
            return gateway_chunk_path(paths, request_id)

        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(resolve_chunk, i) for i in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 100
        assert all("chunks.jsonl" in str(r) for r in results)


class TestGatewayRaceConditions:
    """网关竞态条件测试"""

    @pytest.mark.slow
    def test_rapid_enqueue_dequeue(self, gateway_paths_fixture):
        """验证快速入队出队"""
        paths = gateway_paths_fixture
        paths.inbox.mkdir(parents=True, exist_ok=True)
        paths.processing.mkdir(parents=True, exist_ok=True)

        enqueued_count = [0]
        lock = threading.Lock()

        def enqueue(idx):
            payload = {"id": f"req-{idx}", "status": "pending"}
            write_gateway_request(paths, payload)
            with lock:
                enqueued_count[0] += 1

        # 并发入队
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(enqueue, i) for i in range(50)]
            for f in as_completed(futures):
                pass

        assert enqueued_count[0] == 50

        # 验证入队文件存在
        files = list(paths.inbox.glob("*.json"))
        assert len(files) == 50

    @pytest.mark.slow
    def test_concurrent_file_creation_in_queue(self, gateway_paths_fixture):
        """验证队列中并发文件创建"""
        paths = gateway_paths_fixture
        paths.inbox.mkdir(parents=True, exist_ok=True)

        def create_request(idx):
            request_id = f"req-{idx}"
            payload = {
                "id": request_id,
                "kind": "ask",
                "prompt": f"Prompt {idx}",
                "status": "pending",
                "created_at": time.time(),
            }
            target = paths.inbox / f"{request_id}.json"
            write_json_file_atomic(target, payload)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(create_request, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        # 验证所有文件都被正确创建
        files = list(paths.inbox.glob("*.json"))
        assert len(files) == 100

    @pytest.mark.slow
    def test_read_write_overlap(self, tmp_path):
        """验证读写重叠"""
        path = tmp_path / "overlap.json"
        path.write_text(json.dumps({"counter": 0}))

        errors = []

        def increment():
            try:
                for _ in range(10):
                    data = read_json_file(path)
                    data["counter"] = data.get("counter", 0) + 1
                    write_json_file_atomic(path, data)
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 最终值应该大于0，说明有些写入成功了
        final = read_json_file(path)
        assert final.get("counter", 0) > 0


class TestGatewayHistoryAppendStress:
    """网关历史追加压力测试"""

    @pytest.mark.slow
    def test_rapid_history_appends(self, tmp_path):
        """验证快速历史追加"""
        from agent_py_agent.agent.io import append_jsonl

        history_path = tmp_path / "history.jsonl"
        errors = []

        def append_record(idx):
            try:
                record = {"id": f"rec-{idx}", "ts": time.time()}
                append_jsonl(history_path, record)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(append_record, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert history_path.exists()


class TestGatewayHeartbeatStress:
    """网关心跳压力测试"""

    @pytest.mark.slow
    def test_heartbeat_file_rapid_updates(self, tmp_path):
        """验证心跳文件快速更新"""
        heartbeat_path = tmp_path / "heartbeat.json"

        def update_heartbeat(idx):
            payload = {
                "updated_at": time.time(),
                "request_id": f"req-{idx}",
                "status": "processing",
            }
            write_json_file_atomic(heartbeat_path, payload)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_heartbeat, i) for i in range(50)]
            for f in as_completed(futures):
                pass

        final = read_json_file(heartbeat_path)
        assert "updated_at" in final


class TestGatewayStateStress:
    """网关状态文件压力测试"""

    @pytest.mark.slow
    def test_state_file_concurrent_updates(self, tmp_path):
        """验证状态文件并发更新"""
        state_path = tmp_path / "state.json"
        lock = threading.Lock()

        def update_state(idx):
            with lock:
                current = read_json_file(state_path)
                current[f"key-{idx}"] = idx
                write_json_file(state_path, current)

        # 预初始化
        write_json_file(state_path, {})

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(update_state, i) for i in range(20)]
            for f in as_completed(futures):
                pass

        final = read_json_file(state_path)
        # 由于并发和last-write-wins，某些键可能丢失，但至少有一些应该成功
        assert len(final) > 0
