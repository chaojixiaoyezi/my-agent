"""锁定同 owner/thread 发现、停止/排队状态和 history→progress 原 HTTP 游标合同。"""
from __future__ import annotations

import copy
import http.client
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest

from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import client_inflight_projection as projection
from agent_py_agent.agent.gateway_parts.client_service import GatewayClientHistoryResult
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _scope_request_payload,
)
from agent_py_agent.agent.gateway_parts.http_handlers import _read_public_progress_events
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_owner_binding import REQUEST_OWNER_KEY
from agent_py_agent.agent.gateway_parts.request_worker import _resolve_request_owner_from_config
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_gateway_coordinator_client_history import (
    case as coordinator_case,  # noqa: F401
)
from agent_py_agent.tests.test_gateway_coordinator_client_history import post, seed, snapshot


@pytest.fixture(autouse=True)
def isolated_home_and_cursor_cache(tmp_path, monkeypatch):
    """所有测试读写都留在临时 HOME；位置缓存不得跨用例保留。"""
    root = tmp_path / "isolated-home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("MY_AGENT_HOME", str(root / ".my-agent"))
    projection._CURSORS.clear()
    yield
    projection._CURSORS.clear()


@dataclass
class InflightCase:
    server: GatewayHTTPServer
    scope: GatewayControlScope
    thread_id: str
    home: Path


@pytest.fixture(params=["single", "coordinator"])
def inflight_case(request, tmp_path, monkeypatch):
    """两种入口都使用原 server、owner resolver、Store 和认证，不伪造 history JSON。"""
    if request.param == "coordinator":
        original = request.getfixturevalue("coordinator_case")
        yield InflightCase(original.server, GatewayControlScope("alice", "local", "shared-name"),
                           original.threads["alice"].thread_id, original.home)
        return
    home = tmp_path / "single-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    config = AgentConfig(my_agent_home=str(home), gateway_per_user_owner_scoping=True,
                         conversation_workspace=str(tmp_path / "single-conversations"),
                         model_backend="echo", enable_plugins=False, enable_subagents=False,
                         prompt_files=[])
    _, thread = seed(config, home_paths(home), OwnerIdentity.local_main(), "local-agent", "main-session")
    agent = SimpleAgent(config, tmp_path / "workspace")
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"),
                               params=GatewayHTTPServerParams(agent=agent))
    try:
        server.start()
        yield InflightCase(server, GatewayControlScope("local-agent", "local", "main-session"),
                           thread.thread_id, home)
    finally:
        server.stop()


def request_record(case, request_id, *, pending=False, **updates):
    """按原 writer 的 scope、冻结 owner 与 claim 形状准备磁盘输入，不替换被测读口。"""
    payload = {**_scope_request_payload(case.scope), "id": request_id,
               "status": "pending" if pending else "processing", "created_at": 1,
               "turn_phase": "open", "conversation_thread_id": case.thread_id}
    if not pending:
        payload["conversation_claim"] = {"schema_version": "gateway_conversation_claim.v1",
            "request_id": request_id, "thread_id": case.thread_id, "task_id": f"gateway:{request_id}"}
    payload.update(updates)
    control = case.server.control_plane
    config = control.config if control is not None else case.server.agent.config
    home = control.home_root if control is not None else case.server.agent.home_paths.root
    owner = _resolve_request_owner_from_config(config, payload, home)
    payload.setdefault(REQUEST_OWNER_KEY, asdict(owner))
    folder = case.server.paths.inbox if pending else case.server.paths.processing
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def chunk_rows(case, request_id, texts, *, append=False):
    path = gateway_chunk_path(case.server.paths, request_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = b"".join((json.dumps({"kind": "assistant_commentary", "text": text}) + "\n").encode()
                    for text in texts)
    with path.open("ab" if append else "wb") as handle:
        handle.write(rows)
    return path


def discover(case, **kwargs):
    return projection.client_inflight_projection(case.server, case.scope, case.thread_id, **kwargs)


def idle_fields():
    body = GatewayClientHistoryResult(True).to_dict()
    return {key: body[key] for key in ("active_request_id", "in_flight_status", "turn_phase",
                                     "stream_cursor", "progress_path", "queued_request_ids")}


def get_progress(case, path):
    connection = http.client.HTTPConnection("127.0.0.1", case.server.server.server_address[1], timeout=3)
    try:
        connection.request("GET", path, headers={"X-Gateway-Token": case.server._local_client_credential,
            "X-User-Id": case.scope.user_id, "X-Channel": case.scope.channel})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_history_result_defaults_and_queue_are_json_safe():
    assert idle_fields() == {"active_request_id": "", "in_flight_status": "idle", "turn_phase": "",
                             "stream_cursor": 0, "progress_path": "", "queued_request_ids": []}
    body = GatewayClientHistoryResult(True, queued_request_ids=("q-1", "q-2")).to_dict()
    assert json.loads(json.dumps(body))["queued_request_ids"] == ["q-1", "q-2"]


def test_helper_idle_is_read_only(inflight_case):
    case = inflight_case
    before = snapshot(case.home)
    assert discover(case) == idle_fields()
    assert snapshot(case.home) == before


def test_helper_five_rows_then_only_two_new_progress_rows(inflight_case):
    case = inflight_case
    request_record(case, "active")
    chunk_rows(case, "active", [f"old-{i}" for i in range(5)])
    before = snapshot(case.home)
    body = discover(case)
    assert body == {"active_request_id": "active", "in_flight_status": "processing", "turn_phase": "open",
                    "stream_cursor": 5, "progress_path": "/progress/active?since=5", "queued_request_ids": []}
    assert snapshot(case.home) == before
    chunk_rows(case, "active", ["new-5", "new-6"], append=True)
    status, progress = get_progress(case, body["progress_path"])
    assert status == 200, progress
    assert [row["text"] for row in progress["events"]] == ["new-5", "new-6"]
    assert progress["next"] == 7
    assert get_progress(case, "/progress/active?since=7")[1]["events"] == []


@pytest.mark.parametrize("foreign", ["actor", "owner", "thread", "claim", "conversation", "terminal"])
def test_helper_never_exposes_foreign_or_closed_request(inflight_case, foreign):
    case = inflight_case
    updates = {}
    if foreign == "actor":
        updates["user_id"] = "another-actor"
    elif foreign == "owner":
        updates[REQUEST_OWNER_KEY] = asdict(OwnerIdentity.provider_user("local", "another-owner"))
    elif foreign == "thread":
        updates["conversation_thread_id"] = "another-thread"
    elif foreign == "claim":
        updates["conversation_claim"] = {"thread_id": "another-thread"}
    elif foreign == "conversation":
        updates["conversation"] = {**_scope_request_payload(case.scope)["conversation"],
                                    "channel_conversation_id": "another-conversation"}
    else:
        updates["status"] = "interrupted"
    request_record(case, "foreign-only", **updates)
    chunk_rows(case, "foreign-only", ["foreign text"])
    assert discover(case) == idle_fields()


def test_helper_pending_order_and_active_precedence(inflight_case):
    case = inflight_case
    request_record(case, "queue-z", pending=True, created_at=2)
    request_record(case, "queue-b", pending=True, created_at=1)
    request_record(case, "queue-a", pending=True, created_at=1)
    queued = discover(case)
    assert queued["queued_request_ids"] == ["queue-a", "queue-b", "queue-z"]
    assert (queued["active_request_id"], queued["in_flight_status"], queued["stream_cursor"]) == ("queue-a", "pending", 0)
    request_record(case, "active")
    active = discover(case)
    assert active["active_request_id"] == "active"
    assert active["queued_request_ids"] == queued["queued_request_ids"]


def test_helper_stopping_keeps_original_closing_phase(inflight_case):
    case = inflight_case
    request_record(case, "stopping", cancel_requested=True, control_status="stopping", turn_phase="closing")
    assert discover(case)["in_flight_status"] == "stopping"
    assert discover(case)["turn_phase"] == "closing"


def test_helper_recovery_conflict_does_not_guess_latest_active(inflight_case):
    request_record(inflight_case, "active-1", created_at=1)
    request_record(inflight_case, "active-2", created_at=2)
    assert discover(inflight_case) == idle_fields()


@pytest.mark.parametrize("budget", [{"max_request_files": 0}, {"max_request_files": 1}, {"max_request_bytes": 1}])
def test_helper_request_budget_fails_closed(inflight_case, budget):
    request_record(inflight_case, "active")
    request_record(inflight_case, "queue", pending=True)
    assert discover(inflight_case, **budget) == idle_fields()


def test_helper_chunk_budget_can_only_return_confirmed_prefix(inflight_case):
    case = inflight_case
    request_record(case, "active")
    path = chunk_rows(case, "active", [f"row-{i}" for i in range(8)])
    raw = path.read_bytes()
    budget = len(raw) // 3
    body = discover(case, max_chunk_scan_bytes=budget)
    assert body["stream_cursor"] == raw[:budget].count(b"\n")
    assert body["stream_cursor"] < 8
    events, next_cursor = _read_public_progress_events(path, body["stream_cursor"])
    assert len(events) == 8 - body["stream_cursor"] and next_cursor == 8
    assert discover(case, max_chunk_scan_bytes=0)["stream_cursor"] == 0


def test_stream_cache_incremental_budget_half_line_and_replacement(tmp_path):
    path = tmp_path / "cursor.chunks.jsonl"
    path.write_bytes(b"x\n" * 10 + b"half")
    def cursor():
        return projection._stream_cursor(path, max_scan_bytes=4, max_cached_streams=2)
    assert cursor() == 2
    assert cursor() == 4
    assert cursor() == 6
    assert cursor() == 8
    assert cursor() == 10
    assert cursor() == 10
    with path.open("ab") as handle:
        handle.write(b"\n")
    assert cursor() == 11
    # 同 inode 截断后立即长回：有界首尾摘要必须丢弃旧前缀。
    path.write_bytes(b"different-long-prefix\n" * 3)
    assert cursor() == 0
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"a\nb\n")
    replacement.replace(path)
    assert cursor() == 2


def test_stream_cache_is_bounded_and_missing_unreadable_returns_zero(tmp_path, monkeypatch):
    for i in range(4):
        path = tmp_path / f"{i}.chunks.jsonl"
        path.write_bytes(b"a\n")
        assert projection._stream_cursor(path, max_scan_bytes=4, max_cached_streams=2) == 1
    assert len(projection._CURSORS) == 2
    assert projection._stream_cursor(tmp_path / "missing", max_scan_bytes=4, max_cached_streams=2) == 0
    original = projection._open_chunk_file

    def denied(path):
        if path.name == "denied":
            raise PermissionError("synthetic-private-do-not-echo")
        return original(path)

    monkeypatch.setattr(projection, "_open_chunk_file", denied)
    assert projection._stream_cursor(tmp_path / "denied", max_scan_bytes=4, max_cached_streams=2) == 0


def test_stream_cursor_reads_only_bounded_increment_and_boundary(tmp_path, monkeypatch):
    path = tmp_path / "incremental.chunks.jsonl"
    path.write_bytes(b"old\n" * 1024)
    original = projection._open_chunk_file
    reads = []

    class ObservedReader:
        """仅统计真实文件读取字节，仍由原 fd、stat 和游标逻辑完成读取。"""

        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size):
            assert size >= 0
            data = self.handle.read(size)
            reads.append(len(data))
            return data

    monkeypatch.setattr(projection, "_open_chunk_file", lambda target: ObservedReader(original(target)))
    assert projection._stream_cursor(path, max_scan_bytes=8192, max_cached_streams=2) == 1024
    assert sum(reads) <= path.stat().st_size + 128
    reads.clear()
    assert projection._stream_cursor(path, max_scan_bytes=8192, max_cached_streams=2) == 1024
    assert sum(reads) <= 256
    with path.open("ab") as handle:
        handle.write(b"new\n")
    reads.clear()
    assert projection._stream_cursor(path, max_scan_bytes=8192, max_cached_streams=2) == 1025
    assert sum(reads) <= 4 + 256


@pytest.mark.parametrize("fault", ["copy_control", "missing_runtime", "closing", "change_during_read"])
def test_helper_coordinator_keeps_original_source_gate(request, monkeypatch, fault):
    original = request.getfixturevalue("coordinator_case")
    case = InflightCase(original.server, GatewayControlScope("alice", "local", "shared-name"),
                        original.threads["alice"].thread_id, original.home)
    request_record(case, "active")
    if fault == "copy_control":
        case.server.control_plane = copy.copy(case.server.control_plane)
    elif fault == "missing_runtime":
        case.server.drain_runtime = None
    elif fault == "closing":
        case.server.drain_runtime._closing = True
    else:
        read = projection._live_window_requests

        def close_after_read(*args, **kwargs):
            records = read(*args, **kwargs)
            case.server.drain_runtime._closing = True
            return records

        monkeypatch.setattr(projection, "_live_window_requests", close_after_read)
    assert discover(case) == idle_fields()


def test_http_history_idle_defaults(inflight_case):
    case = inflight_case
    status, body = post(case, {"conversation_id": case.scope.conversation_id}, actor=case.scope.user_id)
    assert status == 200 and body["ok"], body
    assert {key: body[key] for key in idle_fields()} == idle_fields()


def test_http_history_active_five_to_seven_roundtrip(inflight_case):
    case = inflight_case
    request_record(case, "active")
    chunk_rows(case, "active", [f"old-{i}" for i in range(5)])
    status, body = post(case, {"conversation_id": case.scope.conversation_id}, actor=case.scope.user_id)
    assert status == 200 and body["ok"], body
    assert body["active_request_id"] == "active" and body["stream_cursor"] == 5
    assert body["in_flight_status"] == "processing" and body["turn_phase"] == "open"
    assert body["progress_path"] == "/progress/active?since=5"
    chunk_rows(case, "active", ["new-5", "new-6"], append=True)
    status, progress = get_progress(case, body["progress_path"])
    assert status == 200 and progress["next"] == 7, progress
    assert [event["text"] for event in progress["events"]] == ["new-5", "new-6"]


def test_http_history_auth_rejection_precedes_discovery(inflight_case, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("认证拒绝不得进入活动发现")

    monkeypatch.setattr(projection, "client_inflight_projection", forbidden)
    case = inflight_case
    status, _ = post(case, {"conversation_id": case.scope.conversation_id},
                     actor=case.scope.user_id, credential=False)
    assert status == 403
