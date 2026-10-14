from __future__ import annotations

import json
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter, _FeishuCallbackHandler
from agent_py_agent.agent.adapter.feishu_mentions import bot_mention_decision
from agent_py_agent.agent.adapter.feishu_ws import FeishuWsClient
from agent_py_agent.agent.adapter.manager import ChannelManager, GatewayAskSubmission
from agent_py_agent.agent.adapter.protocol import FeishuEventDisposition, FeishuRetryableEventError


# LLM: 仅构造供应商结构和合成正文，不发送网络、读取生产配置或替模型执行任务。
# 函数用途: 生成群聊富文本事件，让换行与补文字的对照只改变一个节点。
def _payload(*, own_line=False, tail=False, message_id='om_synthetic', mentions=True):
    bot = {'tag': 'at', 'user_id': '@_user_1'}
    body = {'tag': 'text', 'text': 'job01：合成清单', 'style': ['bold', 'inline_code']}
    rows = [[bot], [body]] if own_line else [[bot, body]]
    if tail:
        rows.append([{'tag': 'text', 'text': '你看看'}])
    message = {'message_id': message_id, 'message_type': 'post', 'chat_type': 'group',
               'chat_id': 'oc_synthetic', 'content': json.dumps({'zh_cn': {'title': '', 'content': rows}})}
    if mentions:
        message['mentions'] = [{'key': '@_user_1', 'id': {'open_id': 'ou_bot'}}]
    return {'event': {'message': message, 'sender': {'sender_id': {'open_id': 'ou_user'}}}}


# LLM: SDK 只归一同一原事件，测试不调用 WS 握手或供应商接口。
# 函数用途: 将合成 webhook 形状投影成 SDK 属性对象。
def _sdk(payload):
    message = payload['event']['message']
    values = {key: value for key, value in message.items() if key != 'mentions'}
    values['mentions'] = [SimpleNamespace(key=row['key'], id=SimpleNamespace(**row.get('id', {})))
                          for row in message.get('mentions', [])]
    return SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(**values),
        sender=SimpleNamespace(sender_id=SimpleNamespace(open_id='ou_user'))))


def _adapter(callback):
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    adapter.on_message(callback)
    return adapter


@pytest.mark.parametrize('own_line', [False, True])
@pytest.mark.parametrize('tail', [False, True])
def test_rich_post_matrix_keeps_content_and_reports_actual_acceptance(own_line, tail):
    seen = []
    adapter = _adapter(lambda message: seen.append(message) or True)
    result = adapter._handle_feishu_event(_payload(own_line=own_line, tail=tail))
    assert result.status == 'accepted'
    assert len(seen) == 1 and 'job01' in seen[0].content
    assert ('你看看' in seen[0].content) is tail
    assert '@_user_1' not in seen[0].content


def test_temporary_bot_identity_failure_releases_same_ws_message_for_redelivery():
    seen, bot = [], ['']
    adapter = _adapter(lambda message: seen.append(message) or True)
    adapter._resolve_bot_open_id = lambda: bot[0]
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    event = _sdk(_payload())
    with pytest.raises(FeishuRetryableEventError, match='FEISHU_BOT_IDENTITY_UNAVAILABLE'):
        client._handle_event(event)
    bot[0] = 'ou_bot'
    client._handle_event(event)
    client._handle_event(event)
    assert len(seen) == 1


def test_ws_inflight_claim_is_exclusive_and_uncommitted():
    entered, release = threading.Event(), threading.Event()
    seen = []

    def consume(message):
        seen.append(message)
        entered.set()
        assert release.wait(2)
        return True

    adapter = _adapter(consume)
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    event = _sdk(_payload())
    first = threading.Thread(target=lambda: client._handle_event(event))
    first.start()
    try:
        assert entered.wait(1)
        with pytest.raises(FeishuRetryableEventError, match='FEISHU_EVENT_INFLIGHT'):
            client._handle_event(event)
    finally:
        release.set()
        first.join(2)
    client._handle_event(event)
    assert len(seen) == 1


def test_rejected_ingress_callback_is_retryable_not_seen_done():
    accepted, seen = [False], []
    adapter = _adapter(lambda message: seen.append(message) or accepted[0])
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    event = _sdk(_payload())
    with pytest.raises(FeishuRetryableEventError, match='FEISHU_INGRESS_UNAVAILABLE'):
        client._handle_event(event)
    accepted[0] = True
    client._handle_event(event)
    assert len(seen) == 2


def test_callback_exception_after_original_ingress_accept_reuses_same_row_once():
    manager = ChannelManager(delivery_state_dir=None)
    failure = [True]

    def consume(message):
        assert manager.route_message(message)
        if failure[0]:
            failure[0] = False
            raise RuntimeError('synthetic-secret-must-not-log')
        return True

    adapter = _adapter(consume)
    manager.register_adapter(adapter)
    adapter._get_tenant_access_token = lambda: None
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    event = _sdk(_payload())
    with pytest.raises(FeishuRetryableEventError):
        client._handle_event(event)
    client._handle_event(event)
    rows = manager._delivery_worker.store.records()
    assert len(rows) == 1
    submitted = []
    manager._submit_gateway_payload = lambda _body, message: submitted.append(message.message_id) or GatewayAskSubmission('gw_test')
    manager._delivery_worker._process_record(rows[0], time.time())
    assert submitted == ['om_synthetic']
    assert len(manager._reply_delivery.store.pending()) == 1


def test_webhook_failure_uses_retry_status_and_replay_can_accept(monkeypatch):
    result = [False]
    adapter = _adapter(lambda _: result[0])
    payload = _payload()
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu_crypto.verify_and_decode_webhook', lambda _: payload)
    responses = []
    handler = object.__new__(_FeishuCallbackHandler)
    handler.path = '/feishu/callback'
    handler.server = SimpleNamespace(adapter=adapter)
    handler.headers = {'Content-Length': '2'}
    handler.rfile = BytesIO(b'{}')
    handler._write_json = lambda code, body: responses.append((code, body))
    handler.do_POST()
    result[0] = True
    handler.rfile = BytesIO(b'{}')
    handler.do_POST()
    assert [code for code, _ in responses] == [503, 200]


def test_stable_unaddressed_and_at_all_do_not_dispatch_or_retry():
    seen = []
    adapter = _adapter(lambda message: seen.append(message) or True)
    value = _payload()
    value['event']['message']['mentions'] = [{'key': '@_all', 'id': {'open_id': 'all'}}]
    value['event']['message']['content'] = json.dumps({'text': '@_all 合成清单'})
    value['event']['message']['message_type'] = 'text'
    result = adapter._handle_feishu_event(value)
    assert result.status == 'ignored' and not seen


def test_post_explicit_bot_at_remains_trusted_when_top_mentions_are_incomplete():
    value = _payload()
    message = value['event']['message']
    message['mentions'] = [{'key': '@_user_2', 'id': {'open_id': 'ou_other'}}]
    message['content'] = json.dumps({'content': [[{'tag': 'at', 'user_id': 'ou_bot'}, {'tag': 'text', 'text': 'job01'}]]})
    decision = bot_mention_decision(message, 'ou_bot')
    assert decision.allowed and decision.verified


def test_unresolved_structured_mention_is_retryable_without_name_matching():
    value = _payload()
    value['event']['message']['mentions'] = [{'key': '@_user_1', 'name': '安全运营小助手', 'id': {}}]
    result = _adapter(lambda _: True)._handle_feishu_event(value)
    assert result.status == 'retryable' and result.reason_code == 'FEISHU_MENTION_UNVERIFIED'


def test_post_at_missing_target_is_unverified_and_corrected_same_id_can_replay():
    seen = []
    adapter = _adapter(lambda msg: seen.append(msg) or True)
    value = _payload(mentions=False)
    value['event']['message']['content'] = json.dumps({'content': [[{'tag': 'at'}, {'tag': 'text', 'text': 'job01'}]]})
    result = adapter._handle_feishu_event(value)
    assert result.status == 'retryable' and result.reason_code == 'FEISHU_MENTION_UNVERIFIED'
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    with pytest.raises(FeishuRetryableEventError, match='FEISHU_MENTION_UNVERIFIED'):
        client._handle_event(_sdk(value))
    value['event']['message']['content'] = json.dumps({'content': [[{'tag': 'at', 'user_id': 'ou_bot'}, {'tag': 'text', 'text': 'job01'}]]})
    client._handle_event(_sdk(value))
    assert len(seen) == 1


def test_missing_callback_cannot_be_reported_as_accepted():
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False})
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    assert adapter._handle_feishu_event(_payload()).status == 'retryable'


def test_ingress_and_reaction_diagnostics_do_not_log_body_or_exception_secret(caplog):
    caplog.set_level('INFO')
    adapter = _adapter(lambda _: (_ for _ in ()).throw(RuntimeError('exception-secret')))
    value = _payload()
    value['event']['message']['content'] = json.dumps({'content': [[{'tag': 'at', 'user_id': 'ou_bot'},
        {'tag': 'text', 'text': 'body-secret'}]]})
    result = adapter._handle_feishu_event(value)
    assert result.status == 'retryable'
    assert 'body-secret' not in caplog.text and 'exception-secret' not in caplog.text
    assert 'message_ref' in caplog.text and 'retryable' in caplog.text


def test_reaction_uses_documented_typing_and_retains_safe_http_code(caplog):
    from agent_py_agent.agent.adapter.feishu_typing import _add_reaction

    caplog.set_level('INFO')
    adapter = _adapter(lambda _: True)
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    error = HTTPError('https://synthetic.invalid', 400, 'synthetic-secret', {},
                      BytesIO(b'{"code":231008,"msg":"response-secret"}'))
    with patch('agent_py_agent.agent.adapter.feishu_typing.open_authenticated_request', side_effect=error):
        response = _add_reaction('om_test', 'Typing', 'synthetic-token')
    assert response['code'] == 231008 and response['http_status'] == 400
    with patch('agent_py_agent.agent.adapter.feishu_typing._add_reaction', return_value=response) as add:
        assert adapter.send_progress_placeholder('ou_user', 'om_test') == ''
    assert add.call_args.args[1] == 'Typing'
    assert '231008' in caplog.text and '400' in caplog.text
    assert 'response-secret' not in caplog.text and 'synthetic-secret' not in caplog.text


def test_reaction_failure_does_not_turn_an_accepted_request_into_failure():
    manager = ChannelManager(delivery_state_dir=None)
    adapter = _adapter(manager.route_message)
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    manager.register_adapter(adapter)
    manager._submit_gateway_payload = lambda _body, _message: GatewayAskSubmission('gw_synthetic')
    assert adapter._handle_feishu_event(_payload()).status == 'accepted'
    record = manager._delivery_worker.store.records()[0]
    with patch('agent_py_agent.agent.adapter.feishu_typing._add_reaction', return_value={'code': 231008, 'http_status': 400}):
        manager._delivery_worker._process_record(record, time.time())
    assert manager._delivery_worker.store.records()[0].state == 'completed'
    assert len(manager._reply_delivery.store.pending()) == 1


def test_ws_unknown_callback_result_cannot_commit_unverified_receipt():
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=lambda _: None)
    with pytest.raises(FeishuRetryableEventError):
        client._handle_event(_sdk(_payload()))
    assert not client._seen_ids


def test_typed_disposition_rejects_unknown_status():
    with pytest.raises(ValueError):
        FeishuEventDisposition('finished')


def test_protocol_reason_cannot_forward_arbitrary_text_to_sdk_or_logs():
    with pytest.raises(ValueError):
        FeishuEventDisposition('retryable', 'response-secret')
    assert str(FeishuRetryableEventError('response-secret')) == 'FEISHU_INGRESS_UNAVAILABLE'


def test_string_id_is_accepted_only_with_explicit_open_id_discriminator():
    from agent_py_agent.agent.adapter.feishu_mentions import normalize_mentions

    assert normalize_mentions([{'key': '@_user_1', 'id': 'ou_bot', 'id_type': 'open_id'}])[0]['open_id'] == 'ou_bot'
    assert normalize_mentions([{'key': '@_user_1', 'id': 'ou_bot', 'id_type': 'user_id'}])[0]['open_id'] == ''


def test_auth_service_unavailable_cannot_mark_private_message_handled():
    adapter = FeishuAdapter({'feishu_session_lock_enabled': True})
    value = _payload()
    value['event']['message']['chat_type'] = 'p2p'
    assert adapter._handle_feishu_event(value).reason_code == 'FEISHU_SESSION_LOCK_UNAVAILABLE'
    assert adapter._handle_feishu_event(value).status == 'retryable'


def test_stable_ignored_message_commits_ws_seen_without_consumer_dispatch():
    seen = []
    adapter = _adapter(lambda msg: seen.append(msg) or True)
    value = _payload()
    value['event']['message']['mentions'] = [{'key': '@_all', 'id': {'open_id': 'all'}}]
    value['event']['message']['message_type'] = 'text'
    value['event']['message']['content'] = json.dumps({'text': '@_all 合成'})
    client = FeishuWsClient(app_id='synthetic', app_secret='synthetic', on_payload=adapter._handle_feishu_event)
    client._handle_event(_sdk(value))
    client._handle_event(_sdk(value))
    assert not seen and client._seen_ids == {'om_synthetic'}


def test_concurrent_webhook_admission_reuses_original_ingress_record():
    manager = ChannelManager(delivery_state_dir=None)
    adapter = _adapter(manager.route_message)
    manager.register_adapter(adapter)
    barrier = threading.Barrier(3)
    outcomes = []

    def consume():
        barrier.wait()
        outcomes.append(adapter._handle_feishu_event(_payload()).status)

    workers = [threading.Thread(target=consume) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(2)
    assert outcomes == ['accepted', 'accepted']
    assert len(manager._delivery_worker.store.records()) == 1
