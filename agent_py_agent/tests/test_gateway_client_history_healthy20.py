"""健康C实际base Agent也须走原只读history leaf；不创建foreign业务Agent。"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import request_worker
from agent_py_agent.agent.gateway_parts.client_service import read_gateway_client_history_from_store
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_coordinator_client_history import post, seed, snapshot


# LLM: 本载体只保存原C/HTTP与实际SimpleAgent，不实现或转发Agent接口；不能用它授owner/actor权限。
# 类用途: 在临时回环HTTP测试中持有真实健康C及原Store。
@dataclass
class HealthyHistoryCase:
    server: GatewayHTTPServer
    agent: SimpleAgent
    stores: dict
    threads: dict
    home: object
    forbidden_calls: list


# LLM: 先原Store播种，再由原C的start_business实际构造一次SimpleAgent echo；HTTP健康赋值沿CLI真实时序，随后禁止任何foreign构造/登记。
# 函数用途: 启动隔离的健康C回环设施，不启动W/真实模型或日常服务。
@pytest.fixture
def healthy(tmp_path, monkeypatch):
    home = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    workspace = tmp_path / "workspace"
    config = AgentConfig(my_agent_home=str(home), gateway_per_user_owner_scoping=True,
        conversation_workspace=str(tmp_path / "main-conversations"), model_backend="echo",
        enable_plugins=False, enable_subagents=False, prompt_files=[])
    base, stores, threads = home_paths(home), {}, {}
    for actor in ("alice", "bob"):
        stores[actor], threads[actor] = seed(config, base, OwnerIdentity.provider_user("local", actor), actor, "shared-name")
        stores["group-" + actor], threads["group-" + actor] = seed(config, base, OwnerIdentity.provider_group("local", "group-1"), actor, "group-1")
    stores["main"], threads["main"] = seed(config, base, OwnerIdentity.local_main(), "local-agent", "main-session")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    with coordinator_guard(paths.root, 2):
        controller = prepare_coordinator_takeover(paths, 2, "healthy-history-C")
        control = GatewayControlPlane(config, home, paths, 2, controller.c_run_id,
            agent_factory=lambda: SimpleAgent(config, workspace), agent_root=workspace)
        runtime = GatewayDrainRuntime(control)
        server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(control_plane=control, drain_runtime=runtime))
        try:
            server.start()
            agent = control.start_business(control.agent_factory)
            assert type(agent) is SimpleAgent
            agent._gateway_control_plane = control
            server.agent = agent
            forbidden_calls = []

            # LLM: 钩子只观察/禁止读取期不该发生的写者创建；原C、Store、认证、分页与权限均不替换。
            # 函数用途: 若回退foreign Agent主链就留下原红，不能借假Agent满足接口。
            def forbidden(*_args, **_kwargs):
                forbidden_calls.append("foreign-agent-or-owner-write")
                raise AssertionError("健康C history不得初始化foreign Agent/pool或登记active owner")

            monkeypatch.setattr(request_worker, "_owner_pool", forbidden)
            monkeypatch.setattr(request_worker, "_record_active_owner", forbidden)
            monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
            yield HealthyHistoryCase(server, agent, stores, threads, home, forbidden_calls)
        finally:
            server.stop()


@pytest.mark.parametrize("key,actor,conversation,metadata", [
    ("alice", "alice", "shared-name", {}),
    ("bob", "bob", "shared-name", {}),
    ("group-alice", "alice", "group-1", {"channel_chat_type": "group", "channel_chat_id": "group-1"}),
    ("group-bob", "bob", "group-1", {"channel_chat_type": "group", "channel_chat_id": "group-1"}),
    ("main", "local-agent", "main-session", {}),
])
def test_healthy_c_reads_exact_owner_actor_original_json_and_media_without_writes(healthy, key, actor, conversation, metadata):
    scope = GatewayControlScope(actor, "local", conversation, metadata=metadata)
    expected = read_gateway_client_history_from_store(healthy.stores[key], scope=scope, max_turns=2).to_dict()
    before = snapshot(healthy.home)
    status, result = post(healthy, {"conversation_id": conversation, "limit": 2,
        "user_id": "body-cannot-override", "channel": "chat", "metadata": metadata}, actor=actor)
    assert status == 200 and result == json.loads(json.dumps(expected)), result
    assert result["thread_id"] == healthy.threads[key].thread_id
    assert any("fixture.png" in str(event) for event in result["display_events"])
    assert healthy.server.agent is healthy.agent and healthy.forbidden_calls == []
    assert snapshot(healthy.home) == before


def test_healthy_c_reverse_pages_keep_original_byte_cursors_and_all_display_groups(healthy):
    before, found = None, []
    original = read_gateway_client_history_from_store(healthy.stores["alice"],
        scope=GatewayControlScope("alice", "local", "shared-name"), max_turns=200).to_dict()
    expected = [event["payload"]["text"] for event in original["display_events"] if event["kind"] == "user_message"]
    baseline = snapshot(healthy.home)
    while True:
        body = {"conversation_id": "shared-name", "limit": 2}
        if before is not None:
            body["before_message_cursor"] = before
        status, result = post(healthy, body)
        assert status == 200 and result["ok"], result
        found[:0] = [event["payload"]["text"] for event in result["display_events"] if event["kind"] == "user_message"]
        assert result["message_cursor"] == (before if before is not None else healthy.stores["alice"].storage.message_path(healthy.threads["alice"].thread_id).stat().st_size)
        if not result["has_older"]:
            break
        assert 0 < result["before_message_cursor"] < result["message_cursor"]
        before = result["before_message_cursor"]
    assert found == expected and len(found) == len(set(found)) == 8
    assert all(f"alice问题{turn}" in found[turn] for turn in range(8))
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == baseline


def test_healthy_c_keeps_original_source_gate_even_when_base_agent_is_usable(healthy):
    healthy.server.control_plane = copy.copy(healthy.server.control_plane)
    before = snapshot(healthy.home)
    status, result = post(healthy, {"conversation_id": "main-session"}, actor="local-agent")
    assert status == 200 and not result["ok"] and not result["turns"]
    assert result["load_errors"] == [{"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": "ValueError"}]
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == before


@pytest.mark.parametrize("missing", ["control_plane", "drain_runtime"])
def test_healthy_c_partial_transport_cannot_fall_back_to_existing_base_agent(healthy, missing):
    setattr(healthy.server, missing, None)
    before = snapshot(healthy.home)
    status, result = post(healthy, {"conversation_id": "main-session"}, actor="local-agent")
    assert status == 200 and not result["ok"] and not result["turns"] and not result["display_events"]
    assert result["load_errors"] == [{"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": "ValueError"}]
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == before


def test_healthy_c_source_closing_after_read_discards_original_base_page(healthy, monkeypatch):
    from agent_py_agent.agent.conversation.store_messages import MessageStore
    original = MessageStore.history_page_report

    def close_after_read(*args, **kwargs):
        result = original(*args, **kwargs)
        healthy.server.drain_runtime._closing = True
        return result

    monkeypatch.setattr(MessageStore, "history_page_report", close_after_read)
    before = snapshot(healthy.home)
    status, result = post(healthy, {"conversation_id": "main-session"}, actor="local-agent")
    assert status == 200 and not result["ok"] and not result["turns"] and not result["display_events"]
    assert result["load_errors"] == [{"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": "ValueError"}]
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == before


@pytest.mark.parametrize("fault", ["credential", "duplicate_actor", "cursor"])
def test_healthy_c_original_http_rejections_still_precede_owner_reads(healthy, monkeypatch, fault):
    from agent_py_agent.agent.conversation.store import ConversationStore

    def forbidden(*_args, **_kwargs):
        raise AssertionError("HTTP拒绝不得打开owner Store")

    monkeypatch.setattr(ConversationStore, "__init__", forbidden)
    body = {"conversation_id": "shared-name"}
    if fault == "cursor":
        body["before_message_cursor"] = True
    before = snapshot(healthy.home)
    status, _ = post(healthy, body, credential=fault != "credential",
        headers=(("X-User-Id", "bob"),) if fault == "duplicate_actor" else ())
    assert status == (400 if fault == "cursor" else 403)
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == before


def test_healthy_c_unreadable_foreign_store_keeps_original_safe_error(healthy, monkeypatch):
    from agent_py_agent.agent.conversation.store_threads import ThreadStore

    def unreadable(*_args, **_kwargs):
        raise PermissionError("/synthetic/private/never-echo")

    monkeypatch.setattr(ThreadStore, "resolve_report", unreadable)
    before = snapshot(healthy.home)
    status, result = post(healthy)
    assert status == 200 and not result["ok"] and not result["turns"]
    assert result["load_errors"] == [{"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": "PermissionError"}]
    assert "never-echo" not in json.dumps(result)
    assert healthy.forbidden_calls == [] and snapshot(healthy.home) == before
