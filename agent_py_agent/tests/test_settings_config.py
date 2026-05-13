"""Tests for settings/config.py: config loading, field validation, and default value handling.

给人看的解释：
测试配置模块：配置加载、字段验证、默认值处理。
"""
import logging
import tempfile
from pathlib import Path

import pytest

from agent_py_agent.agent.settings.config import (
    AgentConfig,
    load_config,
    load_simple_yaml,
    normalize_agent_config,
    normalize_subagent_workflow_config,
    parse_scalar,
)


class TestParseScalar:
    """测试 parse_scalar 标量解析。"""

    def test_parse_scalar_true(self):
        """验证解析 true。"""
        assert parse_scalar("true") is True
        assert parse_scalar("True") is True
        assert parse_scalar("TRUE") is True

    def test_parse_scalar_false(self):
        """验证解析 false。"""
        assert parse_scalar("false") is False
        assert parse_scalar("False") is False

    def test_parse_scalar_integer(self):
        """验证解析整数。"""
        assert parse_scalar("42") == 42
        assert parse_scalar("-10") == -10

    def test_parse_scalar_string(self):
        """验证字符串原样返回。"""
        assert parse_scalar("hello") == "hello"
        assert parse_scalar("hello world") == "hello world"

    def test_parse_scalar_quoted_string(self):
        """验证带引号字符串去除引号。"""
        assert parse_scalar('"hello"') == "hello"
        assert parse_scalar("'world'") == "world"

    def test_parse_scalar_inline_list(self):
        """Inline lists are convenient for workspace_root."""
        assert parse_scalar('["", "C:/work"]') == ["", "C:/work"]


class TestLoadSimpleYaml:
    """测试 load_simple_yaml 简化 YAML 加载。"""

    def test_load_simple_yaml_basic(self):
        """验证基本 key-value 加载。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("key1: value1\nkey2: value2\n")
            f.flush()
            path = Path(f.name)

        try:
            data = load_simple_yaml(path)
            assert data["key1"] == "value1"
            assert data["key2"] == "value2"
        finally:
            path.unlink()

    def test_load_simple_yaml_with_list(self):
        """验证列表项加载。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("items:\n  - item1\n  - item2\n  - item3\n")
            f.flush()
            path = Path(f.name)

        try:
            data = load_simple_yaml(path)
            assert data["items"] == ["item1", "item2", "item3"]
        finally:
            path.unlink()

    def test_load_simple_yaml_with_inline_list(self):
        """Inline list syntax should work for compact config values."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write('workspace_root: ["", "C:/work"]\n')
            f.flush()
            path = Path(f.name)

        try:
            data = load_simple_yaml(path)
            assert data["workspace_root"] == ["", "C:/work"]
        finally:
            path.unlink()

    def test_load_simple_yaml_with_integer_values(self):
        """验证整数值加载。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("count: 42\nenabled: true\n")
            f.flush()
            path = Path(f.name)

        try:
            data = load_simple_yaml(path)
            assert data["count"] == 42
            assert data["enabled"] is True
        finally:
            path.unlink()

    def test_load_simple_yaml_with_comments(self):
        """验证注释被忽略。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("key1: value1  # comment\nkey2: value2\n")
            f.flush()
            path = Path(f.name)

        try:
            data = load_simple_yaml(path)
            assert data["key1"] == "value1"
            assert data["key2"] == "value2"
        finally:
            path.unlink()


    def test_load_simple_yaml_accepts_utf8_bom(self, tmp_path: Path):
        """PowerShell-created UTF-8 config files may include a BOM."""
        path = tmp_path / "agent_config.yaml"
        path.write_bytes(b"\xef\xbb\xbfworkspace_root: workspace\n")

        data = load_simple_yaml(path)

        assert data["workspace_root"] == "workspace"


class TestNormalizeAgentConfig:
    """测试 normalize_agent_config 配置归一化。"""

    def test_normalize_agent_config_valid_data(self):
        """验证有效数据保持不变。"""
        data = {
            "model_backend": "echo",
            "max_tool_rounds": 10,
            "memory_top_k": 5,
        }
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "echo"
        assert normalized["max_tool_rounds"] == 10
        assert len(warnings) == 0

    def test_normalize_agent_config_invalid_model_backend(self):
        """验证无效 model_backend 回退到默认值。"""
        data = {"model_backend": "invalid_backend"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["model_backend"] == "echo"
        assert len(warnings) > 0

    def test_normalize_agent_config_negative_tool_rounds(self):
        """验证负数 tool_rounds 回退到默认值。"""
        data = {"max_tool_rounds": -5}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["max_tool_rounds"] == 0  # 默认值，0 表示不限制
        assert len(warnings) > 0

    # LLM: Tool write inline limits must be user-configurable through the standard config normalizer.
    # 函数用途: 验证用户能通过配置调整 write_file/append_file 单次正文上限。
    def test_normalize_agent_config_tool_write_inline_max_chars(self):
        data = {"tool_write_inline_max_chars": 16384}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["tool_write_inline_max_chars"] == 16384
        assert warnings == []

    # LLM: Tool catalog prompt budgets must be real config fields, not hidden registry constants.
    # 函数用途: 验证工具目录分页、模式、类别和展示上限能通过配置归一化。
    def test_normalize_agent_config_tool_catalog_fields(self):
        data = {
            "tool_catalog_mode": "full",
            "tool_catalog_offset": 2,
            "tool_catalog_categories": ["filesystem", "shell"],
            "tool_catalog_include_examples": False,
            "tool_catalog_entry_max_chars": 900,
            "tool_catalog_show_truncated_notice": False,
            "tool_detail_max_chars": 3000,
        }
        normalized, warnings = normalize_agent_config(data)
        assert normalized["tool_catalog_mode"] == "full"
        assert normalized["tool_catalog_offset"] == 2
        assert normalized["tool_catalog_categories"] == ["filesystem", "shell"]
        assert normalized["tool_catalog_include_examples"] is False
        assert normalized["tool_catalog_entry_max_chars"] == 900
        assert normalized["tool_catalog_show_truncated_notice"] is False
        assert normalized["tool_detail_max_chars"] == 3000
        assert warnings == []

    # LLM: Invalid inline write limits should fall back before reaching ToolRegistry.
    # 函数用途: 验证过小的写入正文上限会回退默认值，并产生配置告警。
    def test_normalize_agent_config_invalid_tool_write_inline_max_chars(self):
        data = {"tool_write_inline_max_chars": 10}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["tool_write_inline_max_chars"] == AgentConfig().tool_write_inline_max_chars
        assert len(warnings) > 0

    def test_normalize_agent_config_temperature_out_of_range(self):
        """验证超出范围的 temperature 回退到默认值。"""
        data = {"temperature": "5.0"}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["temperature"] == "0.2"  # 默认值
        assert len(warnings) > 0

    def test_normalize_agent_config_gateway_port_valid(self):
        """验证有效的 gateway_port。"""
        data = {"gateway_port": 8000}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["gateway_port"] == 8000
        assert len(warnings) == 0

    def test_normalize_agent_config_gateway_port_out_of_range(self):
        """验证超出范围的 gateway_port 回退到默认值。"""
        data = {"gateway_port": 99999}
        normalized, warnings = normalize_agent_config(data)
        assert normalized["gateway_port"] == 8420  # 默认值
        assert len(warnings) > 0


class TestNormalizeSubagentWorkflowConfig:
    """测试 normalize_subagent_workflow_config 子代理工作流配置归一化。"""

    def test_normalize_workflow_mode_auto(self):
        """验证 auto 模式保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "auto"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "auto"
        assert len(warnings) == 0

    def test_normalize_workflow_mode_manual(self):
        """验证 manual 模式保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "manual"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "manual"
        assert len(warnings) == 0

    def test_normalize_workflow_mode_invalid(self):
        """验证无效 workflow_mode 回退到默认值。"""
        config = AgentConfig()
        config.subagent_workflow_mode = "invalid"
        warnings = normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_mode == "auto"  # 默认值
        assert len(warnings) > 0

    def test_normalize_builtin_workflows_valid_bool(self):
        """验证有效的 builtin_workflows 保持不变。"""
        config = AgentConfig()
        config.subagent_builtin_workflows = False
        normalize_subagent_workflow_config(config)
        assert config.subagent_builtin_workflows is False

    def test_normalize_review_rounds_valid(self):
        """验证有效的 review_rounds 保持不变。"""
        config = AgentConfig()
        config.subagent_workflow_review_rounds = 3
        normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_review_rounds == 3

    def test_normalize_review_rounds_out_of_range(self):
        """验证超出范围的 review_rounds 回退到默认值。"""
        config = AgentConfig()
        config.subagent_workflow_review_rounds = 10
        normalize_subagent_workflow_config(config)
        assert config.subagent_workflow_review_rounds == 1  # 默认值


class TestLoadConfig:
    """测试 load_config 完整配置加载。"""

    def test_load_config_file_not_found(self):
        """验证文件不存在时抛出异常。"""
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path/config.yaml")

    def test_load_config_with_valid_yaml(self):
        """验证加载有效 YAML 配置。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model_backend: echo\nmax_tool_rounds: 10\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_config(path)
            assert config.model_backend == "echo"
            assert config.max_tool_rounds == 10
        finally:
            path.unlink()

    # LLM: User config files should be able to tune write/append inline transport limits.
    # 函数用途: 验证完整 load_config 会保留用户写入的 tool_write_inline_max_chars 配置项。
    def test_load_config_with_tool_write_inline_max_chars(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model_backend: echo\ntool_write_inline_max_chars: 16000\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_config(path)
            assert config.tool_write_inline_max_chars == 16000
        finally:
            path.unlink()

    def test_load_config_applies_log_level(self):
        """验证 log_level 配置加载后会影响包内 logger。"""
        logger = logging.getLogger("agent_py_agent")
        previous_level = logger.level
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model_backend: echo\nlog_level: debug\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_config(path)
            assert config.log_level == "debug"
            assert logger.level == logging.DEBUG
        finally:
            logger.setLevel(previous_level)
            path.unlink()

    def test_load_config_with_invalid_field(self):
        """验证无效字段被忽略。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model_backend: echo\ninvalid_field: value\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_config(path)
            assert config.model_backend == "echo"
            # invalid_field 应该被忽略
            assert not hasattr(config, "invalid_field") or config.invalid_field is None
        finally:
            path.unlink()

    def test_load_config_applies_memory_normalization(self):
        """验证加载时应用 memory 配置归一化。"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("model_backend: echo\n")
            f.flush()
            path = Path(f.name)

        try:
            config = load_config(path)
            # memory 配置应该有安全默认值
            assert config.memory_archive_level in [0, 1, 2, 3]
            assert config.memory_hook_enabled in [True, False]
        finally:
            path.unlink()


class TestAgentConfigDefaults:
    """测试 AgentConfig 默认值。"""

    def test_agent_config_default_values(self):
        """验证默认配置值。"""
        config = AgentConfig()
        assert config.model_backend == "echo"
        assert config.max_tool_rounds == 0
        assert config.memory_top_k == 5
        assert config.max_subagents == 1000

    def test_agent_config_dispatch_defaults(self):
        """验证 dispatch 相关默认值。"""
        config = AgentConfig()
        assert config.dispatch_max_consecutive_rounds == 20
        assert config.dispatch_active_interval == 5
        assert config.dispatch_idle_interval == 30

    def test_agent_config_watchdog_defaults(self):
        """验证 watchdog 相关默认值。"""
        config = AgentConfig()
        assert config.watchdog_enabled is False
        assert config.watchdog_interval == 60
        assert config.watchdog_max_restarts == 3
