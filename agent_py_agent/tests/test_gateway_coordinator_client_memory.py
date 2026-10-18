# LLM: 只用临时原C/control/Root/lease、原middleware与Handler内存I/O；不绑定socket、建W或调用模型。
# 模块用途: 证明recent只读叶子与cold/healthy C真实入口缺口，保owner隔离、虚方法和零写入。
from __future__ import annotations

import copy
import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from importlib import import_module
from pathlib import Path

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_service, request_worker
from agent_py_agent.agent.gateway_parts.control_plane import GatewayControlPlane
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPHandler,
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator import coordinator_guard
from agent_py_agent.agent.gateway_parts.multiprocess_drain_runtime import GatewayDrainRuntime
from agent_py_agent.agent.gateway_parts.multiprocess_takeover_runtime import (
    prepare_coordinator_takeover,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory, MemoryRecord
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    home_paths_with_owner,
    resolve_owner_home,
)
from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent


# LLM: 所有快照只读本测试临时树，包含目录、内容与mtime；不接触真实owner或密钥。
# 函数用途: 捕获读取前后是否新增目录、索引、锁或改写记忆。
def snapshot(root):
    return {
        str(p.relative_to(root)): (
            p.is_dir(),
            None if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest(),
            p.stat().st_mtime_ns,
        )
        for p in root.rglob("*")
    }


# LLM: 预置的正式事件只是测试数据，不经过用户确认或声称真实promotion；保原版本/删除/过期及LF容错输入。
# 函数用途: 写出可由原JsonlMemory.all验证的唯一JSONL，之后读取必须零副作用。
def seed(path, label):
    records = [
        MemoryRecord("user", label + "旧", entry_id="stable", version=1),
        MemoryRecord("user", label + "当前", entry_id="stable", action="replace", version=2),
        MemoryRecord("user", label + "删除", entry_id="removed"),
        MemoryRecord("user", "", entry_id="removed", action="remove", version=2),
        MemoryRecord("user", label + "过期", entry_id="expired", expires_at=1),
        MemoryRecord("user", label + "末项", entry_id="tail"),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(record.to_json() for record in records) + "\n{broken\n[1]\n", encoding="utf-8"
    )
    return [label + "当前", label + "末项"]


# LLM: 载体只保存真实对象和预备事实，不实现Agent/权限接口或伪造原scope。
# 类用途: 把临时C和唯一记忆路径传给原Handler测试。
@dataclass
class Case:
    server: GatewayHTTPServer
    home: Path
    paths: dict
    expected: dict
    forbidden_calls: list


# LLM: 先原路径预置数据，再建立真实C和可选实际Echo主Agent；禁foreign/JsonlMemory构造，不启动任何HTTP服务或socket。
# 函数用途: 为cold和healthy两种C入口提供同一受管凭据与冻结来源。
@contextmanager
def memory_case(tmp_path, monkeypatch, *, healthy):
    root = (tmp_path / "home").resolve()
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    config = AgentConfig(
        my_agent_home=str(root),
        gateway_per_user_owner_scoping=True,
        model_backend="echo",
        enable_plugins=False,
        enable_subagents=False,
        prompt_files=[],
    )
    base = home_paths(root)
    paths = {}
    expected = {}
    owners = {
        "alice": OwnerIdentity.provider_user("local", "alice"),
        "bob": OwnerIdentity.provider_user("local", "bob"),
        "group": OwnerIdentity.provider_group("local", "same-name"),
        "main": OwnerIdentity.local_main(),
    }
    for key, owner in owners.items():
        home = home_paths_with_owner(base, resolve_owner_home(root, owner))
        paths[key] = resolve_runtime_paths_for_agent(config, home.owner_home_dir, home).paths[
            "memory_path"
        ]
        expected[key] = seed(paths[key], key)
    gateway = gateway_paths_from_root(tmp_path / "gateway")
    with coordinator_guard(gateway.root, 2):
        controller = prepare_coordinator_takeover(gateway, 2, "memory-C")
        control = GatewayControlPlane(
            config,
            root,
            gateway,
            2,
            controller.c_run_id,
            agent_root=tmp_path / "workspace",
            agent_factory=lambda: SimpleAgent(config, tmp_path / "workspace"),
        )
        runtime = GatewayDrainRuntime(control)
        agent = control.start_business(control.agent_factory) if healthy else None
        if healthy:
            assert type(agent) is SimpleAgent
            agent._gateway_control_plane = control
        middleware = AuthMiddleware(AuthManager(auth_enabled=True))
        server = GatewayHTTPServer(
            0,
            gateway,
            params=GatewayHTTPServerParams(
                agent=agent,
                control_plane=control,
                drain_runtime=runtime,
                auth_middleware=middleware,
            ),
        )
        server._prepare_local_credential()
        assert server.server is None and server._thread is None
        monkeypatch.setattr(http_service, "_server_instance", server)
        calls = []

        # LLM: 只禁止读期间发生不应有的构造与登记；不替代真实凭据、owner或C来源判断。
        # 函数用途: 若产品回落foreign Agent或带副作用的记忆构造器就保留真实原红。
        def forbidden(*args, **kwargs):
            calls.append("foreign-or-constructor")
            raise AssertionError("C recent cannot construct owner services")

        monkeypatch.setattr(SimpleAgent, "__init__", forbidden)
        monkeypatch.setattr(JsonlMemory, "__init__", forbidden)
        monkeypatch.setattr(request_worker, "_owner_pool", forbidden)
        monkeypatch.setattr(request_worker, "_record_active_owner", forbidden)
        yield Case(server, root, paths, expected, calls)


# LLM: 原HTTP Handler只换内存字节流，不开监听/网络；真实route、G1 middleware、身份副本、body和JSON编码全部执行。
# 函数用途: 用原宿主临时凭据调用客户端memory入口并返回原状态/完整JSON。
def request(case, body=None, *, actor="alice", credential=True, headers=()):
    payload = {"conversation_id": "same-name", "operation": "recent", "limit": 2, **(body or {})}
    raw = json.dumps(payload).encode()
    handler = GatewayHTTPHandler.__new__(GatewayHTTPHandler)
    handler.command = "POST"
    handler.path = "/client/memory"
    handler.request_version = "HTTP/1.1"
    handler.requestline = "POST /client/memory HTTP/1.1"
    handler.client_address = ("127.0.0.1", 1)
    handler.headers = Message()
    handler.headers["Content-Type"] = "application/json"
    handler.headers["Content-Length"] = str(len(raw))
    if credential:
        handler.headers["X-Gateway-Token"] = case.server._local_client_credential
    handler.headers["X-User-Id"] = actor
    handler.headers["X-Channel"] = "local"
    for name, value in headers:
        handler.headers[name] = value
    handler.rfile = io.BytesIO(raw)
    handler.wfile = io.BytesIO()
    handler._dispatch_route("POST")
    head, content = handler.wfile.getvalue().split(b"\r\n\r\n", 1)
    return int(head.split(b" ", 2)[1]), json.loads(content)


@pytest.mark.parametrize("healthy", [False, True], ids=["cold", "healthy"])
@pytest.mark.parametrize(
    "key,actor,metadata",
    [
        ("alice", "alice", {}),
        ("bob", "bob", {}),
        ("group", "alice", {"channel_chat_type": "group", "channel_chat_id": "same-name"}),
        ("group", "bob", {"channel_chat_type": "group", "channel_chat_id": "same-name"}),
        ("main", "local-agent", {}),
    ],
)
def test_c_recent_reads_only_original_owner_without_constructing_or_writing(
    tmp_path, monkeypatch, healthy, key, actor, metadata
):
    with memory_case(tmp_path, monkeypatch, healthy=healthy) as case:
        before = snapshot(case.home)
        status, result = request(
            case, {"metadata": metadata, "user_id": "body-spoof", "channel": "chat"}, actor=actor
        )
        assert status == 200 and result["ok"], result
        assert [r["content"] for r in result["records"]] == case.expected[key]
        assert [r["entry_id"] for r in result["records"]] == ["stable", "tail"]
        assert all(set(r) == {"entry_id", "kind", "role", "content"} for r in result["records"])
        assert case.forbidden_calls == [] and snapshot(case.home) == before
        assert case.server.server is None and case.server._thread is None


@pytest.mark.parametrize("healthy", [False, True])
@pytest.mark.parametrize("operation", ["search", "remember"])
def test_c_nonrecent_uses_original_current_owner_resources_without_agent(
    tmp_path, monkeypatch, healthy, operation
):
    from agent_py_agent.tests.test_coordinator_model_menu import post_model
    from agent_py_agent.tests.test_coordinator_private_credential_transactions import original_case

    # 原recent内存dispatch夹具没有真实HTTP work，不能伪造来给写源；两个已支持节点改用既有真实loopback设施。
    with original_case(tmp_path, monkeypatch) as (agent, _path, row, _body, server, _runtime):
        server.control_plane.config.memory_semantic_recall = False
        if healthy:
            server.agent = agent
        status, result = post_model(server,
            {"operation": operation, "conversation_id": "j3-supported-original",
             "query": "needle", "content": "save current owner"},
            route="/client/memory", actor=row["actor_id"])
        assert status == 200 and result["ok"], result
        if operation == "search":
            assert result["records"] == []
        else:
            assert result["records"][0]["content"] == "save current owner"
        assert server.agent is (agent if healthy else None)


@pytest.mark.parametrize("healthy", [False, True])
def test_c_missing_owner_stays_empty_without_creating_home(tmp_path, monkeypatch, healthy):
    with memory_case(tmp_path, monkeypatch, healthy=healthy) as case:
        before = snapshot(case.home)
        status, result = request(case, actor="never-seen")
        assert status == 200 and result == {
            "operation": "recent",
            "ok": True,
            "records": [],
            "error_code": "",
        }
        assert case.forbidden_calls == [] and snapshot(case.home) == before


@pytest.mark.parametrize(
    "fault", ["copy_control", "copy_runtime", "paths", "closed", "no_control", "no_runtime"]
)
def test_c_recent_rejects_original_source_fault_without_returning_records(
    tmp_path, monkeypatch, fault
):
    with memory_case(tmp_path, monkeypatch, healthy=True) as case:
        server = case.server
        if fault == "copy_control":
            server.control_plane = copy.copy(server.control_plane)
        elif fault == "copy_runtime":
            server.drain_runtime = copy.copy(server.drain_runtime)
        elif fault == "paths":
            server.paths = gateway_paths_from_root(server.paths.root)
        elif fault == "closed":
            server.drain_runtime._closing = True
        elif fault == "no_control":
            server.control_plane = None
        else:
            server.drain_runtime = None
        before = snapshot(case.home)
        status, result = request(case)
        assert status == 200 and not result["ok"] and result["records"] == [], result
        assert result["error_code"] == "ValueError"
        assert case.forbidden_calls == [] and snapshot(case.home) == before


@pytest.mark.parametrize("fault", ["credential", "duplicate_actor"])
def test_original_auth_and_scope_reject_before_memory_body_or_leaf(tmp_path, monkeypatch, fault):
    with memory_case(tmp_path, monkeypatch, healthy=False) as case:
        before = snapshot(case.home)
        status, _ = request(
            case,
            {},
            credential=fault != "credential",
            headers=(("X-User-Id", "bob"),) if fault == "duplicate_actor" else (),
        )
        assert status == 403
        assert case.forbidden_calls == [] and snapshot(case.home) == before


def test_explicit_path_leaf_reuses_original_materialization_without_constructor(
    tmp_path, monkeypatch
):
    path = tmp_path / "memory.jsonl"
    expected = seed(path, "leaf")
    original = JsonlMemory(path)
    before = snapshot(tmp_path)
    values = original.all()
    assert [r.content for r in values] == expected
    module = import_module("agent_py_agent.agent.memory_store.jsonl")

    def forbidden(*args, **kwargs):
        raise AssertionError("read leaf cannot construct JsonlMemory")

    monkeypatch.setattr(JsonlMemory, "__init__", forbidden)
    actual = module.read_memory_records_from_path(path)
    assert actual == values and snapshot(tmp_path) == before


def test_all_keeps_original_file_and_event_virtual_overrides(tmp_path):
    record = MemoryRecord("user", "override", entry_id="override")

    class Events(JsonlMemory):
        def _read_memory_events(self, path):
            assert path == self.path
            return [record]

    class File(JsonlMemory):
        def _read_memory_file(self, path):
            assert path == self.path
            return [record]

    assert Events(tmp_path / "events.jsonl").all() == [record]
    assert File(tmp_path / "file.jsonl").all() == [record]


@pytest.mark.parametrize("fault", ["closing", "control", "runtime", "paths"])
def test_c_source_closing_after_original_read_discards_records(tmp_path, monkeypatch, fault):
    with memory_case(tmp_path, monkeypatch, healthy=False) as case:
        module = import_module("agent_py_agent.agent.memory_store.jsonl")
        original = module.read_memory_records_from_path

        def close_after_read(*args, **kwargs):
            records = original(*args, **kwargs)
            if fault == "closing":
                case.server.drain_runtime._closing = True
            elif fault == "control":
                case.server.control_plane = copy.copy(case.server.control_plane)
            elif fault == "runtime":
                case.server.drain_runtime = copy.copy(case.server.drain_runtime)
            else:
                case.server.paths = gateway_paths_from_root(case.server.paths.root)
            return records

        monkeypatch.setattr(module, "read_memory_records_from_path", close_after_read)
        before = snapshot(case.home)
        status, result = request(case)
        assert status == 200 and not result["ok"] and result["records"] == []
        assert result["error_code"] == "ValueError" and snapshot(case.home) == before


def test_c_default_conversation_keeps_original_same_owner_semantics(tmp_path, monkeypatch):
    with memory_case(tmp_path, monkeypatch, healthy=False) as case:
        before = snapshot(case.home)
        status, result = request(case, {"conversation_id": ""})
        assert status == 200 and result["ok"]
        assert [row["content"] for row in result["records"]] == case.expected["alice"]
        assert case.forbidden_calls == [] and snapshot(case.home) == before


def test_c_materialization_error_remains_safe_and_does_not_return_partial_records(
    tmp_path, monkeypatch
):
    with memory_case(tmp_path, monkeypatch, healthy=False) as case:
        case.paths["alice"].write_text(
            json.dumps(
                {
                    "role": "user",
                    "content": "never expose",
                    "entry_id": "bad",
                    "version": "not-an-integer",
                }
            )
            + "\n"
        )
        before = snapshot(case.home)
        status, result = request(case)
        assert status == 200 and result == {
            "operation": "recent",
            "ok": False,
            "records": [],
            "error_code": "ValueError",
        }
        assert "never expose" not in str(result) and snapshot(case.home) == before
