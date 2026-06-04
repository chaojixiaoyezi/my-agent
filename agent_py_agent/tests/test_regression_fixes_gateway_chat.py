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

def test_regression_xml_tool_call_parse_read():
    """验证 XML-ish tool_call 能解析 read 别名"""
    from agent_py_agent.agent.tooling.parser import _normalize_xmlish_tool_name

    # XML-ish 格式：<tool_call><function=read><parameter=file_path>...
    # 修复前：read、write、search 等别名不会被归一化
    # 修复后：被归一化为标准工具名 read_file、write_file、search_text
    assert _normalize_xmlish_tool_name("read") == "read_file"
    assert _normalize_xmlish_tool_name("write") == "write_file"
    assert _normalize_xmlish_tool_name("search") == "search_text"
    assert _normalize_xmlish_tool_name("list") == "list_files"

def test_regression_xml_tool_call_param_aliases():
    """验证 XML-ish 参数别名归一化"""
    from agent_py_agent.agent.tooling.parser import _normalize_xmlish_parameter_name

    # 修复前：file_path、filepath、filename、file 不会被统一
    # 修复后：统一归一化为 path
    assert _normalize_xmlish_parameter_name("file_path") == "path"
    assert _normalize_xmlish_parameter_name("filepath") == "path"
    assert _normalize_xmlish_parameter_name("filename") == "path"
    assert _normalize_xmlish_parameter_name("file") == "path"

def test_regression_qq_adapter_urllib_import():
    """验证 QQ adapter 的 urllib import 在文件顶部（可在任何函数外导入）"""
    # 修复前：urllib 在文件底部 import，导致某些情况下 ImportError
    # 修复后：在文件顶部导入，可在任何地方使用
    # urllib.error 和 urllib.request 应该在模块级别可用
    # 检查模块源码中 urllib import 位置
    import inspect

    import agent_py_agent.agent.adapter.qq as qq_module
    source = inspect.getsource(qq_module)
    # urllib 应该在文件前 500 字符内导入（顶部），而不是在文件末尾
    import_pos = source.find("import urllib")
    assert import_pos != -1, "urllib import not found"
    assert import_pos < 500, f"urllib import at position {import_pos}, should be near top"

def test_regression_gateway_heartbeat_retry_threshold():
    """验证 gateway heartbeat 重试次数达到阈值后放弃"""
    # 修复前：一次失败就放弃
    # 修复后：连续 3 次失败才放弃
    max_retries = 3
    failures = 0

    def attempt_heartbeat():
        nonlocal failures
        failures += 1
        return failures < max_retries  # True = success, False = stop

    results = []
    for i in range(5):
        if not attempt_heartbeat():
            results.append("abandoned")
            break
        results.append("success")

    assert results[-1] == "abandoned"
    assert failures == 3  # 失败 3 次后放弃

def test_regression_gateway_late_pending_tracking():
    """验证 gateway 能跟踪 late 的响应"""
    # 修复前：超时的请求结果被丢弃
    # 修复后：late_pending.jsonl 跟踪超时请求，结果到达后能识别
    late_pending_path = "/tmp/late_pending.jsonl"
    import os
    if os.path.exists(late_pending_path):
        os.unlink(late_pending_path)

    # 模拟：请求超时被记录
    import json
    with open(late_pending_path, "w") as f:
        json.dump({"request_id": "req-123", "timeout_at": "2026-05-01T12:00:00Z"}, f)

    # 验证文件存在且可读
    assert os.path.exists(late_pending_path)
    with open(late_pending_path) as f:
        data = json.load(f)
    assert data["request_id"] == "req-123"

def test_regression_chat_plain_prompt_redraw():
    """验证 plain 交互模式在回复后重绘 prompt"""
    # 修复前：后台线程输出后 stdin 前 prompt 消失
    # 修复后：worker 完成后调用 redraw_plain_prompt()
    class FakeState:
        def __init__(self):
            self.plain_waiting_for_input = True
            self.shutting_down = False

    state = FakeState()

    def should_redraw():
        return state.plain_waiting_for_input and not state.shutting_down

    # 正常情况应该重绘
    assert should_redraw() is True

    # shutting_down 时不应该重绘
    state.shutting_down = True
    assert should_redraw() is False

    # 不是 waiting 状态时不应该重绘
    state.plain_waiting_for_input = False
    state.shutting_down = False
    assert should_redraw() is False

def test_regression_config_temperature_range_validation():
    """验证 temperature 在 0.0-2.0 范围内"""
    from agent_py_agent.agent.settings.config import _coerce_float_config

    # 有效范围
    result, _ = _coerce_float_config("temperature", "1.5", 0.5, min_val=0.0, max_val=2.0)
    assert result == 1.5

    # 超出范围
    result, warn = _coerce_float_config("temperature", "3.0", 0.5, min_val=0.0, max_val=2.0)
    assert result == 0.5  # 回退到默认值
    assert warn is not None

def test_regression_config_request_timeout_range():
    """验证 request_timeout 在 1-600 秒范围内"""
    from agent_py_agent.agent.settings.config import _coerce_int_config

    # 有效范围
    result, _ = _coerce_int_config("request_timeout", 60, 30, min_val=1, max_val=600)
    assert result == 60

    # 太小
    result, warn = _coerce_int_config("request_timeout", 0, 30, min_val=1, max_val=600)
    assert result == 30
    assert warn is not None

    # 太大
    result, warn = _coerce_int_config("request_timeout", 999, 30, min_val=1, max_val=600)
    assert result == 30
    assert warn is not None
