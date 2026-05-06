"""压力测试：memory_archive 内存归档高并发和竞态场景"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.memory_archive.models import CompressionSnapshot, utc_now_iso
from agent_py_agent.agent.memory_archive.snapshots import (
    _compression_hooks,
    _dedupe_texts,
    _normalize_archive_level,
    _preview,
    _snapshot_id,
    _tool_snapshot,
    clear_compression_hooks,
    register_compression_hook,
    write_compression_snapshot,
    write_recovery_snapshot,
)


def _run_hook_snapshots(tmp_path, run_snapshot):
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(run_snapshot, i) for i in range(20)]
        for future in as_completed(futures):
            future.result()


class TestSnapshotWriteStress:
    """快照写入压力测试"""

    @pytest.mark.slow
    def test_many_recovery_snapshots_rapidly(self, tmp_path):
        """验证快速写入大量恢复快照"""
        results = []
        errors = []

        def write_snapshot(idx):
            try:
                result = write_recovery_snapshot(
                    tmp_path,
                    session_id=f"sess-{idx % 10}",
                    user_prompt=f"User prompt {idx}",
                    response_text=f"Response {idx}",
                    backend="test",
                    source="test",
                    request_id=f"req-{idx}",
                    run_id=f"run-{idx}",
                    task_id=f"task-{idx}",
                    created_at="2026-05-01T00:00:00Z",
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(write_snapshot, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert len(results) == 100
        assert all(r.ok for r in results)

    @pytest.mark.slow
    def test_concurrent_compression_snapshots(self, tmp_path):
        """验证并发压缩快照写入"""
        results = []

        def write_compression(idx):
            result = write_compression_snapshot(
                tmp_path,
                session_id=f"sess-{idx % 5}",
                turn_id=f"turn-{idx}",
                role="user" if idx % 2 == 0 else "assistant",
                content=f"Content for snapshot {idx}",
                archive_level=idx % 4,
                request_id=f"req-{idx}",
                run_id=f"run-{idx}",
                task_id=f"task-{idx}",
                source="stress_test",
                backend="test",
                created_at="2026-05-01T00:00:00Z",
            )
            results.append(result)
            return result

        with ThreadPoolExecutor(max_workers=15) as executor:
            futures = [executor.submit(write_compression, i) for i in range(50)]
            for f in as_completed(futures):
                pass

        assert len(results) == 50
        assert all(r.snapshot_id for r in results)

    @pytest.mark.slow
    def test_snapshot_with_many_tool_calls(self, tmp_path):
        """验证带大量工具调用的快照"""
        tool_calls = [
            {
                "tool": f"tool-{i}",
                "id": f"call-{i}",
                "ok": True,
                "status": "success",
                "parameters": {"input": f"value-{i}", "index": i},
            }
            for i in range(50)
        ]

        result = write_compression_snapshot(
            tmp_path,
            session_id="sess-tools",
            turn_id="turn-tools",
            role="assistant",
            content="Testing many tool calls",
            tool_calls=tool_calls,
            archive_level=2,
            created_at="2026-05-01T00:00:00Z",
        )

        assert result.snapshot_id
        assert result.token_estimate > 0


class TestSnapshotHookStress:
    """快照钩子压力测试"""

    @pytest.fixture(autouse=True)
    def clear_hooks(self):
        """每个测试后清理钩子"""
        clear_compression_hooks()
        yield
        clear_compression_hooks()

    @pytest.mark.slow
    def test_many_hooks_registration(self):
        """验证注册大量钩子"""
        registered = []

        def make_hook(idx):
            def hook(**kwargs):
                registered.append(idx)
            return hook

        for i in range(100):
            register_compression_hook(make_hook(i))

        assert len(_compression_hooks) == 100

    @pytest.mark.slow
    def test_concurrent_hook_execution(self, tmp_path):
        """验证并发钩子执行"""
        from agent_py_agent.agent.memory_archive.snapshots import on_before_compression

        execution_count = [0]
        lock = threading.Lock()

        def counting_hook(**kwargs):
            time.sleep(0.001)
            with lock:
                execution_count[0] += 1

        register_compression_hook(counting_hook)

        def run_snapshot(idx):
            return on_before_compression(
                tmp_path,
                session_id=f"sess-{idx}",
                turn_id=f"turn-{idx}",
                role="user",
                content=f"Content {idx}",
                source="stress_test",
                created_at="2026-05-01T00:00:00Z",
            )

        try:
            _run_hook_snapshots(tmp_path, run_snapshot)
            assert execution_count[0] == 20
        finally:
            clear_compression_hooks()

    @pytest.mark.slow
    def test_hook_failure_blocks_snapshot(self, tmp_path):
        """验证钩子失败时阻止快照"""
        from agent_py_agent.agent.memory_archive.snapshots import on_before_compression

        def failing_hook(**kwargs):
            raise RuntimeError("Hook failed")

        register_compression_hook(failing_hook)
        try:
            with pytest.raises(RuntimeError, match="Hook failed"):
                on_before_compression(
                    tmp_path,
                    session_id="sess-fail",
                    turn_id="turn-fail",
                    role="user",
                    content="Test content",
                    created_at="2026-05-01T00:00:00Z",
                )
        finally:
            clear_compression_hooks()


class TestSnapshotNormalizationStress:
    """快照归一化压力测试"""

    @pytest.mark.slow
    def test_normalize_archive_level_many_values(self):
        """验证大量归档级别归一化"""
        results = []

        def normalize(value):
            return _normalize_archive_level(value)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(normalize, i)
                for i in range(-100, 200)
            ]
            for f in as_completed(futures):
                results.append(f.result())

        assert all(0 <= r <= 3 for r in results)

    @pytest.mark.slow
    def test_preview_many_contents(self):
        """验证大量内容预览"""
        results = []

        def preview(idx):
            content = f"Content number {idx} with some padding text to make it longer"
            level = idx % 4
            return _preview(content, level)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(preview, i) for i in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 100

    @pytest.mark.slow
    def test_dedupe_texts_massive_input(self):
        """验证大量文本去重"""
        texts = [f"text-{i % 20}" for i in range(1000)]

        result = _dedupe_texts(texts)

        assert len(result) == 20
        assert len(result) == len(set(result))


class TestSnapshotIdempotency:
    """快照幂等性压力测试"""

    @pytest.mark.slow
    def test_identical_snapshots_same_id(self, tmp_path):
        """验证相同内容生成相同 ID"""
        payload = {
            "session_id": "sess-idempotent",
            "user_prompt": "Same prompt",
            "response_text": "Same response",
            "backend": "test",
            "source": "test",
            "created_at": "2026-05-01T00:00:00Z",
        }

        result1 = write_recovery_snapshot(tmp_path, **payload)
        result2 = write_recovery_snapshot(tmp_path, **payload)

        assert result1.ok and result2.ok
        assert result1.snapshot_id == result2.snapshot_id

    @pytest.mark.slow
    def test_snapshot_id_generation_stress(self):
        """验证快照 ID 生成压力"""
        ids = []

        def generate_id(idx):
            payload = {
                "created_at": f"2026-05-01T00:00:{idx:02d}Z",
                "session_id": f"sess-{idx}",
                "content": f"Content {idx}",
            }
            return _snapshot_id(payload)

        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [executor.submit(generate_id, i) for i in range(100)]
            for f in as_completed(futures):
                ids.append(f.result())

        assert len(set(ids)) == 100


class TestSnapshotConcurrencyEdgeCases:
    """快照并发边界情况测试"""

    @pytest.mark.slow
    def test_empty_content_snapshot(self, tmp_path):
        """验证空内容快照"""
        result = write_recovery_snapshot(
            tmp_path,
            session_id="sess-empty",
            user_prompt="",
            response_text="",
            backend="test",
            source="test",
        )

        assert result.ok
        assert result.snapshot_id

    @pytest.mark.slow
    def test_extremely_long_content(self, tmp_path):
        """验证极长内容快照"""
        long_content = "x" * 100000

        result = write_compression_snapshot(
            tmp_path,
            session_id="sess-long",
            turn_id="turn-long",
            role="user",
            content=long_content,
            archive_level=0,
        )

        assert result.snapshot_id
        assert result.token_estimate > 0

    @pytest.mark.slow
    def test_special_characters_in_content(self, tmp_path):
        """验证特殊字符内容"""
        content = "内容 with émojis 🎉 and\nnewlines\ttabs"

        result = write_recovery_snapshot(
            tmp_path,
            session_id="sess-special",
            user_prompt=content,
            response_text=content,
            backend="test",
            source="test",
        )

        assert result.ok

    @pytest.mark.slow
    def test_concurrent_different_roots(self):
        """验证并发写入不同目录"""
        results = []
        errors = []

        def write_to_root(root_idx, idx):
            try:
                root = Path(f"/tmp/test_memory_stress_{root_idx}")
                root.mkdir(exist_ok=True)
                result = write_recovery_snapshot(
                    root,
                    session_id=f"sess-{idx}",
                    user_prompt=f"Prompt {idx}",
                    response_text=f"Response {idx}",
                    backend="test",
                    source="test",
                    created_at="2026-05-01T00:00:00Z",
                )
                results.append(result)
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(write_to_root, i % 3, i)
                for i in range(30)
            ]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0
        assert len(results) == 30


class TestToolSnapshotStress:
    """工具快照压力测试"""

    @pytest.mark.slow
    def test_tool_snapshot_many_calls(self):
        """验证大量工具快照生成"""
        results = []

        def snapshot_tool(idx):
            tool_call = {
                "tool": f"tool-{idx}",
                "id": f"call-{idx}",
                "ok": idx % 2 == 0,
                "status": "success" if idx % 2 == 0 else "failed",
                "error_code": f"ERR-{idx}" if idx % 3 == 0 else "",
                "parameters": {"key": f"value-{idx}", "nested": {"a": idx}},
            }
            return _tool_snapshot(tool_call, archive_level=2)

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(snapshot_tool, i) for i in range(100)]
            for f in as_completed(futures):
                results.append(f.result())

        assert len(results) == 100
        assert all("tool" in r for r in results)

    @pytest.mark.slow
    def test_tool_snapshot_various_levels(self):
        """验证各级别工具快照"""
        tool_call = {
            "tool": "test_tool",
            "id": "call-1",
            "parameters": {"input": "test" * 100},
        }

        results = []
        for level in range(4):
            result = _tool_snapshot(tool_call, archive_level=level)
            results.append(result)

        assert len(results) == 4
        assert all("parameters_preview" in r for r in results)


class TestSnapshotRaceConditions:
    """快照竞态条件测试"""

    @pytest.mark.slow
    def test_rapid_snapshot_read_write(self, tmp_path):
        """验证快速读写快照"""
        errors = []

        def write_snapshot(idx):
            try:
                return write_recovery_snapshot(
                    tmp_path,
                    session_id=f"sess-{idx}",
                    user_prompt=f"Prompt {idx}",
                    response_text=f"Response {idx}",
                    backend="test",
                    source="test",
                    created_at="2026-05-01T00:00:00Z",
                )
            except Exception as e:
                errors.append(e)

        with ThreadPoolExecutor(max_workers=20) as executor:
            # 20个线程同时读写
            futures = [executor.submit(write_snapshot, i) for i in range(100)]
            for f in as_completed(futures):
                pass

        assert len(errors) == 0

    @pytest.mark.slow
    def test_overlapping_sessions(self, tmp_path):
        """验证重叠会话的并发写入"""
        results = []

        def write_session(session_idx):
            session_results = []
            for turn in range(10):
                result = write_recovery_snapshot(
                    tmp_path,
                    session_id=f"sess-{session_idx}",
                    user_prompt=f"Session {session_idx} turn {turn}",
                    response_text=f"Response {session_idx}-{turn}",
                    backend="test",
                    source="test",
                )
                session_results.append(result)
            return session_results

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(write_session, i) for i in range(10)]
            for f in as_completed(futures):
                results.extend(f.result())

        assert len(results) == 100
        assert all(r.ok for r in results)
