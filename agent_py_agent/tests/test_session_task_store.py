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
)


@pytest.fixture()
def store(tmp_path):
    return ConversationStore(tmp_path / "conversations").session_tasks


def test_create_returns_queued_task(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", body_guidance_id="g1", now=1.0)
    assert task.status == SESSION_TASK_QUEUED
    assert task.task_id
    assert store.load(task.task_id).goal == "做 X"


def test_idempotent_replay_returns_same_task(store) -> None:
    a = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1", now=1.0)
    b = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1", now=2.0)
    assert a.task_id == b.task_id
    assert len(store.list_report()[0]) == 1


def test_dedupe_key_reused_with_different_input_raises(store) -> None:
    store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", dedupe_key="k1", now=1.0)
    with pytest.raises(ValueError):
        store.create(sender_thread_id="A", target_thread_id="C", goal="做 Y", dedupe_key="k1", now=2.0)


def test_legal_transitions(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", now=1.0)
    assert store.advance(task.task_id, status=SESSION_TASK_ACCEPTED, now=2.0).status == SESSION_TASK_ACCEPTED
    done = store.advance(
        task.task_id, status=SESSION_TASK_DONE, summary="完成", result_refs=("ref-1",), now=3.0
    )
    assert done.status == SESSION_TASK_DONE
    assert done.summary == "完成"
    assert done.result_refs == ("ref-1",)


def test_queued_can_be_cancelled_directly(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", now=1.0)
    assert store.advance(task.task_id, status=SESSION_TASK_CANCELLED, now=2.0).status == SESSION_TASK_CANCELLED


def test_terminal_status_cannot_be_overwritten(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", now=1.0)
    store.advance(task.task_id, status=SESSION_TASK_ACCEPTED, now=2.0)
    store.advance(task.task_id, status=SESSION_TASK_DONE, now=3.0)
    with pytest.raises(ValueError):
        store.advance(task.task_id, status=SESSION_TASK_FAILED, now=4.0)


def test_illegal_transition_rejected(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", now=1.0)
    # queued 不能直接到 done（必须先 accepted）。
    with pytest.raises(ValueError):
        store.advance(task.task_id, status=SESSION_TASK_DONE, now=2.0)


def test_same_status_advance_is_idempotent(store) -> None:
    task = store.create(sender_thread_id="A", target_thread_id="B", goal="做 X", now=1.0)
    store.advance(task.task_id, status=SESSION_TASK_ACCEPTED, now=2.0)
    same = store.advance(task.task_id, status=SESSION_TASK_ACCEPTED, now=3.0)
    assert same.status == SESSION_TASK_ACCEPTED


def test_chain_depth_follows_origin_task_id(store) -> None:
    root = store.create(sender_thread_id="A", target_thread_id="B", goal="根", now=1.0)
    child = store.create(
        sender_thread_id="B", target_thread_id="C", goal="子", origin_task_id=root.task_id, now=2.0
    )
    grand = store.create(
        sender_thread_id="C", target_thread_id="D", goal="孙", origin_task_id=child.task_id, now=3.0
    )
    assert store.chain_depth(root) == 0
    assert store.chain_depth(child) == 1
    assert store.chain_depth(grand) == 2


def test_chain_depth_breaks_cycle(store) -> None:
    a = store.create(sender_thread_id="A", target_thread_id="B", goal="a", now=1.0)
    b = store.create(
        sender_thread_id="B", target_thread_id="C", goal="b", origin_task_id=a.task_id, now=2.0
    )
    # 手工制造环：把 a 的 origin 指向 b。
    store.advance(a.task_id, status=SESSION_TASK_ACCEPTED, now=3.0)
    cyclic = SessionTask.from_dict({**a.to_dict(), "origin_task_id": b.task_id})
    assert store.chain_depth(cyclic) >= 64 or store.chain_depth(cyclic) == 1


def test_load_missing_returns_none(store) -> None:
    assert store.load("stask-nonexistent") is None


def test_list_report_sorted_by_created_at(store) -> None:
    store.create(sender_thread_id="A", target_thread_id="B", goal="1", now=1.0)
    store.create(sender_thread_id="A", target_thread_id="C", goal="2", now=2.0)
    tasks, errors = store.list_report()
    assert [t.goal for t in tasks] == ["1", "2"]
    assert errors == []
