"""压缩调用撞模型额度用完时，后台唤醒按额度用完处理（2026-09-29，step16k 前置）。

压缩摘要和业务调用走同一个后端、同一份额度。压缩撞到 429 额度用完时，异常被包成 ConversationCompactError
（错误码 COMPACT_PROVIDER_QUOTA_EXHAUSTED），原来的额度分路只认 ProviderQuotaExhaustedError，于是走非额度分路：
Goal 记 blocked、不发额度通知。修复后在额度分路入口按结构化事实补认，不看文案。
"""

from __future__ import annotations

import json
import time
import urllib.error
from io import BytesIO

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
)
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
    # 大线程：已结束的旧回合远超触发线，下一次模型请求前必须先压缩。
    for position in range(8):
        store.messages.append({
            "thread_id": thread.thread_id, "role": "user" if position % 2 == 0 else "assistant",
            "content": f"第{position}轮核对的原始材料：" + "资料内容与核对记录" * 4000,
            "metadata": {"conversation_request_id": f"prior-turn-{position}", "task_id": goal.task_id},
        })
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
