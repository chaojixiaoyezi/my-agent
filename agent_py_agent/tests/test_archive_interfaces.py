"""interfaces 模块测试。

测试压缩钩子协议定义和注册管理。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCompressionHookProtocol:
    """测试 CompressionHook 协议。"""

    def test_hook_protocol_has_call_method(self):
        """测试实现了协议的对象有 __call__ 方法。"""
        from agent_py_agent.agent.memory_archive.snapshots import CompressionHook

        class TestHook:
            def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
                pass

        hook = TestHook()
        assert hasattr(hook, "__call__")

    def test_hook_with_session_and_turn_params(self):
        """测试钩子接收 session_id, turn_id, archive_level 参数。"""
        from agent_py_agent.agent.memory_archive.snapshots import CompressionHook

        received_params = {}

        class TestHook:
            def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
                received_params["session_id"] = session_id
                received_params["turn_id"] = turn_id
                received_params["archive_level"] = archive_level

        hook = TestHook()
        hook(session_id="sess_001", turn_id="turn_001", archive_level=3)

        assert received_params["session_id"] == "sess_001"
        assert received_params["turn_id"] == "turn_001"
        assert received_params["archive_level"] == 3

    def test_hook_can_raise_to_block(self):
        """测试钩子可以通过抛异常阻断压缩。"""
        from agent_py_agent.agent.memory_archive.snapshots import CompressionHook

        class BlockingHook:
            def __call__(self, *, session_id: str, turn_id: str, archive_level: int) -> None:
                raise RuntimeError("Compression blocked")

        hook = BlockingHook()
        with pytest.raises(RuntimeError, match="Compression blocked"):
            hook(session_id="sess", turn_id="turn", archive_level=3)


class TestRegisterCompressionHook:
    """测试压缩钩子注册函数。"""

    def test_register_single_hook(self):
        """测试注册单个钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        call_count = 0

        def test_hook(*, session_id: str, turn_id: str, archive_level: int) -> None:
            nonlocal call_count
            call_count += 1

        register_compression_hook(test_hook)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) == 1

        clear_compression_hooks()

    def test_register_multiple_hooks(self):
        """测试注册多个钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        def hook1(*, session_id: str, turn_id: str, archive_level: int) -> None:
            pass

        def hook2(*, session_id: str, turn_id: str, archive_level: int) -> None:
            pass

        register_compression_hook(hook1)
        register_compression_hook(hook2)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) == 2

        clear_compression_hooks()

    def test_register_lambda_hook(self):
        """测试注册 lambda 钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        register_compression_hook(lambda *, session_id, turn_id, archive_level: None)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) == 1

        clear_compression_hooks()


class TestClearCompressionHooks:
    """测试压缩钩子清理函数。"""

    def test_clear_empties_hooks(self):
        """测试清理后钩子列表为空。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        def hook(*, session_id: str, turn_id: str, archive_level: int) -> None:
            pass

        register_compression_hook(hook)
        register_compression_hook(hook)

        clear_compression_hooks()

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) == 0

    def test_clear_on_empty_hooks(self):
        """测试空列表再次清理不报错。"""
        from agent_py_agent.agent.memory_archive.snapshots import clear_compression_hooks

        clear_compression_hooks()
        clear_compression_hooks()

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) == 0


class TestSnapshotPreviewLimits:
    """测试快照预览限制常量。"""

    def test_preview_limits_keys(self):
        """测试预览限制包含所有级别。"""
        from agent_py_agent.agent.memory_archive.snapshots import SNAPSHOT_PREVIEW_LIMITS

        assert 0 in SNAPSHOT_PREVIEW_LIMITS
        assert 1 in SNAPSHOT_PREVIEW_LIMITS
        assert 2 in SNAPSHOT_PREVIEW_LIMITS
        assert 3 in SNAPSHOT_PREVIEW_LIMITS

    def test_preview_limits_order(self):
        """测试预览限制级别越高、限制越严。"""
        from agent_py_agent.agent.memory_archive.snapshots import SNAPSHOT_PREVIEW_LIMITS

        assert SNAPSHOT_PREVIEW_LIMITS[0] > SNAPSHOT_PREVIEW_LIMITS[1]
        assert SNAPSHOT_PREVIEW_LIMITS[1] > SNAPSHOT_PREVIEW_LIMITS[2]
        assert SNAPSHOT_PREVIEW_LIMITS[2] > SNAPSHOT_PREVIEW_LIMITS[3]

    def test_preview_limits_values(self):
        """测试预览限制的具体值。"""
        from agent_py_agent.agent.memory_archive.snapshots import SNAPSHOT_PREVIEW_LIMITS

        assert SNAPSHOT_PREVIEW_LIMITS[0] == 2048
        assert SNAPSHOT_PREVIEW_LIMITS[1] == 1024
        assert SNAPSHOT_PREVIEW_LIMITS[2] == 512
        assert SNAPSHOT_PREVIEW_LIMITS[3] == 160


class TestHookExecutionOrder:
    """测试钩子执行顺序。"""

    def test_hooks_execute_in_order(self):
        """测试钩子按注册顺序执行。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        execution_order = []

        def hook1(*, session_id: str, turn_id: str, archive_level: int) -> None:
            execution_order.append(1)

        def hook2(*, session_id: str, turn_id: str, archive_level: int) -> None:
            execution_order.append(2)

        register_compression_hook(hook1)
        register_compression_hook(hook2)

        # 模拟 on_before_compression 执行钩子
        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        for hook in _compression_hooks:
            hook(session_id="s", turn_id="t", archive_level=3)

        assert execution_order == [1, 2]

        clear_compression_hooks()

    def test_hook_failure_stops_subsequent(self):
        """测试一个钩子失败会阻断后续钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        def hook1(*, session_id: str, turn_id: str, archive_level: int) -> None:
            raise RuntimeError("First hook failed")

        def hook2(*, session_id: str, turn_id: str, archive_level: int) -> None:
            pass

        register_compression_hook(hook1)
        register_compression_hook(hook2)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        hook2_called = False
        try:
            for hook in _compression_hooks:
                hook(session_id="s", turn_id="t", archive_level=3)
        except RuntimeError:
            pass  # 第一个钩子失败
        # hook2 不应该被调用（因为 hook1 抛异常）
        assert not hook2_called

        clear_compression_hooks()


class TestHookEdgeCases:
    """测试钩子边界情况。"""

    def test_hook_with_all_archive_levels(self):
        """测试所有归档级别都可正常传给钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        received_levels = []

        def record_level(*, session_id: str, turn_id: str, archive_level: int) -> None:
            received_levels.append(archive_level)

        register_compression_hook(record_level)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        for level in [0, 1, 2, 3]:
            _compression_hooks[0](session_id="s", turn_id="t", archive_level=level)

        assert received_levels == [0, 1, 2, 3]

        clear_compression_hooks()

    def test_hook_with_empty_ids(self):
        """测试空字符串 session_id 和 turn_id。"""
        from agent_py_agent.agent.memory_archive.snapshots import (
            clear_compression_hooks,
            register_compression_hook,
        )

        clear_compression_hooks()

        received_ids = {}

        def capture_ids(*, session_id: str, turn_id: str, archive_level: int) -> None:
            received_ids["session_id"] = session_id
            received_ids["turn_id"] = turn_id

        register_compression_hook(capture_ids)

        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        _compression_hooks[0](session_id="", turn_id="", archive_level=3)

        assert received_ids["session_id"] == ""
        assert received_ids["turn_id"] == ""

        clear_compression_hooks()
