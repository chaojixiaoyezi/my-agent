"""SessionTaskStore 的合同测试（无真实模型请求）。

覆盖：创建、幂等重放、状态机（合法/非法迁移）、终态保护、结果字段、
链深按 origin_task_id 结构化计算（不信任模型传入的深度）、列表与读坏文件。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_tasks import (
    SESSION_TASK_ACCEPTED,
    SESSION_TASK_CANCELLED,
    SESSION_TASK_DONE,
    SESSION_TASK_FAILED,
    SESSION_TASK_QUEUED,
    SessionTask,
    SessionTaskDraft,
    SessionTaskUpdate,
)


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations").session_tasks


def test_create_returns_queued_task(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X", body_guidance_id="g1"), now=1.0)
    assert task.status == SESSION_TASK_QUEUED
    assert task.task_id
    assert store.load(task.task_id).goal == "做 X"


def test_idempotent_replay_returns_same_task(store) -> None:
    a = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1"), now=1.0)
    b = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1"), now=2.0)
    assert a.task_id == b.task_id
    assert len(store.list_report()[0]) == 1


def test_dedupe_key_reused_with_different_input_raises(store) -> None:
    store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1"), now=1.0)
    with pytest.raises(ValueError):
        store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="C", goal="做 Y", dedupe_key="k1"), now=2.0)


def test_legal_transitions(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X"), now=1.0)
    assert store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_ACCEPTED), now=2.0).status == SESSION_TASK_ACCEPTED
    done = store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_DONE, summary="完成", result_refs=("ref-1",)), now=3.0
    )
    assert done.status == SESSION_TASK_DONE
    assert done.summary == "完成"
    assert done.result_refs == ("ref-1",)


def test_queued_can_be_cancelled_directly(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X"), now=1.0)
    assert store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_CANCELLED), now=2.0).status == SESSION_TASK_CANCELLED


def test_terminal_status_cannot_be_overwritten(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X"), now=1.0)
    store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_ACCEPTED), now=2.0)
    store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_DONE), now=3.0)
    with pytest.raises(ValueError):
        store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_FAILED), now=4.0)


def test_illegal_transition_rejected(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X"), now=1.0)
    # queued 不能直接到 done（必须先 accepted）。
    with pytest.raises(ValueError):
        store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_DONE), now=2.0)


def test_same_status_advance_is_idempotent(store) -> None:
    task = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="做 X"), now=1.0)
    store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_ACCEPTED), now=2.0)
    same = store.advance(task.task_id, SessionTaskUpdate(SESSION_TASK_ACCEPTED), now=3.0)
    assert same.status == SESSION_TASK_ACCEPTED


def test_chain_depth_follows_origin_task_id(store) -> None:
    root = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="根"), now=1.0)
    child = store.create(
        SessionTaskDraft(
            sender_thread_id="B", target_thread_id="C", goal="子", origin_task_id=root.task_id
        ),
        now=2.0,
    )
    grand = store.create(
        SessionTaskDraft(
            sender_thread_id="C", target_thread_id="D", goal="孙", origin_task_id=child.task_id
        ), now=3.0,
    )
    assert store.chain_depth(root) == 0
    assert store.chain_depth(child) == 1
    assert store.chain_depth(grand) == 2


def test_chain_depth_breaks_cycle(store) -> None:
    a = store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="a"), now=1.0)
    b = store.create(
        SessionTaskDraft(
            sender_thread_id="B", target_thread_id="C", goal="b", origin_task_id=a.task_id
        ),
        now=2.0,
    )
    # 手工制造环：把 a 的 origin 指向 b。
    store.advance(a.task_id, SessionTaskUpdate(SESSION_TASK_ACCEPTED), now=3.0)
    cyclic = SessionTask.from_dict({**a.to_dict(), "origin_task_id": b.task_id})
    assert store.chain_depth(cyclic) >= 64 or store.chain_depth(cyclic) == 1


def test_load_missing_returns_none(store) -> None:
    assert store.load("stask-nonexistent") is None


def test_list_report_sorted_by_created_at(store) -> None:
    store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="B", goal="1"), now=1.0)
    store.create(SessionTaskDraft(sender_thread_id="A", target_thread_id="C", goal="2"), now=2.0)
    tasks, errors = store.list_report()
    assert [t.goal for t in tasks] == ["1", "2"]
    assert errors == []


def test_same_key_concurrent_creation_allocates_once(store, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.conversation import session_tasks as module

    original = module.new_id
    allocated = []
    lock = threading.Lock()

    def allocate(prefix):
        with lock:
            allocated.append(prefix)
        time.sleep(0.01)
        return original(prefix)

    monkeypatch.setattr(module, "new_id", allocate)
    draft = SessionTaskDraft("A", "B", "same", dedupe_key="concurrent")
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = list(pool.map(lambda _: store.create(draft), range(8)))
    assert len({task.task_id for task in tasks}) == 1
    assert allocated == ["stask"]


def _business_store(tmp_path):
    import inspect

    from agent_py_agent.agent.conversation.business_contracts import (
        BusinessContextIntent,
        BusinessExecutionContext,
    )

    conversation = ConversationStore(tmp_path / "business")
    source = conversation.threads.create(owner_id="owner", title="source")
    target = conversation.threads.create(owner_id="owner", title="target")
    store = conversation.session_tasks
    assert "business_context_factory" in inspect.signature(store.create).parameters
    draft = SessionTaskDraft(source.thread_id, target.thread_id, "business", dedupe_key="business-key")
    intent = BusinessContextIntent("owner", None, None, None)
    calls = []

    def factory(task_id):
        calls.append(task_id)
        return BusinessExecutionContext("owner", "space-a", None, task_id, 1, None, (), (), "business-context.v1")

    return store, draft, intent, factory, calls


def test_business_replay_keeps_frozen_context_and_does_not_call_factory(tmp_path):
    store, draft, intent, factory, calls = _business_store(tmp_path)
    first = store.create(draft, business_context_intent=intent, business_context_factory=factory)
    replay = store.create(draft, business_context_intent=intent,
                          business_context_factory=lambda _: pytest.fail("default space must not be resolved again"))
    assert replay.metadata["business_context"] == first.metadata["business_context"]
    assert replay.metadata["business_context"]["task_id"] == first.task_id
    assert calls == [first.task_id]
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        store.create(draft)


def test_canonical_write_failure_preserves_reserved_id(tmp_path, monkeypatch):
    from agent_py_agent.agent.conversation import session_tasks as module

    store, draft, intent, factory, calls = _business_store(tmp_path)
    writer = module.write_json_file_atomic
    failed = False

    def fail_once(path, payload, **options):
        nonlocal failed
        if path.parent == store._dir() and not failed:
            failed = True
            raise OSError("canonical EIO")
        return writer(path, payload, **options)

    monkeypatch.setattr(module, "write_json_file_atomic", fail_once)
    with pytest.raises(OSError):
        store.create(draft, business_context_intent=intent, business_context_factory=factory)
    task = store.create(draft, business_context_intent=intent, business_context_factory=factory)
    assert calls == [task.task_id, task.task_id]
    assert len(store.list_report(limit=0)[0]) == 1


def test_business_wrong_owner_intent_and_wrong_task_context_fail_closed(tmp_path):
    from dataclasses import replace
    store, draft, intent, factory, calls = _business_store(tmp_path)
    with pytest.raises(ValueError, match="owner"):
        store.create(draft, business_context_intent=replace(intent, owner_id="foreign"), business_context_factory=factory)
    assert calls == []
    with pytest.raises(ValueError, match="task_id"):
        store.create(draft, business_context_intent=intent,
                     business_context_factory=lambda task_id: replace(factory(task_id), task_id="forged"))
    task = store.create(draft, business_context_intent=intent, business_context_factory=factory)
    with pytest.raises(ValueError, match="IDEMPOTENCY_CONFLICT"):
        store.create(draft, business_context_intent=replace(intent, requested_space_id="space-b"),
                     business_context_factory=factory)
    assert store.load(task.task_id).metadata["business_context"]["space_id"] == "space-a"
