from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu_cli
from agent_py_agent.agent.adapter.feishu_cloud import FeishuCloudError
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.settings.parameter_registry import parameter_registry


# LLM: 测试后端只接合成token，返回确定协议；不启动官方API或使用真实帐号。
# 函数用途: 创建隔离可执行协议替身，验证宿主进程和错误路径。
def _binary(tmp_path: Path, code: str) -> Path:
    file = tmp_path / "host-cli"
    file.write_text("#!" + sys.executable + "\n" + code)
    file.chmod(0o700)
    return file


def test_private_stdin_does_not_export_token_to_env_or_argv(tmp_path, monkeypatch):
    code = """import os,sys,json
p=json.load(sys.stdin)
assert p['token']=='synthetic-private-token'
assert len(sys.argv)==1
assert 'synthetic-private-token' not in str(dict(os.environ))
print(json.dumps({'schema':'my-agent.feishu-cli.v1','http_status':200,'request_sent':True,'response':{'code':0,'data':{'value':42}}}))
"""
    monkeypatch.setattr(feishu_cli, "cli_binary", lambda: _binary(tmp_path, code))
    result = feishu_cli.execute_cli_request({"token": "synthetic-private-token"})
    assert result["response"]["data"]["value"] == 42
    assert "synthetic-private-token" not in json.dumps(result)


def test_cli_timeout_stops_its_process(tmp_path, monkeypatch):
    file = _binary(tmp_path, "import time\ntime.sleep(30)\n")
    monkeypatch.setattr(feishu_cli, "cli_binary", lambda: file)
    with pytest.raises(TimeoutError):
        feishu_cli.execute_cli_request({}, timeout=0.05)


@pytest.mark.parametrize("response", [{}, {"code": "0"}, {"code": True}, {"code": 0.0}, {"code": 0, "data": []}])
def test_invalid_provider_response_is_unknown_write(monkeypatch, response):
    monkeypatch.setattr(feishu_cli, "execute_cli_request", lambda *a, **k: {
        "schema": feishu_cli.PROTOCOL, "http_status": 200, "request_sent": True, "response": response})
    adapter = SimpleNamespace(app_id="cli_synthetic", app_secret="synthetic-secret", _get_tenant_access_token=lambda: "synthetic-token")
    with pytest.raises(FeishuCloudError) as captured:
        feishu_cli.request_cli_cloud_api(adapter, "POST", "/sheets/v2/spreadsheets/synthetic/values_append", {})
    assert captured.value.unknown
    assert "synthetic-secret" not in str(captured.value)


def test_cli_redirect_does_not_become_success(monkeypatch):
    monkeypatch.setattr(feishu_cli, "execute_cli_request", lambda *a, **k: {
        "schema": feishu_cli.PROTOCOL, "http_status": 307, "request_sent": True, "response": {"code": 0}})
    adapter = SimpleNamespace(app_id="cli_synthetic", _get_tenant_access_token=lambda: "synthetic-token")
    with pytest.raises(FeishuCloudError, match="重定向") as captured:
        feishu_cli.request_cli_cloud_api(adapter, "POST", "/sheets/v2/spreadsheets/synthetic/values_append", {})
    assert captured.value.code == "FEISHU_CLOUD_REDIRECT_REFUSED"


def test_raw_response_is_cleaned_by_original_host_projection(monkeypatch):
    monkeypatch.setattr(feishu_cli, "execute_cli_request", lambda *a, **k: {
        "schema": feishu_cli.PROTOCOL, "http_status": 200, "request_sent": True,
        "response": {"code": 0, "data": {"value": "token=synthetic-token", "obj_token": "resource-one"}}})
    adapter = SimpleNamespace(app_id="cli_synthetic", app_secret="synthetic-secret", _get_tenant_access_token=lambda: "synthetic-token")
    result = feishu_cli.request_cli_cloud_api(adapter, "GET", "/wiki/v2/spaces/get_node")
    assert result["resource_id"] == "resource-one"
    assert "synthetic-token" not in json.dumps(result)


@pytest.mark.parametrize(("key", "value"), [("owner_default_permission_mode", "full-access"),
    ("owner_default_permission_mode", []), ("tool_approval_timeout_seconds", 0), ("tool_approval_timeout_seconds", -1),
    ("tool_approval_timeout_seconds", True), ("feishu_cli_backend_enabled", "invalid"),
    ("feishu_user_oauth_enabled", 1)])
def test_deployment_policy_invalid_does_not_fall_back_to_auto(tmp_path, key, value):
    file = tmp_path / "capability.yaml"
    file.write_text(f"{key}: {json.dumps(value)}\n")
    with pytest.raises(ValueError):
        load_capability_config(file)


def test_deployment_settings_are_not_model_writable():
    registry = parameter_registry()
    for key in ("owner_default_permission_mode", "tool_approval_timeout_seconds", "feishu_cli_backend_enabled"):
        assert not registry[key].writable
    assert CapabilityConfig().owner_default_permission_mode == "auto"


@pytest.mark.parametrize('root', [None, '', 'owner-local'])
def test_deployment_source_requires_absolute_host_home(root):
    from agent_py_agent.agent.capability.runtime_config_reload import deployment_capability_config
    with pytest.raises(ValueError):
        deployment_capability_config(SimpleNamespace(root=root))


def test_user_cli_uses_private_verified_actor_and_no_bot_token(monkeypatch):
    captured = []
    def transport(payload, **_kwargs):
        captured.append(payload)
        return {'schema': feishu_cli.PROTOCOL, 'http_status': 200, 'request_sent': True,
                'response': {'code': 0, 'data': {'ok': True}}}
    monkeypatch.setattr(feishu_cli, 'execute_cli_request', transport)
    adapter = feishu_cli.UserCloudIdentity('cli_synthetic', 'synthetic-secret', 'synthetic-user-token', 'ou_synthetic')
    assert feishu_cli.request_cli_cloud_api(adapter, 'GET', '/docx/v1/documents/synthetic/blocks/root/children', user_token=adapter.token) == {'ok': True}
    assert captured[0]['identity'] == 'user' and captured[0]['actor_id'] == 'ou_synthetic'
    assert adapter.token not in repr(adapter)


def test_user_cli_missing_or_unverified_actor_does_not_report_business_success(monkeypatch):
    adapter = SimpleNamespace(app_id='cli_synthetic')
    monkeypatch.setattr(feishu_cli, 'execute_cli_request', lambda *_a, **_k: pytest.fail('missing actor must not start CLI'))
    with pytest.raises(FeishuCloudError) as missing:
        feishu_cli.request_cli_cloud_api(adapter, 'POST', '/sheets/v2/spreadsheets/synthetic/values_append', {}, user_token='synthetic-user-token')
    assert missing.value.code == 'FEISHU_USER_AUTH_REQUIRED' and not missing.value.unknown
    monkeypatch.setattr(feishu_cli, 'execute_cli_request', lambda *_a, **_k: {
        'schema': feishu_cli.PROTOCOL, 'http_status': 0, 'request_sent': False, 'error_code': 'identity_unverified'})
    adapter.actor_id = 'ou_synthetic'
    with pytest.raises(FeishuCloudError) as wrong:
        feishu_cli.request_cli_cloud_api(adapter, 'POST', '/sheets/v2/spreadsheets/synthetic/values_append', {}, user_token='synthetic-user-token')
    assert wrong.value.code == 'FEISHU_USER_AUTH_REQUIRED' and not wrong.value.unknown
