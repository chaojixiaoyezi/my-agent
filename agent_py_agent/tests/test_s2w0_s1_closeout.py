"""S2-W0：部署配置从文件到真实读取方及热刷新边界的运行时回归。"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.configuration import build_gateway_auth_middleware
from agent_py_agent.agent.settings import live_switches as live
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry

ROOT = Path(__file__).resolve().parents[1]
YAML = ROOT / "config" / "agent_config.yaml"


# LLM: 只读随包声明并经正式 loader 验证默认；不调用模型、外部服务或写部署文件。
# 函数用途: 核对三项默认、紧邻中文说明及部署只读层级。
@pytest.mark.parametrize("key,default", [
    ("model_http_max_idle_connections", 0),
    ("scale_owner_snapshot_mode", "full"),
    ("gateway_edge_identity_required", False),
])
def test_defaults_comments_and_deployment_tier(key, default):
    assert getattr(AgentConfig(), key) == default
    assert getattr(load_config(YAML), key) == default
    lines = YAML.read_text().splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(key + ":"))
    assert lines[index - 1].startswith("#") and re.search(r"[\u4e00-\u9fff]", lines[index - 1])
    spec = parameter_registry()[key]
    assert spec.tier == "deployment" and not spec.writable and not spec.user_editable


# LLM: 真实文件改动经 refresh_live_switches 进入同一个 config，不重建 middleware。
# 函数用途: 验证部署身份门可反向热切换，并拒绝不安全的认证组合。
def test_same_middleware_follows_live_file_and_rejects_invalid_auth(tmp_path):
    path = tmp_path / "agent.yaml"
    base = "gateway_auth_token: fixture-deployment\n"
    path.write_text(base + "gateway_edge_identity_required: false\n")
    config = load_config(path)
    middleware = build_gateway_auth_middleware(config)
    live.register_live_config(config)
    headers = {"X-Gateway-Token": "fixture-deployment"}
    for enabled in (True, False, True):
        path.write_text(base + f"gateway_edge_identity_required: {str(enabled).lower()}\n")
        assert "gateway_edge_identity_required" in live.refresh_live_switches(config, force=True)
        assert config.gateway_edge_identity_required is enabled
        assert middleware.edge_identity_error(headers) == ("GATEWAY_EDGE_IDENTITY_REQUIRED" if enabled else None)
    path.write_text(base + "gateway_edge_identity_required: false\nauth_enabled: false\ngateway_per_user_owner_scoping: true\n")
    assert not live.refresh_live_switches(config, force=True)
    assert config.gateway_edge_identity_required is True and config.auth_enabled is True
    assert middleware.edge_identity_error(headers) == "GATEWAY_EDGE_IDENTITY_REQUIRED"
    assert "live config auth update rejected: multiowner_without_auth" in config.config_warnings


# LLM: 正式loader承担类型/范围判断，测试不替换归一服务。
# 函数用途: 验证池容量夹取、非法值告警、模式枚举和字符串布尔解析。
@pytest.mark.parametrize("key,raw,expected,warn", [
    ("model_http_max_idle_connections", "-1", 0, True),
    ("model_http_max_idle_connections", "257", 256, True),
    ("model_http_max_idle_connections", "256", 256, False),
    ("model_http_max_idle_connections", "32", 32, False),
    ("model_http_max_idle_connections", '"bad"', 0, True),
    ("scale_owner_snapshot_mode", "invalid", "full", True),
    ("scale_owner_snapshot_mode", "delta_compat", "delta_compat", False),
    ("scale_owner_snapshot_mode", "DELTA", "delta", False),
    ("gateway_edge_identity_required", '"false"', False, False),
    ("gateway_edge_identity_required", '"true"', True, False),
    ("gateway_edge_identity_required", '"off"', False, False),
    ("gateway_edge_identity_required", '"on"', True, False),
])
def test_normalization(tmp_path, key, raw, expected, warn):
    path = tmp_path / "agent.yaml"
    path.write_text(f"{key}: {raw}\n")
    config = load_config(path)
    assert getattr(config, key) == expected
    assert any(key in warning for warning in config.config_warnings) is warn


# LLM: 模板只读；不启动真实Cell或修改其部署凭据。
# 函数用途: 证明原有Cell模板值不再被未知键过滤器丢弃。
def test_cell_template_values_are_loaded():
    config = load_config(ROOT.parent / "deploy" / "cell-template" / "agent_config.cell.yaml")
    assert not [warning for warning in config.config_warnings if "unknown config key" in warning]
    assert config.gateway_edge_identity_required is True
    assert config.model_http_max_idle_connections == 32


# LLM: 无config沿原flag；给config后以逐次读取为准，G1和角色验证仍走原入口。
# 函数用途: 覆盖旧调用、缺省字段、本机凭据及合法/管理员/重复身份头。
def test_legacy_and_live_identity_boundaries():
    from email.message import Message

    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware

    headers = {"X-Gateway-Token": "fixture-deployment"}
    manager = AuthManager()
    assert AuthMiddleware(manager, "fixture-deployment").edge_identity_error(headers) is None
    assert AuthMiddleware(manager, "fixture-deployment", edge_identity_required=True).edge_identity_error(headers) == "GATEWAY_EDGE_IDENTITY_REQUIRED"
    config = SimpleNamespace()
    middleware = AuthMiddleware(manager, "fixture-deployment", edge_identity_required=True, config=config)
    assert middleware.edge_identity_error(headers) is None
    config.gateway_edge_identity_required = True
    assert middleware.edge_identity_error(headers) == "GATEWAY_EDGE_IDENTITY_REQUIRED"
    middleware.set_local_client_credential("fixture-local")
    assert middleware.edge_identity_error({"X-Gateway-Token": "fixture-local"}) is None
    assert middleware.extract_identity({"X-Gateway-Token": "fixture-local"}) == ("admin", "chat")
    assert middleware.edge_identity_error({**headers, "X-User-Id": "ordinary", "X-Channel": "web"}) is None
    assert middleware.edge_identity_error({**headers, "X-User-Id": "admin", "X-Channel": "chat"}) == "GATEWAY_EDGE_IDENTITY_FORBIDDEN"
    repeated = Message()
    for key, value in [*headers.items(), ("X-User-Id", "ordinary"), ("X-User-Id", "ordinary"), ("X-Channel", "web")]:
        repeated[key] = value
    assert middleware.edge_identity_error(repeated) == "GATEWAY_EDGE_IDENTITY_REQUIRED"


# LLM: 只用临时宿主文件；真实CLI入口写入并正式回读，用户聊天作用域不能取得部署写权。
# 函数用途: 验证CLI允许而/settings与模型工具拒绝同一个部署开关。
def test_writer_boundaries(tmp_path):
    from agent_py_agent.agent.settings import parameter_changes as changes
    from agent_py_agent.agent.tooling.user_config_tool import UserConfigTool

    path = tmp_path / "agent.yaml"
    path.write_text("gateway_edge_identity_required: false\n")
    env = dict(os.environ, HOME=str(tmp_path), MY_AGENT_HOME=str(tmp_path), PYTHONPATH=".:agent_py_agent")
    result = subprocess.run([sys.executable, "-m", "agent_py_agent", "--config", str(path),
        "config", "set", "gateway_edge_identity_required", "true"], cwd=ROOT.parent, env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert load_config(path).gateway_edge_identity_required is True
    before = path.read_bytes()
    with changes.user_settings_write_scope():
        report = changes.set_parameter("gateway_edge_identity_required", "false",
            paths=changes.WritePaths(user_path=path), origin=changes.ChangeOrigin("chat"))
    assert report["code"] == "PARAMETER_SOURCE_READ_ONLY" and not report["ok"]
    agent = SimpleNamespace(config=load_config(path), root=tmp_path, home_root=tmp_path,
        home_paths=SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="main"))
    report = UserConfigTool(agent).execute({"action": "set", "key": "gateway_edge_identity_required", "value": "false"})
    assert not report.ok and report.error_code == "TOOL_PERMISSION_DENIED"
    assert report.reported_error_code == "PARAMETER_SOURCE_READ_ONLY"
    assert path.read_bytes() == before


# LLM: 模型不发网络请求，规模worker只替换外部存储装配；配置文件与生产读取方保持真实。
# 函数用途: 核对后端池容量和worker快照模式确实来自正式加载结果。
@pytest.mark.parametrize("mode", ["full", "delta_compat", "delta"])
def test_real_readers(tmp_path, monkeypatch, mode):
    from agent_py_agent.agent import scale_downstream as scale
    from agent_py_agent.agent.backends.factory import get_backend

    path = tmp_path / "agent.yaml"
    path.write_text(f"model_http_max_idle_connections: 32\nscale_owner_snapshot_mode: {mode}\n")
    config = load_config(path)
    config.api_base, config.model_name = "https://fixture.invalid/v1", "fixture"
    backend = get_backend("openai_compatible", config)
    assert backend.model_http_max_idle_connections == 32
    monkeypatch.setenv("MY_AGENT_CONFIG", str(path))
    monkeypatch.setenv("MY_AGENT_SCALE_WORKSPACE_ROOT", str(tmp_path / "worker"))
    monkeypatch.setattr(scale.ScaleRuntimeConfig, "from_env", lambda *_args: SimpleNamespace(database_url="fixture"))
    monkeypatch.setattr(scale, "StorageBackend", lambda _url: object())
    store = object()
    monkeypatch.setattr(scale, "owner_store_from_runtime", lambda *_args: store)
    pool = scale.ScaleAgentPool()
    assert pool._snapshot_mode == mode and pool._store is store
