"""真实 owner Store、认证 HTTP 与有界列表/定点历史回归；不替换权限或 owner 裁决。"""
from __future__ import annotations

import base64
import http.client
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import client_inflight_projection as projection
from agent_py_agent.agent.gateway_parts.client_service import (
    GatewayClientHistoryResult,
    read_gateway_client_history_from_store,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.agent.user_space.runtime_paths import resolve_runtime_paths_for_agent
from agent_py_agent.tests.test_gateway_coordinator_client_history import (
    HistoryCase,
    post,
    seed,
    snapshot,
)
from agent_py_agent.tests.test_gateway_coordinator_client_history import (
    case as coordinator_case,  # noqa: F401
)
from agent_py_agent.tests.test_v13_w2_client_history_inflight import InflightCase, request_record


@pytest.fixture(autouse=True)
def isolated_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    projection._CURSORS.clear()
    module = sys.modules.get("agent_py_agent.agent.gateway_parts.client_session_list")
    if module is not None:
        module._COUNT_CACHE.clear()
    yield
    projection._CURSORS.clear()
    module = sys.modules.get("agent_py_agent.agent.gateway_parts.client_session_list")
    if module is not None:
        module._COUNT_CACHE.clear()


@pytest.fixture
def shared_case(request):
    return request.getfixturevalue("coordinator_case")


@pytest.fixture(params=["single", "coordinator"])
def session_case(request, tmp_path, monkeypatch):
    if request.param == "coordinator":
        yield request.getfixturevalue("shared_case")
        return
    root = tmp_path / "single-home"
    monkeypatch.setenv("MY_AGENT_HOME", str(root))
    config = AgentConfig(my_agent_home=str(root), gateway_per_user_owner_scoping=True,
                         conversation_workspace=str(tmp_path / "single-conversations"),
                         model_backend="echo", enable_plugins=False, enable_subagents=False,
                         prompt_files=[])
    stores, threads = {}, {}
    base = home_paths(root)
    for actor in ("alice", "bob"):
        stores[actor], threads[actor] = seed(
            config, base, OwnerIdentity.provider_user("local", actor), actor, "shared-name")
    stores["main"], threads["main"] = seed(
        config, base, OwnerIdentity.local_main(), "local-agent", "main-session")
    agent = SimpleAgent(config, tmp_path / "workspace")
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "gateway"),
                               params=GatewayHTTPServerParams(agent=agent))
    try:
        server.start()
        yield HistoryCase(server, config, stores, threads, root)
    finally:
        server.stop()


def post_as(case, body, headers):
    connection = http.client.HTTPConnection("127.0.0.1", case.server.server.server_address[1], timeout=5)
    try:
        connection.request("POST", "/client/history", json.dumps(body), headers={
            "Content-Type": "application/json", "X-Gateway-Token": case.server._local_client_credential,
            **headers,
        })
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def get_as(case, path, headers):
    connection = http.client.HTTPConnection("127.0.0.1", case.server.server.server_address[1], timeout=5)
    try:
        connection.request("GET", path, headers={
            "X-Gateway-Token": case.server._local_client_credential, **headers})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def seed_tui(config, base_home, conversation_id, turns=2):
    root = resolve_runtime_paths_for_agent(config, base_home.owner_home_dir, base_home).paths["conversation_workspace"]
    store = ConversationStore(root)
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat",
        "channel_conversation_id": conversation_id, "channel_user_id": "local-agent",
    })
    for turn in range(turns):
        request_id = f"{conversation_id}-{turn}"
        store.messages.append({"thread_id": thread.thread_id, "role": "user", "content": f"问题 {turn}",
                               "metadata": {"gateway_request_id": request_id}})
        store.messages.append({"thread_id": thread.thread_id, "role": "assistant", "content": f"回答 {turn}",
                               "metadata": {"gateway_request_id": request_id, "assistant_part_id": "final"}})
    return store, thread


def page_threads(case, count=120):
    threads = [("main-session", case.stores["main"], case.threads["main"])]
    for index in range(count - 1):
        name = f"sess_page_{index:03}"
        store, thread = seed_tui(case.config, home_paths(case.home), name, turns=0)
        threads.append((name, store, thread))
    for index, (_, store, thread) in enumerate(threads):
        stamp = 1_700_000_000_000_000_000 + index
        os.utime(store.storage.thread_path(thread.thread_id), ns=(stamp, stamp))
    return threads


def test_admin_lists_local_tui_sessions_and_not_alice(session_case):
    case = session_case
    expected = {}
    for index in range(3):
        name = f"sess_tui_{index}"
        _, thread = seed_tui(case.config, home_paths(case.home), name)
        expected[name] = thread.thread_id
    before = snapshot(case.home)
    status, body = post_as(case, {"operation": "list"}, {})
    assert status == 200 and body["ok"], body
    assert body.get("operation") == "list", body
    assert body["owner_id"] == resolve_owner_home(case.home, OwnerIdentity.local_main()).owner_id
    selected = {item["conversation_id"]: item for item in body["items"]}
    assert expected.keys() <= selected.keys()
    for name, thread_id in expected.items():
        item = selected[name]
        assert (item["thread_id"], item["channel"], item["channel_user_id"]) == (thread_id, "chat", "local-agent")
        assert item["message_count"] == 4 and item["preview"] == "回答 1"
    assert not {case.threads[actor].thread_id for actor in ("alice", "bob")} & {i["thread_id"] for i in body["items"]}
    assert body["coverage"] == "complete" and snapshot(case.home) == before


def test_alice_lists_only_her_sessions(shared_case):
    case = shared_case
    status, body = post_as(case, {"operation": "list", "user_id": "bob", "channel": "chat"},
                           {"X-User-Id": "alice", "X-Channel": "local"})
    assert status == 200 and body["ok"], body
    assert body["owner_id"] == resolve_owner_home(case.home, OwnerIdentity.provider_user("local", "alice")).owner_id
    assert [(i["conversation_id"], i["thread_id"], i["channel_user_id"]) for i in body["items"]] == [
        ("shared-name", case.threads["alice"].thread_id, "alice")]


def test_list_inflight_matches_history_projection(shared_case):
    case = shared_case
    bindings = {}
    for name, pending in (("sess_running", False), ("sess_queued", True)):
        _, thread = seed_tui(case.config, home_paths(case.home), name)
        scope = GatewayControlScope("local-agent", "chat", name)
        request_record(InflightCase(case.server, scope, thread.thread_id, case.home),
                       "req-queue" if pending else "req-run", pending=pending,
                       runtime_authority={"run_id": "main-turn-run"})
        bindings[name] = scope, thread.thread_id
    status, body = post_as(case, {"operation": "list"}, {})
    assert status == 200 and body["inflight_coverage"] == "complete", body
    items = {i["conversation_id"]: i for i in body["items"]}
    for name, (scope, thread_id) in bindings.items():
        observed = projection.client_inflight_projection(case.server, scope, thread_id)
        item = items[name]
        expected = "queued" if observed["in_flight_status"] == "pending" else "running"
        assert item["status"] == expected and item["in_flight"]
        assert item["active_request_id"] == observed["active_request_id"]
    assert items["sess_running"]["active_run_id"] == "main-turn-run"
    assert items["sess_queued"]["active_run_id"] is None


def test_direct_tui_running_claim_reports_running(shared_case):
    case = shared_case
    store, thread = seed_tui(case.config, home_paths(case.home), "sess_claim")
    path = store.storage.background_claim_path(thread.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for claim, expected in (({"status": "running", "expires_at": time.time() + 600}, "running"),
                            ({"status": "running", "expires_at": time.time() - 1}, "idle"),
                            ({"status": "failed"}, "failed")):
        path.write_text(json.dumps(claim))
        status, body = post_as(case, {"operation": "list"}, {})
        item = next(i for i in body["items"] if i["thread_id"] == thread.thread_id)
        assert status == 200 and item["status"] == expected
        assert item["active_request_id"] is None and item["active_run_id"] is None


def test_cursor_pages_are_disjoint_and_complete(shared_case):
    case = shared_case
    expected = page_threads(case)
    cursor, pages = None, []
    for _ in range(3):
        status, body = post_as(case, {"operation": "list", "limit": 50, "cursor": cursor}, {})
        assert status == 200 and body["ok"], body
        pages.append(body["items"])
        cursor = body["next_cursor"]
        if len(pages) == 1:
            first_cursor = cursor
    assert [len(page) for page in pages] == [50, 50, 20] and cursor is None
    names = [i["conversation_id"] for page in pages for i in page]
    assert names == [name for name, _, _ in reversed(expected)] and len(set(names)) == 120
    headers = {"X-User-Id": "alice", "X-Channel": "local"}
    assert post_as(case, {"operation": "list", "cursor": first_cursor}, headers) == (400, {"error": "invalid session cursor"})
    data = json.loads(base64.urlsafe_b64decode(first_cursor + "=" * (-len(first_cursor) % 4)))
    data[0] = "s2"
    mangled = base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")
    assert post_as(case, {"operation": "list", "cursor": mangled}, {}) == (400, {"error": "invalid session cursor"})


@pytest.mark.parametrize("fields,error", [
    ({"limit": 0}, "invalid session list limit"), ({"limit": -1}, "invalid session list limit"),
    ({"limit": True}, "invalid session list limit"), ({"limit": "5"}, "invalid session list limit"),
    ({"cursor": 123}, "invalid session cursor"), ({"cursor": "x" * 2049}, "invalid session cursor"),
    ({"cursor": "!"}, "invalid session cursor"), ({"include_inflight": "yes"}, "invalid include_inflight"),
])
def test_list_rejects_invalid_params(shared_case, fields, error):
    assert post_as(shared_case, {"operation": "list", **fields}, {}) == (400, {"error": error})


def test_list_clamps_limit_and_can_skip_gateway_inflight(shared_case):
    case = shared_case
    page_threads(case)
    status, body = post_as(case, {"operation": "list", "limit": 500, "include_inflight": False}, {})
    assert status == 200 and len(body["items"]) == 100 and body["next_cursor"]
    assert body["inflight_coverage"] == "skipped"
    assert all(i["status"] in {"idle", "failed", "running"} and i["active_request_id"] is None for i in body["items"])


def test_list_cost_is_bounded(shared_case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import client_session_list as sessions

    case = shared_case
    page_threads(case)
    store = case.stores["main"]
    cls = type(store.threads)
    catalog = cls._read_bindings_report
    marker = sessions.canonical_message_tail_marker
    reads = observe_hot_bytes(monkeypatch)
    read_text = Path.read_text
    thread_dir = store.storage.thread_path(case.threads["main"].thread_id).parent
    counts = {"thread_reads": 0, "catalog": 0, "bytes": 0, "markers": 0}

    def forbidden(*args, **kwargs):
        raise AssertionError("列表不得遍历目录、旧索引或重读绑定解析器")

    def observe_read(path, *args, **kwargs):
        if path.parent == thread_dir:
            counts["thread_reads"] += 1
        return read_text(path, *args, **kwargs)

    def observe_catalog(self):
        counts["catalog"] += 1
        return catalog(self)

    def observe_marker(path, thread_id):
        counts["markers"] += 1
        return marker(path, thread_id)

    monkeypatch.setattr(Path, "rglob", forbidden)
    monkeypatch.setattr(os, "walk", forbidden)
    monkeypatch.setattr(cls, "list_report", forbidden)
    monkeypatch.setattr(cls, "resolve_report", forbidden)
    monkeypatch.setattr(Path, "read_text", observe_read)
    monkeypatch.setattr(cls, "_read_bindings_report", observe_catalog)
    monkeypatch.setattr(sessions, "canonical_message_tail_marker", observe_marker)
    fields = {"owner_id": "local/main", "viewer_user_id": "admin", "viewer_channel": "chat", "all_user_access": True}
    body = sessions.list_owner_sessions(store, limit=10, **fields)
    assert body["ok"] and len(body["items"]) == 10
    assert counts["catalog"] == 1 and counts["thread_reads"] <= 10 and sum(reads) <= 8_388_608
    # 非空 canonical transcript 的同版本第二次读取必须命中计数缓存。
    sessions._COUNT_CACHE.clear()
    path = store.storage.message_path(case.threads["main"].thread_id)
    counts.update(bytes=0, markers=0)
    first = sessions._message_count(path, case.threads["main"].thread_id, [8_388_608], 2)
    called = counts["markers"]
    assert first == 24 and called == 1
    assert sessions._message_count(path, case.threads["main"].thread_id, [0], 2) == first
    assert counts["markers"] == called and len(sessions._COUNT_CACHE) <= 2
    sessions._COUNT_CACHE.clear()
    counts.update(bytes=0, markers=0)
    reads.clear()
    assert sessions._message_count(path, case.threads["main"].thread_id, [path.stat().st_size - 1], 2) is None
    assert reads == [] and counts["markers"] == 0


def test_cold_owner_list_creates_nothing(shared_case):
    case = shared_case
    expected = resolve_owner_home(case.home, OwnerIdentity.provider_user("local", "carol"))
    before = snapshot(case.home)
    status, body = post_as(case, {"operation": "list"}, {"X-User-Id": "carol", "X-Channel": "local"})
    assert status == 200 and body["ok"] and body["items"] == [], body
    assert snapshot(case.home) == before and not expected.home_dir.exists()


def test_admin_opens_tui_session_by_target_thread_id(session_case):
    case = session_case
    _, thread = seed_tui(case.config, home_paths(case.home), "sess_open")
    status, old = post_as(case, {"conversation_id": "sess_open"}, {})
    assert status == 200 and old["ok"] and old["thread_id"] == ""
    scope = GatewayControlScope("local-agent", "chat", "sess_open")
    request_record(InflightCase(case.server, scope, thread.thread_id, case.home), "req-tui-open")
    status, body = post_as(case, {"conversation_id": "sess_open", "target_thread_id": thread.thread_id}, {})
    assert status == 200 and body["ok"] and body["thread_id"] == thread.thread_id, body
    assert body["turns"] == [{"request_id": f"sess_open-{i}", "user_message": f"问题 {i}",
                              "assistant_message": f"回答 {i}"} for i in range(2)]
    assert body["active_request_id"] == "req-tui-open" and body["progress_path"]
    status, progress = get_as(case, body["progress_path"], {})
    assert status == 200 and progress["request_id"] == "req-tui-open", progress


def test_target_thread_id_does_not_cross_owner_or_actor(shared_case):
    case = shared_case
    for actor, key, conversation, metadata in (
        ("alice", "main", "main-session", {}), ("bob", "alice", "shared-name", {}),
        ("bob", "group-alice", "group-1", {"channel_chat_type": "group", "channel_chat_id": "group-1"}),
    ):
        status, body = post_as(case, {"conversation_id": conversation,
            "target_thread_id": case.threads[key].thread_id, "metadata": metadata},
            {"X-User-Id": actor, "X-Channel": "local"})
        assert status == 200 and body["ok"] and body["thread_id"] == "", body
    for value in (5, "", " " * 3, "x" * 257):
        assert post_as(case, {"target_thread_id": value}, {}) == (400, {"error": "invalid target_thread_id"})
    for value in ("../escape", "missing-thread"):
        status, body = post_as(case, {"conversation_id": "shared-name", "target_thread_id": value}, {})
        assert status == 200 and body["ok"] and body["thread_id"] == ""


def test_coordinator_list_and_target_read(shared_case, monkeypatch):
    from agent_py_agent.agent.conversation.store_messages import MessageStore

    case = shared_case
    cls = type(case.stores["alice"].threads)
    original_load = cls.load_report

    def close_after_load(self, thread_id):
        result = original_load(self, thread_id)
        case.server.drain_runtime._closing = True
        return result

    before = snapshot(case.home)
    with monkeypatch.context() as patch:
        patch.setattr(cls, "load_report", close_after_load)
        status, body = post_as(case, {"operation": "list", "include_inflight": False},
                               {"X-User-Id": "alice", "X-Channel": "local"})
        assert status == 200 and not body["ok"] and body["items"] == [], body
        assert body["coverage"] == "unknown"
    case.server.drain_runtime._closing = False
    original_page = MessageStore.history_page_report

    def close_after_page(self, *args, **kwargs):
        result = original_page(self, *args, **kwargs)
        case.server.drain_runtime._closing = True
        return result

    with monkeypatch.context() as patch:
        patch.setattr(MessageStore, "history_page_report", close_after_page)
        status, body = post_as(case, {"conversation_id": "shared-name", "target_thread_id": case.threads["alice"].thread_id},
                               {"X-User-Id": "alice", "X-Channel": "local"})
        assert status == 200 and not body["ok"] and body["turns"] == [], body
    assert case.server.agent is None and snapshot(case.home) == before


def test_history_without_new_fields_is_unchanged(shared_case):
    case = shared_case
    status, body = post(case, {"conversation_id": "shared-name", "limit": 2})
    expected = read_gateway_client_history_from_store(case.stores["alice"],
        scope=GatewayControlScope("alice", "local", "shared-name"), max_turns=2).to_dict()
    assert status == 200 and body == json.loads(json.dumps(expected))
    assert body.keys() == GatewayClientHistoryResult(True).to_dict().keys()
    assert "operation" not in body and "items" not in body


def test_claim_error_keeps_item_with_partial_coverage(shared_case):
    case = shared_case
    store, thread = seed_tui(case.config, home_paths(case.home), "sess_bad_claim")
    path = store.storage.background_claim_path(thread.thread_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("broken")
    status, body = post_as(case, {"operation": "list"}, {})
    assert status == 200 and body["ok"] and body["coverage"] == "partial", body
    item = next(i for i in body["items"] if i["thread_id"] == thread.thread_id)
    assert item["status"] == "idle" and item["active_request_id"] is None
    assert any(e["code"] == "CLIENT_SESSION_LOAD_FAILED" and e["conversation_id"] == "sess_bad_claim"
               for e in body["load_errors"])
    assert str(path) not in json.dumps(body)


def test_catalog_failure_and_binding_mismatch_are_not_hidden(shared_case):
    case = shared_case
    store, thread = seed_tui(case.config, home_paths(case.home), "sess_binding")
    bindings = json.loads(store.storage.bindings_path.read_text())
    bindings["chat\x1fwrong-conversation\x1flocal-agent"] = thread.thread_id
    store.storage.bindings_path.write_text(json.dumps(bindings))
    status, body = post_as(case, {"operation": "list"}, {})
    assert status == 200 and body["ok"]
    assert "wrong-conversation" not in {i["conversation_id"] for i in body["items"]}
    store.storage.bindings_path.write_text("broken")
    status, body = post_as(case, {"operation": "list"}, {})
    assert status == 200 and not body["ok"] and body["items"] == []
    assert body["coverage"] == "unknown" and body["load_errors"][0]["code"] == "CLIENT_SESSION_CATALOG_UNAVAILABLE"


def test_list_request_precedes_legacy_history_cursor_validation(shared_case):
    status, body = post_as(shared_case, {"operation": "list", "before_message_cursor": -1}, {})
    assert status == 200 and body.get("operation") == "list", body
    assert post_as(shared_case, {"target_thread_id": "valid", "before_message_cursor": -1}, {}) == (
        400, {"error": "invalid history cursor"})


def test_inflight_index_budget_conflict_and_source_checks(shared_case):
    case = shared_case
    _, thread = seed_tui(case.config, home_paths(case.home), "sess_conflict")
    scope = GatewayControlScope("local-agent", "chat", "sess_conflict")
    target = InflightCase(case.server, scope, thread.thread_id, case.home)
    request_record(target, "active-1")
    request_record(target, "active-2")
    entries = {"binding": (scope, thread.thread_id)}
    found, coverage = projection.client_inflight_index(case.server, scope, entries)
    assert coverage == "complete" and found == {"binding": {
        "status": "running", "active_request_id": "", "active_run_id": ""}}
    assert projection.client_inflight_index(case.server, scope, entries, max_request_files=1) == ({}, "partial")
    case.server.drain_runtime._closing = True
    assert projection.client_inflight_index(case.server, scope, entries) == ({}, "unavailable")


def observe_hot_bytes(monkeypatch):
    from agent_py_agent.agent.conversation import message_scan

    original = message_scan._open_hot_transcript
    reads = []

    class ObservedHotReader:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return self.handle.__exit__(exc_type, exc, traceback)

        def read(self, size=-1):
            raw = self.handle.read(size)
            reads.append(len(raw))
            return raw

        def readline(self, size=-1):
            raw = self.handle.readline(size)
            reads.append(len(raw))
            return raw

        def __iter__(self):
            return self

        def __next__(self):
            raw = self.readline()
            if not raw:
                raise StopIteration
            return raw

    def opened(path, root):
        return ObservedHotReader(original(path, root))

    monkeypatch.setattr(message_scan, "_open_hot_transcript", opened)
    return reads


@pytest.mark.parametrize("role", [[], {}])
def test_invalid_preview_role_keeps_healthy_sessions(shared_case, role):
    case = shared_case
    store, thread = seed_tui(case.config, home_paths(case.home), "sess_invalid_role")
    path = store.storage.message_path(thread.thread_id)
    with path.open("ab") as handle:
        handle.write((json.dumps({"role": role, "content": "bad row"}) + "\n").encode())
    status, body = post_as(case, {"operation": "list", "include_inflight": False}, {})
    assert status == 200 and body["ok"], body
    items = {i["thread_id"]: i for i in body["items"]}
    assert case.threads["main"].thread_id in items and thread.thread_id in items
    assert items[thread.thread_id]["preview"] == "回答 1"


def test_message_count_budget_includes_actual_marker_reads(shared_case, monkeypatch):
    from agent_py_agent.agent.gateway_parts import client_session_list as sessions

    case = shared_case
    path = case.stores["main"].storage.message_path(case.threads["main"].thread_id)
    reads = observe_hot_bytes(monkeypatch)
    budget = path.stat().st_size
    count = sessions._message_count(path, case.threads["main"].thread_id, [budget], 2)
    assert sum(reads) <= budget
    assert count is None
    reads.clear()
    remaining = [8_388_608]
    assert sessions._message_count(path, case.threads["main"].thread_id, remaining, 2) == 24
    assert sum(reads) <= 8_388_608
    assert remaining[0] == 8_388_608 - sum(reads)
    reads.clear()
    assert sessions._message_count(path, case.threads["main"].thread_id, [0], 2) == 24
    assert reads == []



def test_count_path_exposes_canonical_identity_without_bypassing_read_budget(shared_case, monkeypatch):
    from agent_py_agent.agent.conversation import store_messages
    from agent_py_agent.agent.gateway_parts import client_session_list as sessions

    case = shared_case
    thread = case.threads["main"]
    path = case.stores["main"].storage.message_path(thread.thread_id)
    remaining = [8_388_608]
    bounded = sessions._CountPath(path, remaining)
    assert os.fspath(bounded) == os.fspath(path) and Path(bounded) == path
    observations = []
    original = store_messages.file_identity_snapshot
    def observe_identity(source):
        observations.append(source)
        return original(source)
    monkeypatch.setattr(store_messages, "file_identity_snapshot", observe_identity)
    reads = observe_hot_bytes(monkeypatch)
    marker = sessions.canonical_message_tail_marker(bounded, thread.thread_id)
    assert marker["message_count"] == 24
    assert observations == [bounded]
    assert sum(reads) > 0 and remaining[0] == 8_388_608 - sum(reads)
    assert bounded.readers and all(reader.handle.closed for reader in bounded.readers)
