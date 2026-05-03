"""snapshots 模块测试。

测试快照生成、压缩钩子、恢复快照核心函数。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


class TestCheckTokenBudget:
    """测试 token 预算检查函数。"""

    def test_budget_ok_normal_ratio(self):
        """测试正常比例时返回 ok。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=100, max_tokens=1000, archive_level=3)

        assert result.status == "ok"
        assert result.current_tokens == 100
        assert result.max_tokens == 1000
        assert result.ratio == 0.1

    def test_budget_warning_at_threshold(self):
        """测试达到警告阈值时返回 warning。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        # level 3 warning at 80%
        result = check_token_budget(current_tokens=800, max_tokens=1000, archive_level=3)

        assert result.status == "warning"
        assert "接近上限" in result.message

    def test_budget_block_at_threshold(self):
        """测试达到阻断阈值时返回 block。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        # level 3 block at 100%
        result = check_token_budget(current_tokens=1000, max_tokens=1000, archive_level=3)

        assert result.status == "block"
        assert "超限" in result.message

    def test_budget_negative_max_tokens(self):
        """测试 max_tokens 为负数时返回 ok。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=100, max_tokens=-1, archive_level=3)

        assert result.status == "ok"
        assert "未设置" in result.message

    def test_budget_zero_max_tokens(self):
        """测试 max_tokens 为零时返回 ok。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=100, max_tokens=0, archive_level=3)

        assert result.status == "ok"
        assert "未设置" in result.message

    def test_budget_level_clamping(self):
        """测试 archive_level 被限制在 0-3 范围。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=50, max_tokens=100, archive_level=10)

        assert result.archive_level == 3

    def test_budget_level_0_strict(self):
        """测试 level 0 使用更严格的阈值。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        # level 0 warning at 60%, block at 85%
        # 70% should be warning
        result = check_token_budget(current_tokens=700, max_tokens=1000, archive_level=0)

        assert result.status == "warning"
        assert result.archive_level == 0

    def test_budget_different_levels(self):
        """测试不同 archive_level 有不同阈值。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        # level 1: warning at 70%, block at 90%
        result_warn = check_token_budget(current_tokens=750, max_tokens=1000, archive_level=1)
        assert result_warn.status == "warning"

        result_block = check_token_budget(current_tokens=950, max_tokens=1000, archive_level=1)
        assert result_block.status == "block"


class TestEstimateTokens:
    """测试 token 估算函数。"""

    def test_estimate_string_basic(self):
        """测试基本字符串估算。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens("hello world")
        assert result >= 1

    def test_estimate_cjk_chars(self):
        """测试中文字符估算（每个汉字计 1 token）。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens("你好世界")
        assert result >= 4

    def test_estimate_dict(self):
        """测试字典估算包含结构化开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"key": "value", "nested": {"inner": "data"}}
        result = estimate_tokens(data)
        assert result >= 1

    def test_estimate_list(self):
        """测试列表估算。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens(["item1", "item2", "item3"])
        assert result >= 1

    def test_estimate_empty_string(self):
        """测试空字符串返回最小值 1。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens("")
        assert result == 1

    def test_estimate_large_content(self):
        """测试大内容估算不会溢出。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        large_text = "x" * 10000
        result = estimate_tokens(large_text)
        assert result > 0


class TestAppendSessionTokenUsage:
    """测试会话 token 用量追加函数。"""

    def test_append_creates_file(self, tmp_path: Path):
        """测试首次追加创建文件。"""
        from agent_py_agent.agent.memory_archive.tokens import append_session_token_usage

        result = append_session_token_usage(
            root=tmp_path,
            session_id="sess_001",
            turn_id="turn_001",
            input_tokens=100,
            output_tokens=200,
            tool_tokens=50,
            created_at="2026-05-03T10:00:00Z",
        )

        assert result["session_id"] == "sess_001"
        assert result["turn_total"] == 350
        assert result["cumulative_tokens"] == 350

    def test_append_cumulative_tokens(self, tmp_path: Path):
        """测试多次追加累计 token。"""
        from agent_py_agent.agent.memory_archive.tokens import append_session_token_usage

        append_session_token_usage(
            root=tmp_path,
            session_id="sess_001",
            turn_id="turn_001",
            input_tokens=100,
            output_tokens=100,
            tool_tokens=0,
            created_at="2026-05-03T10:00:00Z",
        )

        result = append_session_token_usage(
            root=tmp_path,
            session_id="sess_001",
            turn_id="turn_002",
            input_tokens=200,
            output_tokens=200,
            tool_tokens=0,
            created_at="2026-05-03T10:01:00Z",
        )

        assert result["cumulative_tokens"] == 600
        assert result["turn_count"] == 2

    def test_append_negative_tokens(self, tmp_path: Path):
        """测试负数 token 被处理为 0。"""
        from agent_py_agent.agent.memory_archive.tokens import append_session_token_usage

        result = append_session_token_usage(
            root=tmp_path,
            session_id="sess_001",
            turn_id="turn_001",
            input_tokens=-100,
            output_tokens=-200,
            tool_tokens=-50,
            created_at="2026-05-03T10:00:00Z",
        )

        assert result["turn_total"] == 0

    def test_token_ledger_dir(self, tmp_path: Path):
        """测试账本目录路径构建。"""
        from agent_py_agent.agent.memory_archive.tokens import token_ledger_dir

        result = token_ledger_dir(tmp_path)

        assert "memory_archive" in str(result)
        assert "tokens" in str(result)


class TestCompressionHooks:
    """测试压缩钩子注册和管理。"""

    def test_register_and_clear_hooks(self):
        """测试钩子注册和清理。"""
        from agent_py_agent.agent.memory_archive.snapshots import clear_compression_hooks, register_compression_hook

        clear_compression_hooks()

        hook_called = False

        def test_hook(*, session_id: str, turn_id: str, archive_level: int) -> None:
            nonlocal hook_called
            hook_called = True

        register_compression_hook(test_hook)
        clear_compression_hooks()

        # hook 应该已被清除
        assert not hook_called

    def test_multiple_hooks_registered(self):
        """测试可以注册多个钩子。"""
        from agent_py_agent.agent.memory_archive.snapshots import clear_compression_hooks, register_compression_hook

        clear_compression_hooks()

        call_count = 0

        def hook1(*, session_id: str, turn_id: str, archive_level: int) -> None:
            nonlocal call_count
            call_count += 1

        def hook2(*, session_id: str, turn_id: str, archive_level: int) -> None:
            nonlocal call_count
            call_count += 1

        register_compression_hook(hook1)
        register_compression_hook(hook2)

        # 验证都注册了
        from agent_py_agent.agent.memory_archive.snapshots import _compression_hooks
        assert len(_compression_hooks) >= 2

        clear_compression_hooks()


class TestSnapshotHelpers:
    """测试快照辅助函数。"""

    def test_normalize_archive_level_valid(self):
        """测试有效的 archive_level。"""
        from agent_py_agent.agent.memory_archive.snapshots import _normalize_archive_level

        assert _normalize_archive_level(0) == 0
        assert _normalize_archive_level(1) == 1
        assert _normalize_archive_level(2) == 2
        assert _normalize_archive_level(3) == 3

    def test_normalize_archive_level_out_of_range(self):
        """测试超出范围的值被限制到 3。"""
        from agent_py_agent.agent.memory_archive.snapshots import _normalize_archive_level

        assert _normalize_archive_level(-1) == 3
        assert _normalize_archive_level(5) == 3
        assert _normalize_archive_level(100) == 3

    def test_normalize_archive_level_bool(self):
        """测试布尔值被转换为 3。"""
        from agent_py_agent.agent.memory_archive.snapshots import _normalize_archive_level

        assert _normalize_archive_level(True) == 3
        assert _normalize_archive_level(False) == 3

    def test_preview_truncation(self):
        """测试内容预览截断。"""
        from agent_py_agent.agent.memory_archive.snapshots import _preview

        long_content = "x" * 3000
        result = _preview(long_content, archive_level=3)

        assert len(result) < len(long_content)
        assert result.endswith("...")

    def test_preview_short_content(self):
        """测试短内容不被截断。"""
        from agent_py_agent.agent.memory_archive.snapshots import _preview

        short = "hello"
        result = _preview(short, archive_level=3)
        assert result == short

    def test_dedupe_texts(self):
        """测试文本去重。"""
        from agent_py_agent.agent.memory_archive.snapshots import _dedupe_texts

        result = _dedupe_texts(["a", "b", "a", "c", "b"])
        assert result == ["a", "b", "c"]

    def test_dedupe_texts_preserves_order(self):
        """测试去重保持首次出现的顺序。"""
        from agent_py_agent.agent.memory_archive.snapshots import _dedupe_texts

        result = _dedupe_texts(["first", "second", "first", "third"])
        assert result == ["first", "second", "third"]

    def test_dedupe_texts_empty(self):
        """测试空列表去重。"""
        from agent_py_agent.agent.memory_archive.snapshots import _dedupe_texts

        result = _dedupe_texts([])
        assert result == []

    def test_snapshot_id_format(self):
        """测试快照 ID 格式。"""
        from agent_py_agent.agent.memory_archive.snapshots import _snapshot_id

        payload = {"key": "value"}
        result = _snapshot_id(payload)

        assert result.startswith("snapshot:")
        assert len(result) > 10

    def test_content_hash_format(self):
        """测试内容哈希格式。"""
        from agent_py_agent.agent.memory_archive.snapshots import _content_hash

        result = _content_hash("test content")

        assert result.startswith("sha256:")
        assert len(result) > 10

    def test_stable_json_deterministic(self):
        """测试稳定 JSON 输出。"""
        from agent_py_agent.agent.memory_archive.snapshots import _stable_json

        data = {"b": 2, "a": 1}
        result1 = _stable_json(data)
        result2 = _stable_json(data)

        assert result1 == result2
        assert '"a":1' in result1
        assert '"b":2' in result1
