"""安装升级仅迁移已知旧并发默认；所有文件都在pytest临时目录。"""
from __future__ import annotations

import re

import pytest

from scripts.private_release import installer

_TEMPLATE = """# 测试发行模板
gateway_user_inflight_limit: 128
gateway_global_inflight_limit: 500
# HTTP请求池容量
gateway_http_max_request_workers: 128
# HTTP在途请求容量
gateway_http_max_outstanding_requests: 256
# 控制面VIP快速车道，stop/cancel和健康检查不排在普通线程后面
gateway_http_control_lane_workers: 10
# Web/SSE独立流池，飞书不占用
gateway_native_stream_max: 128
# 后台会话全局线程池
background_owner_workers: 128
# 普通owner后台并发
background_threads_per_owner: 2
# 管理员后台并发
background_admin_threads_per_owner: 64
# Owner实例池上限
owner_agent_pool_max_agents: 128
gateway_port: 8420
"""

_OLD_CONFIG = """# 用户模型与端口必须原样保留
api_key: "user-private-placeholder"
model_name: "user-choice"
gateway_port: 18420
gateway_workspace: "/custom/workspace"

# 用户的请求额度注释
gateway_user_inflight_limit: 50
gateway_global_inflight_limit: 900
gateway_http_max_request_workers: 16
gateway_http_max_outstanding_requests: 128
background_owner_workers: 32
background_threads_per_owner: 4
owner_agent_pool_max_agents: 64
# 已退役的进程数配置
# 旧Count0说明
gateway_worker_process_count: 0
# 未知字段也要保留
custom_unknown_key: "keep-me"
"""


# LLM: 只在临时prefix造既有配置与本次发行模板，不启动安装或服务。
# 函数用途: 准备真实initialize_data升级分支的输入。
def _prepare(tmp_path, config_text=_OLD_CONFIG, template=_TEMPLATE, *, python_dir="python3.11"):
    prefix = tmp_path / "app"
    config_dir = prefix / "data/config"
    config_dir.mkdir(parents=True)
    config = config_dir / "agent_config.yaml"
    config.write_text(config_text, encoding="utf-8")
    release = tmp_path / "release"
    package = release / "runtime/lib" / python_dir / "site-packages/agent_py_agent/config"
    package.mkdir(parents=True)
    (package / "agent_config.yaml").write_text(template, encoding="utf-8")
    return prefix, release, config


# LLM: 模板和既有配置都在临时目录；同时验证首装与旧配置迁移，不依赖系统解释器版本。
# 函数用途: 检查3.11/3.12/3.14/3.14t布局的配置模板都真正参与安装。
@pytest.mark.parametrize("python_dir", ["python3.11", "python3.12", "python3.14", "python3.14t"])
@pytest.mark.parametrize("existing", [False, True])
def test_initialize_data_resolves_bundled_runtime_template(tmp_path, python_dir, existing):
    prefix, release, config = _prepare(tmp_path, python_dir=python_dir)
    if not existing:
        config.unlink()
    installer.initialize_data(prefix, release)
    text = config.read_text(encoding="utf-8")
    assert _value(text, "gateway_port") == "18420"
    assert _value(text, "gateway_http_control_lane_workers") == "10"
    assert _value(text, "gateway_native_stream_max") == "128"
    assert config.stat().st_mode & 0o777 == 0o600
    if existing:
        assert 'model_name: "user-choice"' in text
        assert 'gateway_workspace: "/custom/workspace"' in text


# LLM: 只读取顶层标量，重复键返回None，不能把重复追加误当通过。
# 函数用途: 取得唯一一行配置值。
def _value(text, key):
    matches = re.findall(rf"^{re.escape(key)}:[ \t]*([^\r\n#]*?)(?:[ \t]*#.*)?$", text, re.M)
    return matches[0].strip() if len(matches) == 1 else None


# LLM: 走initialize_data既有配置分支，保留非迁移数据的精确内容与用户端口。
# 函数用途: 验证旧机器升级迁移、补齐、删除及0600权限，并检查二次升级幂等。
def test_initialize_data_migrates_old_defaults_and_adds_new_keys(tmp_path):
    prefix, release, config = _prepare(tmp_path)
    installer.initialize_data(prefix, release)
    text = config.read_text(encoding="utf-8")
    for preserved in (
        'api_key: "user-private-placeholder"', 'model_name: "user-choice"',
        'gateway_workspace: "/custom/workspace"', 'custom_unknown_key: "keep-me"',
        '# 用户的请求额度注释',
    ):
        assert preserved in text
    assert _value(text, "gateway_port") == "18420"
    assert _value(text, "gateway_global_inflight_limit") == "900"
    for key, expected in (
        ("gateway_http_max_request_workers", "128"),
        ("gateway_http_max_outstanding_requests", "256"),
        ("gateway_user_inflight_limit", "128"),
        ("owner_agent_pool_max_agents", "128"),
        ("background_threads_per_owner", "2"),
        ("background_owner_workers", "128"),
        ("gateway_http_control_lane_workers", "10"),
        ("gateway_native_stream_max", "128"),
        ("background_admin_threads_per_owner", "64"),
    ):
        assert _value(text, key) == expected
    assert "VIP快速车道" in text and "飞书不占用" in text
    assert "gateway_worker_process_count" not in text
    assert "旧Count0" not in text and "已退役的进程数配置" not in text
    assert config.stat().st_mode & 0o777 == 0o600
    installer.initialize_data(prefix, release)
    assert config.read_text(encoding="utf-8") == text


# LLM: 自定义值不属于已知旧出厂值，保持原字节；带引号旧默认的行内注释也保留。
# 函数用途: 验证有限迁移不覆盖用户自定义容量或其它内容。
def test_upgrade_keeps_custom_values_and_inline_comments(tmp_path):
    custom = """gateway_port: 18420
# 用户注释
gateway_http_max_request_workers: 96  # 手写说明
gateway_user_inflight_limit: "50"  # 保留行内说明
background_owner_workers: 96
background_threads_per_owner: "3"
gateway_http_control_lane_workers: 0
gateway_native_stream_max: 3
background_admin_threads_per_owner: 5
custom_unknown_key: [a, b]
"""
    prefix, release, config = _prepare(tmp_path, config_text=custom)
    installer.initialize_data(prefix, release)
    assert config.read_text(encoding="utf-8") == custom.replace(
        'gateway_user_inflight_limit: "50"', 'gateway_user_inflight_limit: 128')


# LLM: 仅替代最终原子rename令其失败，原文件字节/权限必须保持且无残留临时文件。
# 函数用途: 验证写盘失败不能破坏已有配置。
def test_upgrade_write_failure_keeps_original_file(tmp_path, monkeypatch):
    _prefix, _release, config = _prepare(tmp_path)
    config.chmod(0o600)
    before = config.read_bytes()

    # LLM: 故障注入只作用于此测试临时配置的最终替换。
    # 函数用途: 模拟原子替换失败。
    def fail(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(installer.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        installer.upgrade_existing_config_defaults(config, _TEMPLATE)
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o777 == 0o600
    assert [path.name for path in config.parent.iterdir()] == ["agent_config.yaml"]


# LLM: 不完整模板不能制造虚假默认；已有用户数据保持，缺项须明确告警。
# 函数用途: 检查无法补齐时的可观测降级。
def test_upgrade_skips_key_absent_from_template(tmp_path, capsys):
    _prefix, _release, config = _prepare(tmp_path)
    installer.upgrade_existing_config_defaults(config, _TEMPLATE.replace("gateway_native_stream_max: 128\n", ""))
    text = config.read_text(encoding="utf-8")
    assert _value(text, "gateway_http_control_lane_workers") == "10"
    assert _value(text, "background_admin_threads_per_owner") == "64"
    assert "gateway_native_stream_max" not in text
    assert "gateway_native_stream_max" in capsys.readouterr().out
    assert 'api_key: "user-private-placeholder"' in text


# LLM: 内容已经完成迁移时不替换inode或权限，避免重复升级无谓写盘。
# 函数用途: 验证已有新参数不重复追加且无变更即无写入。
def test_upgrade_repeat_is_idempotent(tmp_path):
    current = "gateway_port: 18420\ngateway_http_control_lane_workers: 10\ngateway_native_stream_max: 128\nbackground_admin_threads_per_owner: 64\nbackground_threads_per_owner: 2\n"
    _prefix, _release, config = _prepare(tmp_path, config_text=current)
    config.chmod(0o600)
    before = config.stat()
    installer.upgrade_existing_config_defaults(config, _TEMPLATE)
    assert config.read_text(encoding="utf-8") == current
    assert config.stat().st_ino == before.st_ino
    assert config.stat().st_mtime_ns == before.st_mtime_ns


# LLM: 缩进的#行可能是用户块标量内容，不是退役键注释；嵌套同名键也不是全局配置。
# 函数用途: 防止删除退役项时顺带截断用户的多行文字或改写嵌套未知设置。
def test_upgrade_keeps_indented_scalar_content_and_nested_values(tmp_path):
    original = """custom_map:
  gateway_user_inflight_limit: 50
custom_notes: |
  # 这行是用户文字而非YAML注释
# 已退役的进程数配置
gateway_worker_process_count: 0
gateway_http_control_lane_workers: 0
gateway_native_stream_max: 0
background_admin_threads_per_owner: 7
"""
    _prefix, _release, config = _prepare(tmp_path, config_text=original)
    installer.upgrade_existing_config_defaults(config, _TEMPLATE)
    assert config.read_text(encoding="utf-8") == original.replace(
        "# 已退役的进程数配置\ngateway_worker_process_count: 0\n", "")


# LLM: 覆盖出厂历史中的另一组已知值，不能只认最早的16/32；所有新键已存在避免混入补齐影响。
# 函数用途: 检查64线程、8全局后台和16管理员这组历史默认都能迁移。
def test_upgrade_handles_other_historical_defaults(tmp_path):
    original = "gateway_http_max_request_workers: 64\nbackground_owner_workers: 8\nbackground_admin_threads_per_owner: 16\ngateway_http_control_lane_workers: 0\ngateway_native_stream_max: 0\n"
    _prefix, _release, config = _prepare(tmp_path, config_text=original)
    installer.upgrade_existing_config_defaults(config, _TEMPLATE)
    expected = original.replace("workers: 64", "workers: 128").replace("workers: 8", "workers: 128").replace("owner: 16", "owner: 64")
    assert config.read_text(encoding="utf-8") == expected
