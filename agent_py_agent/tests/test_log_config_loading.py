"""配置加载测试 - config_loading.py 配置加载、缺省值、格式校验、错误容错。"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.config.config_loading import (
    load_log_analysis_config,
    normalize_log_analysis_config,
)


class TestNormalizeLogAnalysisConfigDefaults:
    """normalize_log_analysis_config 默认值测试。"""

    def test_none_input_returns_defaults(self):
        """验证 None 输入返回默认配置。"""
        config, warnings = normalize_log_analysis_config(None)
        assert config.enabled is False
        assert config.capability_level == "L0"
        assert config.worker_enabled is False
        assert config.auto_dispatch_enabled is False

    def test_empty_dict_returns_defaults(self):
        """验证空字典返回默认配置。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.enabled is False
        assert config.query_default_limit == 100
        assert config.query_max_limit == 1000


class TestNormalizeBoolFields:
    """布尔字段校验测试。"""

    def test_valid_bool_true(self):
        """验证有效的 True 值。"""
        config, warnings = normalize_log_analysis_config({"enabled": True})
        assert config.enabled is True

    def test_valid_bool_false(self):
        """验证有效的 False 值。"""
        config, warnings = normalize_log_analysis_config({"enabled": False})
        assert config.enabled is False

    def test_string_true_coerced(self):
        """验证字符串 "true" 被强制转换。"""
        config, warnings = normalize_log_analysis_config({"enabled": "true"})
        assert config.enabled is True
        assert len(warnings) == 0

    def test_invalid_bool_warns_and_defaults(self):
        """验证无效布尔值产生警告并回退。"""
        config, warnings = normalize_log_analysis_config({"enabled": "not_a_bool"})
        assert config.enabled is False
        assert len(warnings) > 0


class TestNormalizeChoiceFields:
    """枚举字段校验测试。"""

    def test_valid_capability_level(self):
        """验证有效的能力等级。"""
        config, warnings = normalize_log_analysis_config({"capability_level": "L3"})
        assert config.capability_level == "L3"

    def test_capability_level_case_insensitive(self):
        """验证能力等级大小写不敏感。"""
        config, warnings = normalize_log_analysis_config({"capability_level": "l2"})
        assert config.capability_level == "L2"

    def test_invalid_capability_level_warns(self):
        """验证无效能力等级产生警告。"""
        config, warnings = normalize_log_analysis_config({"capability_level": "L9"})
        assert config.capability_level == "L0"
        assert any("capability_level" in str(w.field_name) for w in warnings)

    def test_valid_response_mode(self):
        """验证有效的响应模式。"""
        config, warnings = normalize_log_analysis_config({"response_mode": "dry_run"})
        assert config.response_mode == "dry_run"

    def test_invalid_response_mode_warns(self):
        """验证无效响应模式产生警告。"""
        config, warnings = normalize_log_analysis_config({"response_mode": "invalid"})
        assert config.response_mode == "recommend"

    def test_valid_local_store_backend(self):
        """验证有效的存储后端。"""
        config, warnings = normalize_log_analysis_config({"local_store_backend": "sqlite"})
        assert config.local_store_backend == "sqlite"


class TestNormalizeIntFields:
    """整数字段校验测试。"""

    def test_valid_int(self):
        """验证有效的整数。"""
        config, warnings = normalize_log_analysis_config({"config_version": 2})
        assert config.config_version == 2

    def test_string_int_coerced(self):
        """验证字符串整数被强制转换。"""
        config, warnings = normalize_log_analysis_config({"config_version": "3"})
        assert config.config_version == 3

    def test_negative_value_clamped(self):
        """验证负数被限制为最小值。"""
        config, warnings = normalize_log_analysis_config({"max_parallel_analyst_agents": -5})
        assert config.max_parallel_analyst_agents == 0

    def test_exceeds_max_clamped(self):
        """验证超过最大值被限制。"""
        config, warnings = normalize_log_analysis_config({"config_version": 999})
        assert config.config_version <= 100

    def test_invalid_int_warns_and_defaults(self):
        """验证无效整数产生警告并回退。"""
        config, warnings = normalize_log_analysis_config({"config_version": "bad"})
        assert config.config_version == 1
        assert len(warnings) > 0


class TestNormalizeQueryLimits:
    """查询限制特殊校验测试。"""

    def test_default_limit_valid(self):
        """验证正常默认限制。"""
        config, warnings = normalize_log_analysis_config({
            "query_default_limit": 200,
            "query_max_limit": 5000,
        })
        assert config.query_default_limit == 200
        assert config.query_max_limit == 5000

    def test_default_exceeds_max_warns_and_defaults(self):
        """验证默认超限时产生警告并回退到默认值。"""
        config, warnings = normalize_log_analysis_config({
            "query_default_limit": 5000,
            "query_max_limit": 100,
        })
        assert config.query_default_limit == 100
        assert len(warnings) > 0
        assert any("query_default_limit" in str(w.field_name) for w in warnings)


class TestLoadLogAnalysisConfig:
    """load_log_analysis_config 文件加载测试。"""

    def test_missing_file_with_missing_ok(self, tmp_path):
        """验证文件缺失时返回默认配置。"""
        config = load_log_analysis_config(tmp_path / "nonexistent.yaml", missing_ok=True)
        assert config.enabled is False

    def test_missing_file_raises_without_missing_ok(self, tmp_path):
        """验证文件缺失时抛出异常。"""
        with pytest.raises(FileNotFoundError):
            load_log_analysis_config(tmp_path / "nonexistent.yaml", missing_ok=False)

    @patch("agent_py_agent.agent.log_analysis.config.config_loading.load_simple_yaml")
    @patch("pathlib.Path.exists")
    def test_load_from_file(self, mock_exists, mock_load_yaml, tmp_path):
        """验证从文件加载配置。"""
        mock_exists.return_value = True
        mock_load_yaml.return_value = {"enabled": True, "capability_level": "L2"}
        config = load_log_analysis_config(tmp_path / "config.yaml")
        assert config.enabled is True
        assert config.capability_level == "L2"

    @patch("agent_py_agent.agent.log_analysis.config.config_loading.load_simple_yaml")
    def test_load_with_warnings(self, mock_load_yaml, tmp_path):
        """验证加载时包含警告。"""
        mock_load_yaml.return_value = {"enabled": "yes", "unknown_field": "value"}
        config = load_log_analysis_config(tmp_path / "config.yaml")
        assert hasattr(config, "config_warnings")
        assert isinstance(config.config_warnings, list)


class TestNormalizePathFields:
    """路径字段校验测试。"""

    def test_valid_data_dir(self):
        """验证有效的 data_dir。"""
        config, warnings = normalize_log_analysis_config({"data_dir": "/custom/path"})
        assert config.data_dir == "/custom/path"

    def test_empty_data_dir_warns(self):
        """验证空 data_dir 产生警告。"""
        config, warnings = normalize_log_analysis_config({"data_dir": ""})
        assert config.data_dir == "data/log_analysis"
        assert len(warnings) > 0


class TestCapabilityLevels:
    """能力等级测试。"""

    def test_all_valid_capability_levels(self):
        """验证所有有效能力等级。"""
        for level in ["L0", "L1", "L2", "L3", "L4", "L5"]:
            config, warnings = normalize_log_analysis_config({"capability_level": level})
            assert config.capability_level == level


class TestRetentionAndLimits:
    """保留天数和限制测试。"""

    def test_source_retention_days_range(self):
        """验证保留天数范围。"""
        config, warnings = normalize_log_analysis_config({"source_retention_days": 365})
        assert config.source_retention_days == 365

    def test_source_retention_days_clamped(self):
        """验证保留天数超限时回退到默认值。"""
        config, warnings = normalize_log_analysis_config({"source_retention_days": 9999})
        assert config.source_retention_days == 30
        assert len(warnings) > 0

    def test_detector_window_minutes(self):
        """验证检测器窗口分钟数。"""
        config, warnings = normalize_log_analysis_config({"detector_window_minutes": 60})
        assert config.detector_window_minutes == 60


class TestHighRiskCapabilities:
    """高风险能力默认关闭测试。"""

    def test_ml_enabled_default_off(self):
        """验证 ML 能力默认关闭。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.ml_enabled is False

    def test_cluster_enabled_default_off(self):
        """验证集群能力默认关闭。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.cluster_enabled is False

    def test_response_execution_enabled_default_off(self):
        """验证响应执行能力默认关闭。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.response_execution_enabled is False

    def test_auto_dispatch_enabled_default_off(self):
        """验证自动派工能力默认关闭。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.auto_dispatch_enabled is False

    def test_worker_enabled_default_off(self):
        """验证 worker 能力默认关闭。"""
        config, warnings = normalize_log_analysis_config({})
        assert config.worker_enabled is False


class TestMultipleWarnings:
    """多警告场景测试。"""

    def test_multiple_bad_values(self):
        """验证多个坏值产生多个警告。"""
        config, warnings = normalize_log_analysis_config({
            "enabled": "not_bool",
            "capability_level": "INVALID",
            "config_version": "bad",
            "query_default_limit": "not_int",
        })
        assert len(warnings) >= 3
