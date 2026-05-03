"""doctor 模块测试。

测试 collect_doctor_status、_path_status 等函数。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.log_analysis.doctor import (
    RUNTIME_DIRS,
    _path_status,
    collect_doctor_status,
    doctor_status,
    get_log_analysis_status,
)

# ── _path_status 测试 ───────────────────────────────────────────────────────

def test_path_status_exists_file(tmp_path):
    """测试存在的文件。"""
    file_path = tmp_path / "test.txt"
    file_path.write_text("content")

    result = _path_status(file_path)

    assert result["path"] == str(file_path)
    assert result["exists"] is True
    assert result["is_dir"] is False


def test_path_status_exists_directory(tmp_path):
    """测试存在的目录。"""
    dir_path = tmp_path / "test_dir"
    dir_path.mkdir()

    result = _path_status(dir_path)

    assert result["exists"] is True
    assert result["is_dir"] is True


def test_path_status_not_exists():
    """测试不存在的路径。"""
    result = _path_status(Path("/nonexistent/path/xyz"))

    assert result["exists"] is False
    assert result["is_dir"] is False


# ── collect_doctor_status 基本测试 ─────────────────────────────────────────

def test_collect_doctor_status_default_config():
    """测试使用默认配置。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = False
        mock_config.ml_enabled = False
        mock_config.cluster_enabled = False
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {"gate1": True}

                result = collect_doctor_status()

    assert result["module"] == "log_analysis"
    assert result["state"] == "enabled"
    assert result["enabled"] is True
    assert result["capability_level"] == "full"
    assert result["heavy_dependencies_loaded"] is False


def test_collect_doctor_status_disabled_module():
    """测试禁用的模块。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = False
        mock_config.capability_level = "minimal"
        mock_config.config_warnings = []
        mock_config.worker_enabled = False
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = False
        mock_config.ml_enabled = False
        mock_config.cluster_enabled = False
        mock_config.response_execution_enabled = False
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    assert result["state"] == "disabled"
    assert result["enabled"] is False


def test_collect_doctor_status_with_workspace_root():
    """测试指定 workspace_root。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "async"
        mock_config.local_store_backend = "duckdb"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/custom/workspace/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {"gate_a": True, "gate_b": False}

                result = collect_doctor_status(workspace_root="/custom/workspace")

    assert result["enabled"] is True


# ── collect_doctor_status 路径检查测试 ─────────────────────────────────────

def test_collect_doctor_status_includes_runtime_dirs():
    """测试包含所有运行时目录。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    assert "paths" in result
    for dir_name in RUNTIME_DIRS:
        assert dir_name in result["paths"]


def test_collect_doctor_status_path_structure():
    """测试路径结果结构。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    base_path = result["paths"]["base"]
    assert "path" in base_path
    assert "exists" in base_path
    assert "is_dir" in base_path


# ── collect_doctor_status 配置警告测试 ─────────────────────────────────────

def test_collect_doctor_status_with_config_warnings():
    """测试带配置警告的情况。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_warning = MagicMock()
        mock_warning.to_dict.return_value = {
            "field_name": "data_dir",
            "raw_value": "/invalid",
            "fallback_value": "/tmp",
            "reason": "path not accessible",
        }
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = [mock_warning]
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    assert len(result["config"]["warnings"]) > 0


def test_collect_doctor_status_with_load_error():
    """测试配置文件加载失败时的处理。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_load.side_effect = Exception("Config file corrupted")

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    # 应该使用安全默认值并在 warnings 中包含错误
    assert len(result["config"]["warnings"]) > 0


# ── collect_doctor_status feature_gates 测试 ────────────────────────────────

def test_collect_doctor_status_feature_gates():
    """测试功能门包含在结果中。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {"feature_a": True, "feature_b": False}

                result = collect_doctor_status()

    assert "feature_gates" in result
    assert result["feature_gates"]["feature_a"] is True
    assert result["feature_gates"]["feature_b"] is False


# ── doctor_status 和 get_log_analysis_status 别名测试 ───────────────────────

def test_doctor_status_is_collect_doctor_status():
    """测试 doctor_status 是 collect_doctor_status 的别名。"""
    assert doctor_status is collect_doctor_status


def test_get_log_analysis_status_is_collect_doctor_status():
    """测试 get_log_analysis_status 是 collect_doctor_status 的别名。"""
    assert get_log_analysis_status is collect_doctor_status


# ── 边界场景测试 ──────────────────────────────────────────────────────────

def test_collect_doctor_status_custom_config_path(tmp_path):
    """测试自定义配置文件路径。"""
    config_file = tmp_path / "custom_config.yaml"
    config_file.write_text("enabled: true", encoding="utf-8")

    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status(config_path=config_file)

    assert result["config"]["path"] == str(config_file)


def test_collect_doctor_status_config_not_exists(tmp_path):
    """测试配置文件不存在。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = True
        mock_config.cluster_enabled = True
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status(config_path="/nonexistent/config.yaml")

    assert result["config"]["exists"] is False


def test_path_status_symlink_to_existing_file(tmp_path):
    """测试符号链接指向存在文件的情况。"""
    file_path = tmp_path / "real_file.txt"
    file_path.write_text("content")
    link_path = tmp_path / "link_file.txt"
    try:
        link_path.symlink_to(file_path)
    except OSError:
        pytest.skip("symlink not supported")

    result = _path_status(link_path)

    assert result["exists"] is True
    assert result["is_dir"] is False


def test_collect_doctor_status_effective_config_fields():
    """测试有效配置字段包含在结果中。"""
    with patch("agent_py_agent.agent.log_analysis.doctor.load_log_analysis_config") as mock_load:
        mock_config = MagicMock()
        mock_config.enabled = True
        mock_config.capability_level = "full"
        mock_config.config_warnings = []
        mock_config.worker_enabled = True
        mock_config.security_prompt_enabled = True
        mock_config.auto_dispatch_enabled = True
        mock_config.ml_enabled = False
        mock_config.cluster_enabled = False
        mock_config.response_execution_enabled = True
        mock_config.response_mode = "direct"
        mock_config.local_store_backend = "sqlite"
        mock_load.return_value = mock_config

        with patch("agent_py_agent.agent.log_analysis.doctor.resolve_log_analysis_data_dir") as mock_resolve:
            mock_resolve.return_value = Path("/tmp/data")
            with patch("agent_py_agent.agent.log_analysis.doctor.effective_feature_gates") as mock_gates:
                mock_gates.return_value = {}

                result = collect_doctor_status()

    effective = result["config"]["effective"]
    assert effective["worker_enabled"] is True
    assert effective["ml_enabled"] is False
    assert effective["local_store_backend"] == "sqlite"