"""capability_config 模块测试。

测试 CapabilityConfig 数据类和 load_capability_config 函数。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config

# ── CapabilityConfig 默认值测试 ───────────────────────────────────────────

def test_capability_config_defaults():
    """测试 CapabilityConfig 默认值。"""
    config = CapabilityConfig()
    assert config.enable_capability_routing is False
    assert config.capability_request_max_tokens == 600
    assert config.capability_escalation_max_hops == 4
    assert config.capability_candidate_limit == 5
    assert config.capability_bundle_max_tokens == 3000
    assert config.capability_fallback_max_attempts == 3
    assert config.subagent_heartbeat_timeout == 180
    assert config.subagent_run_timeout == 900
    assert config.subagent_due_check_interval == 120
    assert config.subagent_min_evidence_for_done == 1


def test_capability_config_custom_values():
    """测试 CapabilityConfig 自定义值。"""
    config = CapabilityConfig(
        enable_capability_routing=True,
        capability_request_max_tokens=1200,
        capability_escalation_max_hops=8,
        subagent_run_timeout=1800,
    )
    assert config.enable_capability_routing is True
    assert config.capability_request_max_tokens == 1200
    assert config.capability_escalation_max_hops == 8
    assert config.subagent_run_timeout == 1800


def test_capability_config_zero_values():
    """测试零值配置。"""
    config = CapabilityConfig(
        capability_request_max_tried_items=0,
        capability_request_max_evidence_items=0,
        capability_grant_max_skills=0,
        capability_grant_max_tools=0,
    )
    assert config.capability_request_max_tried_items == 0
    assert config.capability_request_max_evidence_items == 0
    assert config.capability_grant_max_skills == 0
    assert config.capability_grant_max_tools == 0


# ── load_capability_config 测试 ───────────────────────────────────────────

def test_load_capability_config_file_not_found():
    """测试加载不存在的配置文件抛出异常。"""
    with pytest.raises(FileNotFoundError, match="能力路由配置文件不存在"):
        load_capability_config("/nonexistent/path/config.yaml")


def test_load_capability_config_success(tmp_path):
    """测试成功加载配置文件。"""
    config_file = tmp_path / "capability.yaml"
    config_file.write_text(
        "enable_capability_routing: true\n"
        "capability_request_max_tokens: 800\n"
        "capability_escalation_max_hops: 6\n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)

    assert config.enable_capability_routing is True
    assert config.capability_request_max_tokens == 800
    assert config.capability_escalation_max_hops == 6


def test_load_capability_config_partial(tmp_path):
    """测试加载部分配置项。"""
    config_file = tmp_path / "partial.yaml"
    config_file.write_text(
        "enable_capability_routing: true\n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)

    # 指定的值
    assert config.enable_capability_routing is True
    # 未指定的用默认值
    assert config.capability_request_max_tokens == 600
    assert config.capability_escalation_max_hops == 4


def test_load_capability_config_ignores_unknown_fields(tmp_path):
    """测试加载时忽略未知字段。"""
    config_file = tmp_path / "unknown.yaml"
    config_file.write_text(
        "enable_capability_routing: true\n"
        "unknown_field_xyz: 123\n"
        "another_unknown: test\n",
        encoding="utf-8",
    )

    # 不应抛出异常
    config = load_capability_config(config_file)
    assert config.enable_capability_routing is True


def test_load_capability_config_empty_file(tmp_path):
    """测试加载空配置文件。"""
    config_file = tmp_path / "empty.yaml"
    config_file.write_text("", encoding="utf-8")

    config = load_capability_config(config_file)

    # 空文件使用所有默认值
    assert config.enable_capability_routing is False
    assert config.capability_request_max_tokens == 600


def test_load_capability_config_path_object(tmp_path):
    """测试接受 Path 对象作为参数。"""
    config_file = tmp_path / "path_object.yaml"
    config_file.write_text("capability_candidate_limit: 10\n", encoding="utf-8")

    config = load_capability_config(config_file)
    assert config.capability_candidate_limit == 10


# ── 配置合并测试 ──────────────────────────────────────────────────────────

def test_capability_config_boolean_override(tmp_path):
    """测试布尔配置项覆盖。"""
    config_file = tmp_path / "bool.yaml"
    config_file.write_text(
        "enable_capability_routing: false\n"
        "capability_grant_expires_after_task: false\n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)

    assert config.enable_capability_routing is False
    assert config.capability_grant_expires_after_task is False


def test_capability_config_integer_fields(tmp_path):
    """测试所有整数字段。"""
    config_file = tmp_path / "integers.yaml"
    config_file.write_text(
        "capability_request_max_tokens: 1000\n"
        "capability_escalation_max_hops: 10\n"
        "capability_candidate_limit: 20\n"
        "capability_bundle_max_tokens: 5000\n"
        "capability_fallback_max_attempts: 5\n"
        "subagent_heartbeat_timeout: 300\n"
        "subagent_run_timeout: 3600\n"
        "subagent_due_check_interval: 60\n"
        "subagent_min_evidence_for_done: 3\n"
        "capability_request_max_tried_items: 10\n"
        "capability_request_max_evidence_items: 20\n"
        "capability_request_max_per_task: 50\n"
        "capability_grant_max_skills: 15\n"
        "capability_grant_max_tools: 25\n"
        "skill_card_max_tokens: 2000\n"
        "tool_card_max_tokens: 1500\n"
        "skill_body_max_tokens: 8000\n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)

    assert config.capability_request_max_tokens == 1000
    assert config.capability_escalation_max_hops == 10
    assert config.capability_candidate_limit == 20
    assert config.capability_bundle_max_tokens == 5000
    assert config.skill_body_max_tokens == 8000


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_capability_config_with_string_integers(tmp_path):
    """测试字符串形式的整数值（YAML 会自动转换）。"""
    config_file = tmp_path / "string_int.yaml"
    config_file.write_text(
        "capability_request_max_tokens: '800'\n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)
    assert config.capability_request_max_tokens == 800


def test_capability_config_whitespace_values(tmp_path):
    """测试值前后有空白字符时的处理。"""
    config_file = tmp_path / "whitespace.yaml"
    config_file.write_text(
        "enable_capability_routing:   true  \n"
        "capability_request_max_tokens:   800  \n",
        encoding="utf-8",
    )

    config = load_capability_config(config_file)
    assert config.enable_capability_routing is True
    assert config.capability_request_max_tokens == 800