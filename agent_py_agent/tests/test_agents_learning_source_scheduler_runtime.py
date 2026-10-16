"""B 路学习来源：scheduler/runtime 信封来源的消息级投影（第 2 步）。"""

import json
from types import SimpleNamespace

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation import learning_source as ls
from agent_py_agent.agent.conversation.background_delivery import (
    _background_owner_message_metadata,
)
from agent_py_agent.agent.conversation.runtime import _record_quota_fallback_message
from agent_py_agent.agent.scheduler.service import _wake_metadata


def _run_row(**overrides):
    values = {
        "job_id": "job-1",
        "run_id": "run-1",
        "job_version": 3,
        "trigger": "cron",
        "scheduled_for": 123.0,
        "prompt": "say hi",
        "skill_refs": [],
        "source_task_id": "task-1",
    }
    values.update(overrides)
    return values


def _delivery_request(**overrides):
    values = {
        "thread_id": "thread-1",
        "task_id": "",
        "reason": "scheduled_job_due",
        "conversation_turn_id": "turn-1",
        "wake_signal": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _owner_message_metadata(request, *, delivery_reason="owner_delivery", frozen_retry=None):
    return _background_owner_message_metadata(
        request,
        delivery_reason=delivery_reason,
        projection_status="clean",
        delivery_artifacts=(),
        evidence_refs=(),
        operation_verification=None,
        frozen_retry=frozen_retry,
    )


def _scheduled_wake(**metadata_overrides):
    metadata = {
        "scheduler_run_id": "run-1",
        "scheduler_job_id": "job-1",
        "learning_source": ls.schedule_projection(),
    }
    metadata.update(metadata_overrides)
    return {"wake_signal_id": "wake-1", "metadata": metadata}


# ---------- scheduler 信封 ----------


def test_wake_metadata_carries_schedule_projection():
    metadata = _wake_metadata(_run_row())
    assert metadata["learning_source"] == ls.schedule_projection()
    serialized = json.loads(json.dumps(metadata))
    assert serialized["learning_source"]["can_learn"] is False
    assert serialized["learning_source"]["source_kind"] == "schedule"


# ---------- 消息级：后台回复 metadata ----------


def test_owner_message_metadata_takes_schedule_from_wake_envelope():
    metadata = _owner_message_metadata(_delivery_request(wake_signal=_scheduled_wake()))
    assert metadata["learning_source"] == ls.schedule_projection()


def test_owner_message_metadata_derives_schedule_from_legacy_envelope_traces():
    wake = {"wake_signal_id": "wake-1", "metadata": {"scheduler_run_id": "run-1"}}
    metadata = _owner_message_metadata(_delivery_request(wake_signal=wake))
    assert metadata["learning_source"]["source_kind"] == "schedule"


def test_owner_message_metadata_defaults_to_background():
    metadata = _owner_message_metadata(_delivery_request(reason="subagent_completion"))
    assert metadata["learning_source"]["source_kind"] == "background"
    assert metadata["learning_source"]["can_learn"] is False


def test_owner_message_metadata_keeps_frozen_projection_on_retry():
    frozen = SimpleNamespace(
        message_metadata={
            "learning_source": ls.schedule_projection(),
            "reason": "scheduled_job_due",
        }
    )
    metadata = _owner_message_metadata(
        _delivery_request(),
        delivery_reason="cached_owner_delivery_retry",
        frozen_retry=frozen,
    )
    assert metadata["learning_source"] == ls.schedule_projection()
    assert metadata["background_delivery_retry_reason"] == "cached_owner_delivery_retry"


def test_owner_message_metadata_backfills_legacy_frozen_payload():
    frozen = SimpleNamespace(message_metadata={"reason": "scheduled_job_due"})
    metadata = _owner_message_metadata(
        _delivery_request(),
        delivery_reason="cached_owner_delivery_retry",
        frozen_retry=frozen,
    )
    assert metadata["learning_source"]["source_kind"] == "background"
    assert metadata["learning_source"]["can_learn"] is False


# ---------- 端到端：普通线程混入定时回合 ----------


def _user_thread(store, chat_id="chat-1"):
    return store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": chat_id,
            "channel_user_id": "user-1",
            "now": 10.0,
            "learning_source": ls.user_dialogue_projection(),
        }
    )


def test_scheduled_turn_message_in_user_thread_is_unlearnable(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _user_thread(store)
    metadata = _owner_message_metadata(
        _delivery_request(thread_id=thread.thread_id, wake_signal=_scheduled_wake())
    )
    message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "定时结果",
            "now": 11.0,
            "metadata": metadata,
        }
    )
    facts = ls.facts_for_message(
        message.metadata,
        owner="user-1",
        thread_id=thread.thread_id,
        thread_source=thread.learning_source,
    )
    assert facts.can_learn is False
    assert facts.source_kind == "schedule"


def test_background_turn_message_without_envelope_is_unlearnable(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _user_thread(store, chat_id="chat-2")
    metadata = _owner_message_metadata(_delivery_request(reason="subagent_completion"))
    message = store.messages.append(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": "后台结果",
            "now": 11.0,
            "metadata": metadata,
        }
    )
    facts = ls.facts_for_message(
        message.metadata,
        owner="user-1",
        thread_id=thread.thread_id,
        thread_source=thread.learning_source,
    )
    assert facts.can_learn is False
    assert facts.source_kind == "background"


def test_quota_fallback_message_carries_background_projection(tmp_path):
    store = ConversationStore(tmp_path / "conversations")
    thread = _user_thread(store, chat_id="chat-3")
    scheduler = SimpleNamespace(store=store)
    signal = SimpleNamespace(
        thread_id=thread.thread_id,
        wake_signal_id="wake-9",
        root_task_id="task-9",
    )
    _record_quota_fallback_message(scheduler, signal, "quota notice", "internal")
    entries, errors = store.messages.after_report(
        thread.thread_id, after_message_id="", limit=10
    )
    assert not errors
    assert len(entries) == 1
    facts = ls.facts_for_message(
        entries[0].metadata,
        owner="user-1",
        thread_id=thread.thread_id,
        thread_source=thread.learning_source,
    )
    assert facts.can_learn is False
    assert facts.source_kind == "background"
