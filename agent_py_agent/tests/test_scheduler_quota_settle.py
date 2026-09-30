"""定时任务撞模型额度用完后的结算（2026-09-29，step16k 前置）。

复审 9a 的限额窗口改判时，用真实 scheduler.tick 加可控时钟探出：额度通知送达后，额度报告没有 task_status，
_finish_scheduler_wake_claim 按「释放 + 30 秒重试」处理，唤醒不确认，于是每 30 秒重跑一次模型、再追加一条通知。
修复后：每次到期最多一次模型调用、一条通知，run 记 failed 并带 PROVIDER_QUOTA_EXHAUSTED，唤醒在同一拍确认。
通知没送达时保持原语义：释放租约，30 秒后只重投通知、不再调用模型。
"""

from __future__ import annotations

import json
import time as _time_module
import urllib.error
from io import BytesIO
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.gateway_helpers import _runtime_http_error
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.channels import DeliveryReceipt
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.settings import AgentConfig

T0 = 1_800_000_000.0
PERIOD_SECONDS = 300
# 真实错误体的脱敏版（09-29 生产日志里的结构化事件）：只有每周额度窗口，不命中硬额度错误码表。
WEEKLY_LIMIT_BODY = {
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
    "metadata": {"workspace": "wrk_redacted", "limitName": "weekly"},
}
# 改判之前就走额度分路的硬额度错误码。
INSUFFICIENT_QUOTA_BODY = {"error": {"type": "insufficient_quota", "message": "quota"}}


# LLM: 只在 pytest 临时目录里用；每次调用都按真实 _runtime_http_error 把 429 错误体分类后抛出，不连网络。
# 类用途: 记录模型被调用的时刻（相对 T0 的秒数），证明额度用完后不会被反复调用。
class _QuotaBackend:
    name = "quota-settle-test"

    # LLM: clock 是测试共享的假时钟，调用时刻按它记录。
    # 函数用途: 保存要返回的 429 错误体和假时钟。
    def __init__(self, body: dict, clock: dict) -> None:
        self.body = body
        self.clock = clock
        self.calls: list[float] = []

    # LLM: 与生产后端同名同参；每次都抛出同一个 429 分类结果。
    # 函数用途: 记录一次调用后抛出额度错误。
    def generate(self, prompt, on_chunk=None, on_thinking_delta=None, **_kwargs):
        del prompt, on_chunk, on_thinking_delta
        self.calls.append(self.clock["now"] - T0)
        raise _runtime_http_error(urllib.error.HTTPError(
            "https://api.example.com", 429, "Too Many Requests", {"Content-Type": "application/json"},
            BytesIO(json.dumps(self.body).encode()),
        ))


# LLM: 只替换外发回执，调度、唤醒、额度通知和结算都走真实实现；statuses 依次作为每次投递的结果，用完后一律 sent。
# 类用途: 记录额度通知的每次投递尝试（幂等键和结果），可以模拟先失败后送达。
class _NoticeDelivery(FakeDeliveryService):
    # LLM: 每个测试独立的投递记录。
    # 函数用途: 创建可按顺序返回投递结果的飞书替身渠道。
    def __init__(self, statuses: tuple[str, ...] = ()) -> None:
        super().__init__()
        self.statuses = list(statuses)
        self.attempts: list[tuple[str, str]] = []

    # LLM: 声明飞书渠道，额度通知才会走主动投递。
    # 函数用途: 声明本替身支持的渠道。
    def declares_channel(self, channel: str) -> bool:
        return channel == "feishu"

    # LLM: 主动投递资格与渠道声明一致。
    # 函数用途: 允许向飞书主动投递。
    def declared_proactive(self, channel: str) -> bool:
        return self.declares_channel(channel)

    # LLM: 回执原样带回投递上下文，只在进程内记录，不外发。
    # 函数用途: 按预设顺序返回投递结果。
    def deliver(self, context, envelope):
        status = self.statuses.pop(0) if self.statuses else "sent"
        self.attempts.append((context.idempotency_key, status))
        return DeliveryReceipt(
            channel=context.channel, target=context.target, content=envelope.content,
            thread_id=context.thread_id, task_id=context.task_id, delivery_status=status,
            evidence_refs=envelope.evidence_refs, receipt_id=f"receipt-{len(self.attempts)}" if status == "sent" else "",
        )


# LLM: 全局替换 time.time，让调度账本、租约和结算时刻都跟着假时钟走；只在本测试进程内生效。
# 函数用途: 提供可手动推进的时钟，按秒驱动定时任务的到期和 30 秒重试边界。
@pytest.fixture
def clock(monkeypatch):
    state = {"now": T0}
    monkeypatch.setattr(_time_module, "time", lambda: state["now"])
    return state


# LLM: 只在 pytest 临时目录里经原 create_job 建立每 300 秒一次的定时任务，调度器、唤醒、额度分路和结算都用真实实现；
#   关掉孤儿巡检，避免与本测试无关的后台扫描。
# 函数用途: 搭好飞书会话、定时任务、额度错误后端和替身渠道，返回测试要读的各个对象。
def _case(tmp_path, clock, body: dict, delivery: _NoticeDelivery):
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", orphan_supervision_interval_seconds=0),
        tmp_path,
    )
    backend = _QuotaBackend(body, clock)
    agent.backend = backend
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "feishu",
        "channel_conversation_id": "chat-quota", "channel_user_id": "open-1", "now": T0 - 100,
    })
    agent.scheduler_repository.create_job(SchedulerJobCreateRequest(
        name="board", prompt="巡检看板", thread_id=thread.thread_id, source_task_id="",
        schedule={"kind": "every", "every_seconds": PERIOD_SECONDS, "anchor_at": T0, "timezone": "UTC"},
        misfire_grace_seconds=60, skill_refs=[], source_request_id="board", now=T0 - 100,
    ))
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=delivery)
    scheduler = BackgroundMainAgentScheduler({
        "runtime": runtime, "store": store, "scheduler_service": agent.scheduler_service,
    })
    return SimpleNamespace(agent=agent, backend=backend, store=store, thread=thread, delivery=delivery,
                           scheduler=scheduler)


def _tick(case, clock, offset: int) -> None:
    clock["now"] = T0 + offset
    case.scheduler.tick(now=clock["now"])


def _history(case) -> list[tuple[str, str]]:
    rows, errors = case.agent.scheduler_repository.history(limit=100)
    assert errors == []
    return [(str(row.get("status")), str(row.get("error_code") or "")) for row in rows]


def _quota_messages(case) -> int:
    return sum(
        1 for row in case.store.messages.recent(case.thread.thread_id, limit=500)
        if (getattr(row, "metadata", None) or {}).get("reason") == "provider_quota_exhausted_fallback"
    )


@pytest.mark.parametrize("body", [WEEKLY_LIMIT_BODY, INSUFFICIENT_QUOTA_BODY], ids=["weekly", "insufficient_quota"])
def test_scheduled_quota_settles_each_due_run_once(tmp_path, clock, body) -> None:
    case = _case(tmp_path, clock, body, _NoticeDelivery())

    for offset in range(0, 3 * PERIOD_SECONDS + 1, 10):
        _tick(case, clock, offset)
        if offset % PERIOD_SECONDS == 0:
            # 到期的那一拍里就结算完：run 记 failed、唤醒确认，不留给下一拍按 stale 收口。
            assert case.store.wakes.pending(limit=0) == [], offset

    due = [float(offset) for offset in range(0, 3 * PERIOD_SECONDS + 1, PERIOD_SECONDS)]
    assert case.backend.calls == due
    assert len(case.delivery.attempts) == len(due)
    assert len({key for key, _status in case.delivery.attempts}) == len(due)
    assert _quota_messages(case) == len(due)
    assert _history(case) == [("failed", "PROVIDER_QUOTA_EXHAUSTED")] * len(due)


def test_undelivered_quota_notice_is_redelivered_without_another_model_call(tmp_path, clock) -> None:
    case = _case(tmp_path, clock, WEEKLY_LIMIT_BODY, _NoticeDelivery(("failed",)))

    _tick(case, clock, 0)
    # 通知没送达：租约释放、唤醒保留、run 不结算。
    assert case.backend.calls == [0.0]
    assert [status for _key, status in case.delivery.attempts] == ["failed"]
    assert len(case.store.wakes.pending(limit=0)) == 1
    assert _history(case) == []

    for offset in range(10, PERIOD_SECONDS - 10 + 1, 10):
        _tick(case, clock, offset)

    # 30 秒后只重投通知（同一幂等键），不再调用模型；送达后 run 结算、唤醒确认。
    assert case.backend.calls == [0.0]
    assert [status for _key, status in case.delivery.attempts] == ["failed", "sent"]
    assert len({key for key, _status in case.delivery.attempts}) == 1
    assert _quota_messages(case) == 1
    assert _history(case) == [("failed", "PROVIDER_QUOTA_EXHAUSTED")]
    assert case.store.wakes.pending(limit=0) == []
