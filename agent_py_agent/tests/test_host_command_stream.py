"""临时 HTTP、真实执行器/MCP 与 TUI 控制器组件测试；不计真实产品 TUI 或模型验收。"""

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.gateway_parts import command_stream, http_service
from agent_py_agent.agent.gateway_parts.command_stream_protocol import (
    COMMAND_STREAM_MAX_BYTES,
    CommandStreamFrame,
    command_approval_path,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.cli.chat_parts.command_interaction import CommandInteraction
from agent_py_agent.cli.chat_parts.plugin_command_client import PluginCommandClient
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
from agent_py_agent.tests.test_plugin_invocation import enabled_plugin
from agent_py_agent.tests.test_tui_runtime import _approval_request


# LLM: 只轮询组件测试中的结构化条件，限时结束；不按模型文案认定执行状态。
# 函数用途: 等待临时 HTTP/审批线程产生可核对的状态。
def wait_until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail("组件未在期限内到达预期状态")


# LLM: 临时服务只绑定随机 loopback 端口，不启动模型或真实 Gateway 守护进程；结束恢复原进程内指针。
# 函数用途: 将原 HTTP handler、实际插件执行与临时客户端连在一起。
@contextmanager
def plugin_host(tmp_path, monkeypatch, *, owner=None):
    service, source, command = enabled_plugin(tmp_path)
    inside_owner = service.context.owner.home_dir / source.name
    source.rename(inside_owner)
    source, command = inside_owner, f'/plugins@sample-peek read "{inside_owner}"'
    paths = gateway_paths_from_root(tmp_path / "shared-gateway")
    base = SimpleNamespace(config=AgentConfig(auth_enabled=False, path_access_mode="full", gateway_per_user_owner_scoping=False),
                           home_paths=home_paths(service.context.owner.root))
    monkeypatch.setattr(command_stream, "_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(http_service, "_server_instance", None)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=base))
    server.start()
    port = server.server.server_address[1]
    config = AgentConfig(gateway_port=port, gateway_workspace="not-the-connected-service")
    agent = SimpleNamespace(config=config, owner_identity=owner or OwnerIdentity.local_main(), root=service.context.owner.home_dir,
                            home_paths=base.home_paths)
    client = PluginCommandClient(agent, "transport-session", use_gateway=True)
    try:
        yield client, paths, source, command
    finally:
        server.stop()
        wait_until(lambda: not any(thread.name in {"host-command-heartbeat", "host-command-permission"}
                                   for thread in threading.enumerate()))
        removed = service.command("/plugins remove sample-peek", revision=service.catalog().revision,
                                  request_id="fixture-cleanup")
        assert removed["state"] == "succeeded", removed


@pytest.mark.parametrize("kind", ["connected", "heartbeat", "permission_requested", "permission_resolved", "result"])
def test_frame_identity_schema_and_length_are_closed(kind):
    raw = CommandStreamFrame("request-a", kind, {"text": "中文"}).encode()
    assert CommandStreamFrame.decode(raw, request_id="request-a").payload == {"text": "中文"}
    for invalid in (raw[:-1], b"{}\n", raw.replace(b'"host_command_stream.v1"', b'"v0"')):
        with pytest.raises(ValueError):
            CommandStreamFrame.decode(invalid, request_id="request-a")
    with pytest.raises(ValueError):
        CommandStreamFrame.decode(raw, request_id="request-b")
    with pytest.raises(ValueError):
        CommandStreamFrame("request-a", kind, {"text": "x" * COMMAND_STREAM_MAX_BYTES}).encode()


def test_approval_parent_is_bound_to_connected_gateway_owner_and_command(tmp_path):
    paths = gateway_paths_from_root(tmp_path)
    alice, bob = (OwnerIdentity.provider_user("local", who) for who in ("alice", "bob"))
    parents = {command_approval_path(paths, owner, request).parent
               for owner in (alice, bob) for request in ("one", "two")}
    assert len(parents) == 4
    assert all(path.is_relative_to(paths.processing / ".commands") for path in parents)
    assert not list(tmp_path.iterdir())
    for invalid in ("../other", "", "a/b", "a" * 129):
        with pytest.raises(ValueError):
            command_approval_path(paths, alice, invalid)


@pytest.mark.parametrize("case", ["missing", "duplicate", "wrong_id", "bad_owner", "path_payload"])
def test_client_rejects_unbound_or_changed_connection_before_opening_approval(tmp_path, case):
    from agent_py_agent.cli.chat_parts.plugin_command_stream import (
        _PermissionConsumer,
        _read_command_frames,
    )

    def unexpected(*args, **kwargs):
        pytest.fail("无效连接不能打开审批")
    interaction = CommandInteraction("original", unexpected, CancellationToken(), gateway_paths_from_root(tmp_path))
    owner = {"provider": "local", "owner_kind": "main", "owner_id": "main"}
    ready = CommandStreamFrame("original", "connected", {"owner": owner}).encode()
    rows = {
        "missing": CommandStreamFrame("original", "result", {"ok": True}).encode(),
        "duplicate": ready + ready,
        "wrong_id": CommandStreamFrame("other", "connected", {"owner": owner}).encode(),
        "bad_owner": CommandStreamFrame("original", "connected", {"owner": {"owner_id": "main"}}).encode(),
        "path_payload": CommandStreamFrame("original", "connected", {"owner": owner, "path": str(tmp_path)}).encode(),
    }
    consumer = _PermissionConsumer(interaction)
    with pytest.raises(ValueError):
        _read_command_frames(BytesIO(rows[case]), interaction.request_id, consumer)
    assert consumer.worker is None and not list(tmp_path.iterdir())


@pytest.mark.parametrize("decision", ["approved", "denied", "cancelled"])
def test_real_http_to_tui_approval_preserves_original_execution_and_foreground(tmp_path, monkeypatch, decision):
    with plugin_host(tmp_path, monkeypatch) as (client, paths, source, command):
        runtime = TuiRuntime("command-ui")
        runtime.begin_turn("core-turn")
        before = runtime.store.snapshot()
        controller = runtime.command_permission_controller("business")
        token = CancellationToken()
        interaction = CommandInteraction("business", controller.request_permission, token, paths)
        revision = client.refresh()["catalog"]["revision"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.command, command, revision=revision, interaction=interaction)
            try:
                overlay = wait_until(lambda: runtime.store.snapshot().permission or future.done())
                assert overlay is not True, future.result()
                assert not source.with_suffix(".calls").exists()
                assert list(runtime._turns) == ["core-turn"]
                assert controller.resolve(overlay.permission_id, decision)
                result = future.result(timeout=10)
            finally:
                token.cancel()
                controller.cancel_pending()
        assert result["state"] == ("succeeded" if decision == "approved" else "rejected"), result
        assert result["connection_cleanup"]["confirmed"]
        assert source.with_suffix(".calls").exists() is (decision == "approved")
        after = runtime.store.snapshot()
        assert after.permission is None
        assert replace(after.status, last_event_at=before.status.last_event_at) == before.status
        replay = client.command(command, revision=revision,
                                interaction=replace(interaction, cancellation_token=CancellationToken()))
        assert replay["state"] == result["state"] and runtime.store.snapshot().permission is None
        queried = client.command("/plugins status business")
        assert queried["state"] == result["state"]
        if decision == "approved":
            assert source.with_suffix(".calls").read_text().splitlines() == ["called"]
            assert "插件原生结果" in queried["message"]


def test_disconnect_cancels_only_original_wait_and_replay_cannot_execute(tmp_path, monkeypatch):
    ended = threading.Event()
    original_stream = command_stream.serve_command_stream
    def observe_stream(handler, paths, owner, request_id, execute):
        try:
            return original_stream(handler, paths, owner, request_id, execute)
        finally:
            if request_id == "departed":
                ended.set()
    monkeypatch.setattr(command_stream, "serve_command_stream", observe_stream)
    with plugin_host(tmp_path, monkeypatch) as (client, paths, source, command):
        runtime = TuiRuntime("disconnect-ui")
        controller = runtime.command_permission_controller("departed")
        token = CancellationToken()
        interaction = CommandInteraction("departed", controller.request_permission, token, paths)
        revision = client.refresh()["catalog"]["revision"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.command, command, revision=revision, interaction=interaction)
            try:
                assert wait_until(lambda: runtime.store.snapshot().permission or future.done()) is not True
                token.cancel()
                result = future.result(timeout=10)
                assert result["state"] == "outcome_unknown" and result["request_id"] == "departed"
                closed = wait_until(lambda: (row if (row := client.command("/plugins status departed"))["state"] != "running" else None))
                assert closed["state"] == "rejected" and closed["error_code"] == "CANCELLED", closed
                assert runtime.store.snapshot().permission is None
                assert not source.with_suffix(".calls").exists()
                assert ended.wait(10)  # 原任务终态与单次连接退出是两个不同事实。
            finally:
                token.cancel()
                controller.cancel_pending()
        def approve(value, **_):
            return {"permission_id": value["permission_id"], "decision": "approved"}
        fresh = CommandInteraction("survivor", approve, CancellationToken(), paths)
        survived = client.command(command, revision=revision, interaction=fresh)
        assert survived["state"] == "succeeded", {key: value for key, value in survived.items() if key != "catalog"}
        repeated = CommandInteraction("departed", approve, CancellationToken(), paths)
        assert client.command(command, revision=revision, interaction=repeated)["state"] == "rejected"
        assert source.with_suffix(".calls").read_text().splitlines() == ["called"]


def test_command_approval_queue_does_not_create_or_cancel_foreground_turn():
    runtime = TuiRuntime("fifo-ui")
    foreground = runtime.begin_turn("core")
    first = runtime.command_permission_controller("one")
    second = runtime.command_permission_controller("two")
    requests = [_approval_request(name) for name in ("core", "one", "two")]
    controllers = [foreground, first, second]
    token = CancellationToken()
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = []
        try:
            for index, (controller, request) in enumerate(zip(controllers, requests)):
                futures.append(pool.submit(controller.request_permission, request.to_dict(), cancellation_token=token))
                if index == 0:
                    wait_until(lambda: runtime.store.snapshot().permission)
            wait_until(lambda: len(runtime._permission_coordinator._queued) == 2)
            first.cancel_pending()
            assert futures[1].result(timeout=2)["decision"] == "cancelled"
            assert runtime.store.snapshot().permission.permission_id == requests[0].permission_id
            assert runtime.resolve_permission(requests[0].permission_id, "approved")
            assert futures[0].result(timeout=2)["decision"] == "approved"
            wait_until(lambda: runtime.store.snapshot().permission and
                       runtime.store.snapshot().permission.permission_id == requests[2].permission_id)
            assert second.resolve(requests[2].permission_id, "denied")
            assert futures[2].result(timeout=2)["decision"] == "denied"
            assert list(runtime._turns) == ["core"]
        finally:
            token.cancel()
            first.cancel_pending()
            second.cancel_pending()


def test_noninteractive_request_returns_without_waiting_for_a_missing_consumer(tmp_path, monkeypatch):
    with plugin_host(tmp_path, monkeypatch) as (client, _paths, source, command):
        result = client.command(command)
        assert result["state"] == "approval_required" and result["connection_cleanup"]["confirmed"], result
        assert not source.with_suffix(".calls").exists()


def test_gateway_owner_mapping_uses_server_canonical_owner_for_approval(tmp_path, monkeypatch):
    alice = OwnerIdentity.provider_user("local", "alice")
    with plugin_host(tmp_path, monkeypatch, owner=alice) as (client, paths, source, command):
        def approve(value, **_):
            return {"permission_id": value["permission_id"], "decision": "approved"}
        interaction = CommandInteraction("mapped", approve, CancellationToken(), paths)
        result = client.command(command, interaction=interaction)
        assert result["state"] == "succeeded", result
        assert source.with_suffix(".calls").read_text().splitlines() == ["called"]
        assert not command_approval_path(paths, alice, "mapped").parent.exists()


def test_invalid_interactive_body_cannot_supply_a_path_or_start_execution(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_client_context import post_gateway_json

    with plugin_host(tmp_path, monkeypatch) as (client, _paths, source, command):
        port = client.agent.config.gateway_port
        for change in ({"interactive": "true"}, {"plugin_request_id": "../wrong"}):
            body = {"operation": "command", "command": command, "interactive": True,
                    "plugin_request_id": "original", "conversation_id": "transport-session",
                    "chunk_path": str(tmp_path / "injected"), **change}
            status, result = post_gateway_json(port, OwnerIdentity.local_main(), "/client/plugins", body, timeout=3)
            assert status == 400 and result["ok"] is False
        assert not source.with_suffix(".calls").exists()
        assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize("phase", ["before_open", "visible", "queued"])
def test_tui_coroutine_cancellation_releases_only_its_command(monkeypatch, phase):
    from agent_py_agent.cli.chat_parts import tui, tui_actions
    from agent_py_agent.cli.chat_parts.tui_params import TuiHandleCommandParams
    from agent_py_agent.cli.chat_parts.tui_plugin_commands import submit_plugin_command

    runtime = TuiRuntime("cancel-ui")
    runtime.begin_turn("core")
    other = runtime.command_permission_controller("other")
    if phase == "queued":
        other.open(_approval_request("other"))
    template = TuiHandleCommandParams(
        "/plugins list", None, None, [], [], False, None, threading.Lock(),
        [True], [0], ["core"], ["core"], [0], [False], threading.Event(), [],
    )
    monkeypatch.setattr(tui_actions, "_handle_command_params", lambda *_: template)
    started, release, finished = (threading.Event() for _ in range(3))
    observed = {}

    def worker(*, params):
        interaction = params.command_interaction
        observed["interaction"] = interaction
        started.set()
        try:
            if phase == "before_open":
                assert release.wait(5)
            observed["decision"] = interaction.request_permission(
                _approval_request(interaction.request_id).to_dict(), cancellation_token=interaction.cancellation_token)
        finally:
            finished.set()

    monkeypatch.setattr(tui, "_tui_handle_command", worker)

    async def scenario():
        tasks = []
        def launch(coro):
            tasks.append(asyncio.create_task(coro))
        event = SimpleNamespace(app=SimpleNamespace(create_background_task=launch, invalidate=lambda: None))
        try:
            assert submit_plugin_command(event, SimpleNamespace(tui_runtime=runtime, paths=None), "/plugins list", None, "")
            await asyncio.to_thread(wait_until, started.is_set)
            if phase == "visible":
                await asyncio.to_thread(wait_until, lambda: runtime.store.snapshot().permission)
            elif phase == "queued":
                await asyncio.to_thread(wait_until, lambda: len(runtime._permission_coordinator._queued) == 1)
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
            assert observed["interaction"].cancellation_token.cancelled
            if phase == "before_open":
                assert not finished.is_set()
            release.set()
            await asyncio.to_thread(wait_until, finished.is_set)
            assert observed["decision"]["decision"] == "cancelled"
            assert list(runtime._turns) == ["core"]
            overlay = runtime.store.snapshot().permission
            assert (overlay.permission_id == _approval_request("other").permission_id) if phase == "queued" else overlay is None
        finally:
            release.set()
            other.cancel_pending()

    asyncio.run(scenario())


def test_management_uses_original_cancel_token_before_install_side_effect(tmp_path):
    from agent_py_agent.tests.test_plugin_management import manager

    service, source = manager(tmp_path)
    token = CancellationToken()
    token.cancel()
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                             request_id="cancelled-install", cancellation_token=token)
    assert result["state"] == "rejected" and result["error_code"] == "CANCELLED", result
    assert not service.installations.snapshot() and source.exists()
