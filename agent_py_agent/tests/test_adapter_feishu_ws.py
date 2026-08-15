
from __future__ import annotations

"""飞书长连接(WebSocket)入站单测 —— 免公网/不绑端口。

核心:lark 事件对象 → 归一化成 webhook 同构 dict → 复用 feishu_to_incoming → 与 webhook 完全
同一条下游;以及 adapter 按 feishu_connection_mode 走长连/webhook 分支(默认长连接支持卡片)。
"""

from types import SimpleNamespace

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_ws import (
    FeishuWsClient,
    lark_event_to_webhook_payload,
)
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming


def _fake_lark_event(
    *,
    message_id: str = "om_x",
    text: str = "你好",
    open_id: str = "ou_abc",
    chat_type: str = "p2p",
    root_id: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(
            message_id=message_id,
            message_type="text",
            content=f'{{"text":"{text}"}}',
            chat_id="oc_1",
            chat_type=chat_type,
            root_id=root_id,
        ),
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id=open_id)),
    ))


def test_lark_event_normalizes_into_webhook_downstream() -> None:
    """长连的 lark 事件对象归一化后,必须能被现有 webhook 解析器 feishu_to_incoming 正确消费
    (长连/webhook 同下游,不为长连另造一条解析链)。"""
    payload = lark_event_to_webhook_payload(_fake_lark_event(text="hello", open_id="ou_1"))
    assert payload is not None
    msg = feishu_to_incoming(payload)
    assert msg is not None
    assert msg.content == "hello" and msg.user_id == "ou_1" and msg.message_id == "om_x"
    assert msg.conversation_id == "oc_1"
    assert msg.metadata["feishu_chat_type"] == "p2p"


def test_lark_thread_identity_survives_normalization() -> None:
    payload = lark_event_to_webhook_payload(_fake_lark_event(root_id="om_root"))
    msg = feishu_to_incoming(payload)
    assert msg is not None and msg.conversation_id == "oc_1:thread:om_root"


def test_lark_event_invalid_returns_none() -> None:
    """取不到 message_id / 事件残缺 → None,不会污染下游。"""
    assert lark_event_to_webhook_payload(_fake_lark_event(message_id="")) is None
    assert lark_event_to_webhook_payload(SimpleNamespace(event=None)) is None
    assert lark_event_to_webhook_payload(SimpleNamespace()) is None


def test_ws_client_routes_event_to_on_payload() -> None:
    """FeishuWsClient 收事件 → 归一化 → 交 on_payload(= adapter._handle_feishu_event 同下游);
    异常事件不掀翻长连(handle 内吞掉)。"""
    seen: list[dict] = []
    client = FeishuWsClient(app_id="a", app_secret="b", on_payload=seen.append)
    client._handle_event(_fake_lark_event(text="收到", open_id="ou_2"))
    assert len(seen) == 1 and feishu_to_incoming(seen[0]).content == "收到"
    client._handle_event(_fake_lark_event(message_id=""))  # 无效事件:不交下游、不抛
    assert len(seen) == 1


def test_ws_client_dedups_repeated_message_id() -> None:
    """同一 message_id 重复投递(飞书长连重连/重放会重发)只处理一次——防重复回复
    (回归:移植时漏了去重,真机出现"一条消息回好几条")。"""
    seen: list[dict] = []
    client = FeishuWsClient(app_id="a", app_secret="b", on_payload=seen.append)
    dup = _fake_lark_event(message_id="om_dup", text="hi", open_id="ou_3")
    client._handle_event(dup)
    client._handle_event(dup)  # 同一条再投
    client._handle_event(dup)  # 再投
    assert len(seen) == 1  # 只处理一次,不重复回复
    client._handle_event(_fake_lark_event(message_id="om_other", text="hi2"))  # 不同消息正常处理
    assert len(seen) == 2


def test_adapter_connection_mode_selects_long_or_webhook() -> None:
    """默认/未配走长连接；显式 webhook 仍保留。"""
    long_conn = FeishuAdapter(config={"feishu_app_id": "a", "feishu_app_secret": "b", "feishu_connection_mode": "long_connection"})
    assert long_conn._is_long_connection() is True
    default = FeishuAdapter(config={"feishu_app_id": "a", "feishu_app_secret": "b"})
    assert default._is_long_connection() is True
    assert FeishuAdapter(config={"feishu_connection_mode": "webhook"})._is_long_connection() is False
    assert FeishuAdapter(config={"feishu_connection_mode": "ws"})._is_long_connection() is True


def _fake_card_action(*, token: str = "tok1", choice: str = "confirm", open_id: str = "ou_c") -> SimpleNamespace:
    return SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(tag="button", value={"token": token, "choice": choice}),
        operator=SimpleNamespace(open_id=open_id),
    ))


def _card_client(sink) -> FeishuWsClient:
    client = FeishuWsClient(app_id="a", app_secret="b", on_payload=lambda _p: None)
    client.on_card_action = sink  # adapter 构造后按需设置(不进 __init__ 参数)
    return client


def test_ws_card_action_routes_to_on_card_action() -> None:
    """卡片按钮回调 → 归一化 {value, operator_open_id} 交 on_card_action(飞书人设确认落地入口)。"""
    seen: list[dict] = []
    client = _card_client(seen.append)
    client._handle_card_action(_fake_card_action(token="t9", choice="confirm", open_id="ou_9"))
    assert len(seen) == 1
    assert seen[0]["value"] == {"token": "t9", "choice": "confirm"} and seen[0]["operator_open_id"] == "ou_9"


def test_ws_card_action_dedups_repeated_click() -> None:
    """同一 (token, choice) 重复回调只处理一次(卡片可能重复回调;pop 原子领取兜底,这里再挡一层去重)。"""
    seen: list[dict] = []
    client = _card_client(seen.append)
    dup = _fake_card_action(token="tdup", choice="confirm")
    client._handle_card_action(dup)
    client._handle_card_action(dup)  # 重复回调
    assert len(seen) == 1
    client._handle_card_action(_fake_card_action(token="tdup", choice="decline"))  # 不同 choice 正常处理
    assert len(seen) == 2


def test_ws_card_action_invalid_payload_no_crash() -> None:
    """残缺回调对象不交下游、不抛(不掀翻长连)。"""
    seen: list[dict] = []
    client = _card_client(seen.append)
    client._handle_card_action(SimpleNamespace(event=None))
    client._handle_card_action(SimpleNamespace())
    assert seen == []
