# LLM: 仅以临时文件锁验证实际scope的handle所有权；不启动服务、Agent或迁移，不修改旧冻结测试。
# 模块用途: 防止同inode未持锁句柄冒充原句柄，并保证收尾只释放原scope资源。
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts.standalone_lifecycle import (
    StandaloneLifecycleUnavailable,
    standalone_lifecycle_guard,
)


# LLM: 此helper只观察真实临时scope；分别保留require与cleanup错误，避免收尾错误遮住身份拒绝失败。
# 函数用途: 返回换句柄后的结构事实，所有临时资源由原拥有方显式关闭。
def _replaced_handle_result(root: Path, *, close_original: bool, close_replacement: bool = False):
    guard = standalone_lifecycle_guard(root)
    lifecycle = guard.__enter__()
    original = lifecycle.handle
    replacement = lifecycle.path.open("r+", encoding="utf-8")
    if close_original:
        original.close()
    if close_replacement:
        replacement.close()
    lifecycle.handle = replacement
    result = {"rejected": False, "cleanup_error": None}
    try:
        try:
            lifecycle.require_held()
        except StandaloneLifecycleUnavailable:
            result["rejected"] = True
        try:
            guard.__exit__(None, None, None)
        except Exception as exc:
            result["cleanup_error"] = type(exc).__name__
        result["original_closed"] = original.closed
        result["replacement_closed_by_cleanup"] = replacement.closed and not close_replacement
        with standalone_lifecycle_guard(root):
            result["original_lock_released"] = True
        return result
    finally:
        original.close()
        replacement.close()


@pytest.mark.parametrize("close_original", [False, True])
def test_same_inode_replacement_is_not_the_original_handle(tmp_path, close_original):
    result = _replaced_handle_result(tmp_path, close_original=close_original)
    assert result["rejected"] is True


def test_already_closed_original_does_not_break_scope_cleanup(tmp_path):
    guard = standalone_lifecycle_guard(tmp_path)
    lifecycle = guard.__enter__()
    lifecycle.handle.close()
    with pytest.raises(StandaloneLifecycleUnavailable):
        lifecycle.require_held()
    guard.__exit__(None, None, None)
    with standalone_lifecycle_guard(tmp_path):
        pass


def test_cleanup_releases_only_its_original_capture(tmp_path):
    result = _replaced_handle_result(tmp_path, close_original=False)
    assert result["cleanup_error"] is None
    assert result["original_closed"] is True
    assert result["replacement_closed_by_cleanup"] is False
    assert result["original_lock_released"] is True


def test_closed_replacement_never_becomes_original_authority(tmp_path):
    result = _replaced_handle_result(tmp_path, close_original=False, close_replacement=True)
    assert result["rejected"] is True
    assert result["cleanup_error"] is None
    assert result["original_closed"] is True


def test_different_inode_replacement_is_rejected_without_owning_it(tmp_path):
    with standalone_lifecycle_guard(tmp_path) as lifecycle:
        original = lifecycle.handle
        other = tmp_path / "other.lock"
        other.write_text("")
        with other.open("r+", encoding="utf-8") as replacement:
            lifecycle.handle = replacement
            with pytest.raises(StandaloneLifecycleUnavailable):
                lifecycle.require_held()
        assert not original.closed
    assert original.closed
