"""插件目录沿可信宿主身份校验工作区，读取失败保留可执行诊断。"""
from dataclasses import replace

import pytest

from agent_py_agent.agent.gateway_parts.plugin_command_service import _scope_management
from agent_py_agent.agent.gateway_parts.workspace_scope import GatewayWorkspaceScopeError
from agent_py_agent.agent.plugin_command_service import plugin_catalog_unavailable
from agent_py_agent.agent.plugin_installation import PluginInstallationError
from agent_py_agent.cli.chat_parts import plugin_command_client
from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient
from agent_py_agent.tests.test_plugin_command_client import host as client_host
from agent_py_agent.tests.test_plugins_chat_control import (
    _scope,
    host,  # noqa: F401
)


def test_trusted_admin_empty_catalog_accepts_existing_project_without_creating_install_state(host, tmp_path_factory):  # noqa: F811
    project = tmp_path_factory.mktemp('external-project')
    scope = replace(_scope(admin=True), workspace={'cwd': str(project), 'roots': [str(project)]})
    manager = _scope_management(host, scope, True)
    assert manager.context.workspace == project
    assert manager.context.is_admin is True
    assert manager.catalog().plugins == ()
    assert not manager.installations.root.exists()


def test_client_claims_cannot_give_normal_owner_an_external_project(host, tmp_path):  # noqa: F811
    project = tmp_path / 'project'
    project.mkdir()
    scope = replace(_scope(channel='local'), workspace={'cwd': str(project), 'roots': [str(project)]},
                    metadata={'owner_id': 'main', 'is_admin': True})
    with pytest.raises(GatewayWorkspaceScopeError, match='自己的家目录'):
        _scope_management(host, scope, False)


def test_catalog_invalid_state_reports_reason_type_no_secret_or_side_effect():
    error = PluginInstallationError('invalid_state', 'private-secret-path token=synthetic-secret')
    result = plugin_catalog_unavailable(error)
    assert result['error_code'] == 'PLUGIN_INSTALLATION_STATE_INVALID'
    assert result['reason'] == 'invalid_state'
    assert result['details']['exception_type'] == 'PluginInstallationError'
    assert result['effect_outcome'] == 'not_started'
    assert 'installations.json' in result['message'] and 'private-secret' not in str(result)
    assert 'PluginInstallationError' in result['message']
    assert 'synthetic-secret' not in str(result)


def test_client_preserves_server_catalog_refusal_without_submitting_command(monkeypatch):
    body = plugin_catalog_unavailable(GatewayWorkspaceScopeError('普通用户的工作目录必须在自己的家目录内'))
    calls = []

    def transport(agent, path, payload, **kwargs):
        calls.append(payload)
        return 503, body

    monkeypatch.setattr(plugin_command_client, 'post_gateway_json', transport)
    client = PluginCommandClient(client_host(), 'session', use_gateway=True)
    assert client.command('/plugins list') == body
    assert len(calls) == 1 and calls[0]['operation'] == 'catalog'
    assert client.snapshot() is None


def test_cached_catalog_read_failure_is_not_unknown_write_result():
    from agent_py_agent.agent.gateway_parts.plugin_command_service import _plugin_failure

    error = PluginInstallationError('invalid_state', '登记表不可读')
    read = _plugin_failure(error, '/plugins list', 'original-read')
    assert read['request_id'] == 'original-read' and read['effect_outcome'] == 'not_started'
    assert read['reason'] == 'invalid_state' and read['state'] == 'rejected'
    write = _plugin_failure(error, '/plugins enable sample-peek', 'original-write')
    assert write['state'] == 'outcome_unknown' and write['request_id'] == 'original-write'
