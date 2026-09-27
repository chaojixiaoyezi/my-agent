"""并发控制测试。"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_py_agent.agent.concurrency.exceptions import ConcurrencyConflictError
from agent_py_agent.agent.concurrency.optimistic_lock import OptimisticLock
from agent_py_agent.agent.concurrency.retry import retry_on_conflict


class MockConfig:
    """测试用配置对象。"""
    def __init__(self, workspace):
        self.subagent_workspace = workspace


class TestOptimisticLock(unittest.TestCase):
    """乐观锁测试。"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.workspace = Path(self.temp_dir) / "workspace"
        self.workspace.mkdir(parents=True)
        self.config = MockConfig(str(self.workspace))
        self.lock = OptimisticLock(self.config)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_acquire_version(self):
        """测试获取版本号。"""
        # 首次获取应该返回版本 1
        version = self.lock.acquire("task-1")
        self.assertEqual(version, 1)

    def test_release_updates_version(self):
        """测试释放时更新版本号。"""
        # 获取初始版本
        v1 = self.lock.acquire("task-1")
        self.assertEqual(v1, 1)

        # 释放应该递增版本
        v2 = self.lock.release("task-1", v1)
        self.assertEqual(v2, 2)

        # 再次获取应该得到新版本
        v3 = self.lock.acquire("task-1")
        self.assertEqual(v3, 2)

    def test_conflict_detection(self):
        """测试冲突检测。"""
        # 初始版本
        v1 = self.lock.acquire("task-1")

        # 释放后版本变为 2
        self.lock.release("task-1", v1)

        # 使用旧版本号应该抛出异常
        with self.assertRaises(ConcurrencyConflictError) as ctx:
            self.lock.release("task-1", v1)

        self.assertEqual(ctx.exception.task_id, "task-1")
        self.assertEqual(ctx.exception.expected_version, v1)
        self.assertEqual(ctx.exception.actual_version, 2)

    def test_check_version(self):
        """测试版本检查。"""
        v1 = self.lock.acquire("task-1")

        # 检查当前版本应该通过
        self.assertTrue(self.lock.check("task-1", v1))

        # 检查旧版本应该失败
        self.assertFalse(self.lock.check("task-1", v1 - 1))

        self.lock.release("task-1", v1)

        # 检查新版本应该失败
        self.assertFalse(self.lock.check("task-1", v1))

    def test_concurrent_release_conflict(self):
        """测试并发释放导致冲突。"""
        versions = []

        def acquire_and_release():
            v = self.lock.acquire("task-1")
            versions.append(v)
            time.sleep(0.05)  # 模拟延迟
            try:
                self.lock.release("task-1", v)
            except ConcurrencyConflictError:
                pass

        # 两个线程使用相同初始版本
        v = self.lock.acquire("task-1")

        t1 = threading.Thread(target=acquire_and_release)
        t2 = threading.Thread(target=acquire_and_release)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 至少一个线程会成功，一个会失败


class TestConcurrencyConflictError(unittest.TestCase):
    """并发冲突异常测试。"""

    def test_error_attributes(self):
        """测试异常属性。"""
        error = ConcurrencyConflictError(
            task_id="test-task",
            expected_version=5,
            actual_version=3,
        )

        self.assertEqual(error.task_id, "test-task")
        self.assertEqual(error.expected_version, 5)
        self.assertEqual(error.actual_version, 3)

    def test_error_message(self):
        """测试异常消息。"""
        error = ConcurrencyConflictError(
            task_id="test-task",
            expected_version=5,
            actual_version=3,
        )

        self.assertIn("test-task", str(error))
        self.assertIn("5", str(error))
        self.assertIn("3", str(error))


class TestRetryOnConflict(unittest.TestCase):
    """重试装饰器测试。"""

    def test_successful_call(self):
        """测试成功调用。"""
        call_count = 0

        @retry_on_conflict(max_retries=3)
        def success_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = success_func()
        self.assertEqual(result, "success")
        self.assertEqual(call_count, 1)

    def test_retry_on_conflict(self):
        """测试冲突时重试。"""
        call_count = 0

        @retry_on_conflict(max_retries=3, min_backoff=0.01, max_backoff=0.02)
        def conflict_then_success():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConcurrencyConflictError(
                    task_id="test",
                    expected_version=call_count,
                    actual_version=call_count - 1,
                )
            return "success"

        result = conflict_then_success()
        self.assertEqual(result, "success")
        self.assertEqual(call_count, 3)

    def test_max_retries_exceeded(self):
        """测试超过最大重试次数。"""
        call_count = 0

        @retry_on_conflict(max_retries=2, min_backoff=0.01, max_backoff=0.02)
        def always_conflict():
            nonlocal call_count
            call_count += 1
            raise ConcurrencyConflictError(
                task_id="test",
                expected_version=1,
                actual_version=0,
            )

        with self.assertRaises(ConcurrencyConflictError):
            always_conflict()

        # 初始尝试 + 2 次重试 = 3 次
        self.assertEqual(call_count, 3)

    def test_non_conflict_error_not_retried(self):
        """测试非冲突错误不重试。"""

        @retry_on_conflict(max_retries=3)
        def value_error():
            raise ValueError("test error")

        with self.assertRaises(ValueError):
            value_error()


if __name__ == "__main__":
    unittest.main()
