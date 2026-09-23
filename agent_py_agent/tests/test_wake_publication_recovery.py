from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from agent_py_agent.agent.conversation import ConversationStore, store_wakes
from agent_py_agent.agent.conversation import store_wake_publication as publication
from agent_py_agent.agent.conversation.goal_runtime import raise_goal_continuation_wake
from agent_py_agent.agent.conversation.models import ThreadGoal
from agent_py_agent.agent.runtime_errors import DataCorruptionError


# LLM: 只在 pytest 临时目录创建 Store；请求带稳定键与完整观察字段，不使用真实 owner 或模型。
# 函数用途: 构造原文件发布接口的最小配对请求，供故障矩阵和并发验收复用。
def _fixture(tmp_path, *, retain_handled=True):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "test", "channel": "internal",
        "channel_conversation_id": "publication", "channel_user_id": "test",
    })
    observation = {
        "thread_id": thread.thread_id, "event_type": "child_finished", "summary": "原始结论",
        "source_agent_id": "child-1", "parent_agent_id": "parent-1", "root_task_id": "root-1",
        "requires_main_agent": True, "requires_llm_report": True, "evidence_refs": ["artifact://original"],
        "metadata": {"original": [1, 2]}, "now": 20.0,
    }
    wake = {"thread_id": thread.thread_id, "reason": "child_finished", "dedupe_key": "child:attempt-1",
            "retain_handled": retain_handled, "urgency": "normal", "now": 21.0}
    return store, observation, wake


# LLM: 只统计真实 pending/handled 信号，排除 dedupe 发布记录；同一个消费中信号的两份不算新通知。
# 函数用途: 读取发布后的信号身份集合，避免以回执文件数量冒充通知数量。
def _signal_ids(store):
    return {
        json.loads(path.read_text())["wake_signal_id"]
        for kind in ("urgent", "normal", "handled")
        for path in (store.storage.wake_queue_dir / kind).glob("*.json")
    }


@pytest.mark.parametrize("stage", [
    "reserve_before", "reserve_after", "wake_before", "wake_after",
    "observation_before", "observation_after", "publish_before", "publish_after",
])
@pytest.mark.parametrize("consume", [False, True])
def test_pair_recovers_fixed_payload_at_every_file_boundary(tmp_path, monkeypatch, stage, consume):
    store, observation, wake = _fixture(tmp_path)
    original_receipt = publication.write_json_file_atomic_unlocked
    original_signal = publication.write_json_file_atomic
    original_observation = publication.append_jsonl
    injected = False

    def fail_at(point):
        nonlocal injected
        if point != stage or injected:
            return
        injected = True
        if consume and store.wakes.pending():
            store.wakes.mark_handled(store.wakes.pending()[0].wake_signal_id)
        raise OSError(f"injected {point}")

    def receipt_write(path, payload):
        name = "reserve" if payload["phase"] == "prepared" else "publish"
        fail_at(f"{name}_before")
        original_receipt(path, payload)
        fail_at(f"{name}_after")

    def signal_write(path, payload):
        fail_at("wake_before")
        original_signal(path, payload)
        fail_at("wake_after")

    def observation_write(path, payload, **kwargs):
        fail_at("observation_before")
        original_observation(path, payload, **kwargs)
        fail_at("observation_after")

    monkeypatch.setattr(publication, "write_json_file_atomic_unlocked", receipt_write)
    monkeypatch.setattr(publication, "write_json_file_atomic", signal_write)
    monkeypatch.setattr(publication, "append_jsonl", observation_write)
    with pytest.raises(OSError, match="injected"):
        store.wakes.append_observation(observation, wake)
    assert injected
    receipt_path = store.storage.wake_dedupe_path(wake["thread_id"], wake["dedupe_key"])
    before = receipt_path.read_bytes() if receipt_path.exists() else None
    receipt = store.wakes.delivery_receipt(wake["thread_id"], wake["dedupe_key"])
    assert receipt in {"pending", "handled"} if stage == "publish_after" else receipt == ""
    assert (receipt_path.read_bytes() if receipt_path.exists() else None) == before

    # 新 Store 模拟重启，新请求字段故意改变；已预留的旧负载仍须完整恢复。
    store = ConversationStore(store.storage.root)
    changed = {**observation, "summary": "重试的新内容", "metadata": {"changed": True}, "now": 99.0}
    recovered, signal = store.wakes.append_observation(changed, wake)
    expected = changed if stage == "reserve_before" else observation
    assert recovered.summary == expected["summary"]
    assert recovered.metadata == expected["metadata"]
    assert recovered.observed_at == expected["now"]
    assert recovered.requires_main_agent and recovered.requires_llm_report
    assert recovered.evidence_refs == ("artifact://original",)
    assert recovered.wake_signal_id == signal.wake_signal_id
    assert signal.observation_id == recovered.observation_id
    assert len(_signal_ids(store)) == 1
    rows = store.observations.recent(wake["thread_id"], limit=0)
    assert len(rows) == 1 and rows[0].observation_id == recovered.observation_id
    if signal.status == "handled":
        assert rows[0].handled_at > 0
        assert store.observations.unhandled_requiring_main() == []
    for _ in range(2):
        again, replay = store.wakes.append_observation(changed, wake)
        assert again == recovered and replay.wake_signal_id == signal.wake_signal_id
    assert len(store.observations.recent(wake["thread_id"], limit=0)) == 1
    assert len(_signal_ids(store)) == 1


def test_generic_handled_pair_starts_new_generation(tmp_path):
    store, observation, wake = _fixture(tmp_path, retain_handled=False)
    first, signal = store.wakes.append_observation(observation, wake)
    same, duplicate = store.wakes.append_observation({**observation, "summary": "不能覆盖"}, wake)
    assert same == first and duplicate.wake_signal_id == signal.wake_signal_id
    store.wakes.mark_handled(signal.wake_signal_id)
    second, next_signal = store.wakes.append_observation({**observation, "summary": "新一轮"}, wake)
    assert next_signal.wake_signal_id != signal.wake_signal_id
    assert second.observation_id != first.observation_id and second.summary == "新一轮"
    assert len(store.observations.recent(wake["thread_id"], limit=0)) == 2


def test_goal_can_continue_after_previous_fixed_key_wake_is_handled(tmp_path):
    store, observation, _ = _fixture(tmp_path)
    goal = ThreadGoal(goal_id="goal-test", thread_id=observation["thread_id"], objective="继续任务", task_id="task-1")
    first = raise_goal_continuation_wake(store, goal)
    store.wakes.mark_handled(first.wake_signal_id)
    second = raise_goal_continuation_wake(store, goal)
    assert first.dedupe_key == second.dedupe_key
    assert first.wake_signal_id != second.wake_signal_id


def test_retain_handled_is_decided_under_same_lock_during_concurrent_publish(tmp_path, monkeypatch):
    store, observation, wake = _fixture(tmp_path)
    installed, release = Event(), Event()
    original = publication.write_json_file_atomic_unlocked

    def hold_completion(path, payload):
        if payload["phase"] == "published" and not installed.is_set():
            installed.set()
            assert release.wait(5)
        return original(path, payload)

    monkeypatch.setattr(publication, "write_json_file_atomic_unlocked", hold_completion)
    another = ConversationStore(store.storage.root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(store.wakes.append_observation, observation, wake)
        assert installed.wait(5)
        try:
            # 正在发布时查询也只读取 prepared 快照，不等写锁，不提前报告交付。
            assert store.wakes.delivery_receipt(wake["thread_id"], wake["dedupe_key"]) == ""
        except BaseException:
            release.set()
            raise
        second = pool.submit(another.wakes.append_observation, {**observation, "summary": "重复调用"}, wake)
        store.wakes.mark_handled(store.wakes.pending()[0].wake_signal_id)
        release.set()
        first_result, second_result = first.result(timeout=5), second.result(timeout=5)
    assert first_result[1].wake_signal_id == second_result[1].wake_signal_id
    assert first_result[0] == second_result[0]
    assert len(_signal_ids(store)) == 1 and not store.wakes.pending()
    assert len(store.observations.recent(wake["thread_id"], limit=0)) == 1


@pytest.mark.parametrize("handled", [False, True])
@pytest.mark.parametrize("retain", [False, True])
def test_v1_receipt_query_is_read_only_and_migration_is_explicit(tmp_path, handled, retain):
    store, observation, wake = _fixture(tmp_path, retain_handled=retain)
    original_observation, signal = store.wakes.append_observation(observation, wake)
    if handled:
        store.wakes.mark_handled(signal.wake_signal_id)
    path = store.storage.wake_dedupe_path(wake["thread_id"], wake["dedupe_key"])
    path.write_text(json.dumps({"schema_version": "wake_dedupe.v1", "thread_id": signal.thread_id,
                                "dedupe_key": signal.dedupe_key, "wake_signal_id": signal.wake_signal_id,
                                "updated_at": signal.created_at}))
    before = path.read_bytes()
    for lock_path in store.storage.root.rglob("*.lock"):
        lock_path.unlink()
    files_before = sorted(str(item.relative_to(store.storage.root)) for item in store.storage.root.rglob("*"))
    assert store.wakes.delivery_receipt(signal.thread_id, signal.dedupe_key) == ("handled" if handled else "pending")
    assert path.read_bytes() == before
    assert sorted(str(item.relative_to(store.storage.root)) for item in store.storage.root.rglob("*")) == files_before
    returned_observation, returned = store.wakes.append_observation({**observation, "summary": "新请求"}, wake)
    migrated = json.loads(path.read_text())
    assert migrated["schema_version"] == "wake_dedupe.v2"
    assert migrated["migration"] == {"from_schema": "wake_dedupe.v1", "wake_signal_id": signal.wake_signal_id}
    if handled and not retain:
        assert returned.wake_signal_id != signal.wake_signal_id
    else:
        assert returned.wake_signal_id == signal.wake_signal_id and returned_observation == original_observation


@pytest.mark.parametrize("content", ["not-json", "{}", '{"schema_version":"unknown"}'])
def test_corrupt_receipt_cannot_be_cleared_or_replaced(tmp_path, content):
    store, observation, wake = _fixture(tmp_path)
    path = store.storage.wake_dedupe_path(wake["thread_id"], wake["dedupe_key"])
    path.write_text(content)
    with pytest.raises(DataCorruptionError):
        store.wakes.delivery_receipt(wake["thread_id"], wake["dedupe_key"])
    with pytest.raises(DataCorruptionError):
        store.wakes.append_observation(observation, wake)
    assert path.read_text() == content and not _signal_ids(store)


def test_observation_corruption_preserves_prepared_publication(tmp_path):
    store, observation, wake = _fixture(tmp_path)
    ledger = store.storage.observation_path(wake["thread_id"])
    ledger.write_text("broken-row\n")
    with pytest.raises(DataCorruptionError):
        store.wakes.append_observation(observation, wake)
    path = store.storage.wake_dedupe_path(wake["thread_id"], wake["dedupe_key"])
    assert json.loads(path.read_text())["phase"] == "prepared"
    assert ledger.read_text() == "broken-row\n" and len(_signal_ids(store)) == 1


def test_cached_owner_delivery_is_preserved_by_publication_replay(tmp_path):
    store, observation, wake = _fixture(tmp_path)
    _, signal = store.wakes.append_observation(observation, wake)
    store.wakes.cache_delivery(signal.wake_signal_id, {"text": "已冻结外发正文"})
    _, replay = store.wakes.append_observation(observation, wake)
    assert replay.metadata["owner_delivery"] == {"text": "已冻结外发正文"}


def test_v1_incomplete_observation_is_not_rebuilt_from_new_request(tmp_path):
    store, observation, wake = _fixture(tmp_path)
    _, signal = store.wakes.append_observation(observation, wake)
    store.storage.observation_path(wake["thread_id"]).unlink()
    path = store.storage.wake_dedupe_path(wake["thread_id"], wake["dedupe_key"])
    path.write_text(json.dumps({"schema_version": "wake_dedupe.v1", "thread_id": signal.thread_id,
                                "dedupe_key": signal.dedupe_key, "wake_signal_id": signal.wake_signal_id}))
    before = path.read_bytes()
    with pytest.raises(DataCorruptionError, match="complete paired observation"):
        store.wakes.append_observation({**observation, "summary": "不可伪造原观察"}, wake)
    assert path.read_bytes() == before and len(_signal_ids(store)) == 1


@pytest.mark.parametrize("consume", [False, True])
def test_signal_only_half_install_replays_original_generation(tmp_path, monkeypatch, consume):
    store, _, wake = _fixture(tmp_path, retain_handled=False)
    original = publication.write_json_file_atomic_unlocked
    injected = False

    def fail_completion(path, payload):
        nonlocal injected
        if payload["phase"] == "published" and not injected:
            injected = True
            if consume:
                store.wakes.mark_handled(store.wakes.pending()[0].wake_signal_id)
            raise OSError("injected incomplete signal publication")
        return original(path, payload)

    monkeypatch.setattr(publication, "write_json_file_atomic_unlocked", fail_completion)
    with pytest.raises(OSError):
        store.wakes.raise_signal({**wake, "summary": "原信号"})
    replay = store.wakes.raise_signal({**wake, "summary": "重试不覆盖"})
    assert replay.summary == "原信号" and len(_signal_ids(store)) == 1
    if consume:
        assert store.wakes.raise_signal(wake).wake_signal_id != replay.wake_signal_id


def test_no_key_failure_does_not_fabricate_observation_only_success(tmp_path, monkeypatch):
    store, observation, wake = _fixture(tmp_path)
    wake.pop("dedupe_key")

    def unavailable(*_args, **_kwargs):
        raise OSError("wake storage unavailable")

    monkeypatch.setattr(store_wakes, "write_json_file_atomic", unavailable)
    with pytest.raises(OSError):
        store.wakes.append_observation(observation, wake)
    assert store.observations.recent(wake["thread_id"], limit=0) == []
    assert not _signal_ids(store)
