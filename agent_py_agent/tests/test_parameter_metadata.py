"""P8 参数登记表元数据：单位/范围/归属模块/读取方的推导与展示（2026-10-01）。

来源：三线收口 P8（设计目标 1，见 docs/design/PARAMETER_CENTER.md 第 2 节）。
推导规则：
- 单位：按键名结尾后缀（_seconds/_ms/_chars/_bytes/_tokens/_percent 等），推不出留空；
- 范围：从现有规范化/校验规格取（_memory_coercion._FIELDS、runtime_tool_field_specs.TOOL_INT_FIELDS、
  settings/services/_normalize.py 的 Service 规格与枚举），没有校验的留空；
- 读取方：与 test_config_field_readers 同一套属性访问/字符串键引用扫描；归属模块取主要读取方的顶层模块。
展示：/settings show 与 user_config 的 view/search 只在有值时显示这四项。

复现方法：
    cd <worktree> && PYTHONPATH=$PWD $PY -m pytest agent_py_agent/tests/test_parameter_metadata.py -q --tb=short

变异验证（各做一个，杀掉推导逻辑应变红）：
    1. parameter_metadata.py 删掉 _UNIT_SUFFIXES 里的 ("_seconds", "秒") → test_unit_derived_from_key_suffix 红；
    2. parameter_metadata.py 的 _EXTRA_INT_RANGES 删掉 "request_timeout" → test_range_from_validation_specs 红；
    3. parameter_metadata.py 的 field_readers 跳过全部文件（_SELF_EXCLUDED 覆盖所有 .py）→ test_every_key_has_a_reader 红。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.settings_control_service import _show
from agent_py_agent.agent.settings.parameter_metadata import field_readers, ranges, unit_for_key
from agent_py_agent.agent.settings.parameter_registry import (
    LOADER_METADATA_KEYS,
    parameter_registry,
)


def test_unit_derived_from_key_suffix():
    """单位按键名后缀推导：秒/毫秒/字符/字节/tokens/百分比/天；没有匹配后缀的留空。"""
    assert unit_for_key("memory_curator_interval_seconds") == "秒"
    assert unit_for_key("foo_ms") == "毫秒"
    assert unit_for_key("tool_read_max_chars") == "字符"
    assert unit_for_key("input_media_max_bytes") == "字节"
    assert unit_for_key("compact_landmark_max_tokens") == "tokens"
    assert unit_for_key("memory_compact_auto_trigger_percent") == "%"
    assert unit_for_key("cli_audit_cleanup_days") == "天"
    assert unit_for_key("model_name") == ""


def test_registry_units_attach_to_real_fields():
    """真实字段的单位进登记表；无单位的键留空，不硬猜。"""
    registry = parameter_registry()
    assert registry["memory_curator_interval_seconds"].unit == "秒"
    assert registry["tool_read_max_chars"].unit == "字符"
    assert registry["model_name"].unit == ""


def test_range_from_validation_specs():
    """范围从现有校验规格取：整数 min-max / ≥min、百分比、枚举；没有校验的留空。"""
    registry = parameter_registry()
    assert registry["request_timeout"].range == "1-600"
    assert registry["max_tokens"].range == "≥1"
    assert registry["memory_curator_interval_seconds"].range == "60-604800"
    assert registry["memory_compact_auto_trigger_percent"].range == "50-100"
    assert registry["top_p"].range == "0-1"
    assert registry["log_level"].range == "debug|info|warning|error|critical"
    assert registry["agent_name"].range == ""


def test_ranges_table_has_no_blank_entries():
    """ranges() 表只含推导成功的键，空文本不会进表。"""
    assert all(text for text in ranges().values())
    assert "request_timeout" in ranges()


def test_every_listed_key_has_a_reader():
    """219 个用户可见参数都有读取方与归属模块（加载器元数据不在其中）；推导表本身不读真实文件正文。"""
    registry = parameter_registry()
    readers = field_readers()
    missing = sorted(
        key for key, spec in registry.items()
        if key not in LOADER_METADATA_KEYS and (not spec.owner_module or not spec.reader))
    assert missing == [], f"这些参数没有推导出读取方/归属模块：{missing}"
    assert not missing and set(readers) >= {key for key in registry if key not in LOADER_METADATA_KEYS}


def test_owner_module_is_top_level_without_slash():
    """归属模块是顶层模块名（不含路径分隔符），读取方是相对文件路径。"""
    for key, spec in parameter_registry().items():
        if key in LOADER_METADATA_KEYS or not spec.owner_module:
            continue
        assert "/" not in spec.owner_module, (key, spec.owner_module)
        assert spec.reader


def _config_with_fields() -> SimpleNamespace:
    """构造带全部 AgentConfig 默认值和一个用户配置文件路径的配置对象（_show 需要 config_path）。"""
    values = {item.name: item.default for item in __import__(
        "dataclasses").fields(__import__("agent_py_agent.agent.settings.config", fromlist=["AgentConfig"]).AgentConfig)}
    values["config_path"] = "/tmp/m-ds2-user.yaml"
    return SimpleNamespace(**values)


def test_show_displays_metadata_only_when_present():
    """/settings show 显示单位/范围/归属模块/读取方；没推导出的不显示对应行。"""
    config = _config_with_fields()
    shown = _show(config, "request_timeout")
    assert "范围：1-600" in shown
    shown_unit = _show(config, "memory_curator_interval_seconds")
    assert "单位：秒" in shown_unit
    bare = _show(config, "model_name")
    assert "范围：" not in bare and "单位：" not in bare


def test_user_config_view_metadata_only_when_present():
    """user_config 的 view 只在有值时带 unit/range/owner_module/reader，避免空字段噪音。"""
    from agent_py_agent.agent.tooling.user_config_tool import _spec_view

    registry = parameter_registry()
    view = _spec_view(registry["request_timeout"], None)
    assert view["range"] == "1-600" and "unit" not in view
    assert view.get("owner_module") and view.get("reader")
    with_unit = _spec_view(registry["memory_curator_interval_seconds"], None)
    assert with_unit["unit"] == "秒"
    plain = _spec_view(registry["agent_name"], None)
    assert "unit" not in plain and "range" not in plain