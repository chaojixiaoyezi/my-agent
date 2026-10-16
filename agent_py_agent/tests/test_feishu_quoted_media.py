"""引用附件以当前群和当前执行者为边界，真实 GET/下载接口用合成供应商替身。"""
import json
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from PIL import Image

from agent_py_agent.agent.adapter.feishu import FeishuAdapter, _fetch_feishu_resource
from agent_py_agent.agent.adapter.feishu_quoted_media import (
    FeishuQuotedMediaError,
    quoted_media_references,
    read_quoted_media,
)
from agent_py_agent.agent.adapter.ingress import build_gateway_ingress_record
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.path_access_policy import PathAccessPolicy
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 引用图片替身必须能被正式解码器完整读取，不靠关闭图片开关或伪png绕过图片入口。
# 函数用途: 生成隔离测试用真实小PNG，普通文件仍使用原合成字节。
def _small_png_bytes():
    stream = BytesIO()
    Image.new('RGB', (2, 2), color='red').save(stream, format='PNG')
    return stream.getvalue()


# LLM: 合成人类入口显式携带 provider sender_type；父消息不派生当前身份，不靠产品接受未知 sender 才通过测试。
# 函数用途: 生成引用根文件的群内明确提及消息，允许逐字段负例，遵守真实入口来源合同。
def _event(*, actor='ou_user', chat='oc_team', content='读这个文件', mention='ou_bot', parent='om_file'):
    return {'event': {'sender': {'sender_type': 'user', 'sender_id': {'open_id': actor}}, 'message': {
        'message_id': 'om_reply', 'chat_id': chat, 'chat_type': 'group', 'message_type': 'text',
        'parent_id': parent, 'root_id': parent, 'content': {'text': '@_user1 ' + content},
        'mentions': [{'key': '@_user1', 'id': {'open_id': mention}}],
    }}}


def _parent(*, chat='oc_team', key='file_synthetic', name='example.txt'):
    return {'message_id': 'om_file', 'chat_id': chat, 'deleted': False, 'msg_type': 'file',
        'sender': {'id': 'ou_admin', 'id_type': 'open_id'},
        'body': {'content': {'file_key': key, 'file_name': name}}}


# LLM: 宿主owner/落盘/图片解码保留生产实现；资源替身接收原max_bytes并真实拒绝超限，不伪造图片格式。
# 函数用途: 构造绑定宿主的管理器与适配器，记录准确源消息；图片给真实小PNG，普通文件保持原字节。
def _manager(tmp_path, monkeypatch, response=None, enabled=True, text_enabled=False):
    root = tmp_path / 'home'
    root.mkdir()
    host = SimpleNamespace(home_paths=SimpleNamespace(root=root), config=AgentConfig())
    manager = ChannelManager(delivery_state_dir=tmp_path / 'delivery')
    manager._inbound_media_agent = host
    adapter = FeishuAdapter({'feishu_session_lock_enabled': False,
        'feishu_quoted_media_enabled': enabled, 'feishu_quoted_text_enabled': text_enabled})
    adapter._resolve_bot_open_id = lambda: 'ou_bot'
    adapter._get_tenant_access_token = lambda: 'synthetic-token'
    gets, resources = [], []

    def api(method, path, body, token):
        gets.append((method, path, body))
        if isinstance(response, Exception):
            raise response
        return deepcopy(response if response is not None else {'code': 0, 'data': {'items': [_parent()]}})

    def resource(message_id, key, kind, token, *, max_bytes=None):
        resources.append((message_id, key, kind))
        data = _small_png_bytes() if kind == 'image' else b'own attachment'
        if max_bytes is not None and len(data) > max_bytes:
            from agent_py_agent.agent.adapter.inbound_images import InboundMediaInputError
            raise InboundMediaInputError('合成资源超过本次输入上限。')
        return data

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu._feishu_msg_api', api)
    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu._fetch_feishu_resource', resource)
    manager._adapters['feishu'] = adapter
    return manager, adapter, host, gets, resources


def _prepare(manager, event):
    msg = feishu_to_incoming(event, bot_open_id='ou_bot')
    assert msg is not None
    record = build_gateway_ingress_record(channel=msg.channel, user_id=msg.user_id,
        conversation_id=msg.conversation_id, provider_message_id=msg.message_id,
        content=msg.content, metadata=msg.metadata, timestamp=msg.timestamp)
    return manager._ingress_callbacks.prepare_payload(record)


def test_quoted_root_file_uses_parent_resource_current_group_and_current_actor(tmp_path, monkeypatch):
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch)
    bind_admin_channel_identity(host.home_paths.root, 'feishu', 'ou_admin')
    payload = _prepare(manager, _event())
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    paths = list((owner.home_dir / 'media' / 'input').iterdir())
    assert gets == [('GET', '/im/v1/messages/om_file', {})]
    assert resources == [('om_file', 'file_synthetic', 'file')]
    assert len(paths) == 1 and paths[0].read_bytes() == b'own attachment'
    assert paths[0].stat().st_mode & 0o777 == 0o600
    assert str(paths[0]) in payload['prompt']
    assert payload['metadata']['user_id'] == 'ou_user'
    assert payload['metadata']['channel_chat_id'] == 'oc_team'
    assert payload['conversation_id'] == 'oc_team:thread:om_file'
    assert not (host.home_paths.root / 'owners' / 'local' / 'main' / 'media').exists()


@pytest.mark.parametrize('change,code', [
    ({'chat_id': 'oc_other'}, 'FEISHU_QUOTED_MEDIA_CHAT_MISMATCH'),
    ({'chat_id': 'oc_private'}, 'FEISHU_QUOTED_MEDIA_CHAT_MISMATCH'),
    ({'message_id': 'om_other'}, 'FEISHU_QUOTED_MEDIA_ID_MISMATCH'),
    ({'deleted': True}, 'FEISHU_QUOTED_MEDIA_DELETED'),
    ({'deleted': None}, 'FEISHU_QUOTED_MEDIA_INVALID'),
    ({'thread_id': 'omt_other'}, 'FEISHU_QUOTED_MEDIA_TOPIC_MISMATCH'),
])
def test_parent_proof_failure_is_structured_no_download(tmp_path, monkeypatch, change, code):
    parent = _parent()
    parent.update(change)
    event = _event()
    event['event']['message']['thread_id'] = 'omt_current'
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch, {'code': 0, 'data': {'items': [parent]}})
    payload = _prepare(manager, event)
    assert len(gets) == 1 and not resources
    assert code in payload['prompt'] and 'quoted_media.v1' in payload['prompt']
    assert '[已下载到:' not in payload['prompt']
    assert 'ou_admin' not in payload['prompt']


@pytest.mark.parametrize('code', [230027, 230002, 230050, 230110, 123456])
def test_provider_error_never_leaks_body_or_becomes_infinite_prepare_retry(tmp_path, monkeypatch, code):
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch,
        {'code': code, 'msg': 'SECRET_BODY', 'data': {'items': [_parent()]}})
    payload = _prepare(manager, _event())
    assert len(gets) == 1 and not resources
    assert 'SECRET_BODY' not in payload['prompt'] and str(code) in payload['prompt']
    assert '"status": "unavailable"' in payload['prompt']


@pytest.mark.parametrize('body', ['/status', '/schedule', '/verbose stream', '/help', '/approve password'])
def test_controls_and_disabled_feature_make_no_parent_request(tmp_path, monkeypatch, body):
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch)
    payload = _prepare(manager, _event(content=body))
    assert payload['prompt'] == body and not gets and not resources


def test_config_off_and_private_quote_make_no_parent_request(tmp_path, monkeypatch):
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch, enabled=False)
    _prepare(manager, _event())
    assert not gets and not resources
    manager._adapters['feishu'].quoted_media_enabled = True
    event = _event()
    event['event']['message']['chat_type'] = 'p2p'
    _prepare(manager, event)
    assert not gets and not resources


@pytest.mark.parametrize('mention', ['all', 'ou_other'])
def test_unaddressed_group_stops_before_quote_and_ingress(tmp_path, monkeypatch, mention):
    manager, adapter, _, gets, resources = _manager(tmp_path, monkeypatch)
    called = []
    adapter.on_message(lambda msg: called.append(msg) or manager.route_message(msg))
    result = adapter._handle_feishu_event(_event(mention=mention))
    assert result.status == 'ignored' and not called and not gets and not resources
    assert not manager._delivery_worker.store.pending()


def test_exact_parent_only_no_merge_child_or_recursive_parent_fetch(tmp_path, monkeypatch):
    parent = _parent()
    parent['msg_type'], parent['body'] = 'text', {'content': {'text': 'parent text MUST NOT ENTER PROMPT'}}
    parent['parent_id'] = 'om_older'
    response = {'code': 0, 'data': {'items': [parent, _parent(key='child-file')]}}
    response['data']['items'][1]['message_id'] = 'om_child'
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch, response)
    payload = _prepare(manager, _event())
    assert len(gets) == 1 and not resources
    assert 'FEISHU_QUOTED_MEDIA_NO_RESOURCE' in payload['prompt']
    assert 'MUST NOT ENTER' not in payload['prompt']


def test_source_ids_are_url_parameters_and_filename_never_changes_owner(tmp_path, monkeypatch):
    parent = _parent(name='../../foreign/secret.txt')
    parent['message_id'] = '../om_file?x=1'
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch, {'code': 0, 'data': {'items': [parent]}})
    payload = _prepare(manager, _event(parent=parent['message_id']))
    assert gets[0][1] == '/im/v1/messages/..%2Fom_file%3Fx%3D1'
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    paths = list((owner.home_dir / 'media' / 'input').iterdir())
    assert len(paths) == 1 and paths[0].name.endswith('secret.txt')
    assert paths[0].parent == owner.home_dir / 'media' / 'input'
    assert resources[0][0] == parent['message_id'] and str(paths[0]) in payload['prompt']
    assert not (tmp_path / 'foreign').exists()


def test_http_failure_has_only_numeric_status_not_exception_text(tmp_path, monkeypatch):
    failure = HTTPError('https://SECRET_URL', 403, 'SECRET_REASON', {}, None)
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch, failure)
    payload = _prepare(manager, _event())
    assert '403' in payload['prompt'] and 'FEISHU_QUOTED_MEDIA_READ_UNAVAILABLE' in payload['prompt']
    assert 'SECRET' not in payload['prompt'] and not resources


def test_host_auth_failure_is_structured_and_does_not_call_message_api(tmp_path, monkeypatch):
    manager, adapter, _, gets, resources = _manager(tmp_path, monkeypatch)
    adapter._get_tenant_access_token = lambda: None
    payload = _prepare(manager, _event())
    assert 'FEISHU_QUOTED_MEDIA_AUTH_UNAVAILABLE' in payload['prompt'] and not gets and not resources


def test_real_owner_access_refuses_other_group_attachment(tmp_path, monkeypatch):
    manager, _, host, _, _ = _manager(tmp_path, monkeypatch)
    paths = []
    for chat in ('oc_one', 'oc_two'):
        manager._adapters['feishu'].fetch_message = lambda _id, chat=chat: {'code': 0, 'data': {'items': [_parent(chat=chat)]}}
        _prepare(manager, _event(chat=chat))
        owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', chat))
        paths.append((owner, next((owner.home_dir / 'media' / 'input').iterdir())))
    assert paths[0][1] != paths[1][1]
    policy = PathAccessPolicy.from_values(owner_scope_root=str(paths[0][0].home_dir))
    assert policy.check(paths[0][1]).allowed
    assert policy.check(paths[1][1]).code == 'PATH_CROSS_OWNER_BLOCKED'
    from agent_py_agent.agent.tooling._filesystem_read import (
        ReadFileTool,
        filesystem_access_options,
    )
    owner = paths[0][0]
    reader = ReadFileTool(owner.home_dir, 1000, [owner.home_dir],
        filesystem_access_options(owner_scope_root=str(owner.home_dir)))
    assert reader.execute({'path': str(paths[0][1])}).ok
    denied = reader.execute({'path': str(paths[1][1])})
    assert not denied.ok and denied.error_code == 'PATH_CROSS_OWNER_BLOCKED'


def test_partial_quoted_multi_download_submits_failure_no_partial_paths(tmp_path, monkeypatch):
    parent = _parent()
    parent['msg_type'], parent['body'] = 'post', {'content': {'content': [[
        {'tag': 'file', 'file_key': 'key1', 'file_name': 'same.txt'},
        {'tag': 'file', 'file_key': 'key2', 'file_name': 'same.txt'}]]}}
    manager, adapter, _, _, resources = _manager(tmp_path, monkeypatch, {'code': 0, 'data': {'items': [parent]}})
    original = adapter.fetch_media_to
    adapter.fetch_media_to = lambda message_id, media, dest: None if media['file_key'] == 'key2' else original(message_id, media, dest)
    payload = _prepare(manager, _event())
    assert resources == [('om_file', 'key1', 'file')]
    assert 'FEISHU_QUOTED_MEDIA_RESOURCE_UNAVAILABLE' in payload['prompt']
    assert '[已下载到:' not in payload['prompt']


def test_owner_symlink_failure_stays_hard_not_provider_failure(tmp_path, monkeypatch):
    manager, _, host, _, _ = _manager(tmp_path, monkeypatch)
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    owner.home_dir.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (owner.home_dir / 'media').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _prepare(manager, _event())
    assert not list(outside.iterdir())


@pytest.mark.parametrize('actor', ['ou_user', 'ou_admin'])
def test_mixed_current_and_parent_media_keep_each_resource_source(tmp_path, monkeypatch, actor):
    manager, _, host, _, resources = _manager(tmp_path, monkeypatch)
    bind_admin_channel_identity(host.home_paths.root, 'feishu', 'ou_admin')
    event = _event(actor=actor)
    message = event['event']['message']
    message['message_type'], message['content'] = 'post', {'content': [[
        {'tag': 'at', 'user_id': '@_user1'}, {'tag': 'text', 'text': '检查两个文件'},
        {'tag': 'file', 'file_key': 'current_key', 'file_name': 'example.txt'}]]}
    payload = _prepare(manager, event)
    assert resources == [('om_reply', 'current_key', 'file'), ('om_file', 'file_synthetic', 'file')]
    assert payload['metadata']['user_id'] == actor and payload['metadata']['channel_chat_id'] == 'oc_team'
    assert payload['prompt'].count('[已下载到:') == 2
    assert not (host.home_paths.root / 'owners' / 'local' / 'main' / 'media').exists()


@pytest.mark.parametrize('current,parent,valid', [
    ({'root_id': 'om_root', 'thread_id': 'omt_same'}, {'root_id': 'om_root', 'thread_id': 'omt_same'}, True),
    ({'root_id': 'om_root'}, {'root_id': 'om_root'}, True),
    ({'root_id': '', 'thread_id': ''}, {}, True),
    ({'root_id': 'om_root'}, {}, False),
    ({'root_id': ''}, {'root_id': 'om_root'}, False),
    ({'root_id': 'om_root', 'thread_id': 'omt_same'}, {'root_id': 'om_other', 'thread_id': 'omt_same'}, False),
])
def test_topic_proofs_use_exact_shared_anchor_without_recursive_query(tmp_path, monkeypatch, current, parent, valid):
    original = _parent()
    original.update(parent)
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch, {'code': 0, 'data': {'items': [original]}})
    event = _event()
    event['event']['message'].update(current)
    payload = _prepare(manager, event)
    assert len(gets) == 1 and bool(resources) is valid
    assert ('FEISHU_QUOTED_MEDIA_TOPIC_MISMATCH' in payload['prompt']) is not valid


@pytest.mark.parametrize('response', [None, {'data': {}}, {'code': '0'}, {'code': 0, 'data': {'items': [_parent(), _parent()]}}, ValueError('SECRET')])
def test_invalid_response_and_duplicate_exact_parent_do_not_download(tmp_path, monkeypatch, response):
    manager, adapter, _, gets, resources = _manager(tmp_path, monkeypatch, response)
    if response is None:
        adapter.fetch_message = lambda _: None
    payload = _prepare(manager, _event())
    assert not resources and '"status": "unavailable"' in payload['prompt']
    assert 'SECRET' not in payload['prompt'] and '[已下载到:' not in payload['prompt']
    assert len(gets) <= 1


def test_no_parent_and_unknown_bot_never_perform_parent_get(tmp_path, monkeypatch):
    manager, adapter, _, gets, resources = _manager(tmp_path, monkeypatch)
    _prepare(manager, _event(parent=''))
    adapter._resolve_bot_open_id = lambda: ''
    adapter.on_message(manager.route_message)
    result = adapter._handle_feishu_event(_event())
    assert result.status == 'retryable' and not gets and not resources


def test_explicit_disabled_original_group_gate_still_accepts_quote(tmp_path, monkeypatch):
    manager, adapter, _, gets, resources = _manager(tmp_path, monkeypatch)
    adapter.group_require_bot_mention = False
    adapter.on_message(manager.route_message)
    result = adapter._handle_feishu_event(_event(mention='ou_other'))
    assert result.status == 'accepted' and not gets and not resources
    record = manager._delivery_worker.store.pending()[0]
    payload = manager._ingress_callbacks.prepare_payload(record)
    assert len(gets) == 1 and resources and '[已下载到:' in payload['prompt']


def test_existing_current_media_cannot_choose_foreign_source_id(tmp_path, monkeypatch):
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch)
    msg = feishu_to_incoming(_event(parent=''), bot_open_id='ou_bot')
    msg.metadata['media'] = {'file_key': 'current', 'source_message_id': 'om_foreign'}
    manager._maybe_download_media(msg, retry_on_error=True)
    assert resources == [('om_reply', 'current', 'file')]


def test_validated_reference_cannot_be_reused_for_other_message_or_source(tmp_path, monkeypatch):
    _, adapter, _, _, _ = _manager(tmp_path, monkeypatch)
    msg = feishu_to_incoming(_event(), bot_open_id='ou_bot')
    quoted = read_quoted_media(adapter, msg)
    for changed in [replace(quoted, current_message_id='om_other'), replace(quoted, chat_id='oc_other'),
                    replace(quoted, items=({'file_key': 'synthetic', 'source_message_id': 'om_other'},))]:
        with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_INVALID'):
            quoted_media_references(msg, changed)


def test_resource_api_escapes_both_message_and_key_and_logs_no_exception_body(monkeypatch, caplog):
    calls = []

    def fail(request, timeout):
        calls.append(request.full_url)
        raise HTTPError('https://SECRET', 403, 'SECRET_REASON', {}, BytesIO(b'SECRET_BODY'))

    monkeypatch.setattr('agent_py_agent.agent.adapter.feishu.open_authenticated_request', fail)
    assert _fetch_feishu_resource('../om?x=1', '../key?a=b', 'file', 'SECRET_TOKEN') is None
    assert calls == ['https://open.feishu.cn/open-apis/im/v1/messages/..%2Fom%3Fx%3D1/resources/..%2Fkey%3Fa%3Db?type=file']
    assert '403' in caplog.text and 'SECRET' not in caplog.text


def test_gateway_retry_reuses_frozen_original_quote_payload_after_cold_reload(tmp_path, monkeypatch):
    from agent_py_agent.agent.adapter.ingress import (
        GatewayAdapterDeliveryWorker,
        GatewayIngressAdvance,
        GatewayIngressStore,
    )

    manager, adapter, host, gets, resources = _manager(tmp_path, monkeypatch)
    adapter.on_message(manager.route_message)
    assert adapter._handle_feishu_event(_event()).status == 'accepted'
    assert not gets and not resources
    store = manager._delivery_worker.store
    record = store.pending()[0]
    assert record.user_id == 'ou_user' and record.owner_id.endswith('oc_team')
    payload = manager._ingress_callbacks.prepare_payload(record)
    ready = replace(record, state='payload_ready', gateway_payload=payload)
    assert store.compare_and_swap(record, ready)
    sent = []
    worker = GatewayAdapterDeliveryWorker(GatewayIngressStore(tmp_path / 'delivery'),
        reply_worker=manager._reply_delivery,
        prepare_payload=lambda _: pytest.fail('frozen payload must not fetch parent again'),
        submit_payload=lambda row: sent.append(row.gateway_payload) or {'ok': True},
        advance_submission=lambda _: GatewayIngressAdvance('completed'), handoff_reply=lambda _: True)
    worker.run_once()
    assert sent == [payload] and len(gets) == 1 and len(resources) == 1
    assert next(row for row in store.records() if row.ingress_id == record.ingress_id).state == 'completed'
    assert json.loads(json.dumps(payload)) == payload


def test_actual_cli_constructs_adapter_with_original_config_toggle():
    from agent_py_agent.cli.adapter import _feishu_adapter_config

    host = SimpleNamespace(config=AgentConfig(feishu_quoted_media_enabled=False))
    config = _feishu_adapter_config(host)
    assert config['feishu_quoted_media_enabled'] is False
    assert FeishuAdapter(config).quoted_media_enabled is False
    host.config.feishu_quoted_media_enabled = True
    assert FeishuAdapter(_feishu_adapter_config(host)).quoted_media_enabled is True


# LLM: 两份材料属于同一请求，父查询失败不得掩盖原当前附件或 owner/no-follow 的硬拒绝。
# 函数用途: 构造带当前文件及明确引用的同一群富文本，避免只覆盖纯引用消息。
def _mixed_event():
    event = _event()
    event['event']['message'].update(message_type='post', content={'content': [[
        {'tag': 'at', 'user_id': '@_user1'}, {'tag': 'text', 'text': '一起检查'},
        {'tag': 'file', 'file_key': 'current_key', 'file_name': 'current.txt'}]]})
    return event


@pytest.mark.parametrize('failure', ['current_resource', 'owner_symlink'])
def test_parent_get_failure_cannot_bypass_existing_current_attachment_hard_gate(tmp_path, monkeypatch, failure):
    manager, adapter, host, gets, _ = _manager(tmp_path, monkeypatch, {'code': 230027})
    if failure == 'current_resource':
        adapter.fetch_media_to = lambda *_: None
    else:
        owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
        owner.home_dir.mkdir(parents=True)
        outside = tmp_path / 'outside'
        outside.mkdir()
        (owner.home_dir / 'media').symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        _prepare(manager, _mixed_event())
    assert len(gets) == 1


def test_parent_get_failure_preserves_current_download_validation_but_no_partial_prompt_paths(tmp_path, monkeypatch):
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch, {'code': 230027})
    payload = _prepare(manager, _mixed_event())
    assert len(gets) == 1 and resources == [('om_reply', 'current_key', 'file')]
    assert '[已下载到:' not in payload['prompt'] and 'FEISHU_QUOTED_MEDIA_PERMISSION_REQUIRED' in payload['prompt']
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    assert len(list((owner.home_dir / 'media' / 'input').iterdir())) == 1
