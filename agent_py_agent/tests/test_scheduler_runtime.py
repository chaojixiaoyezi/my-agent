from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest, _run_params
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig


class _Backend:
    name = "scheduler-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.prompts.append(prompt)
        return ModelResponse(text="已按计划完成。", backend=self.name)


def _create_job(agent: SimpleAgent, thread_id: str, name: str) -> None:
    agent.scheduler_repository.create_job(
        SchedulerJobCreateRequest(
            name=name,
            prompt=f"执行 {name}",
            thread_id=thread_id,
            source_task_id="",
            schedule={
                "kind": "every",
                "every_seconds": 600,
                "anchor_at": 1_000,
                "timezone": "UTC",
            },
            misfire_grace_seconds=60,
            skill_refs=[],
            source_request_id=name,
            now=900,
        )
    )


def test_durable_schedule_run_uses_stable_run_identity_and_full_owner_tools() -> None:
    wake = {
        "wake_signal_id": "wake-1",
        "metadata": {
            "scheduler_job_id": "job-1",
            "scheduler_run_id": "srun-1",
            "scheduler_prompt": "执行日报",
            "scheduler_skill_refs": [],
        },
    }
    request = BackgroundRunRequest(
        thread_id="thread-1",
        reason="scheduled_job_due",
        wake_signal=wake,
    )
    params = _run_params(request.thread_id, request)
    assert params.request_id == "srun-1"
    assert params.run_id == "srun-1"
    assert params.task_id == "srun-1"
    assert params.allowed_tools is None
    assert params.task_attributes == {
        "conversation_thread_id": "thread-1",
        "background_wake_signal_id": "wake-1",
        "scheduler_job_id": "job-1",
        "scheduler_run_id": "srun-1",
        "scheduler_trigger": "",
    }


def test_two_due_jobs_in_one_thread_both_execute_and_close_history(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-1",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "first")
    _create_job(agent, thread.thread_id, "second")
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": runtime,
            "store": store,
            "scheduler_service": agent.scheduler_service,
        }
    )

    reports = scheduler.tick(now=1_000)

    assert len(reports) == 2
    assert len(backend.prompts) == 2
    assert any("执行 first" in prompt for prompt in backend.prompts)
    assert any("执行 second" in prompt for prompt in backend.prompts)
    assert store.pending_wake_signals() == []
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert len(history) == 2
    assert {row["status"] for row in history} == {"done"}
    assert len(channels.adapter("internal").sent_messages) == 2


def test_scheduled_message_tool_delivery_is_mirrored_once_without_fallback_send(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "feishu",
            "channel_conversation_id": "chat-1",
            "channel_user_id": "ou-user-1",
            "now": 900.0,
        }
    )
    delivery = {
        "schema_version": "message_tool_delivery.v1",
        "delivery_status": "sent",
        "source_owner_delivery": True,
        "channel": "feishu",
        "content": "提醒到啦：检查索引备份。",
        "receipt_id": "receipt-one",
        "deduplicated": False,
        "attachments": [],
    }

    def run(*_args, **_kwargs):
        return SimpleNamespace(
            response="模型最终又说了一遍，但这段不应再次外发。",
            archive_tool_calls=[{"tool": "send_message", "ok": True}],
            delivery_artifacts=[],
            message_tool_deliveries=[delivery],
        )

    agent.run = run
    channels = FakeDeliveryService()
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    request = {
        "thread_id": thread.thread_id,
        "reason": "scheduled_job_due",
        "wake_signal": {
            "metadata": {
                "scheduler_job_id": "job-1",
                "scheduler_run_id": "srun-1",
                "scheduler_prompt": "提醒我检查索引备份",
            }
        },
        "now": 1_000.0,
    }

    first = runtime.run_once(request)
    second = runtime.run_once(request)

    assert first.delivery_status == second.delivery_status == "sent"
    assert first.delivery_reason == second.delivery_reason == "scheduled_message_tool_delivery"
    assert first.response == second.response == "提醒到啦：检查索引备份。"
    assert channels.adapter("feishu").sent_messages == []
    messages = store.recent_messages(thread.thread_id)
    assert [row.content for row in messages] == ["提醒到啦：检查索引备份。"]
    assert messages[0].metadata["message_tool_delivery"]["receipt_id"] == "receipt-one"
