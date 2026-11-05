"""真实消息轮转后的两阶段索引回收不改变任何消息内容或地址。"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import message_scan
from agent_py_agent.agent.conversation.store_io import ImmutableJsonIndex
from agent_py_agent.agent.user_space.storage_lanes import validate_lane_receipt
from agent_py_agent.tests.test_compact_scoped_checkpoint import compact_case as _compact_case
from agent_py_agent.tests.test_issues_w3_compact_reachability import aged, home_for, request_for
from agent_py_agent.tests.test_transcript_cold_rotation import _commit_rotation

compact_case = _compact_case


def rotation(case, monkeypatch):
    monkeypatch.setattr(message_scan, "_TRANSCRIPT_SEGMENT_BYTES", 1024)
    thread, messages, original, path = _commit_rotation(case, monkeypatch)
    manifest = message_scan.transcript_manifest(path)
    assert manifest["segment_count"] > 1
    index = ImmutableJsonIndex(path.parent, ("archive", case.thread_id, "index"))
    for node in path.parent.joinpath(*index.parts).glob("*.json"):
        aged(node)
    return thread, messages, original, path, index, manifest


def test_two_phase_sweep_collects_superseded_nodes_and_every_message_remains_readable(
    compact_case, monkeypatch
):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    thread, messages, original, path, index, manifest = rotation(case, monkeypatch)
    namespace = path.parent.joinpath(*index.parts)
    marked = mark_reachable_nodes(index, [manifest["index"]], max_nodes=2048)
    files = {node.name for node in namespace.glob("*.json")}
    assert marked.complete and files - marked.reachable
    now = time.time()
    request = request_for(home_for(case), kind="conversation_messages", now=now)
    first = maintain_transcript_index_lane(request)
    validate_lane_receipt(first)
    assert first["complete"] and first["deleted_items"] == 0
    assert {node.name for node in namespace.glob("*.json")} == files
    early = maintain_transcript_index_lane(
        replace(request, cursor=first["cursor"], now=now + 86400)
    )
    assert early["deleted_items"] == 0
    second = maintain_transcript_index_lane(
        replace(request, cursor=first["cursor"], now=now + 7 * 86400)
    )
    validate_lane_receipt(second)
    assert second["deleted_items"] > 0
    # 墙钟片可先于条数片耗尽；持续驱动同一证明，最终断言仍要求所有旧孤儿消失。
    for _ in range(30):
        if second["complete"]:
            break
        assert second["items"] <= 128
        second = maintain_transcript_index_lane(
            replace(request, cursor=second["cursor"], now=now + 7 * 86400)
        )
        validate_lane_receipt(second)
    assert second["complete"]
    assert {node.name for node in namespace.glob("*.json")} == marked.reachable
    for message in messages:
        row, errors = case.store.messages.by_id_report(thread.thread_id, message.message_id)
        assert not errors and row == message
    with message_scan.TranscriptReader(path) as reader:
        assert reader.read() == original


def test_reused_candidate_between_phases_is_kept(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    _thread, _messages, _original, path, index, _manifest = rotation(case, monkeypatch)
    orphan = index._save({"entries": {"candidate": True}})
    target = aged(path.parent.joinpath(*index.parts, orphan["ref"]["ref"]))
    request = request_for(home_for(case), kind="conversation_messages")
    first = maintain_transcript_index_lane(request)
    assert target.exists()
    assert index._save({"entries": {"candidate": True}}) == orphan
    second = maintain_transcript_index_lane(
        replace(request, cursor=first["cursor"], now=request.now + 7 * 86400)
    )
    assert target.exists() and target.stat().st_mtime > request.now - 86400
    validate_lane_receipt(second)


def test_missing_manifest_node_refuses_deletion(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    _thread, _messages, _original, path, index, manifest = rotation(case, monkeypatch)
    orphan = index._save({"entries": {"orphan": True}})
    target = aged(path.parent.joinpath(*index.parts, orphan["ref"]["ref"]))
    path.parent.joinpath(*index.parts, manifest["index"]["ref"]["ref"]).unlink()
    commits = []
    result = maintain_transcript_index_lane(
        request_for(
            home_for(case),
            kind="conversation_messages",
            commit=lambda operation: (commits.append(True), operation())[1],
        )
    )
    assert result["deleted_items"] == 0 and target.exists() and not commits
    assert any(error["code"] == "transcript_index_incomplete" for error in result["errors"])


def test_empty_hold_and_symlinked_namespace_make_zero_commits(compact_case, monkeypatch, tmp_path):
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    commits = []

    def commit(operation):
        commits.append(True)
        return operation()

    request = request_for(home_for(case), kind="conversation_messages", commit=commit)
    assert maintain_transcript_index_lane(request)["complete"] and not commits
    _thread, _messages, _original, path, index, _manifest = rotation(case, monkeypatch)
    assert (
        maintain_transcript_index_lane(replace(request, legal_hold=True))["deferred_reason"]
        == "legal_hold"
    )
    namespace = path.parent.joinpath(*index.parts)
    external = tmp_path / "external-index"
    namespace.rename(external)
    namespace.symlink_to(external, target_is_directory=True)
    result = maintain_transcript_index_lane(request)
    assert not commits and result["deleted_items"] == 0
    assert list(external.glob("*.json"))
    assert len(json.dumps(result["cursor"]).encode()) < 16384


def test_broken_thread_does_not_block_later_healthy_thread(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    rotation(case, monkeypatch)
    messages = case.root / "conversations/messages"
    (messages / "archive/aaa-broken/index").mkdir(parents=True)
    (messages / "aaa-broken.manifest.json").write_text("{")
    result = maintain_transcript_index_lane(
        request_for(home_for(case), kind="conversation_messages")
    )
    assert any(error["code"] == "transcript_index_incomplete" for error in result["errors"])
    assert [entry["tid"] for entry in result["cursor"]["pending"]] == [case.thread_id]


def test_missing_earlier_message_root_does_not_block_runtime_root(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    rotation(case, monkeypatch)
    runtime = case.root / "workspace/runtime/workspaces/scope/conversations"
    runtime.parent.mkdir(parents=True)
    (case.root / "conversations").rename(runtime)
    (case.root / "conversations/threads").mkdir(parents=True)
    result = maintain_transcript_index_lane(
        request_for(home_for(case), kind="conversation_messages")
    )
    assert [entry["tid"] for entry in result["cursor"]["pending"]] == [case.thread_id]


def test_transcript_zero_candidates_and_hold_make_zero_commits(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    _thread, _messages, _original, path, index, manifest = rotation(case, monkeypatch)
    reachable = mark_reachable_nodes(index, [manifest["index"]], max_nodes=2048).reachable
    for node in path.parent.joinpath(*index.parts).glob("*.json"):
        if node.name not in reachable:
            node.unlink()
    commits = []
    request = request_for(
        home_for(case),
        kind="conversation_messages",
        commit=lambda operation: (commits.append(True), operation())[1],
    )
    result = maintain_transcript_index_lane(request)
    assert result["complete"] and "pending" not in result["cursor"] and not commits
    held = maintain_transcript_index_lane(
        replace(request, legal_hold=True, cursor={"after": "kept"})
    )
    assert held["complete"] and held["cursor"] == {"after": "kept"} and not commits


def test_one_item_budget_prepares_and_finishes_transcript_gc(compact_case, monkeypatch):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    _thread, _messages, _original, path, index, manifest = rotation(case, monkeypatch)
    reachable = mark_reachable_nodes(index, [manifest["index"]], max_nodes=2048).reachable
    for node in path.parent.joinpath(*index.parts).glob("*.json"):
        if node.name not in reachable:
            node.unlink()
    orphan = index._save({"entries": {"one-item-budget": True}})
    target = aged(path.parent.joinpath(*index.parts, orphan["ref"]["ref"]))
    request = request_for(home_for(case), kind="conversation_messages", items=1)
    first = maintain_transcript_index_lane(request)
    assert [entry["tid"] for entry in first["cursor"]["pending"]] == [case.thread_id]
    assert first["deleted_items"] == 0
    cursor = first["cursor"]
    for _ in range(20):
        result = maintain_transcript_index_lane(
            replace(request, cursor=cursor, now=request.now + 7 * 86400)
        )
        assert result["items"] <= 1
        cursor = result["cursor"]
        if result["complete"]:
            break
    assert result["complete"] and not target.exists()


@pytest.mark.parametrize("first_broken", [False, True])
def test_full_name_window_preserves_root_and_reaches_later_thread(
    compact_case, monkeypatch, first_broken
):
    from agent_py_agent.agent.conversation.hamt_reachability import mark_reachable_nodes
    from agent_py_agent.agent.conversation.transcript_index_gc import maintain_transcript_index_lane

    case = compact_case
    other = case.store.threads.get_or_create(
        {"canonical_user_id": "other", "channel_conversation_id": "other-thread", "now": 1.0}
    )
    second_case = SimpleNamespace(**{**vars(case), "thread_id": other.thread_id})
    first_case, later_case = sorted((case, second_case), key=lambda item: item.thread_id)
    _thread, _messages, _original, first_path, first_index, first_manifest = rotation(
        first_case, monkeypatch
    )
    rotation(later_case, monkeypatch)
    if first_broken:
        first_path.with_suffix(".manifest.json").write_text("{")
    else:
        reachable = mark_reachable_nodes(
            first_index, [first_manifest["index"]], max_nodes=2048
        ).reachable
        for node in first_path.parent.joinpath(*first_index.parts).glob("*.json"):
            if node.name not in reachable:
                node.unlink()
    request = request_for(home_for(case), kind="conversation_messages", items=1)
    first = maintain_transcript_index_lane(request)
    assert not first["complete"] and first["cursor"]["after"] == first_case.thread_id
    assert first["items"] == 1 and first["cursor"]["root_index"] == 0
    later = maintain_transcript_index_lane(replace(request, cursor=first["cursor"]))
    assert [entry["tid"] for entry in later["cursor"]["pending"]] == [later_case.thread_id]
