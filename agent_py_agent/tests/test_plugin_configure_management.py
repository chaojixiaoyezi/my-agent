"""配置命令走真实原执行器和临时 RuntimeDB；不计作真实 TUI 或模型验收。"""

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_configuration import PluginConfigureRequest
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.tests.test_plugin_configuration import SETTINGS_SCHEMA
from agent_py_agent.tests.test_plugin_management import manager
from agent_py_agent.tests.test_plugin_package import _bundle


# LLM: 测试经原管理命令安装合成包，配置及内部记录均只写临时目录；不绕过产品配置 handler。
# 函数用途: 提供一个等待配置的插件和包含私有测试值的 JSON 来源。
def configured_service(tmp_path):
    service, package = manager(tmp_path)
    package.write_bytes(_bundle(change=lambda row: row.update(settings_schema=SETTINGS_SCHEMA)))
    result = service.command(f'/plugins install "{package}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded"
    source = tmp_path / "中文 配置.json"
    source.write_text(json.dumps({"limit": 3, "credential": "synthetic-private-value"}))
    return service, source


def test_configure_original_operation_and_result_never_expose_values(tmp_path, monkeypatch):
    service, source = configured_service(tmp_path)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: pytest.fail("配置不得启动进程"))
    revision = service.catalog().revision
    command = f'/plugins configure sample-peek --file "{source}"'
    result = service.command(command, revision=revision, request_id="configure")
    assert result["state"] == "succeeded", result
    assert result["details"]["configured"] is True
    assert result["details"]["revision"] == 2
    assert result["catalog"]["revision"] != revision
    assert result["catalog"]["plugins"][0]["installation_revision"] == 2
    assert not result["catalog"]["plugins"][0]["enabled"]
    assert "synthetic-private-value" in service.installations.snapshot()[0].settings_json
    assert "synthetic-private-value" not in repr(service.installations.snapshot())
    assert "synthetic-private-value" not in json.dumps(result)
    source.unlink()
    replay = service.command(command, revision=revision, request_id="configure")
    query = service.command("/plugins status configure", revision="", request_id="query")
    assert replay["details"] == query["details"] == result["details"]
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir), read_only=True)
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 2
        assert "synthetic-private-value" not in "\n".join(conn.iterdump())


@pytest.mark.parametrize("change,error", [({"is_admin": False}, "PLUGIN_PERMISSION_DENIED"),
                                        ({"enabled": False}, "PLUGIN_DISABLED"),
                                        ({"configure_allowed": False}, "TOOL_DISABLED")])
def test_configure_reuses_admin_switch_and_specific_tool_policy(tmp_path, change, error):
    service, source = configured_service(tmp_path)
    denied = PluginManagement(replace(service.context, **change))
    before = (service.installations.root / "installations.json").read_bytes()
    result = denied.command(f'/plugins configure sample-peek --file "{source}"',
                            revision=denied.catalog().revision, request_id="denied")
    assert result["error_code"] == error, result
    assert (service.installations.root / "installations.json").read_bytes() == before


def test_install_policy_does_not_disable_distinct_configure_tool(tmp_path):
    service, source = configured_service(tmp_path)
    service = PluginManagement(replace(service.context, tool_allowed=False))
    result = service.command(f'/plugins configure sample-peek -f "{source}"',
                             revision=service.catalog().revision, request_id="configure")
    assert result["state"] == "succeeded", result


@pytest.mark.parametrize("kind", ["invalid", "duplicate", "missing", "large", "symlink", "outside"])
def test_configure_bad_or_denied_source_is_single_failed_original_request(tmp_path, kind):
    from agent_py_agent.agent.path_access_policy import PathAccessPolicy

    service, source = configured_service(tmp_path)
    if kind == "invalid":
        source.write_text('{"limit":"3"}')
    elif kind == "duplicate":
        source.write_text('{"limit":3,"limit":4}')
    elif kind == "missing":
        source.unlink()
    elif kind == "large":
        source.write_text(' ' * (64 * 1024 + 1))
    elif kind == "symlink":
        link = source.with_suffix(".link")
        link.symlink_to(source)
        source = link
    else:
        service = PluginManagement(replace(service.context,
            path_policy=PathAccessPolicy.from_values(owner_scope_root=service.context.owner.home_dir)))
    command = f'/plugins configure sample-peek -f "{source}"'
    revision = service.catalog().revision
    result = service.command(command, revision=revision, request_id="bad")
    assert result["state"] == "failed" and result["error_code"] == "TOOL_INVALID_ARGUMENTS", result
    assert service.installations.snapshot()[0].settings_json is None
    source.write_text('{"limit":3}')
    assert service.command(command, revision=revision, request_id="bad")["state"] == "failed"
    assert service.installations.snapshot()[0].settings_json is None


def test_stale_public_catalog_cannot_overwrite_configuration(tmp_path):
    service, source = configured_service(tmp_path)
    revision = service.catalog().revision
    command = f'/plugins configure sample-peek -f "{source}"'
    assert service.command(command, revision=revision, request_id="first")["state"] == "succeeded"
    source.write_text('{"limit":4}')
    result = service.command(command, revision=revision, request_id="stale")
    assert result["error_code"] == "PLUGIN_CATALOG_STALE"
    assert json.loads(service.installations.snapshot()[0].settings_json)["limit"] == 3


def test_configure_freezes_source_and_refuses_race_after_preparation(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sources

    service, source = configured_service(tmp_path)
    original_read = plugin_sources.read_bytes_beneath
    calls = []

    def mutate_after_read(*args, **kwargs):
        content = original_read(*args, **kwargs)
        calls.append(True)
        entry = service.installations.snapshot()[0]
        service.installations.configure(PluginConfigureRequest(
            entry.manifest.plugin_id, entry.package_sha256, "concurrent", entry.revision, '{"limit":8}',
        ))
        source.write_text('{"limit":9}')
        return content

    monkeypatch.setattr(plugin_sources, "read_bytes_beneath", mutate_after_read)
    result = service.command(f'/plugins configure sample-peek -f "{source}"',
                             revision=service.catalog().revision, request_id="race")
    assert result["state"] == "failed" and result["details"]["reason"] == "revision_conflict", result
    assert len(calls) == 1
    assert service.installations.snapshot()[0].settings_json == '{"limit":8}'


def test_unknown_configuration_does_not_reexecute_on_same_request(tmp_path, monkeypatch):
    service, source = configured_service(tmp_path)
    calls = []

    def uncertain(request):
        calls.append(request.operation_id)
        raise RuntimeError("injected unknown write outcome")

    monkeypatch.setattr(service.installations, "configure", uncertain)
    revision = service.catalog().revision
    command = f'/plugins configure sample-peek -f "{source}"'
    first = service.command(command, revision=revision, request_id="unknown")
    second = service.command(command, revision=revision, request_id="unknown")
    assert first["state"] == second["state"] == "outcome_unknown"
    assert len(calls) == 1


def test_configuration_query_survives_disabled_feature_and_damaged_catalog(tmp_path):
    service, source = configured_service(tmp_path)
    first = service.command(f'/plugins configure sample-peek -f "{source}"',
                            revision=service.catalog().revision, request_id="configure")
    (service.installations.root / "installations.json").write_text("{")
    disabled = PluginManagement(replace(service.context, enabled=False))
    query = disabled.command("/plugins status configure", revision="", request_id="query")
    assert query["state"] == "succeeded" and query["details"] == first["details"]
    assert query["catalog_error"]
