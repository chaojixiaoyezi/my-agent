"""飞书图片准备沿真实owner/下载/持久入站，仅供应商HTTP与最终投递使用合成替身。"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from agent_py_agent.agent.adapter.feishu import _fetch_feishu_resource
from agent_py_agent.agent.adapter.inbound_images import InboundMediaInputError
from agent_py_agent.agent.adapter.ingress import (
    GatewayAdapterDeliveryWorker,
    GatewayIngressAdvance,
    GatewayIngressStore,
    build_gateway_ingress_record,
)
from agent_py_agent.agent.adapter.manager import _gateway_ask_payload
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.conversation.input_media import (
    input_media_blocks,
    provider_media_messages,
)
from agent_py_agent.tests.test_feishu_quoted_media import _event, _manager, _prepare


# LLM: 仅生成合成小图，真实解码器必须完整读取；不依赖伪png字节或真实附件。
# 函数用途: 为不同真实MIME及像素预算提供有界图片测试数据。
def image_bytes(kind='PNG', size=(2, 2)):
    stream = BytesIO()
    Image.new('RGB', size, color='red').save(stream, format=kind)
    return stream.getvalue()


# LLM: 原身份/owner/私有写/图片解码仍走产品，末端资源下载只返回合成字节并记录实际上限。
# 函数用途: 建立支持当前与引用图片的隔离飞书管理器，不连接平台。
def native_case(tmp_path, monkeypatch, *, resources=None, parent=None):
    manager, adapter, host, gets, _ = _manager(tmp_path, monkeypatch, parent)
    downloads = []
    content = resources or {'image': image_bytes(), 'jpeg': image_bytes('JPEG'), 'file': b'ordinary file'}

    def resource(message_id, key, kind, token, **options):
        downloads.append((message_id, key, kind, options.get('max_bytes')))
        return content.get(key)

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu._fetch_feishu_resource', resource)
    return manager, adapter, host, gets, downloads


# LLM: 纯图保留原[图片]文字，当前消息和群归属仍由原parser生成；不在metadata注入媒体ref。
# 函数用途: 构造不带引用的真实飞书图片或图文事件。
def current_event(*, items=None, private=False):
    event = _event(parent='')
    message = event['event']['message']
    if private:
        message.update(chat_type='p2p', chat_id='oc_private')
    if items is None:
        message.update(message_type='image', content={'image_key': 'image'})
    else:
        message.update(message_type='post', content={'content': [[
            {'tag': 'text', 'text': '检查附件'}, *items]]})
    return event


@pytest.mark.parametrize('private', [False, True])
def test_direct_image_is_owner_hashed_ref_and_real_model_image_block(tmp_path, monkeypatch, private):
    manager, _, _, _, _ = native_case(tmp_path, monkeypatch)
    payload = _prepare(manager, current_event(private=private))
    ref, = payload['input_media']
    data = image_bytes()
    assert ref['sha256'] == hashlib.sha256(data).hexdigest()
    assert ref['path'].endswith('/' + ref['sha256']) and ref['media_type'] == 'image/png'
    assert ('/groups/oc_team/' if not private else '/users/ou_user/') in ref['path']
    messages = provider_media_messages([{'role': 'user', 'content': input_media_blocks([ref])}])
    assert messages[0]['content'][0]['source']['type'] == 'base64'
    assert '[图片]' in payload['prompt']


def test_multi_image_and_file_keep_order_and_actual_mime(tmp_path, monkeypatch):
    manager, _, _, _, downloads = native_case(tmp_path, monkeypatch)
    payload = _prepare(manager, current_event(items=[{'tag': 'img', 'image_key': 'jpeg'},
        {'tag': 'file', 'file_key': 'file', 'file_name': 'report.pdf'}, {'tag': 'img', 'image_key': 'image'}]))
    assert [ref['media_type'] for ref in payload['input_media']] == ['image/jpeg', 'image/png']
    assert payload['prompt'].count('[已下载到:') == 3 and 'report.pdf' in payload['prompt']
    assert [entry[2] for entry in downloads] == ['image', 'file', 'image']
    assert downloads[2][3] < downloads[0][3]


def test_valid_quoted_image_uses_parent_resource_and_current_owner(tmp_path, monkeypatch):
    parent = {'code': 0, 'data': {'items': [{'message_id': 'om_file', 'chat_id': 'oc_team',
        'deleted': False, 'msg_type': 'image', 'body': {'content': {'image_key': 'jpeg'}}}]}}
    manager, _, _, gets, downloads = native_case(tmp_path, monkeypatch, parent=parent)
    payload = _prepare(manager, _event())
    assert len(gets) == 1 and downloads[0][:3] == ('om_file', 'jpeg', 'image')
    assert payload['metadata']['user_id'] == 'ou_user'
    assert '/groups/oc_team/' in payload['input_media'][0]['path']


def test_failed_parent_read_validates_current_but_emits_no_partial_image_refs(tmp_path, monkeypatch):
    manager, _, _, _, downloads = native_case(tmp_path, monkeypatch, parent={'code': 230027})
    event = current_event()
    event['event']['message'].update(parent_id='om_file', root_id='om_file')
    payload = _prepare(manager, event)
    assert downloads and 'input_media' not in payload and '[已下载到:' not in payload['prompt']
    assert 'FEISHU_QUOTED_MEDIA_PERMISSION_REQUIRED' in payload['prompt']


def test_second_image_download_failure_does_not_submit_partial_refs(tmp_path, monkeypatch):
    manager, _, _, _, _ = native_case(tmp_path, monkeypatch)
    with pytest.raises(OSError, match='INBOUND_MEDIA_DOWNLOAD_INCOMPLETE'):
        _prepare(manager, current_event(items=[{'tag': 'img', 'image_key': 'image'},
            {'tag': 'img', 'image_key': 'missing'}]))


@pytest.mark.parametrize('fault', ['bytes', 'files', 'pixels', 'corrupt', 'empty'])
def test_permanent_image_failures_are_typed_without_partial_input(tmp_path, monkeypatch, fault):
    manager, _, host, _, _ = native_case(tmp_path, monkeypatch,
        resources={'image': b'not an image'} if fault == 'corrupt' else {'image': b''} if fault == 'empty' else None)
    if fault == 'bytes':
        host.config.input_media_max_bytes = 2
    elif fault == 'files':
        host.config.input_media_max_files = 1
    elif fault == 'pixels':
        host.config.input_image_max_pixels = 1
    event = current_event(items=[{'tag': 'img', 'image_key': 'image'}, {'tag': 'img', 'image_key': 'jpeg'}])
    with pytest.raises(InboundMediaInputError):
        _prepare(manager, event)


def test_disabled_image_input_preserves_paths_without_model_refs(tmp_path, monkeypatch):
    manager, _, host, _, _ = native_case(tmp_path, monkeypatch)
    host.config.feishu_image_input_enabled = False
    payload = _prepare(manager, current_event())
    assert 'input_media' not in payload and '[已下载到:' in payload['prompt']


def test_metadata_and_path_prose_cannot_supply_image_refs():
    message = feishu_to_incoming(current_event(), bot_open_id='ou_bot')
    message.metadata['input_media'] = [{'path': '/another-owner/image'}]
    message.content += '\n[已下载到: /another-owner/image]'
    assert 'input_media' not in _gateway_ask_payload(message)


def test_cold_retry_reuses_exact_frozen_image_hashes_without_download(tmp_path, monkeypatch):
    manager, _, _, _, downloads = native_case(tmp_path, monkeypatch)
    message = feishu_to_incoming(current_event(), bot_open_id='ou_bot')
    record = build_gateway_ingress_record(channel=message.channel, user_id=message.user_id,
        conversation_id=message.conversation_id, provider_message_id=message.message_id,
        content=message.content, metadata=message.metadata, timestamp=message.timestamp)
    store = manager._delivery_worker.store
    store.put_if_absent(record)
    payload = manager._ingress_callbacks.prepare_payload(record)
    assert store.compare_and_swap(record, replace(record, state='payload_ready', gateway_payload=payload))
    submitted = []
    worker = GatewayAdapterDeliveryWorker(GatewayIngressStore(tmp_path / 'delivery'),
        reply_worker=manager._reply_delivery, prepare_payload=lambda _: pytest.fail('no re-download'),
        submit_payload=lambda row: submitted.append(row.gateway_payload) or {'ok': True},
        advance_submission=lambda _: GatewayIngressAdvance('completed'), handoff_reply=lambda _: None)
    assert worker.run_once() == 1 and submitted == [payload] and len(downloads) == 1


def test_bad_image_is_durably_settled_and_failed_error_reply_only_retries_reply(tmp_path, monkeypatch):
    manager, _, _, _, downloads = native_case(tmp_path, monkeypatch, resources={'image': b'bad'})
    message = feishu_to_incoming(current_event(), bot_open_id='ou_bot')
    record = build_gateway_ingress_record(channel=message.channel, user_id=message.user_id,
        conversation_id=message.conversation_id, provider_message_id=message.message_id,
        content=message.content, metadata=message.metadata, timestamp=message.timestamp)
    manager._delivery_worker.enqueue(record)
    monkeypatch.setattr(manager, '_submit_gateway_payload', lambda *_: pytest.fail('invalid image must not POST'))
    replies = []
    monkeypatch.setattr(manager, '_send_gateway_reply', lambda *args: replies.append(args) or len(replies) > 1)
    assert manager._delivery_worker.run_once() == 0
    pending, = manager._delivery_worker.store.pending()
    assert pending.state == 'submitted' and pending.submission['error_code'] == 'INPUT_MEDIA_INVALID'
    assert manager._delivery_worker.store.compare_and_swap(pending, replace(pending, next_attempt_at=0))
    assert manager._delivery_worker.run_once() == 1 and len(downloads) == 1
    assert len(replies) == 2 and replies[0][1:] == replies[1][1:]


def test_real_resource_reader_has_actual_byte_limit_and_closes_response(monkeypatch):
    class Response(BytesIO):
        def read(self, size=-1):
            assert size == 4
            return super().read(size)
    response = Response(b'123456')
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu.open_authenticated_request', lambda *_args, **_kwargs: response)
    with pytest.raises(InboundMediaInputError):
        _fetch_feishu_resource('message', 'key', 'image', 'synthetic', max_bytes=3)
    assert response.closed


def test_owner_path_failure_is_not_downgraded_to_permanent_image_error(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.manager import _inbound_media_destination

    manager, adapter, _, _, _ = native_case(tmp_path, monkeypatch)
    message = feishu_to_incoming(current_event(), bot_open_id='ou_bot')
    destination = _inbound_media_destination(manager._inbound_media_agent, message)
    outside = tmp_path / 'outside.png'
    outside.write_bytes(image_bytes())
    (destination / 'linked.png').symlink_to(outside)
    adapter.fetch_media_to = lambda *_args, **_kwargs: 'linked.png'
    with pytest.raises(OSError):
        manager._maybe_download_media(message, retry_on_error=True)


def test_error_delivery_cold_restart_reuses_frozen_rejection_without_preparing_again(tmp_path, monkeypatch):
    manager, _, _, _, downloads = native_case(tmp_path, monkeypatch, resources={'image': b'bad'})
    message = feishu_to_incoming(current_event(), bot_open_id='ou_bot')
    record = build_gateway_ingress_record(channel=message.channel, user_id=message.user_id,
        conversation_id=message.conversation_id, provider_message_id=message.message_id,
        content=message.content, metadata=message.metadata, timestamp=message.timestamp)
    store = manager._delivery_worker.store
    store.put_if_absent(record)
    rejected = manager._delivery_worker._next_record(record)
    assert rejected.state == 'submitted' and store.compare_and_swap(record, rejected)
    replies = []
    monkeypatch.setattr(manager, '_send_gateway_reply', lambda *args: replies.append(args) or True)
    worker = GatewayAdapterDeliveryWorker(GatewayIngressStore(tmp_path / 'delivery'),
        reply_worker=manager._reply_delivery, prepare_payload=lambda _: pytest.fail('no re-prepare'),
        submit_payload=lambda _: pytest.fail('no POST'),
        advance_submission=manager._ingress_callbacks.advance_submission, handoff_reply=lambda _: None)
    assert worker.run_once() == 1 and len(replies) == len(downloads) == 1
