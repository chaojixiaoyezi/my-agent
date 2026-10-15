from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.adapter.feishu_card import (
    extract_card_action,
    extract_webhook_card_action,
)
from agent_py_agent.agent.delivery import (
    ChannelAdapterRegistry,
    ChannelCapabilities,
    DeliveryContext,
    DeliveryService,
)


def test_native_card_uses_original_transport_uuid_and_provider_identity(monkeypatch):
    from agent_py_agent.agent.adapter import feishu

    adapter = FeishuAdapter({"feishu_session_lock_enabled": False})
    adapter._get_tenant_access_token = lambda: "synthetic-token"
    sent = []

    def api(method, path, body, token):
        sent.append((method, path, body))
        return {"code": 0, "data": {"message_id": "om_card", "chat_id": "oc_private"}}

    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry", api)
    card = {"elements": [{"tag": "div", "text": {"tag": "plain_text", "content": "合成审批"}}]}
    assert adapter.send_control_card("ou_actor", card, idempotency_key="exact-call") == {"message_id": "om_card", "chat_id": "oc_private"}
    adapter.send_control_card("ou_actor", card, idempotency_key="exact-call")
    assert sent[0][2]["uuid"] == sent[1][2]["uuid"]
    assert sent[0][2]["receive_id"] == "ou_actor" and sent[0][2]["msg_type"] == "interactive"


def test_service_requires_declared_capability_and_complete_card_receipt():
    registry = ChannelAdapterRegistry()
    sent = []
    adapter = SimpleNamespace(send_control_card=lambda target, card, **_: sent.append(target) or {"message_id": "om_card"})
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(control_cards=True))
    service = DeliveryService(registry)
    context = DeliveryContext(channel="feishu", target="ou_actor", mode="reply", idempotency_key="exact-call")
    result = service.deliver_control_card(context, {"elements": []}, summary="合成审批")
    assert not result.ok and result.error_code == "CHANNEL_CONTROL_RECEIPT_MISSING"
    registry.register_adapter("feishu", adapter, capabilities=ChannelCapabilities(control_cards=False))
    result = service.deliver_control_card(context, {"elements": []}, summary="合成审批")
    assert not result.ok and result.error_code == "CHANNEL_CONTROL_UNSUPPORTED" and sent == ["ou_actor"]


def test_callback_preserves_real_message_chat_and_verified_operator():
    sdk = SimpleNamespace(event=SimpleNamespace(action=SimpleNamespace(value={"kind": "approval"}, form_value={}),
        operator=SimpleNamespace(open_id="ou_actor"), context=SimpleNamespace(open_message_id="om_card", open_chat_id="oc_private")))
    expected = {"value": {"kind": "approval"}, "form_value": {}, "operator_open_id": "ou_actor",
                "message_id": "om_card", "chat_id": "oc_private"}
    assert extract_card_action(sdk) == expected
    assert extract_webhook_card_action({"event": {"action": {"value": {"kind": "approval"}},
        "operator": {"open_id": "ou_actor"}, "context": {"open_message_id": "om_card", "open_chat_id": "oc_private"}}}) == expected


def test_provider_card_rejection_preserves_code_without_provider_message(monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', lambda *a: {
        'code': 230099, 'msg': 'secret-provider-body', 'data': {'secret': 'never'}})
    receipt = adapter.send_control_card('oc_group', {'elements': []}, idempotency_key='group-card')
    assert receipt == {'error_code': 'CHANNEL_CONTROL_PROVIDER_REJECTED', 'provider_code': 230099,
                       'effect_outcome': 'not_started'}
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(control_cards=True))
    result = DeliveryService(registry).deliver_control_card(
        DeliveryContext(channel='feishu', target='oc_group', mode='reply'), {}, summary='输入')
    assert not result.ok and result.provider_code == 230099 and result.effect_outcome == 'not_started'
    assert 'secret-provider-body' not in str(result)


def test_control_no_token_and_network_failure_are_distinct(monkeypatch):
    from agent_py_agent.agent.adapter import feishu
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._get_tenant_access_token = lambda: None
    registry = ChannelAdapterRegistry()
    registry.register_adapter('feishu', adapter, capabilities=ChannelCapabilities(control_cards=True))
    service = DeliveryService(registry)
    context = DeliveryContext(channel='feishu', target='ou_actor', mode='reply')
    result = service.deliver_control_card(context, {}, summary='输入')
    assert result.error_code == 'CHANNEL_CONTROL_AUTH_UNAVAILABLE' and result.effect_outcome == 'not_started'
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    def fail(*args):
        raise OSError('secret-transport-body')
    monkeypatch.setattr(feishu, '_feishu_msg_api_with_retry', fail)
    result = service.deliver_control_card(context, {}, summary='输入')
    assert result.error_code == 'CHANNEL_CONTROL_IO_FAILED' and result.effect_outcome == 'unknown'
    assert 'secret-transport-body' not in str(result)


@pytest.mark.parametrize('result,expected', [
    ({'ok': True, 'message': 'canary-control-private-reply'}, 'IM_CONTROL_COMPLETED'),
    ({'ok': False, 'error_code': 'IM_CONTROL_HTTP_AUTH_REJECTED', 'error_stage': 'http'},
     'IM_CONTROL_HTTP_AUTH_REJECTED'),
    ({'ok': False, 'error_code': 'canary-control-private-code', 'error_stage': 'canary-control-private-stage'},
     'IM_CONTROL_PREPARATION_FAILED'),
    ({'ok': False, 'error_code': 'IM_UNLOCK_REQUIRED', 'message': 'canary-control-private-reply'},
     'IM_UNLOCK_REQUIRED'),
])
def test_control_diagnostics_never_log_fields_refs_or_provider_text(monkeypatch, caplog, result, expected):
    from agent_py_agent.agent.adapter import private_controls

    monkeypatch.setattr(private_controls, '_submit_control_request', lambda *_: result)
    adapter = SimpleNamespace(_send_password_card=lambda *_: None)
    norm = {'value': {'kind': 'my_agent_model_configuration', 'decision': 'save',
                      'reference': 'canary-control-private-reference'},
            'operator_open_id': 'ou_fixture', 'message_id': 'om_fixture', 'chat_id': 'oc_fixture',
            'form_value': {'api_key': 'canary-control-private-key',
                           'arbitrary_field': 'canary-control-private-arbitrary'}}
    with caplog.at_level('INFO', logger=private_controls.__name__):
        reply = private_controls.tool_approval_card_action(adapter, norm)
    assert isinstance(reply, dict)
    assert 'stage=entered' in caplog.text and expected in caplog.text
    assert 'canary-control-private' not in caplog.text


def test_control_logging_sink_failure_does_not_prevent_or_repeat_submission(monkeypatch):
    from agent_py_agent.agent.adapter import private_controls

    calls = []
    monkeypatch.setattr(private_controls.logger, 'info', lambda *_: (_ for _ in ()).throw(OSError('synthetic-log-sink')))
    monkeypatch.setattr(private_controls, '_submit_control_request',
                        lambda *_: calls.append(True) or {'ok': True, 'message': '合成完成'})
    norm = {'value': {'kind': 'my_agent_model_configuration', 'decision': 'add'},
            'operator_open_id': 'ou_fixture', 'message_id': 'om_fixture', 'chat_id': 'oc_fixture'}
    reply = private_controls.tool_approval_card_action(SimpleNamespace(), norm)
    assert calls == [True]
    assert reply['toast'] == {'type': 'success', 'content': '合成完成'}
