"""压缩调用撞模型额度用完时，后台唤醒按额度用完处理（2026-09-29，step16k 前置）。

压缩摘要和业务调用走同一个后端、同一份额度。压缩撞到 429 额度用完时，异常被包成 ConversationCompactError
（错误码 COMPACT_PROVIDER_QUOTA_EXHAUSTED），原来的额度分路只认 ProviderQuotaExhaustedError，于是走非额度分路：
Goal 记 blocked、不发额度通知。修复后在额度分路入口按结构化事实补认，不看文案。

9a 复审补充：车道环境暂停、持久策略失败账和毒丸不计数三处也各自用 isinstance 判额度，同样认不出压缩包装。
现在四处都只读会话层的唯一判定 compact_guard.is_provider_quota_failure。
"""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

import pytest

from agent_py_agent.agent.backends.errors import (
    ProviderQuotaExhaustedError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.backends.gateway_helpers import _runtime_http_error
from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
    FakeDeliveryService,
)
from agent_py_agent.agent.conversation.compact_guard import (
    COMPACT_PROVIDER_QUOTA_EXHAUSTED,
    ConversationCompactError,
    compact_error_is_provider_quota,
    compact_exception_code,
    is_provider_quota_failure,
)
from agent_py_agent.agent.conversation.wake_poison import WAKE_VERDICT_NEUTRAL, verdict_for_error
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig

WEEKLY_LIMIT_BODY = {
    "type": "error",
    "error": {"type": "GoUsageLimitError", "message": "Go usage limit exceeded"},
    "metadata": {"workspace": "wrk_redacted", "limitName": "weekly"},
}


def _compact_error(code: str, cause: BaseException | None = None) -> ConversationCompactError:
    error = ConversationCompactError("上下文压缩未完成，原始会话记录保留不变", code=code)
    error.__cause__ = cause
    return error


def test_compact_quota_code_matches_the_code_compaction_produces() -> None:
    assert compact_exception_code(ProviderQuotaExhaustedError("HTTP 429")) == COMPACT_PROVIDER_QUOTA_EXHAUSTED


def test_compact_error_with_quota_facts_is_quota() -> None:
    by_code = _compact_error(COMPACT_PROVIDER_QUOTA_EXHAUSTED)
    direct = _compact_error("COMPACT_FAILED", ProviderQuotaExhaustedError("HTTP 429"))
    middle = RuntimeError("wrapped")
    middle.__cause__ = ProviderQuotaExhaustedError("HTTP 429")
    indirect = _compact_error("COMPACT_FAILED", middle)

    assert compact_error_is_provider_quota(by_code) is True
    assert compact_error_is_provider_quota(direct) is True
    assert compact_error_is_provider_quota(indirect) is True


def test_compact_error_without_quota_facts_is_not_quota() -> None:
    usage_limit = _compact_error("COMPACT_PROVIDERUSAGELIMITERROR", ProviderUsageLimitError("HTTP 429"))
    # 文案里写了额度码也不算：只看错误码和异常链。
    text_only = ConversationCompactError("PROVIDER_QUOTA_EXHAUSTED COMPACT_PROVIDER_QUOTA_EXHAUSTED", code="COMPACT_FAILED")
    looped = _compact_error("COMPACT_FAILED", RuntimeError("a"))
    looped.__cause__.__cause__ = looped
    wrapped_elsewhere = RuntimeError("not a compact failure")
    wrapped_elsewhere.__cause__ = ProviderQuotaExhaustedError("HTTP 429")

    assert compact_error_is_provider_quota(usage_limit) is False
    assert compact_error_is_provider_quota(text_only) is False
    assert compact_error_is_provider_quota(looped) is False
    # 只补认压缩失败；其它包装和裸额度错误各走原判定。
    assert compact_error_is_provider_quota(wrapped_elsewhere) is False
    assert compact_error_is_provider_quota(ProviderQuotaExhaustedError("HTTP 429")) is False


def test_shared_quota_failure_covers_direct_and_compact_wrapped_errors() -> None:
    assert is_provider_quota_failure(ProviderQuotaExhaustedError("HTTP 429")) is True
    assert is_provider_quota_failure(_compact_error(COMPACT_PROVIDER_QUOTA_EXHAUSTED)) is True
    assert is_provider_quota_failure(_compact_error("COMPACT_FAILED", ProviderQuotaExhaustedError("HTTP 429"))) is True
    assert is_provider_quota_failure(ProviderUsageLimitError("HTTP 429")) is False
    assert is_provider_quota_failure(_compact_error("COMPACT_FAILED", ProviderUsageLimitError("HTTP 429"))) is False


# LLM: 只在 pytest 临时目录里用；业务调用和压缩摘要都经它，每次都按真实 _runtime_http_error 抛每周额度 429，不连网络。
# 类用途: 记录后端被调用的次数，证明额度用完后这条唤醒不会再调用模型。
class _WeeklyQuotaBackend:
    name = "compact-quota-test"

    # LLM: 每个测试独立计数。
    # 函数用途: 创建调用计数为 0 的额度用完后端。
    def __init__(self) -> None:
        self.calls = 0

    # LLM: 与生产后端同名同参；业务调用和压缩摘要都会走到这里。
    # 函数用途: 记录一次调用后抛出每周额度用完。
    def generate(self, prompt, on_chunk=None, on_thinking_delta=None, **_kwargs):
        del prompt, on_chunk, on_thinking_delta
        self.calls += 1
        raise _runtime_http_error(urllib.error.HTTPError(
            "https://api.example.com", 429, "Too Many Requests", {"Content-Type": "application/json"},
            BytesIO(json.dumps(WEEKLY_LIMIT_BODY).encode()),
        ))


# LLM: 只在 pytest 临时目录里写会话消息；旧回合远超压缩触发线，下一次模型请求前必须先压缩。
# 函数用途: 给测试线程铺一段已结束的大历史，让请求前压缩成为第一次模型调用。
def _seed_large_history(store, thread_id: str, task_id: str) -> None:
    for position in range(8):
        store.messages.append({
            "thread_id": thread_id, "role": "user" if position % 2 == 0 else "assistant",
            "content": f"第{position}轮核对的原始材料：" + "资料内容与核对记录" * 4000,
            "metadata": {"conversation_request_id": f"prior-turn-{position}", "task_id": task_id},
        })


def test_goal_wake_whose_compaction_hits_quota_is_usage_limited_with_notice(tmp_path) -> None:
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    backend = _WeeklyQuotaBackend()
    agent.backend = backend
    store = agent.conversation_store
    channels = FakeDeliveryService()
    scheduler = BackgroundMainAgentScheduler({
        "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
        "store": store,
    })
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal",
        "channel_conversation_id": "thread-compact-quota", "channel_user_id": "user-1",
    })
    goal = store.goals.create({"thread_id": thread.thread_id, "objective": "持续核对资料"})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": goal.task_id, "goal": goal.objective, "status": "active"})
    _seed_large_history(store, thread.thread_id, goal.task_id)
    signal = store.wakes.raise_signal({
        "thread_id": thread.thread_id, "root_task_id": goal.task_id, "reason": "thread_goal_continue",
        "dedupe_key": f"thread-goal:{goal.goal_id}", "metadata": {"goal_id": goal.goal_id},
    })

    report = scheduler._run_wake_signal(signal, now=time.time())

    # 走的确实是压缩：线程记下了压缩撞额度的失败码。
    assert store.threads.require(thread.thread_id).compact_failure_code == COMPACT_PROVIDER_QUOTA_EXHAUSTED
    assert backend.calls == 1
    assert report is not None and report.reason == "provider_quota_exhausted"
    assert store.goals.load(thread.thread_id).status == "usage_limited"
    # internal 通道不主动外发，额度通知落在本地会话记录里（TUI 读这里）。
    notices = [
        row for row in store.messages.recent(thread.thread_id, limit=50)
        if (row.metadata or {}).get("reason") == "provider_quota_exhausted_fallback"
    ]
    assert len(notices) == 1
    assert store.wakes.pending() == []



# LLM: 只在 pytest 临时目录里建会话、任务目录、大历史和每 30 秒一次的持久进度策略；后端每次都抛每周额度 429。
# 函数用途: 搭好 9a 复审用的真实链路形态（大线程上的持久策略），返回 store、线程、策略和后台调度器。
def _large_thread_policy(tmp_path):
    agent = SimpleAgent(
        AgentConfig(enable_tools=False, memory_path="memory.jsonl", my_agent_home=str(tmp_path / "home")), tmp_path,
    )
    agent.backend = _WeeklyQuotaBackend()
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "user-1", "channel": "internal",
        "channel_conversation_id": "thread-policy-quota", "channel_user_id": "user-1", "now": 10.0,
    })
    task_root = tmp_path / "home" / "tasks" / "task-policy-quota"
    (task_root / "output").mkdir(parents=True)
    (task_root / "work").mkdir()
    store.tasks.bind({
        "thread_id": thread.thread_id, "task_id": "task-policy-quota", "goal": "持续核对资料",
        "status": "active", "task_path": str(task_root),
    })
    _seed_large_history(store, thread.thread_id, "task-policy-quota")
    policy = store.progress.create({
        "thread_id": thread.thread_id, "task_id": "task-policy-quota", "interval_seconds": 30,
        "route_channel": "internal", "route_target": thread.thread_id, "now": 20.0,
    })
    scheduler = BackgroundMainAgentScheduler({
        "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=FakeDeliveryService()),
        "store": store,
    })
    return store, thread, policy, scheduler


def test_large_thread_policy_quota_pauses_lane_without_retiring_or_poison_count(tmp_path, monkeypatch, capsys) -> None:
    """9a 复审的真实链路形态：大线程上的持久策略，请求前先压缩，压缩撞每周额度 429，连续 3 拍。
    策略仍 enabled、不记失败账；车道进入环境暂停（60 秒起翻倍），不是 30 秒普通冷却；毒丸不计数。"""
    from agent_py_agent.cli import gateway_lane_retry

    store, thread, policy, scheduler = _large_thread_policy(tmp_path)
    lanes = gateway_lane_retry.BackgroundLaneRetry()
    monkeypatch.setattr(gateway_lane_retry.time, "monotonic", lambda: 100.0)
    raised = []
    for tick in (30.0, 40.0, 50.0):
        with pytest.raises(ConversationCompactError) as caught:
            scheduler._run_due_policy(policy, now=tick)
        raised.append(caught.value)
        lanes.failed("owner", thread.thread_id, caught.value, delay=30)
        after = store.progress.load(policy.policy_id)
        assert after is not None and after.enabled is True
        assert "failure_count" not in after.metadata and "retired_at" not in after.metadata

    assert [error.code for error in raised] == [COMPACT_PROVIDER_QUOTA_EXHAUSTED] * 3
    events = [
        json.loads(line.split(" ", 1)[1]) for line in capsys.readouterr().out.splitlines()
        if line.startswith("[gateway-lane-retry] ")
    ]
    assert [(event["event"], event["probe_in_seconds"]) for event in events] == [
        ("lane_environment_paused", 60.0), ("lane_environment_paused", 120.0), ("lane_environment_paused", 240.0),
    ]
    assert [verdict_for_error(error).kind for error in raised] == [WAKE_VERDICT_NEUTRAL] * 3
