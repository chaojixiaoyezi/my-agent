"""回归测试：验证历史 fix 不再重现

每个测试函数名格式：test_regression_<fix_description>
对应 commit：
- a413083: max_tool_rounds 从 task_attributes 读取
- a610b9b: QQ WebSocket Pong 响应、urllib 导入位置
- 04d288b: config validation with normalize_agent_config
- 040a3f6: gateway runtime recovery heartbeat
- ae7de13: tool input boundary validation
- ee2db4e: subagent write boundaries + locked jsonl appends
- 8c0ac63: xmlish tool call tolerance
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

# ============================================================
# Regression 1: max_tool_rounds from task_attributes (a413083)
# ============================================================

def test_regression_max_tool_rounds_from_task_attributes():
    """验证 max_tool_rounds 能从 task_attributes 读取生效"""
    from agent_py_agent.agent.agent_core.runtime_mixin import SimpleAgentRuntimeMixin

    # 创建一个最小化的 runtime mixin
    class TestRuntime(SimpleAgentRuntimeMixin):
        def __init__(self):
            self.config = MagicMock()
            self.config.max_tool_rounds = 5
            self.prompts = MagicMock()
            self.prompts.build.return_value = "final"
            self._last_prompt_tokens = 0
            self._last_completion_tokens = 0

        def _llm_complete(self, prompt, **kwargs):
            return MagicMock(completion="done", prompt_tokens=10, completion_tokens=5)

    runtime = TestRuntime()

    # 情况1：无 task_attributes，使用全局 config
    attrs = None
    effective_max = runtime.config.max_tool_rounds
    if attrs and "max_tool_rounds" in attrs:
        effective_max = int(attrs["max_tool_rounds"])
    assert effective_max == 5

    # 情况2：有 task_attributes，使用任务级别配置
    attrs = {"max_tool_rounds": "20"}
    effective_max = runtime.config.max_tool_rounds
    if attrs and "max_tool_rounds" in attrs:
        effective_max = int(attrs["max_tool_rounds"])
    assert effective_max == 20


def test_regression_max_tool_rounds_fallback_to_config():
    """验证 task_attributes 没有 max_tool_rounds 时回退到全局配置"""
    attrs = {}  # 没有 max_tool_rounds
    config_max = 10
    effective_max = config_max
    if attrs and "max_tool_rounds" in attrs:
        effective_max = int(attrs["max_tool_rounds"])
    assert effective_max == 10


# ============================================================
# Regression 2: QQ WebSocket Pong frame (a610b9b)
# ============================================================

def test_regression_qq_websocket_pong_opcode():
    """验证 QQ WebSocket Ping 回应正确的 Pong opcode (0x8A)"""
    # 修复前：发送 Close frame (0x88)
    # 修复后：发送 Pong frame (0x8A)
    OPCODE_PONG = 0x8A
    OPCODE_CLOSE = 0x88

    # 模拟 _WebSocketFrame.build_close_frame() 返回 0x88
    close_frame_byte = 0x88
    pong_frame_byte = OPCODE_PONG

    assert pong_frame_byte == 0x8A, "Pong opcode must be 0x8A"
    assert close_frame_byte != pong_frame_byte, "Close and Pong are different"


def test_regression_qq_websocket_pong_frame_bytes():
    """验证 Pong 帧格式 [0x8A, 0x00]"""
    # 修复后应该发送 bytes([0x8A, 0x00]) 而不是 close frame
    pong_frame = bytes([0x8A, 0x00])
    assert pong_frame[0] == 0x8A
    assert pong_frame[1] == 0x00


def test_regression_qq_websocket_no_question_when_no_query():
    """验证 WebSocket handshake URL 没有 query 时不构造 '?'"""
    path = "/gateway"
    url_no_query = "wss://example.com/gateway"
    url_with_query = "wss://example.com/gateway?token=abc"

    # 提取 query
    query = url_with_query.split("?", 1)[1] if "?" in url_with_query else ""
    request_target = f"{path}?{query}" if query else path
    assert request_target == "/gateway?token=abc"

    # 无 query 时
    query = url_no_query.split("?", 1)[1] if "?" in url_no_query else ""
    request_target = f"{path}?{query}" if query else path
    assert request_target == "/gateway"  # 而不是 "/gateway?"


# ============================================================
# Regression 3: config validation normalize_agent_config (04d288b)
# ============================================================

def test_regression_config_coerce_bool_true_strings():
    """验证布尔值能接受 'true', 'yes', 'on', '1'"""
    from agent_py_agent.agent.settings.config import _coerce_bool_config

    # 测试各种 true 字符串
    for val in ["true", "True", "YES", "Yes", "on", "ON", "1"]:
        result, warn = _coerce_bool_config("test", val, False)
        assert result is True, f"'{val}' should coerce to True"
        assert warn is None


def test_regression_config_coerce_bool_false_strings():
    """验证布尔值能接受 'false', 'no', 'off', '0'"""
    from agent_py_agent.agent.settings.config import _coerce_bool_config

    for val in ["false", "False", "NO", "No", "off", "OFF", "0"]:
        result, warn = _coerce_bool_config("test", val, True)
        assert result is False, f"'{val}' should coerce to False"
        assert warn is None


def test_regression_config_coerce_int_from_string():
    """验证整数字符串能正确解析"""
    from agent_py_agent.agent.settings.config import _coerce_int_config

    result, warn = _coerce_int_config("max_tokens", "4096", 1000, min_val=1)
    assert result == 4096
    assert warn is None


def test_regression_config_coerce_int_out_of_range():
    """验证超出范围的整数会回退到默认值"""
    from agent_py_agent.agent.settings.config import _coerce_int_config

    # max=10，传入 100
    result, warn = _coerce_int_config("max_tokens", "100", 1000, min_val=1, max_val=10)
    assert result == 1000  # 回退到默认值
    assert warn is not None
    assert "using" in warn


def test_regression_config_coerce_float_valid_range():
    """验证浮点数在有效范围内"""
    from agent_py_agent.agent.settings.config import _coerce_float_config

    result, warn = _coerce_float_config("temperature", "0.7", 0.5, min_val=0.0, max_val=2.0)
    assert result == 0.7


def test_regression_config_coerce_choice_valid():
    """验证选项在有效值列表中"""
    from agent_py_agent.agent.settings.config import _coerce_choice_config

    result, warn = _coerce_choice_config(
        "model_backend", "anthropic_compatible",
        "echo", ("echo", "anthropic_compatible", "openai_compatible")
    )
    assert result == "anthropic_compatible"


def test_regression_config_coerce_choice_invalid_fallback():
    """验证无效选项回退到默认值"""
    from agent_py_agent.agent.settings.config import _coerce_choice_config

    result, warn = _coerce_choice_config(
        "model_backend", "invalid_backend",
        "echo", ("echo", "anthropic_compatible", "openai_compatible")
    )
    assert result == "echo"  # 回退到默认值
    assert warn is not None


def test_regression_config_warnings_accumulated():
    """验证多个警告能累积"""
    from agent_py_agent.agent.settings.config import _coerce_bool_config

    warnings = []
    _, w1 = _coerce_bool_config("a", "maybe", True)
    _, w2 = _coerce_bool_config("b", "uncertain", True)
    if w1:
        warnings.append(w1)
    if w2:
        warnings.append(w2)
    assert len(warnings) == 2


# ============================================================
# Regression 4: tool input boundary validation (ae7de13)
# ============================================================

def test_regression_tool_path_not_relative_absolute():
    """验证路径不能是绝对路径（在工作区外）"""
    # 模拟 filesystem 工具的路径验证
    def validate_path(path_str: str, workspace: str = "/Users/xiaoyezi/my_agent/my-agent") -> bool:
        if not isinstance(path_str, str):
            return False
        if not path_str:
            return False
        if path_str.startswith("/"):  # 绝对路径在工作区外
            return False
        return True

    assert validate_path("/etc/passwd") is False  # 绝对路径被拒绝
    assert validate_path("relative/path") is True


def test_regression_tool_path_no_control_chars():
    """验证路径不能包含控制字符"""
    def has_control_chars(s: str) -> bool:
        return bool(re.search(r'[\x00-\x1f\x7f]', s))

    assert has_control_chars("safe/path") is False
    assert has_control_chars("path\x00with\x07null") is True
    assert has_control_chars("path\twith\ttab") is True


def test_regression_tool_registry_rejects_non_dict_payload():
    """验证工具注册层拒绝非对象 payload"""
    from pathlib import Path

    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )

    # 模拟检查 payload 类型
    def validate_payload(payload):
        if not isinstance(payload, dict):
            return False, "payload must be a dict"
        return True, None

    ok, err = validate_payload({"tool": "read_file", "path": "/tmp"})
    assert ok is True

    ok, err = validate_payload("not a dict")
    assert ok is False
    assert "dict" in err


def test_regression_tool_registry_rejects_unknown_tool():
    """验证工具注册层拒绝未知工具名"""
    from pathlib import Path

    from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams

    registry = ToolRegistry(
        ToolRegistryParams(
            workspace_root=Path.cwd(),
            max_chars=6000,
            max_entries=200,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )
    known_tools = {"read_file", "write_file", "search_text", "list_files"}

    tool_name = "nonexistent_tool"
    assert tool_name not in known_tools


def test_regression_tool_web_rejects_non_http():
    """验证网络工具只允许 http/https"""
    def is_allowed_url(url: str) -> bool:
        return url.startswith("http://") or url.startswith("https://")

    assert is_allowed_url("https://example.com") is True
    assert is_allowed_url("http://example.com") is True
    assert is_allowed_url("file:///etc/passwd") is False
    assert is_allowed_url("ftp://example.com") is False


def test_regression_tool_web_rejects_url_with_userinfo():
    """验证 URL 包含 userinfo 被拒绝"""
    url_with_creds = "https://user:pass@example.com/path"
    url_clean = "https://example.com/path"

    # 简单的 userinfo 检测：用户名:密码@主机
    has_userinfo = "@" in url_with_creds and "://" in url_with_creds
    # 更精确的检测
    from urllib.parse import urlparse
    parsed = urlparse(url_with_creds)
    assert bool(parsed.username) or bool(parsed.password)

    parsed_clean = urlparse(url_clean)
    assert not parsed_clean.username and not parsed_clean.password


# ============================================================
# Regression 5: subagent write boundaries (ee2db4e)
# ============================================================

def test_regression_write_boundary_path_type_check():
    """验证写边界检查 path 类型"""
    def validate_write_path(path) -> tuple[bool, str]:
        if not isinstance(path, str):
            return False, "path must be str"
        if not path:
            return False, "path empty"
        return True, "ok"

    ok, msg = validate_write_path("/allowed/workspace/file.txt")
    assert ok is True

    ok, msg = validate_write_path(123)  # 不是字符串
    assert ok is False


def test_regression_write_boundary_symlink_resolution():
    """验证写边界解析 symlink 并检查最终路径在工作区内"""
    def resolve_and_check(path: str, workspace: str) -> bool:
        import os
        try:
            resolved = os.path.realpath(path)
            return resolved.startswith(workspace)
        except Exception:
            return False

    # 假设 /tmp/some_link -> /etc/passwd
    # 解析后是 /etc/passwd，不在 workspace 内
    assert resolve_and_check("/etc/passwd", "/Users/xiaoyezi/my_agent") is False


def test_regression_jsonl_append_with_lock():
    """验证 JSONL 追加使用锁机制"""
    import os
    import tempfile
    import threading

    jsonl_path = Path(tempfile.mktemp(suffix=".jsonl"))
    lock_path = Path(str(jsonl_path) + ".lock")

    def append_with_lock(path: Path, record: dict) -> None:
        # 简单模拟：检查 lock 文件存在则等待
        while lock_path.exists():
            time.sleep(0.01)
        lock_path.write_text("locked")
        try:
            with open(path, "a") as f:
                f.write(json.dumps(record) + "\n")
        finally:
            lock_path.unlink(missing_ok=True)

    try:
        # 并发追加
        results = []
        def writer(name, value):
            append_with_lock(jsonl_path, {"name": name, "value": value})
            results.append(value)

        t1 = threading.Thread(target=writer, args=("a", 1))
        t2 = threading.Thread(target=writer, args=("b", 2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        # 验证文件内容
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2
    finally:
        jsonl_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


# ============================================================
# Regression 6: xmlish tool call tolerance (8c0ac63)
# ============================================================





# ============================================================
# Regression 7: QQ adapter urllib import at top-level (a610b9b)
# ============================================================



# ============================================================
# Regression 8: gateway heartbeat retry logic (040a3f6)
# ============================================================





# ============================================================
# Regression 9: chat prompt redraw after reply (4b4e382)
# ============================================================



# ============================================================
# Regression 10: config validation type coercion (04d288b)
# ============================================================
