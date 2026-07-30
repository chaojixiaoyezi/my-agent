from __future__ import annotations

from agent.adapter.feishu_unlock_queue import FeishuUnlockQueue
from agent.adapter.protocol import IncomingMessage


def _message(message_id: str, *, user_id: str = "ou_user") -> IncomingMessage:
    return IncomingMessage(
        channel="feishu",
        user_id=user_id,
        content=f"content-{message_id}",
        message_id=message_id,
        conversation_id="oc_chat",
        metadata={"feishu_chat_type": "p2p"},
    )


def test_unlock_queue_is_fifo_and_deduplicates_live_and_resumed_messages():
    queue = FeishuUnlockQueue()
    first = _message("m1")
    second = _message("m2")

    assert queue.enqueue_locked(first) == "added"
    assert queue.enqueue_if_pending(first) == "duplicate"
    assert queue.enqueue_if_pending(second) == "added"
    assert queue.begin_drain("ou_user") is True
    assert queue.begin_drain("ou_user") is False

    assert queue.next_for_drain("ou_user") is first
    queue.complete(first)
    assert queue.next_for_drain("ou_user") is second
    queue.complete(second)
    assert queue.next_for_drain("ou_user") is None

    assert queue.was_resumed(first) is True
    assert queue.was_resumed(second) is True
    assert queue.enqueue_locked(first) == "duplicate"
    assert queue.enqueue_if_pending(_message("m3")) == "inactive"


def test_unlock_queue_caps_are_bounded_without_overwriting_older_messages():
    queue = FeishuUnlockQueue(per_user_cap=2, total_cap=2)
    first = _message("m1")
    second = _message("m2")
    overflow = _message("m3")

    assert queue.enqueue_locked(first) == "added"
    assert queue.enqueue_if_pending(second) == "added"
    assert queue.enqueue_if_pending(overflow) == "full"
    assert queue.depth("ou_user") == 2

    assert queue.begin_drain("ou_user") is True
    assert queue.next_for_drain("ou_user") is first
    queue.complete(first)
    assert queue.next_for_drain("ou_user") is second
    queue.complete(second)
    assert queue.next_for_drain("ou_user") is None


def test_unlock_queue_keeps_users_isolated():
    queue = FeishuUnlockQueue()
    first = _message("same-id", user_id="ou_a")
    second = _message("same-id", user_id="ou_b")

    assert queue.enqueue_locked(first) == "added"
    assert queue.enqueue_locked(second) == "added"
    assert queue.begin_drain("ou_a") is True
    assert queue.next_for_drain("ou_a") is first
    queue.complete(first)
    assert queue.next_for_drain("ou_a") is None
    assert queue.depth("ou_b") == 1
