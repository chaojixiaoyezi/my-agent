from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.display_archive import (
    DISPLAY_PAGE_CHARS,
    DISPLAY_PAGE_ROWS,
    DisplayArchiveError,
    archive_display_rows,
    read_display_archive_page,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import display_archive_service
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.display_archive_service import read_gateway_display_page


# LLM: Helpers construct real owner-scoped canonical stores only under pytest temporary roots.
# 函数用途: 生成测试专用会话和存储，不加载模型或访问真实用户资料。
def _owner(root, *, session="session", user="user"):
    store = ConversationStore(root)
    thread = store.get_or_create_thread({
        "canonical_user_id": user, "channel": "chat",
        "channel_conversation_id": session, "channel_user_id": user,
    })
    return SimpleNamespace(conversation_store=store), thread


def test_pages_reconstruct_all_long_unicode_rows_and_keep_snapshots(tmp_path):
    agent, thread = _owner(tmp_path)
    rows = [{"kind": "add", "text": "中🙂e\u0301\t " * 4500},
            {"kind": "remove", "text": ""},
            *({"kind": "text", "text": f"第{index}行\n原样"} for index in range(750))]
    expected = [dict(row) for row in rows]
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=rows)
    rows[0]["text"] = "业务文件稍后已被改写"
    restored = ["" for _ in expected]
    assert ref["row_count"] == len(expected)
    assert ref["page_count"] >= 5
    for index in range(ref["page_count"]):
        page = read_display_archive_page(agent.conversation_store, ref, index)
        assert len(page["rows"]) <= DISPLAY_PAGE_ROWS
        assert sum(len(row["text"]) for row in page["rows"]) <= DISPLAY_PAGE_CHARS
        assert page["has_next"] == (index + 1 < ref["page_count"])
        for row in page["rows"]:
            restored[row["row_index"]] += row["text"]
            assert row["kind"] == expected[row["row_index"]]["kind"]
    assert restored == [row["text"] for row in expected]
    assert ref["char_count"] == sum(map(len, restored))
    assert "path" not in ref


@pytest.mark.parametrize("page", [-1, True, "0", 10000, None])
def test_invalid_page_is_rejected(tmp_path, page):
    agent, thread = _owner(tmp_path)
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[{"text": "x"}])
    with pytest.raises(DisplayArchiveError, match="页码"):
        read_display_archive_page(agent.conversation_store, ref, page)


@pytest.mark.parametrize("patch", [
    {"archive_id": "../secret"}, {"thread_id": "../../another"}, {"path": "/etc/passwd"},
    {"schema": "unknown"}, {"archive_id": 0},
])
def test_path_like_and_unknown_refs_rejected_before_owner_lookup(tmp_path, monkeypatch, patch):
    agent, thread = _owner(tmp_path)
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[])
    monkeypatch.setattr(display_archive_service, "resolve_gateway_scope_agent", lambda *_: pytest.fail("owner lookup"))
    with pytest.raises(DisplayArchiveError) as error:
        read_gateway_display_page(agent, scope=GatewayControlScope("user", "chat", "session"),
                                  reference={**ref, **patch}, page_index=0)
    assert error.value.code == "DISPLAY_REF_INVALID"


def test_scope_accepts_own_root_and_descendants_not_other_sessions_or_owners(tmp_path, monkeypatch):
    agent, thread = _owner(tmp_path / "owner-a")
    other, other_thread = _owner(tmp_path / "owner-b")
    second = agent.conversation_store.get_or_create_thread({
        "canonical_user_id": "user", "channel": "chat", "channel_user_id": "user",
        "channel_conversation_id": "different-session",
    })
    child = agent.conversation_store.ensure_agent_thread({
        "thread_id": "agent-child", "agent_run_id": "run-child", "canonical_user_id": "user",
        "parent_agent_thread_id": thread.thread_id, "root_agent_thread_id": thread.thread_id,
    })
    root_ref, child_ref, different_ref = [
        archive_display_rows(agent, thread_id=value.thread_id, rows=[{"text": value.thread_id}])
        for value in (thread, child, second)
    ]
    foreign_ref = archive_display_rows(other, thread_id=other_thread.thread_id, rows=[{"text": "private"}])
    monkeypatch.setattr(display_archive_service, "resolve_gateway_scope_agent", lambda *_: agent)
    scope = GatewayControlScope("user", "chat", "session")
    for ref in (root_ref, child_ref):
        assert read_gateway_display_page(object(), scope=scope, reference=ref, page_index=0)["ok"]
    for ref in (different_ref, foreign_ref):
        with pytest.raises(DisplayArchiveError) as error:
            read_gateway_display_page(object(), scope=scope, reference=ref, page_index=0)
        assert error.value.code == "DISPLAY_SCOPE_DENIED"
    # A stolen valid id cannot select another owner's directory even when the attacker changes thread_id.
    with pytest.raises(DisplayArchiveError) as error:
        read_gateway_display_page(object(), scope=scope, reference={**foreign_ref, "thread_id": thread.thread_id}, page_index=0)
    assert error.value.code == "DISPLAY_ARCHIVE_UNAVAILABLE"


def test_manifest_counts_are_authoritative_and_missing_archive_is_explicit(tmp_path):
    agent, thread = _owner(tmp_path)
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[])
    page = read_display_archive_page(agent.conversation_store, {**ref, "page_count": 10**8}, 0)
    assert page["page_count"] == 1
    with pytest.raises(DisplayArchiveError):
        read_display_archive_page(agent.conversation_store, {**ref, "archive_id": "0" * 32}, 0)
    assert not list(tmp_path.rglob("*.jsonl")), "display must not enter model/message ledgers"


@pytest.mark.parametrize("level", ["root", "directory", "file"])
def test_symlink_archive_paths_are_rejected(tmp_path, level):
    agent, thread = _owner(tmp_path / "store")
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[{"text": "x"}])
    root = agent.conversation_store.root / "display_archives"
    path = root if level == "root" else root / ref["archive_id"]
    if level == "file":
        path = path / "0.json"
    moved = tmp_path / f"moved-{level}"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=level != "file")
    with pytest.raises(DisplayArchiveError):
        read_display_archive_page(agent.conversation_store, ref, 0)


def test_corrupt_oversized_or_invalid_page_is_rejected(tmp_path):
    agent, thread = _owner(tmp_path)
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[])
    path = agent.conversation_store.root / "display_archives" / ref["archive_id"] / "0.json"
    for value in ("x" * 300000, json.dumps({"rows": [{"text": "x"}]})):
        path.write_text(value)
        with pytest.raises(DisplayArchiveError):
            read_display_archive_page(agent.conversation_store, ref, 0)


def test_client_requests_page_via_canonical_gateway_identity(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent

    client = GatewayChatClientAgent(SimpleNamespace(), SimpleNamespace(), tmp_path, [], SimpleNamespace())
    captured = []
    monkeypatch.setattr(client, "post_gateway_json", lambda path, body, **options: (captured.append((path, body, options)) or (200, {"ok": True})))
    ref = {"schema": "display_archive_ref.v1", "archive_id": "a" * 32, "thread_id": "thread"}
    assert client.request_display_page(session_id="session", reference=ref, page_index=4) == {"ok": True}
    assert captured == [("/client/display-page", {"conversation_id": "session", "reference": ref, "page_index": 4}, {"timeout": 10.0})]


def test_http_route_reads_page_without_model_or_prompt_submission(tmp_path, monkeypatch):
    import socket
    import urllib.request

    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    agent, thread = _owner(tmp_path / "store", user="local-agent")
    ref = archive_display_rows(agent, thread_id=thread.thread_id, rows=[{"kind": "add", "text": "真实显示页"}])
    monkeypatch.setattr(display_archive_service, "resolve_gateway_scope_agent", lambda *_: agent)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = GatewayHTTPServer(port, gateway_paths_from_root(tmp_path / "gateway"), params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        data = {"user_id": "local-agent", "channel": "chat", "conversation_id": "session", "reference": ref, "page_index": 0}
        request = urllib.request.Request(f"http://127.0.0.1:{port}/client/display-page",
                                         data=json.dumps(data).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read())
        assert result["ok"] and result["rows"][0]["text"] == "真实显示页"
    finally:
        server.stop()
