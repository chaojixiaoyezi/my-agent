from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
from agent_py_agent.agent.conversation.runtime import (
    BackgroundRunRequest,
    _finish_scheduler_wake_claim,
    _run_params,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.scheduler.service import SchedulerRunClaim
from agent_py_agent.agent.settings import AgentConfig


class _Backend:
    name = "scheduler-test"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(
        self,
        prompt: str,
        on_chunk=None,
        on_thinking_delta=None,
        **_kwargs: object,
    ) -> ModelResponse:
        del on_chunk, on_thinking_delta
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


def test_scheduler_wake_uses_run_id_as_durable_root_task(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-root-id",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "root-id")

    wake_ids = agent.scheduler_service.enqueue_ready_runs(now=1_000)

    assert len(wake_ids) == 1
    signals = store.wakes.pending()
    assert len(signals) == 1
    signal = signals[0]
    run_id = str(signal.metadata["scheduler_run_id"])
    assert signal.root_task_id == run_id
    assert run_id.startswith("srun_")


def test_two_due_jobs_in_one_thread_both_execute_and_close_history(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    backend = _Backend()
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create(
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
    assert store.wakes.pending() == []
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert len(history) == 2
    assert {row["status"] for row in history} == {"done"}
    assert len(channels.adapter("internal").sent_messages) == 2


def test_scheduled_run_waits_for_same_task_terminal_before_history_close(tmp_path) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": "thread-waiting",
            "channel_user_id": "user-1",
            "now": 900.0,
        }
    )
    _create_job(agent, thread.thread_id, "child-backed")
    run = agent.scheduler_repository.reserve_due_runs(now=1_000)[0]
    claimed = agent.scheduler_repository.claim_run(
        str(run["run_id"]),
        lease_seconds=300,
        now=1_001,
    )
    assert claimed is not None
    claim = SchedulerRunClaim(
        run_id=str(run["run_id"]),
        claim_id=str(claimed["claim_id"]),
        run=claimed,
    )
    agent.scheduler_repository.mark_run_running(
        claim.run_id,
        claim.claim_id,
        now=1_002,
    )
    store.tasks.bind(
        {
            "thread_id": thread.thread_id,
            "task_id": claim.run_id,
            "goal": "等待四个子代理并整合报告",
            "now": 1_002,
        }
    )

    report = BackgroundMainAgentReport(
        thread_id=thread.thread_id,
        task_id=claim.run_id,
        reason="scheduled_job_due",
        response="子代理仍在运行",
        route_channel="internal",
        route_target="",
        created_at=1_003,
        delivery_status="suppressed",
        delivery_reason="nonterminal_children",
        wake_handled=True,
        task_status="active",
    )
    returned = _finish_scheduler_wake_claim(
        SimpleNamespace(
            scheduler_service=agent.scheduler_service,
            _wake_retry_after={},
        ),
        SimpleNamespace(wake_signal_id="wake-waiting"),
        report,
        claim=claim,
        now=1_003,
    )

    assert returned is report
    waiting = agent.scheduler_repository.get_active_run(claim.run_id)
    assert waiting is not None
    assert waiting["status"] == "waiting"
    assert waiting["claim_id"] == ""
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert history == []
    active, errors = agent.scheduler_repository.active_runs_by_job()
    assert errors == []
    assert next(iter(active.values()))["status"] == "waiting"
    assert agent.scheduler_service.reconcile_waiting_run(claim.run_id, now=1_004) is None

    store.tasks.update_status(
        {"task_id": claim.run_id, "status": "completed", "now": 1_005}
    )
    # Gateway 启动/轮询时走批量对账；这条路径保证一次生命周期通知丢失后，
    # 重启仍会从同一个 task link 收敛，而不是让 waiting 永久悬挂。
    settled = agent.scheduler_service.reconcile_waiting_runs(now=1_006)

    assert settled == [claim.run_id]
    assert agent.scheduler_repository.get_active_run(claim.run_id) is None
    history, errors = agent.scheduler_repository.history(limit=10)
    assert errors == []
    assert [row["status"] for row in history] == ["done"]


def test_scheduled_message_tool_delivery_is_mirrored_once_without_fallback_send(
    tmp_path,
) -> None:
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl"),
        tmp_path,
    )
    store = agent.conversation_store
    thread = store.threads.get_or_create(
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
    messages = store.messages.recent(thread.thread_id)
    assert [row.content for row in messages] == ["提醒到啦：检查索引备份。"]
    assert messages[0].metadata["message_tool_delivery"]["receipt_id"] == "receipt-one"
