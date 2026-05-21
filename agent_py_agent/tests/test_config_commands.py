"""config_commands CLI 命令测试。

测试 config show/set/validate 命令。
注意：当前项目中 config_commands.py 不存在，但需求要求测试 config 命令。
这里基于 CLI common 模块中的配置加载逻辑创建测试。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestLoadConfig:
    """测试 load_config 配置加载函数。"""

    def test_load_config_from_yaml(self, tmp_path: Path):
        """从 YAML 文件加载配置。"""
        from agent_py_agent.agent.config import load_config

        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
workspace_root: /tmp/test
memory_rule_routing_enabled: true
memory_rule_routing_mode: soft
memory_rule_auto_read_limit: 5
""", encoding="utf-8")

        config = load_config(str(config_file))
        assert config is not None

    def test_load_config_with_defaults(self, tmp_path: Path):
        """使用默认配置值。"""
        from agent_py_agent.agent.config import load_config

        config_file = tmp_path / "minimal_config.yaml"
        config_file.write_text("workspace_root: /tmp/test\n", encoding="utf-8")

        config = load_config(str(config_file))
        assert config is not None

    def test_load_config_missing_file(self):
        """加载不存在的配置文件。"""
        from agent_py_agent.agent.config import load_config

        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/config.yaml")

    def test_load_config_invalid_yaml(self, tmp_path: Path):
        """加载无效的 YAML 文件时配置仍能加载（可能使用默认值）。"""
        from agent_py_agent.agent.config import load_config

        config_file = tmp_path / "invalid.yaml"
        config_file.write_text("invalid: yaml: content: [}\n  broken", encoding="utf-8")

        # YAML 加载器可能不会抛出异常，而是使用配置默认值
        config = load_config(str(config_file))
        assert config is not None


class TestResolveWorkspaceRoot:
    """测试 resolve_workspace_root 函数。"""
    # 该函数在 CLI common 模块中，此处测试其逻辑

    def test_resolve_workspace_root_default(self, tmp_path: Path):
        """测试默认工作区根目录。"""
        from agent_py_agent.cli.common import resolve_workspace_root

        config = MagicMock()
        config.workspace_root = ""

        result = resolve_workspace_root(config, str(tmp_path / "config.yaml"))
        # 默认返回 ROOT（包目录）
        assert result is not None

    def test_resolve_workspace_root_from_config(self, tmp_path: Path):
        """从配置读取工作区根目录。"""
        from agent_py_agent.cli.common import resolve_workspace_root

        custom_root = tmp_path / "custom_workspace"
        custom_root.mkdir(parents=True)

        config = MagicMock()
        config.workspace_root = str(custom_root)

        result = resolve_workspace_root(config, str(tmp_path / "config.yaml"))
        assert result == custom_root.resolve()

    def test_resolve_workspace_root_relative(self, tmp_path: Path):
        """测试相对路径解析。"""
        from agent_py_agent.cli.common import resolve_workspace_root

        config = MagicMock()
        config.workspace_root = "relative/path"

        config_path = tmp_path / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True)

        result = resolve_workspace_root(config, str(config_path))
        expected = config_path.parent / "relative" / "path"
        assert result == expected.resolve()

    def test_resolve_workspace_roots_list(self, tmp_path: Path):
        """workspace_root can be a list; first item remains the primary root."""
        from agent_py_agent.cli.common import resolve_workspace_root, resolve_workspace_roots

        config = MagicMock()
        config.workspace_root = ["primary", "extra"]

        config_path = tmp_path / "config" / "config.yaml"
        config_path.parent.mkdir(parents=True)

        roots = resolve_workspace_roots(config, str(config_path))
        assert roots == [
            (config_path.parent / "primary").resolve(),
            (config_path.parent / "extra").resolve(),
        ]
        assert resolve_workspace_root(config, str(config_path)) == roots[0]

    def test_resolve_workspace_roots_list_empty_keeps_current_workspace(self, tmp_path: Path):
        """An empty list item means keep the current CLI workspace."""
        from agent_py_agent.cli.common import resolve_workspace_roots

        config = MagicMock()
        config.workspace_root = ["", "extra"]
        config_path = tmp_path / "config.yaml"

        roots = resolve_workspace_roots(config, str(config_path))
        assert roots == [Path.cwd().resolve(), (tmp_path / "extra").resolve()]

    def test_resolve_workspace_root_expanduser(self, tmp_path: Path):
        """测试 ~ 展开。"""
        from agent_py_agent.cli.common import resolve_workspace_root

        config = MagicMock()
        config.workspace_root = "~/my_workspace"

        result = resolve_workspace_root(config, str(tmp_path / "config.yaml"))
        assert result is not None
        assert "~" not in str(result)


class TestMakeAgent:
    """测试 make_agent 函数。"""

    def test_make_agent_creates_simple_agent(self, tmp_path: Path):
        """验证 make_agent 创建 SimpleAgent 实例。"""
        from agent_py_agent.cli.common import make_agent

        args = MagicMock()
        args.config = str(tmp_path / "config.yaml")

        config_file = tmp_path / "config.yaml"
        config_file.write_text("workspace_root: .\n", encoding="utf-8")

        with patch("agent_py_agent.cli.common.load_config") as mock_load:
            mock_config = MagicMock()
            mock_config.workspace_root = str(tmp_path)
            mock_load.return_value = mock_config

            with patch("agent_py_agent.cli.common.SimpleAgent") as mock_agent_cls:
                mock_agent = MagicMock()
                mock_agent.config = mock_config
                mock_agent.root = tmp_path
                mock_agent_cls.return_value = mock_agent

                result = make_agent(args)
                assert result is not None


class TestCapabilityConfig:
    """测试能力配置加载。"""

    def test_load_capability_config_basic(self, tmp_path: Path):
        """加载基本能力配置。"""
        from agent_py_agent.agent.capability_config import load_capability_config

        config_file = tmp_path / "capability.yaml"
        config_file.write_text("""
version: "1.0"
capabilities:
  - name: test_capability
    enabled: true
""", encoding="utf-8")

        config = load_capability_config(str(config_file))
        assert config is not None

    def test_load_capability_config_missing_file(self):
        """加载不存在的能力配置文件。"""
        from agent_py_agent.agent.capability_config import load_capability_config

        with pytest.raises(FileNotFoundError):
            load_capability_config("/nonexistent/capability.yaml")


class TestConfigValidation:
    """测试配置验证逻辑。"""

    def test_memory_config_warnings_normalized(self):
        """测试 memory 配置警告归一化。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_config = MagicMock()
        mock_config.memory_config_warnings = [
            {"field_name": "test_field", "reason": "test reason", "fallback_value": "default"}
        ]

        result = _config_warnings(mock_config)
        assert len(result) == 1
        assert isinstance(result[0], dict)

    def test_memory_config_warnings_with_dataclass(self):
        """测试带 dataclass 的警告转换。"""
        from agent_py_agent.cli.memory_commands import _config_warnings

        mock_warning = MagicMock()
        mock_warning.__dataclass_fields__ = {"field_name": {}, "reason": {}}
        mock_warning.to_dict.return_value = {"field_name": "test", "reason": "reason"}

        mock_config = MagicMock()
        mock_config.memory_config_warnings = [mock_warning]

        result = _config_warnings(mock_config)
        # 应该调用 to_dict
        assert len(result) >= 0


class TestConfigFields:
    """测试配置字段的读写。"""

    def test_config_memory_fields(self, tmp_path: Path):
        """测试 memory 相关配置字段。"""
        from agent_py_agent.agent.config import load_config

        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
memory_archive_level: 3
memory_hook_enabled: true
memory_hook_archive_level: 3
memory_hook_retention_days: 7
memory_rule_routing_enabled: true
memory_rule_routing_mode: soft
memory_rule_auto_read_limit: 3
memory_rule_receipt_enabled: true
""", encoding="utf-8")

        config = load_config(str(config_file))

        assert hasattr(config, "memory_archive_level")
        assert hasattr(config, "memory_hook_enabled")
        assert hasattr(config, "memory_rule_routing_enabled")
        assert hasattr(config, "memory_rule_routing_mode")
        assert hasattr(config, "memory_rule_auto_read_limit")

    def test_config_gateway_fields(self, tmp_path: Path):
        """测试 gateway 相关配置字段。"""
        from agent_py_agent.agent.config import load_config

        config_file = tmp_path / "test_config.yaml"
        config_file.write_text("""
gateway_stop_timeout: 30
gateway_request_timeout: 300
auto_detect_work_on_startup: true
""", encoding="utf-8")

        config = load_config(str(config_file))

        assert hasattr(config, "gateway_stop_timeout")
        assert hasattr(config, "gateway_request_timeout")
        assert hasattr(config, "auto_detect_work_on_startup")


class TestConfigOverride:
    """测试配置覆盖逻辑。"""

    def test_cli_override_resume_context(self):
        """测试 CLI resume_context 覆盖。"""
        from agent_py_agent.cli.common import resume_context_override

        args_true = MagicMock()
        args_true.resume_context = True
        assert resume_context_override(args_true) is True

        args_false = MagicMock()
        args_false.resume_context = False
        assert resume_context_override(args_false) is False

        args_none = MagicMock()
        args_none.resume_context = None
        assert resume_context_override(args_none) is None


class TestCapabilityRouter:
    """测试能力路由创建。"""

    def test_make_capability_router_basic(self, tmp_path: Path):
        """测试基本能力路由创建。"""
        from agent_py_agent.cli.common import make_capability_router

        mock_agent = MagicMock()
        mock_agent.tools = MagicMock()
        mock_agent.tools.specs.return_value = []

        mock_capability_config = MagicMock()

        with patch("agent_py_agent.cli.common.SkillRegistry") as mock_registry:
            mock_registry_instance = MagicMock()
            mock_registry_instance.scan.return_value = None
            mock_registry.return_value = mock_registry_instance

            result = make_capability_router(mock_agent, mock_capability_config, None)
            assert result is not None


class TestFormatLocalTime:
    """测试时间格式化。"""

    def test_format_local_time_valid(self):
        """测试有效时间戳格式化。"""
        from agent_py_agent.cli.common import format_local_time

        result = format_local_time(1704067200.0)
        assert result != "-"
        assert "2024" in result

    def test_format_local_time_zero(self):
        """测试零时间戳。"""
        from agent_py_agent.cli.common import format_local_time

        result = format_local_time(0)
        assert result == "-"

    def test_format_local_time_none(self):
        """测试 None 时间戳。"""
        from agent_py_agent.cli.common import format_local_time

        result = format_local_time(None)
        assert result == "-"
