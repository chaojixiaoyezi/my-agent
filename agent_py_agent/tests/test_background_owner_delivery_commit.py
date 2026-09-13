"""后台答复落账与外部投递解耦的回归守卫。

真实故障背景: 一次测试运行把 owner 的 provider 名(``release-validation``)当成了投递渠道，
该渠道既没有 proactive 能力也不是本地 transcript 路线，于是模型已经写出的最终答复
既没有外发、也没有写进 canonical thread，唤醒却被确认成已处理——答复永久丢失，且日志
只留下 reason/task/thread，无法事后复原。

这里的守卫只依赖结构化事实:
1. canonical 记录由“这条路线是否以本地会话为交付面”决定，不由渠道名或投递状态决定；
2. 外发能力只认投递服务声明，未注册渠道绝不被打开；
3. 唤醒只有在“外发成功”或“已经落到自己的权威记录且本就不欠外发”时才确认；
4. 欠外发却没送达的正文冻结在唤醒上，下次只重投正文，绝不重跑业务。
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

from agent_py_agent.agent.conversation import (
    BackgroundMainAgentRuntime,
    BackgroundMainAgentScheduler,
)
from agent_py_agent.agent.conversation.channels import DeliveryContext, ReplyEnvelope
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.delivery import DeliveryService, build_default_channel_registry
from agent_py_agent.agent.settings import AgentConfig

# 复现故障时用户侧真实使用过的身份/渠道字符串：它不是 IM 渠道，而是 owner provider 名。
UNREGISTERED_IDENTITY = "release-validation"


def _native_probe(self):
    from agent_py_agent.agent.backends.base import ProviderToolCapability, _utc_now_iso

    return ProviderToolCapability(
        provider=str(self.name or "bg-test"),
        endpoint="local://bg-test",
        model="",
        stream=False,
        native_supported=True,
        evidence="test_backend_declares_native_tools",
        observed_at=_utc_now_iso(),
    )


class _ScriptedBackend:
    """按脚本返回一条模型正文，并记录真实模型轮数。"""

    name = "scripted"

    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    def probe_tool_capability(self):
        return _native_probe(self)

    def generate(self, prompt: str, on_chunk=None, **kwargs) -> object:
        from agent_py_agent.agent.backends import ModelResponse

        del on_chunk, kwargs
        self.prompts.append(prompt)
        return ModelResponse(text=self.text, backend=self.name)


class _ScriptedDelivery:
    """按脚本给出投递结果的可控投递服务；只声明被显式打开的能力。"""

    def __init__(self, *, proactive: bool, status: str) -> None:
        self.proactive = proactive
        self.status = status
        self.sent: list[str] = []

    def supports_proactive(self, _channel: str) -> bool:
        return self.proactive

    def supports_transcript(self, channel: str) -> bool:
        from agent_py_agent.agent.conversation.channels import supports_transcript_delivery

        return supports_transcript_delivery(channel)

    def deliver(self, context: DeliveryContext, envelope: ReplyEnvelope) -> object:
        self.sent.append(envelope.content)
        return SimpleNamespace(
            channel=context.channel,
            target=context.target,
            content=envelope.content,
            evidence_refs=envelope.evidence_refs,
            delivery_status=self.status,
            receipt_id="receipt-1" if self.status == "sent" else "",
        )


def _agent(tmp_path, backend_text: str = "最终报告已生成：architecture_comparison_report.md") -> tuple:
    agent = SimpleAgent(
        AgentConfig(
            enable_tools=False,
            memory_path="memory.jsonl",
            orphan_supervision_interval_seconds=0,
        ),
        tmp_path,
    )
    backend = _ScriptedBackend(backend_text)
    agent.backend = backend
    return agent, backend


def test_unregistered_identity_route_still_records_reply_in_canonical_thread(tmp_path) -> None:
    """未注册渠道必须仍然留住模型答复：canonical 记录不依赖 transport 能力。"""
    agent, _backend = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-rv",
            "channel": UNREGISTERED_IDENTITY,
            "channel_conversation_id": "r277-multi",
            "channel_user_id": "owner-rv",
        }
    )
    channels = DeliveryService(build_default_channel_registry(agent.config))
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)

    report = runtime.run_once(
        {
            "thread_id": thread.thread_id,
            "task_id": "task-rv",
            "reason": "scheduled_progress_report",
            "route_channel": UNREGISTERED_IDENTITY,
            "route_target": "r277-multi",
        }
    )

    assert report.wake_handled is True
    assert report.delivery_status == "not_applicable"
    assert report.commit_kind == "canonical_record"
    rows = store.recent_messages(thread.thread_id, limit=0)
    assert [row.content for row in rows] == [report.response]
    assert report.message_id == rows[0].message_id
    assert rows[0].channel == UNREGISTERED_IDENTITY
    assert rows[0].metadata["assistant_part_id"] == "final"
    # 外发没有发生，也不能被当成发生过。
    assert rows[0].metadata["background_delivery_reason"] == "non_subagent_completion"


def test_unregistered_identity_route_is_not_opened_by_delivery_service(tmp_path) -> None:
    """未注册渠道必须 fail-closed：既不声明 proactive，也不回落成本地 transcript。"""
    agent, _backend = _agent(tmp_path)
    channels = DeliveryService(build_default_channel_registry(agent.config))

    assert channels.supports_proactive(UNREGISTERED_IDENTITY) is False
    assert channels.supports_transcript(UNREGISTERED_IDENTITY) is False
    receipt = channels.deliver(
        DeliveryContext(
            channel=UNREGISTERED_IDENTITY,
            target="r277-multi",
            mode="proactive",
            thread_id="thread-x",
        ),
        ReplyEnvelope(content="草稿正文"),
    )
    assert receipt.delivery_status == "not_applicable"
    assert receipt.error_code == "CHANNEL_PROACTIVE_UNSUPPORTED"


def test_undelivered_reply_stays_pending_and_redelivers_without_model_turn(tmp_path) -> None:
    """欠外发却没送达的答复：唤醒不确认、正文被冻结，重投只发正文不重跑业务。"""
    agent, backend = _agent(tmp_path, "阶段汇报：已核对任务树，等待最后一个子代理。")
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-im",
            "channel": "feishu",
            "channel_conversation_id": "chat-im",
            "channel_user_id": "open-id-im",
            "now": 1.0,
        }
    )
    channels = _ScriptedDelivery(proactive=True, status="rejected")
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
            "store": store,
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "scheduled_progress_report",
            "root_task_id": "task-im",
            "now": 2.0,
        }
    )

    assert scheduler.tick(now=20.0) == []
    assert len(backend.prompts) == 1
    pending = store.pending_wake_signal(signal.wake_signal_id)
    assert pending is not None
    frozen = pending.metadata["owner_delivery"]
    assert frozen["schema_version"] == "wake-owner-delivery.v1"
    assert frozen["content"] == "阶段汇报：已核对任务树，等待最后一个子代理。"
    # 欠外发的路线不把失败草稿写进本地 transcript（原有边界保持）。
    assert store.recent_messages(thread.thread_id, limit=0) == []

    channels.status = "sent"
    report = scheduler.tick(now=51.0)
    assert len(backend.prompts) == 1, "重投不得再花一次模型轮"
    # 两次投递携带的是同一份冻结正文，证明重投没有重新生成内容。
    assert channels.sent == [frozen["content"], frozen["content"]]
    assert [item.delivery_status for item in report] == ["sent"]
    assert report[0].wake_handled is True
    assert report[0].delivery_reason == "cached_owner_delivery_retry"
    assert store.pending_wake_signal(signal.wake_signal_id) is None


def test_audit_finding_failed_external_send_never_claims_transcript_receipt(
    tmp_path,
    monkeypatch,
) -> None:
    """外发失败的审计报告不得冒领 transcript 回执，也不得写进本地会话。"""
    from agent_py_agent.agent.conversation.runtime import BackgroundRunRequest
    from agent_py_agent.agent.ingestion import harvester

    agent, _backend = _agent(tmp_path, "发现一项高风险事件。")
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-audit",
            "channel": "feishu",
            "channel_conversation_id": "chat-audit",
            "channel_user_id": "open-id-audit",
        }
    )
    runtime = BackgroundMainAgentRuntime(
        agent=agent,
        store=store,
        channels=_ScriptedDelivery(proactive=True, status="failed"),
    )
    source_ref = "audit://watch-a/candidate/7:0"
    recorded: list[tuple] = []
    monkeypatch.setattr(
        harvester,
        "record_audit_delivery_refs",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )
    request = BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="audit-a",
        reason="audit_finding",
        wake_signal={
            "wake_signal_id": "wake-audit-a",
            "root_task_id": "audit-a",
            "evidence_refs": [source_ref],
            "metadata": {
                "schema_version": "audit-finding-event.v1",
                "audit_id": "audit-a",
                "watch_id": "watch-a",
                "finding_id": "finding-a",
                "requires_llm_report": True,
                "delivery_evidence_refs": [source_ref],
            },
        },
    )
    commit = runtime._record_response(
        request,
        DeliveryContext(
            channel="feishu",
            target="open-id-audit",
            thread_id=thread.thread_id,
            task_id="audit-a",
        ),
        "发现一项高风险事件。",
        deliver=True,
        delivery_reason="audit_finding_report",
    )

    assert commit.delivery_status == "failed"
    assert commit.persisted is False
    assert recorded == []
    assert store.recent_messages(thread.thread_id, limit=0) == []


def test_background_report_log_carries_real_delivery_facts(tmp_path, monkeypatch) -> None:
    """后台报告日志必须能回答“答复到底出没出去、落在哪里”。"""
    from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport
    from agent_py_agent.cli import gateway_loops

    agent, _backend = _agent(tmp_path)
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        gateway_loops,
        "log_gateway_event",
        lambda _agent, name, payload: captured.append((name, payload)),
    )
    report = BackgroundMainAgentReport(
        thread_id="thread-a",
        task_id="task-a",
        reason="scheduled_progress_report",
        response="阶段汇报正文",
        route_channel=UNREGISTERED_IDENTITY,
        route_target="r277-multi",
        created_at=1.0,
        delivery_status="not_applicable",
        delivery_reason="non_subagent_completion",
        wake_handled=True,
        task_status="active",
        commit_kind="canonical_record",
        message_id="msg-1",
    )

    gateway_loops._record_background_main_reports(agent, [report])

    assert [name for name, _payload in captured] == ["gateway_background_main_reported"]
    payload = captured[0][1]
    assert payload["delivery_status"] == "not_applicable"
    assert payload["delivery_reason"] == "non_subagent_completion"
    assert payload["wake_handled"] is True
    assert payload["commit_kind"] == "canonical_record"
    assert payload["message_id"] == "msg-1"
    assert payload["response_chars"] == len("阶段汇报正文")
    assert json.loads(json.dumps(payload, ensure_ascii=False))["task_status"] == "active"


def test_old_implementation_would_have_lost_the_reply(tmp_path) -> None:
    """旧实现必红守卫：同一路径在旧语义下既无 canonical 记录也不进重投兜底。"""
    from agent_py_agent.agent.conversation import runtime as runtime_module

    agent, _backend = _agent(tmp_path)
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-rv",
            "channel": UNREGISTERED_IDENTITY,
            "channel_conversation_id": "r277-multi",
            "channel_user_id": "owner-rv",
        }
    )
    channels = DeliveryService(build_default_channel_registry(agent.config))
    runtime = BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels)
    request = runtime_module.BackgroundRunRequest(
        thread_id=thread.thread_id,
        task_id="task-rv",
        reason="subagent_runner_finished",
        wake_signal={"wake_signal_id": "wake-rv"},
    )
    commit = runtime._record_response(
        request,
        DeliveryContext(
            channel=UNREGISTERED_IDENTITY,
            target="r277-multi",
            mode="proactive",
            thread_id=thread.thread_id,
            task_id="task-rv",
        ),
        "后台最终答复正文",
        deliver=True,
        delivery_reason="root_subagents_terminal",
    )

    # 旧实现的判定式：transcript 能力 ∧ not_applicable ⇒ 不落账；非审计事件唤醒仍然确认。
    assert commit.persisted is True
    assert commit.commit_kind == "canonical_record"
    assert (
        runtime_module._background_owner_delivery_committed(
            request,
            target="r277-multi",
            route_supports_proactive=False,
            commit=runtime_module.BackgroundDeliveryCommit(
                content=commit.content,
                delivery_status="not_applicable",
                persisted=False,
            ),
        )
        is False
    ), "既没送达也没记账的唤醒绝不能确认"


def test_frozen_payload_survives_a_repeated_failure(tmp_path) -> None:
    """重投再次失败时正文必须仍然留在唤醒上，不允许退化成重新跑模型。"""
    agent, backend = _agent(tmp_path, "阶段汇报：还需要一个子代理收口。")
    store = agent.conversation_store
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "owner-im",
            "channel": "feishu",
            "channel_conversation_id": "chat-im",
            "channel_user_id": "open-id-im",
            "now": 1.0,
        }
    )
    channels = _ScriptedDelivery(proactive=True, status="rejected")
    scheduler = BackgroundMainAgentScheduler(
        {
            "runtime": BackgroundMainAgentRuntime(agent=agent, store=store, channels=channels),
            "store": store,
        }
    )
    signal = store.raise_wake_signal(
        {
            "thread_id": thread.thread_id,
            "reason": "scheduled_progress_report",
            "root_task_id": "task-im",
            "now": 2.0,
        }
    )

    assert scheduler.tick(now=20.0) == []
    assert scheduler.tick(now=51.0) == []
    pending = store.pending_wake_signal(signal.wake_signal_id)
    assert pending is not None
    assert pending.metadata["owner_delivery"]["content"] == "阶段汇报：还需要一个子代理收口。"
    assert len(backend.prompts) == 1, "反复失败的唤醒只能重投，不能再次调用模型"
    assert len(channels.sent) == 2
    assert set(channels.sent) == {"阶段汇报：还需要一个子代理收口。"}
    assert time.time() > 0
