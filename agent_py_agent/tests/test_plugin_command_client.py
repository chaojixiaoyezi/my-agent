from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from agent_py_agent.agent.plugin_command_catalog import PluginCommandCatalog
from agent_py_agent.agent.plugin_command_service import execute_plugin_command, read_plugin_catalog
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts import plugin_command_client
from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient
from agent_py_agent.cli.chat_parts.tui_input import TuiInputCompleter, apply_selected_completion
from agent_py_agent.cli.chat_parts.tui_plugin_commands import bind_plugin_input
from agent_py_agent.tests.test_plugin_command_catalog import _plugin


def host(**config):
    return SimpleNamespace(config=SimpleNamespace(gateway_port=9876, **config))


@pytest.mark.parametrize("thin", [False, True])
def test_gateway_mode_uses_original_owner_transport_for_both_client_kinds(monkeypatch, thin):
    agent = host(
        my_agent_owner_provider="local", my_agent_owner_kind="user", my_agent_owner_id="alice"
    )
    if thin:
        agent.gateway_client_only = True
        agent.owner_identity = OwnerIdentity.provider_user("local", "alice")
    snapshot = PluginCommandCatalog("host-scope")
    calls = []

    def transport(port, owner, path, payload, *, timeout):
        calls.append(payload)
        assert port == 9876 and path == "/client/plugins" and timeout == 3.0
        assert owner == OwnerIdentity.provider_user("local", "alice")
        assert payload["conversation_id"] == "session-1"
        return 200, (
            {"ok": True, "catalog": snapshot.to_payload()}
            if payload["operation"] == "catalog"
            else execute_plugin_command(
                snapshot, payload["command"], revision=payload["catalog_revision"]
            )
        )

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    monkeypatch.setattr(
        plugin_command_client.PluginCommandClient,
        "_direct_manager",
        Mock(side_effect=AssertionError("不读本地目录")),
    )
    client = PluginCommandClient(agent, "session-1", use_gateway=True)
    assert calls == [] and client.snapshot() is None
    result = client.command('/plugins install "中文 a.whl"')
    assert result["error_code"] == "PLUGIN_COMMAND_UNAVAILABLE"
    assert [row["operation"] for row in calls] == ["catalog", "command"]
    assert calls[1]["catalog_revision"] == snapshot.revision


def test_direct_client_uses_config_owner_without_http(monkeypatch, tmp_path):
    monkeypatch.setattr(
        plugin_command_client,
        "post_gateway_json",
        Mock(side_effect=AssertionError("本地不发 HTTP")),
    )
    agent = host(
        my_agent_owner_provider="local", my_agent_owner_kind="user", my_agent_owner_id="alice"
    )
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.settings.config import AgentConfig
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )
    owner = resolve_owner_home(tmp_path, OwnerIdentity.provider_user("local", "alice"))
    agent.config = AgentConfig(my_agent_owner_provider="local", my_agent_owner_kind="user", my_agent_owner_id="alice")
    agent.home_paths = home_paths_with_owner(home_paths(tmp_path), owner)
    agent.conversation_store = ConversationStore(owner.home_dir / "conversations", initialize=False)
    agent.effective_workspace_root = owner.home_dir
    client = PluginCommandClient(agent, "session-a", use_gateway=False)
    assert client.command("/plugins help")["ok"]
    expected = read_plugin_catalog(
        OwnerIdentity.provider_user("local", "alice"), channel="chat", conversation_id="session-a"
    )
    assert client.snapshot().scope_ref == expected.scope_ref
    assert client.snapshot().plugins == ()


def test_old_selection_is_not_rebound_to_fresh_catalog_or_automatically_replayed(monkeypatch):
    old, current = PluginCommandCatalog("old-scope"), PluginCommandCatalog("current-scope")
    calls = []

    def transport(port, owner, path, payload, **kwargs):
        calls.append(payload)
        return 200, execute_plugin_command(
            current, payload["command"], revision=payload["catalog_revision"]
        )

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    result = client.command("/plugins list", revision=old.revision)
    assert result["error_code"] == "PLUGIN_CATALOG_STALE"
    assert len(calls) == 1 and calls[0]["catalog_revision"] == old.revision
    assert client.snapshot() == current


@pytest.mark.parametrize(
    "response", [(0, {}), (503, {}), (200, {"ok": True, "catalog": {"revision": "bad"}})]
)
def test_gateway_or_catalog_failure_never_falls_back_or_submits_command(monkeypatch, response):
    transport = Mock(return_value=response)
    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    monkeypatch.setattr(
        plugin_command_client.PluginCommandClient,
        "_direct_manager",
        Mock(side_effect=AssertionError("不允许本地兜底")),
    )
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    result = client.command("/plugins help")
    assert result["error_code"] == "PLUGIN_CATALOG_UNAVAILABLE"
    assert client.snapshot() is None and transport.call_count == 1


def test_submission_timeout_keeps_original_id_without_retry_and_status_skips_catalog(monkeypatch):
    calls = []

    def timeout(_port, _owner, _path, body, **kwargs):
        calls.append(body)
        raise TimeoutError("private transport detail")

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", timeout)
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    result = client.command("/plugins install sample.zip", revision="seen-version")
    assert result["state"] == "outcome_unknown"
    assert result["request_id"] == calls[0]["plugin_request_id"]
    assert "没有执行" not in result["message"] and "private" not in result["message"]
    assert len(calls) == 1
    queried = client.command("/plugins status original-request")
    assert len(calls) == 2 and calls[-1]["operation"] == "command"
    assert queried["request_id"] == "original-request"


@pytest.mark.parametrize("catalog", [None, {"revision": "bad"}])
def test_catalog_refresh_failure_does_not_erase_known_install_result(monkeypatch, catalog):
    def transport(_port, _owner, _path, body, **kwargs):
        return 200, {"ok": True, "kind": "plugin_command", "state": "succeeded", "message": "已完成",
                     "request_id": body["plugin_request_id"], "catalog": catalog}

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    assert client.command("/plugins install sample.zip", revision="seen-version")["state"] == "succeeded"
    assert client.snapshot() is None


def test_slow_old_response_cannot_overwrite_newer_snapshot(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    old, new = PluginCommandCatalog("old"), PluginCommandCatalog("new")
    calls = 0

    def transport(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(3)
            return 200, {"ok": True, "catalog": old.to_payload()}
        return 200, {"ok": True, "catalog": new.to_payload()}

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.refresh)
        assert entered.wait(3)
        assert client.refresh()["ok"]
        release.set()
        assert first.result(timeout=3)["ok"]
    assert client.snapshot() == new


@pytest.mark.parametrize("change", ["owner", "session", "port", "mode"])
def test_response_for_changed_client_scope_is_discarded(monkeypatch, change):
    client = PluginCommandClient(host(), "session-a", use_gateway=True)

    def transport(*args, **kwargs):
        if change == "owner":
            client.agent.owner_identity = OwnerIdentity.provider_user("local", "bob")
        elif change == "session":
            client.conversation_id = "session-b"
        elif change == "port":
            client.agent.config.gateway_port = 4567
        else:
            client.use_gateway = False
        return 200, {"ok": True, "catalog": PluginCommandCatalog("old").to_payload()}

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    assert client.refresh()["error_code"] == "PLUGIN_CATALOG_UNAVAILABLE"
    assert client.snapshot() is None


def test_explicit_tab_refreshes_off_event_thread_but_typing_and_core_commands_do_not(
    tmp_path, monkeypatch
):
    snapshot = PluginCommandCatalog("scope", plugins=(_plugin(),))
    event_thread = threading.get_ident()
    calls = []

    def transport(*args, **kwargs):
        calls.append(threading.get_ident())
        return 200, {"ok": True, "catalog": snapshot.to_payload()}

    monkeypatch.setattr(plugin_command_client, "post_gateway_json", transport)
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    completer = TuiInputCompleter(tmp_path, plugin_client=client)

    async def complete(text, requested=False):
        return [
            row
            async for row in completer.get_completions_async(
                Document(text), CompleteEvent(completion_requested=requested)
            )
        ]

    assert asyncio.run(complete("/plugins@s")) == [] and calls == []
    asyncio.run(complete("/st", requested=True))
    assert calls == []
    result = asyncio.run(complete("/plugins@s", requested=True))
    assert [row.text for row in result] == ["/plugins@sample"]
    assert all(
        row.catalog_revision == snapshot.revision and row.enter_action == "apply" for row in result
    )
    assert len(calls) == 1 and calls[0] != event_thread
    asyncio.run(complete("/plugins@s"))
    assert len(calls) == 1


def test_accepted_completion_keeps_original_revision_across_parameter_edits_and_refresh(
    tmp_path, monkeypatch
):
    initial = PluginCommandCatalog("scope", plugins=(_plugin(),))
    current = initial
    monkeypatch.setattr(
        plugin_command_client,
        "post_gateway_json",
        lambda *args, **kwargs: (200, {"ok": True, "catalog": current.to_payload()}),
    )
    client = PluginCommandClient(host(), "session-a", use_gateway=True)
    client.refresh()
    buffer = Buffer()
    bind_plugin_input(buffer, client)
    buffer.set_document(Document("/plugins@s"))
    choices = list(
        TuiInputCompleter(tmp_path, plugin_client=client).get_completions(
            buffer.document, CompleteEvent()
        )
    )
    buffer.complete_state = CompletionState(buffer.document, choices, complete_index=0)
    apply_selected_completion(buffer)
    assert buffer.text == "/plugins@sample "
    binding = buffer._my_agent_plugin_input
    assert binding.revision == initial.revision
    current = PluginCommandCatalog("scope", plugins=(replace(_plugin(), package_version="2.0"),))
    client.refresh()
    buffer.insert_text('read "中文 参数"')
    assert binding.revision == initial.revision != client.snapshot().revision
    buffer.set_document(Document("/plugins@sample read --li"))
    options = list(
        TuiInputCompleter(tmp_path, plugin_client=client).get_completions(
            buffer.document, CompleteEvent()
        )
    )
    assert options[0].catalog_revision == current.revision
    buffer.complete_state = CompletionState(buffer.document, options, complete_index=0)
    apply_selected_completion(buffer)
    assert buffer.text == "/plugins@sample read --limit "
    assert binding.revision == initial.revision
    buffer.set_document(Document("/plugins@different read"))
    assert binding.revision == ""
    buffer.reset()
    assert binding.revision == "" and binding.namespace == ""
