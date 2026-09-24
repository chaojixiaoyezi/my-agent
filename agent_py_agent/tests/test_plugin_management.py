"""管理命令开发合同；临时 owner 与实际 RuntimeDB，不运行插件或真实模型。"""

from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.plugin_management import (
    PluginManagement,
    PluginManagementContext,
    plugin_management_context,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.runtime_db.schema import runtime_db_path
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.test_plugin_package import _bundle


# LLM: 使用原线程与运行 Store；不替换生产执行器，不把合成包保存当成环境安装验收。
# 函数用途: 给每个用例提供隔离 owner、包文件及明确管理员上下文。
def manager(tmp_path, **changes):
    owner = resolve_owner_home(tmp_path / "home")
    store = ConversationStore(owner.home_dir / "conversations", initialize=False)
    context = PluginManagementContext(
        owner, "tester", "chat", "session", store.threads, tmp_path,
        PathAccessPolicy.from_values(mode="full"), True, True,
    )
    source = tmp_path / "source.zip"
    source.write_bytes(_bundle())
    return PluginManagement(replace(context, **changes)), source


def test_install_real_execution_replay_after_source_deleted_and_readonly_query(tmp_path):
    service, source = manager(tmp_path)
    revision = service.catalog().revision
    result = service.command(f'/plugins install "{source}"', revision=revision, request_id="request-a")
    assert result["state"] == "succeeded", result
    assert result["details"]["enabled"] is False
    assert service.installations.snapshot()[0].manifest.plugin_id == "sample-peek"
    source.unlink()
    replay = service.command(f'/plugins install "{source}"', revision=revision, request_id="request-a")
    assert replay["state"] == "succeeded", replay
    query = service.command("/plugins status request-a", revision="", request_id="query-a")
    assert query["details"] == result["details"]
    repo = RuntimeRepository(runtime_db_path(service.context.owner.home_dir))
    with repo._runtime_connection() as conn:
        assert conn.execute("SELECT count(*) FROM tool_operations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM agent_attempts").fetchone()[0] == 1
        assert conn.execute("SELECT status FROM agent_attempts").fetchone()[0] == "done"


def test_nonadmin_is_rejected_before_creating_owner_or_thread(tmp_path):
    service, source = manager(tmp_path, is_admin=False)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                             request_id="request-a")
    assert result["error_code"] == "PLUGIN_PERMISSION_DENIED"
    assert not service.context.owner.home_dir.exists()


def test_readonly_missing_query_does_not_create_storage(tmp_path):
    service, _source = manager(tmp_path)
    result = service.command("/plugins status missing", revision="", request_id="query")
    assert result["state"] == "not_found"
    assert not service.context.owner.home_dir.exists()


def test_tool_policy_denial_is_persisted_without_operation_and_can_be_queried(tmp_path):
    service, source = manager(tmp_path, tool_allowed=False)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                             request_id="request-a")
    assert result["state"] == "rejected", result
    queried = service.command("/plugins status request-a", revision="", request_id="query")
    assert queried["state"] == "rejected"
    assert service.installations.snapshot() == ()


def test_disabled_feature_still_reads_original_result(tmp_path):
    service, source = manager(tmp_path)
    first = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                            request_id="request-a")
    disabled = PluginManagement(replace(service.context, enabled=False))
    assert disabled.command("/plugins status request-a", revision="", request_id="query")["state"] == first["state"]
    assert disabled.command(f'/plugins install "{source}"', revision=disabled.catalog().revision,
                            request_id="request-b")["error_code"] == "PLUGIN_DISABLED"


@pytest.mark.parametrize("setting", ["false", '"false"'])
def test_config_disable_reaches_real_management_context(tmp_path, setting):
    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"enable_plugins: {setting}\n")
    config = load_config(config_path)
    assert config.enable_plugins is False
    shipped = Path(__file__).resolve().parents[1] / "config" / "agent_config.yaml"
    assert load_config(shipped).enable_plugins is AgentConfig().enable_plugins is True
    service, source = manager(tmp_path)
    owner = service.context.owner
    home = home_paths_with_owner(home_paths(tmp_path / "home"), owner)
    context = plugin_management_context(owner, home, config, service.context.threads,
        actor_id="tester", channel="chat", conversation_id="session", is_admin=True)
    disabled = PluginManagement(context)
    assert not next(action for action in disabled.catalog().management_actions if action.name == "install").available
    result = disabled.command(f'/plugins install "{source}"', revision=disabled.catalog().revision, request_id="a")
    assert result["error_code"] == "PLUGIN_DISABLED"
    assert not owner.home_dir.exists()


@pytest.mark.parametrize("source_kind", ["missing", "symlink", "outside", "invalid", "unsupported"])
def test_source_validation_preserves_core_and_never_installs(tmp_path, monkeypatch, source_kind):
    service, source = manager(tmp_path)
    if source_kind == "missing":
        source.unlink()
    elif source_kind == "symlink":
        link = tmp_path / "link.zip"
        link.symlink_to(source)
        source = link
    elif source_kind == "outside":
        service = PluginManagement(replace(service.context,
            path_policy=PathAccessPolicy.from_values(owner_scope_root=service.context.owner.home_dir)))
    elif source_kind == "invalid":
        source.write_bytes(b"invalid package")
    else:
        monkeypatch.setattr("agent_py_agent.agent.common.nofollow_fs._supports_dir_fd", lambda: False)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="a")
    assert result["state"] == "failed" and result["error_code"] == "TOOL_INVALID_ARGUMENTS"
    assert service.installations.snapshot() == ()


def test_package_is_one_immutable_read_and_catalog_damage_does_not_erase_result(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_sources

    service, source = manager(tmp_path)
    original = plugin_sources.read_bytes_beneath
    calls = []

    def read_then_replace(*args, **kwargs):
        content = original(*args, **kwargs)
        calls.append(True)
        source.write_bytes(b"replaced after read")
        return content

    monkeypatch.setattr(plugin_sources, "read_bytes_beneath", read_then_replace)
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="a")
    assert result["state"] == "succeeded" and len(calls) == 1
    (service.installations.root / "installations.json").write_text("broken")
    query = service.command("/plugins status a", revision="", request_id="query")
    assert query["state"] == "succeeded" and query["catalog_error"]
    assert query["details"] == result["details"]


def test_owner_quota_is_checked_before_package_and_installation_publication(tmp_path):
    service, source = manager(tmp_path)
    owner = service.context.owner
    owner.home_dir.mkdir(parents=True)
    owner.quota_json.write_text('{"max_disk_mb": 1}')
    (owner.home_dir / "existing.bin").write_bytes(b"x" * (1024 * 1024))
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="a")
    assert result["state"] == "failed"
    assert result["details"]["reason"] == "quota_unavailable"
    assert service.installations.snapshot() == ()
    assert not (service.installations.root / "packages").exists()


def test_explicit_command_output_is_shown_as_readable_text():
    # 真实 TUI：/plugins@savepoint-lite list 曾直接显示双重转义的 MCP JSON
    import json as _json

    from agent_py_agent.agent.plugin_management import readable_plugin_output

    inner = _json.dumps({"path": "sample.txt", "snapshots": [{"id": "s1", "current": True}]}, ensure_ascii=False)
    raw = _json.dumps({"result": inner, "content": [{"type": "text", "text": inner}]})
    text = readable_plugin_output(raw)
    assert '"path": "sample.txt"' in text and "\\\"" not in text and text.count("sample.txt") == 1
    assert readable_plugin_output("纯文本结果") == "纯文本结果"
    plain = _json.dumps({"content": [{"type": "text", "text": "第一行"}]})
    assert readable_plugin_output(plain) == "第一行"
