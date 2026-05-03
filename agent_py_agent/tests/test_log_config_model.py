"""日志分析配置模型测试 - config_model.py 配置数据类、默认值、路径解析。"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_py_agent.agent.log_analysis.config.config_model import (
    LogAnalysisConfigWarning,
    LogAnalysisConfig,
    default_log_analysis_config_path,
    default_log_analysis_workspace_root,
    resolve_log_analysis_data_dir,
)


class TestLogAnalysisConfigWarning:
    """LogAnalysisConfigWarning 测试。"""

    def test_warning_fields(self):
        """验证警告字段。"""
        warning = LogAnalysisConfigWarning(
            field_name="enabled",
            raw_value="yes",
            fallback_value=False,
            reason="expected boolean",
        )
        assert warning.field_name == "enabled"
        assert warning.raw_value == "yes"
        assert warning.fallback_value is False
        assert warning.reason == "expected boolean"

    def test_warning_to_dict(self):
        """验证转换为字典。"""
        warning = LogAnalysisConfigWarning(
            field_name="field",
            raw_value="bad",
            fallback_value=0,
            reason="invalid",
        )
        d = warning.to_dict()
        assert d["field_name"] == "field"
        assert d["raw_value"] == "bad"
        assert d["fallback_value"] == 0
        assert d["reason"] == "invalid"


class TestLogAnalysisConfigDefaults:
    """LogAnalysisConfig 默认值测试。"""

    def test_defaults_disabled(self):
        """验证高风险能力默认关闭。"""
        config = LogAnalysisConfig()
        assert config.enabled is False
        assert config.worker_enabled is False
        assert config.security_prompt_enabled is False
        assert config.auto_dispatch_enabled is False
        assert config.ml_enabled is False
        assert config.cluster_enabled is False
        assert config.response_execution_enabled is False

    def test_defaults_capability_level(self):
        """验证能力等级默认 L0。"""
        config = LogAnalysisConfig()
        assert config.capability_level == "L0"

    def test_defaults_data_dir(self):
        """验证数据目录默认路径。"""
        config = LogAnalysisConfig()
        assert config.data_dir == "data/log_analysis"

    def test_defaults_response_mode(self):
        """验证响应模式默认 recommend。"""
        config = LogAnalysisConfig()
        assert config.response_mode == "recommend"

    def test_defaults_local_store_backend(self):
        """验证本地存储后端默认 jsonl。"""
        config = LogAnalysisConfig()
        assert config.local_store_backend == "jsonl"

    def test_defaults_query_limits(self):
        """验证查询限制默认值。"""
        config = LogAnalysisConfig()
        assert config.query_default_limit == 100
        assert config.query_max_limit == 1000

    def test_defaults_retention(self):
        """验证保留天数默认 30 天。"""
        config = LogAnalysisConfig()
        assert config.source_retention_days == 30

    def test_defaults_payload_preview(self):
        """验证 payload 预览默认 2048 字符。"""
        config = LogAnalysisConfig()
        assert config.payload_preview_max_chars == 2048

    def test_defaults_parallel_agents(self):
        """验证并行 agent 默认 0（关闭）。"""
        config = LogAnalysisConfig()
        assert config.max_parallel_analyst_agents == 0

    def test_defaults_dispatch_budget(self):
        """验证派工预算默认 0。"""
        config = LogAnalysisConfig()
        assert config.dispatch_budget_per_hour == 0

    def test_defaults_windows(self):
        """验证时间窗口默认值。"""
        config = LogAnalysisConfig()
        assert config.case_merge_window_minutes == 60
        assert config.detector_window_minutes == 15

    def test_defaults_config_warnings(self):
        """验证警告列表默认为空列表。"""
        config = LogAnalysisConfig()
        assert config.config_warnings == []


class TestLogAnalysisConfigWithValues:
    """LogAnalysisConfig 自定义值测试。"""

    def test_custom_enabled(self):
        """验证自定义启用状态。"""
        config = LogAnalysisConfig(enabled=True)
        assert config.enabled is True

    def test_custom_capability_level(self):
        """验证自定义能力等级。"""
        config = LogAnalysisConfig(capability_level="L3")
        assert config.capability_level == "L3"

    def test_custom_data_dir(self):
        """验证自定义数据目录。"""
        config = LogAnalysisConfig(data_dir="/custom/path")
        assert config.data_dir == "/custom/path"

    def test_custom_query_limits(self):
        """验证自定义查询限制。"""
        config = LogAnalysisConfig(query_default_limit=200, query_max_limit=5000)
        assert config.query_default_limit == 200
        assert config.query_max_limit == 5000

    def test_custom_config_warnings(self):
        """验证自定义警告列表。"""
        warnings = [{"field_name": "test", "raw_value": "bad", "fallback_value": 0, "reason": "test"}]
        config = LogAnalysisConfig(config_warnings=warnings)
        assert len(config.config_warnings) == 1


class TestDefaultPaths:
    """默认路径函数测试。"""

    def test_default_config_path(self):
        """验证默认配置文件路径。"""
        path = default_log_analysis_config_path()
        assert isinstance(path, Path)
        assert path.name == "log_analysis_config.yaml"

    def test_default_workspace_root(self):
        """验证默认工作区根目录。"""
        root = default_log_analysis_workspace_root()
        assert isinstance(root, Path)

    def test_resolve_absolute_path(self, tmp_path: Path):
        """绝对路径直接返回。"""
        absolute = tmp_path / "absolute" / "path"
        result = resolve_log_analysis_data_dir(str(absolute))
        assert result == absolute

    def test_resolve_relative_path(self, tmp_path: Path):
        """相对路径按 workspace_root 解析。"""
        relative = "data/logs"
        result = resolve_log_analysis_data_dir(relative, workspace_root=tmp_path)
        assert result == tmp_path / relative

    def test_resolve_no_workspace_uses_default(self):
        """未指定 workspace 使用默认根目录。"""
        relative = "data/logs"
        result = resolve_log_analysis_data_dir(relative)
        assert isinstance(result, Path)

    def test_resolve_expands_user(self, tmp_path: Path):
        """~ 展开为用户目录。"""
        result = resolve_log_analysis_data_dir("~/test", workspace_root=tmp_path)
        assert "~" not in str(result)


class TestConfigWarningsToDict:
    """配置警告序列化测试。"""

    def test_warnings_survive_round_trip(self):
        """警告能正确序列化/反序列化。"""
        config = LogAnalysisConfig(config_warnings=[{"field_name": "x", "raw_value": 1, "fallback_value": 0, "reason": "y"}])
        d = {
            "enabled": config.enabled,
            "config_warnings": config.config_warnings,
        }
        assert len(d["config_warnings"]) == 1
        assert d["config_warnings"][0]["field_name"] == "x"
