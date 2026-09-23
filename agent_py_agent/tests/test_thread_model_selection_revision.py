"""原线程选择版本、旧记录迁移及同值显式覆盖；不调用真实模型。"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from agent_py_agent.agent.conversation.models import ConversationThread
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
from agent_py_agent.agent.settings.thread_model_selection import (
    SUBAGENT_MODEL_ADVICE_KEY,
    execute_local_model_operation,
    thread_model_profile_id,
)
from agent_py_agent.tests.test_model_profiles import add
from agent_py_agent.tests.test_thread_model_selection import host_with_store

FIELDS = ("model_selection_revision", "model_selection_source", "model_selection_last_explicit_revision")


def test_new_default_has_one_recorded_initialization_and_reads_do_not_advance(tmp_path):
    host = host_with_store(tmp_path)
    result = execute_local_model_operation(host, "session", "list", {})
    store, tid = host.conversation_store.threads, result["thread_id"]
    row = store.load(tid)
    assert (row.model_selection_revision, row.model_selection_source, row.model_selection_last_explicit_revision) == (1, "default", 0)
    before = store.storage.thread_path(tid).read_bytes()
    assert thread_model_profile_id(host, tid) == "default"
    model = add(host, model_name="new-default")[0]
    execute_model_profile_operation(host, "set_default", {"profile_id": model})
    assert thread_model_profile_id(host, tid) == "default"
    assert store.storage.thread_path(tid).read_bytes() == before
    empty_store = ConversationStore(tmp_path / "no-default")
    unknown = empty_store.threads.get_or_create({"canonical_user_id": "local", "channel": "local", "channel_conversation_id": "new"})
    assert unknown.model_selection_revision == 0 and unknown.model_selection_source == "unknown"


@pytest.mark.parametrize("schema", ["", "conversation_thread.v3", "conversation_thread.v9", "conversation_thread.v10"])
def test_legacy_selected_profile_has_unknown_origin_without_read_side_effects(tmp_path, schema):
    host = host_with_store(tmp_path)
    store = host.conversation_store.threads
    payload = ConversationThread("legacy", "alice", owner_id="alice", model_profile_id="default").to_dict()
    for key in FIELDS:
        payload.pop(key)
    payload["schema_version"] = schema
    payload["unknown_extension"] = {"keep": True}
    path = store.storage.thread_path("legacy")
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    assert thread_model_profile_id(host, "legacy") == "default"
    row = store.load("legacy")
    assert [getattr(row, field) for field in FIELDS] == [0, "unknown", 0]
    assert path.read_bytes() == before
    store.update_atomic("legacy", lambda value: replace(value, title="只改标题"))
    migrated = json.loads(path.read_text(encoding="utf-8"))
    assert migrated["unknown_extension"] == {"keep": True}
    assert [migrated[field] for field in FIELDS] == [0, "unknown", 0]
    thread_model_profile_id(host, "legacy", select="default")
    reopened = ConversationStore(host.conversation_store.storage.root).threads.load("legacy")
    assert [getattr(reopened, field) for field in FIELDS] == [1, "explicit", 1]


def test_legacy_empty_binding_records_only_current_default_initialization(tmp_path):
    host = host_with_store(tmp_path)
    store = host.conversation_store.threads
    store.write(ConversationThread("legacy-empty", "alice", owner_id="alice"))
    assert thread_model_profile_id(host, "legacy-empty") == "default"
    row = store.load("legacy-empty")
    assert [getattr(row, field) for field in FIELDS] == [1, "default", 0]
    assert thread_model_profile_id(host, "legacy-empty") == "default"
    assert store.load("legacy-empty").model_selection_revision == 1


@pytest.mark.parametrize("changes", [
    {"model_selection_revision": True}, {"model_selection_revision": -1},
    {"model_selection_revision": "0"}, {"model_selection_revision": 0.0},
    {"model_selection_source": None}, {"model_selection_source": "user said manual"},
    {"model_selection_last_explicit_revision": True},
    {"model_selection_last_explicit_revision": -1},
    {"model_selection_revision": 1},
    {"model_selection_source": "explicit"},
    {"model_selection_revision": 1, "model_selection_source": "explicit"},
    {"model_selection_revision": 1, "model_selection_source": "default", "model_selection_last_explicit_revision": 1},
    {"model_selection_revision": 2, "model_selection_source": "default"},
    {"model_selection_revision": 1, "model_selection_source": "automatic", "model_selection_last_explicit_revision": 1},
    {"model_selection_revision": 1, "model_selection_source": "automatic", "model_selection_last_explicit_revision": 2},
    {"model_selection_revision": 1, "model_selection_source": "inherited", "model_profile_id": ""},
])
def test_bad_selection_facts_fail_closed_without_overwriting_file(tmp_path, changes):
    host = host_with_store(tmp_path)
    store = host.conversation_store.threads
    payload = ConversationThread("bad", "alice", model_profile_id="default").to_dict()
    payload.update(changes)
    path = store.storage.thread_path("bad")
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    row, error = store.load_report("bad")
    assert row is None and error
    with pytest.raises(ValueError):
        store.update_atomic("bad", lambda value: replace(value, title="不得写入"))
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing", FIELDS)
def test_partial_selection_fields_are_not_legacy(tmp_path, missing):
    host = host_with_store(tmp_path)
    row = ConversationThread("partial", "alice", model_profile_id="default").to_dict()
    row.pop(missing)
    with pytest.raises(ValueError, match="不完整"):
        ConversationThread.from_dict(row)


@pytest.mark.parametrize("same_model", [True, False])
def test_selection_revision_and_pending_terminal_share_one_original_commit(tmp_path, monkeypatch, same_model):
    host = host_with_store(tmp_path)
    other = add(host, model_name="other")[0]
    result = execute_local_model_operation(host, "session", "list", {})
    store, tid = host.conversation_store.threads, result["thread_id"]
    store.update_atomic(tid, lambda row: replace(row, summary="历史", compact_generation=7,
        compact_checkpoint_id="checkpoint", provider_context_observation={"input_tokens": 12},
        model_context_usage={"current_tokens": 13}, metadata={"keep": 1, SUBAGENT_MODEL_ADVICE_KEY: {
            "status": "pending", "profile_id": other, "operation_id": "operation",
        }}))
    before, commits = store.load(tid), []
    original = store.update_atomic

    def capture(thread_id, update):
        result = original(thread_id, update)
        commits.append(result)
        return result

    monkeypatch.setattr(store, "update_atomic", capture)
    assert thread_model_profile_id(host, tid, select="default" if same_model else other) == ("default" if same_model else other)
    assert len(commits) == 1
    row = commits[0]
    assert [getattr(row, field) for field in FIELDS] == [before.model_selection_revision + 1, "explicit", before.model_selection_revision + 1]
    assert row.metadata[SUBAGENT_MODEL_ADVICE_KEY] == {
        "status": "retained", "profile_id": other, "operation_id": "operation", "reason": "explicit_model_selection",
    }
    assert row.summary == "历史" and row.compact_generation == 7 and row.compact_checkpoint_id == "checkpoint"
    assert row.metadata["keep"] == 1
    assert row.provider_context_observation == ({"input_tokens": 12} if same_model else {})
    assert row.model_context_usage == ({"current_tokens": 13} if same_model else {})


def test_concurrent_same_profile_explicit_selections_all_advance_from_latest(tmp_path, monkeypatch):
    host = host_with_store(tmp_path)
    tid = execute_local_model_operation(host, "session", "list", {})["thread_id"]
    store = host.conversation_store.threads
    before = store.load(tid).model_selection_revision
    barrier, original = threading.Barrier(8), store.load

    def stale_read(thread_id):
        row = original(thread_id)
        barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(store, "load", stale_read)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda _: thread_model_profile_id(host, tid, select="default"), range(8))) == ["default"] * 8
    row = original(tid)
    assert [getattr(row, field) for field in FIELDS] == [before + 8, "explicit", before + 8]


@pytest.mark.parametrize("changes", [
    {"model_selection_revision": 0, "model_selection_source": "unknown", "model_selection_last_explicit_revision": 0},
    {"model_selection_revision": 4, "model_selection_last_explicit_revision": 4},
    {"model_profile_id": "another"},
    {"model_selection_source": "automatic"},
    {"model_selection_revision": 3, "model_selection_source": "automatic", "model_selection_last_explicit_revision": 0},
    {"model_selection_revision": 1, "model_selection_source": "inherited", "model_selection_last_explicit_revision": 0},
])
def test_original_thread_transaction_rejects_nonmonotonic_selection_facts(tmp_path, changes):
    host = host_with_store(tmp_path)
    tid = execute_local_model_operation(host, "session", "select", {"profile_id": "default"})["thread_id"]
    store = host.conversation_store.threads
    before = store.storage.thread_path(tid).read_bytes()
    assert store.load(tid).model_selection_revision == 2
    with pytest.raises((ValueError, DataCorruptionError)):
        store.update_atomic(tid, lambda row: replace(row, **changes))
    assert store.storage.thread_path(tid).read_bytes() == before


@pytest.mark.parametrize("source", ["default", "inherited"])
def test_existing_legacy_model_cannot_be_relabelled_as_new_initialization(tmp_path, source):
    host = host_with_store(tmp_path)
    store = host.conversation_store.threads
    store.write(ConversationThread("legacy", "alice", model_profile_id="default"))
    with pytest.raises(DataCorruptionError, match="初始绑定"):
        store.update_atomic("legacy", lambda row: replace(row, model_selection_revision=1, model_selection_source=source))
    assert store.load("legacy").model_selection_source == "unknown"


def test_invalid_explicit_profile_never_records_selection_or_retires_pending(tmp_path):
    host = host_with_store(tmp_path)
    tid = execute_local_model_operation(host, "session", "list", {})["thread_id"]
    store = host.conversation_store.threads
    store.update_atomic(tid, lambda row: replace(row, metadata={SUBAGENT_MODEL_ADVICE_KEY: {"status": "pending"}}))
    before = store.storage.thread_path(tid).read_bytes()
    with pytest.raises(ValueError):
        thread_model_profile_id(host, tid, select="missing-profile")
    assert store.storage.thread_path(tid).read_bytes() == before


def test_selection_source_is_a_fact_not_a_permanent_pin(tmp_path):
    host = host_with_store(tmp_path)
    tid = execute_local_model_operation(host, "session", "select", {"profile_id": "default"})["thread_id"]
    store = host.conversation_store.threads
    before = store.load(tid)
    # 只验证原存储允许宿主写入已验证的后续事实；本片没有自动采用或网络入口。
    after = store.update_atomic(tid, lambda row: replace(row,
        model_selection_revision=row.model_selection_revision + 1, model_selection_source="automatic"))
    assert after.model_selection_last_explicit_revision == before.model_selection_revision
    thread_model_profile_id(host, tid, select="default")
    latest = store.load(tid)
    assert latest.model_selection_revision == after.model_selection_revision + 1
    assert latest.model_selection_last_explicit_revision == latest.model_selection_revision
