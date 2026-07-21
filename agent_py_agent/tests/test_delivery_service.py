from __future__ import annotations

from dataclasses import dataclass, field

from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
    ReplyEnvelope,
)


@dataclass
class _SecondImAdapter:
    sent: list[tuple[str, str]] = field(default_factory=list)
    finalized: list[tuple[str, str, str, dict[str, object]]] = field(default_factory=list)

    def send_message(self, target: str, message: object) -> bool:
        self.sent.append((target, str(getattr(message, "content", "") or "")))
        return True

    def finalize_response(self, target: str, handle: str, message: object) -> bool:
        self.finalized.append(
            (
                target,
                handle,
                str(getattr(message, "content", "") or ""),
                dict(getattr(message, "metadata", {}) or {}),
            )
        )
        return True


def test_second_im_adapter_joins_without_delivery_service_branch() -> None:
    registry = ChannelAdapterRegistry()
    adapter = _SecondImAdapter()
    registry.register_adapter(
        "second-im",
        adapter,
        capabilities=ChannelCapabilities(text=True, reply=True, proactive=True),
    )
    service = DeliveryService(registry)

    assert service.supports_proactive("second-im") is True

    receipt = service.deliver(
        DeliveryContext(channel="second-im", target="user-42", mode="proactive"),
        ReplyEnvelope(content="跨平台正文"),
    )

    assert receipt.delivery_status == "sent"
    assert adapter.sent == [("user-42", "跨平台正文")]


def test_reply_context_keeps_target_and_provider_metadata_out_of_envelope() -> None:
    registry = ChannelAdapterRegistry()
    adapter = _SecondImAdapter()
    registry.register_adapter("second-im", adapter)
    service = DeliveryService(registry)
    envelope = ReplyEnvelope(content="普通最终回复", format="text")

    receipt = service.deliver(
        DeliveryContext(
            channel="second-im",
            target="trusted-user",
            mode="reply",
            conversation_id="conversation-1",
            reply_to="message-1",
            progress_handle="typing-1",
            request_id="gateway-1",
        ),
        envelope,
    )

    assert receipt.delivery_status == "sent"
    assert not hasattr(envelope, "target")
    assert adapter.finalized == [
        (
            "trusted-user",
            "typing-1",
            "普通最终回复",
            {
                "gateway_request_id": "gateway-1",
                "reply_to": "message-1",
                "conversation_id": "conversation-1",
                "projection_status": "plain_text",
            },
        )
    ]


def test_reply_projection_blocks_internal_protocol_for_every_adapter() -> None:
    registry = ChannelAdapterRegistry()
    adapter = _SecondImAdapter()
    registry.register_adapter("second-im", adapter)
    service = DeliveryService(registry)
    raw = (
        "报告已经整理好，文件位于 /private/report.pdf。\n"
        '[TOOL_CALL]\n{"tool":"send_message","target":"user-1"}\n[/TOOL_CALL]'
    )

    receipt = service.deliver(
        DeliveryContext(channel="second-im", target="user-1", mode="reply"),
        ReplyEnvelope(content=raw),
    )

    assert receipt.delivery_status == "sent"
    assert adapter.finalized[0][2] == "报告已经整理好，文件位于 report.pdf。"
    assert "TOOL_CALL" not in adapter.finalized[0][2]
    assert "/private" not in adapter.finalized[0][2]


def test_delivery_redacts_only_exact_identifiers_from_trusted_context() -> None:
    registry = ChannelAdapterRegistry()
    adapter = _SecondImAdapter()
    registry.register_adapter("second-im", adapter)
    service = DeliveryService(registry)
    context = DeliveryContext(
        channel="second-im",
        target="ou_private_user_123",
        mode="reply",
        conversation_id="oc_private_chat_456",
        reply_to="om_private_message_789",
        request_id="req_private_request_321",
        thread_id="thread_private_654",
        task_id="task_private_987",
    )
    raw = (
        "用户 ou_private_user_123，会话 oc_private_chat_456，消息 om_private_message_789，"
        "请求 req_private_request_321，线程 thread_private_654，任务 task_private_987。"
        "普通项目名 alpha 保持不变。"
    )

    receipt = service.deliver(context, ReplyEnvelope(content=raw))

    assert receipt.delivery_status == "sent"
    delivered = adapter.finalized[0][2]
    for private in (
        "ou_private_user_123",
        "oc_private_chat_456",
        "om_private_message_789",
        "req_private_request_321",
        "thread_private_654",
        "task_private_987",
    ):
        assert private not in delivered
    assert "普通项目名 alpha 保持不变" in delivered
