"""真实观察追加、确认 journal 与 worker 来源之间的确认合同。"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.observation_index import (
    ObservationIndex,
    ObservationIndexIncomplete,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_request import (
    BackgroundSourceChanged,
    freeze_source,
    read_source,
)
from agent_py_agent.agent.gateway_parts.multiprocess_background_sources import observation_handled
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, _owner_home_dir


def _case(tmp_path, store_ref="conversations"):
    owner = OwnerIdentity.local_main()
    home = _owner_home_dir(tmp_path, owner)
    # 故意不初始化 managed_dirs 和 wake 索引，只由实际写者创建自身所需路径。
    store = ConversationStore(home / store_ref, initialize=False)
    thread = store.threads.get_or_create({"canonical_user_id": "u", "channel": "internal",
        "channel_conversation_id": "acks", "channel_user_id": "u", "now": 1})
    return home, store, thread.thread_id


def _append(store, thread, key, **kwargs):
    return store.observations.append({"thread_id": thread, "_observation_id": key,
        "requires_main_agent": True, "summary": key, "now": 2, **kwargs})


def _freeze(root, store, thread):
    return freeze_source(root, OwnerIdentity.local_main(), store.storage.observation_path(thread), "observation")


def _fds():
    path = Path("/dev/fd") if Path("/dev/fd").exists() else Path("/proc/self/fd")
    return len(os.listdir(path))


@pytest.mark.parametrize("store_ref", ["conversations", "data/conversations",
    "workspace/runtime/workspaces/work-one/conversations"])
def test_worker_reads_only_unacknowledged_and_new_ack_invalidates_ref(tmp_path, store_ref):
    home, store, thread = _case(tmp_path, store_ref)
    first = _append(store, thread, "acked")
    pending = _append(store, thread, "pending", requires_main_agent=False, requires_llm_report=True)
    _append(store, thread, "ordinary", requires_main_agent=False)
    raw = store.storage.observation_path(thread).read_bytes()
    store.observations.mark_handled([first.observation_id], now=10)
    assert not store.storage.observation_handled_path.exists()
    before = _fds()
    ref = _freeze(tmp_path, store, thread)
    for _ in range(5):
        assert read_source(tmp_path, ref) == {"observations": [pending.to_dict()], "thread_id": thread}
    store.observations.mark_handled([pending.observation_id], now=20)
    with pytest.raises(BackgroundSourceChanged):
        read_source(tmp_path, ref)
    successor = _freeze(tmp_path, store, thread)
    assert successor["source_sha256"] == ref["source_sha256"]
    assert successor["input_revision"] != ref["input_revision"]
    assert read_source(tmp_path, successor)["observations"] == []
    store.observations.mark_handled([pending.observation_id], now=30)
    assert _freeze(tmp_path, store, thread)["input_revision"] == successor["input_revision"]
    assert store.storage.observation_path(thread).read_bytes() == raw
    assert not store.storage.wake_task_index_path.exists()
    assert not store.storage.wake_queue_dir.exists()
    assert {path.name for path in store.storage.root.iterdir() if path.is_dir()} == {"threads", "observations"}
    fd_delta = _fds() - before
    assert fd_delta == 0
    print(json.dumps({"store_ref": store_ref, "fd_delta": fd_delta,
        "worker_ids": [pending.observation_id], "revision_changed": True}))
    assert home.is_dir()


def test_legacy_and_journal_merge_max_without_legacy_migration(tmp_path):
    home, store, thread = _case(tmp_path)
    for key in ("legacy", "both", "pending"):
        _append(store, thread, key)
    store.observations.mark_handled(["both", "journal"], now=10)
    legacy = {**{f"old-{i}": 1 for i in range(300)}, "legacy": 20, "both": 30, "journal": 1}
    raw = json.dumps(legacy).encode()
    store.storage.observation_handled_path.write_bytes(raw)
    journal = store.observations._index.journal_path.read_bytes()
    before = _fds()
    handled, _ = observation_handled(home, "conversations", 1_048_576,
        observation_ids=("legacy", "both", "journal", "pending"))
    assert handled["both"] == 30 and handled["legacy"] == 20 and handled["journal"] == 10
    assert "pending" not in handled
    ref = _freeze(tmp_path, store, thread)
    assert [row["observation_id"] for row in read_source(tmp_path, ref)["observations"]] == ["pending"]
    assert store.storage.observation_handled_path.read_bytes() == raw
    assert store.observations._index.journal_path.read_bytes() == journal
    assert _fds() - before == 0


def test_worker_queries_all_raw_ids_before_payload_filtering(tmp_path, monkeypatch):
    _, store, thread = _case(tmp_path)
    _append(store, thread, "pending")
    _append(store, thread, "ordinary", requires_main_agent=False)
    _append(store, thread, "embedded")
    # 历史已内嵌回执与普通观察也必须列入原分片精确ID查询。
    path = store.storage.observation_path(thread)
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    rows[-1]["handled_at"] = 5
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    original = ObservationIndex.handled
    lookups = []

    def handled(index, ids, *, include_legacy=True):
        lookups.append((list(ids), include_legacy))
        return original(index, ids, include_legacy=include_legacy)

    monkeypatch.setattr(ObservationIndex, "handled", handled)
    ref = _freeze(tmp_path, store, thread)
    assert [row["observation_id"] for row in read_source(tmp_path, ref)["observations"]] == ["pending"]
    assert lookups and all(ids == ["pending", "ordinary", "embedded"] and not legacy
                           for ids, legacy in lookups)


def test_source_constructor_and_legacy_flags_ast_guard():
    tree = ast.parse(inspect.getsource(observation_handled))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    storage = [node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "ConversationStorage"]
    lookups = [node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == "handled"]
    assert len(storage) == len(lookups) == 1
    assert any(key.arg == "initialize" and isinstance(key.value, ast.Constant) and key.value.value is False
               for key in storage[0].keywords)
    assert any(key.arg == "include_legacy" and isinstance(key.value, ast.Constant) and key.value.value is False
               for key in lookups[0].keywords)


def test_revision_uses_exact_legacy_bytes_and_nofollow_journal_identity(tmp_path):
    home, store, thread = _case(tmp_path)
    _append(store, thread, "pending")
    for raw in (None, b"{}", b"{ }"):
        if raw is not None:
            store.storage.observation_handled_path.write_bytes(raw)
        _, revision = observation_handled(home, "conversations", 1_048_576)
        expected = hashlib.sha256(b"legacy:" + (b"missing" if raw is None else raw)
            + b"\0journal:" + json.dumps(None).encode()).hexdigest()
        assert revision == expected
    store.observations.mark_handled(["pending"], now=10)
    info = store.observations._index.journal_path.lstat()
    identity = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    expected = hashlib.sha256(b"legacy:{ }\0journal:" + json.dumps(identity).encode()).hexdigest()
    assert observation_handled(home, "conversations", 1_048_576)[1] == expected


def test_keyword_only_ids_and_revision_only_read_does_not_create_store(tmp_path):
    param = inspect.signature(observation_handled).parameters["observation_ids"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY and param.default == ()
    home = tmp_path / "home"
    home.mkdir()
    before = _fds()
    assert observation_handled(home, "data/conversations", 1_048_576)[0] == {}
    assert list(home.iterdir()) == []
    assert _fds() - before == 0


def test_partial_real_journal_maps_to_source_changed_and_resumes(tmp_path):
    _, store, thread = _case(tmp_path)
    pending = _append(store, thread, "pending")
    store.observations.mark_handled([f"ack-{i}" for i in range(129)], now=10)
    ref = _freeze(tmp_path, store, thread)
    # 可丢弃投影损坏后必须逐固定批恢复，不能把未追平 journal 当空确认。
    store.observations._index.path.write_bytes(b"not sqlite")
    before = _fds()
    with pytest.raises(BackgroundSourceChanged) as caught:
        read_source(tmp_path, ref)
    assert isinstance(caught.value.__cause__, ObservationIndexIncomplete)
    assert read_source(tmp_path, ref)["observations"] == [pending.to_dict()]
    assert _fds() - before == 0


@pytest.mark.parametrize("change", ["bad-json", "array", "bool", "thread", "empty-id", "infinite"])
def test_invalid_shard_is_rejected_before_index_lookup(tmp_path, monkeypatch, change):
    _, store, thread = _case(tmp_path)
    event = _append(store, thread, "pending")
    row = event.to_dict()
    if change == "bool":
        row["requires_llm_report"] = 1
    elif change == "thread":
        row["thread_id"] = "other"
    elif change == "empty-id":
        row["observation_id"] = ""
    elif change == "infinite":
        row["handled_at"] = "inf"
    raw = b"{bad\n" if change == "bad-json" else (b"[]\n" if change == "array" else json.dumps(row).encode() + b"\n")
    path = store.storage.observation_path(thread)
    path.write_bytes(raw)
    # freeze 会执行相同 worker 入口；不能先以宽松规则选出 id 再写投影。
    def unexpected_lookup(*args, **kwargs):
        pytest.fail("invalid shard reached observation index")
    monkeypatch.setattr(ObservationIndex, "handled", unexpected_lookup)
    with pytest.raises(ValueError):
        _freeze(tmp_path, store, thread)
    assert not store.observations._index.path.exists()


@pytest.mark.parametrize("target", ["store", "parent", "journal"])
def test_outside_home_symlinks_are_refused_without_writes(tmp_path, target):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if target == "store":
        (home / "conversations").symlink_to(outside, target_is_directory=True)
        store_ref = "conversations"
    elif target == "parent":
        (home / "data").symlink_to(outside, target_is_directory=True)
        store_ref = "data/conversations"
    else:
        (home / "conversations").mkdir()
        journal = outside / "acks.jsonl"
        journal.write_text('{"id":"x","at":10}\n')
        (home / ".conversations-observation-acks.jsonl").symlink_to(journal)
        store_ref = "conversations"
    original = sorted(str(p.relative_to(outside)) for p in outside.rglob("*"))
    before = _fds()
    with pytest.raises((OSError, ValueError)):
        observation_handled(home, store_ref, 1_048_576, observation_ids=("x",))
    assert sorted(str(p.relative_to(outside)) for p in outside.rglob("*")) == original
    assert _fds() - before == 0
