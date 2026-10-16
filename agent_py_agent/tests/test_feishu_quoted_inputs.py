"""引用各类资料仍走当前 owner 私有入站与同次消息证明，供应商请求全部合成。"""
import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.adapter.feishu_quoted_media import (
    FeishuQuotedMediaError,
    quoted_media_references,
    read_quoted_media,
)
from agent_py_agent.agent.adapter.protocol import feishu_to_incoming
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_feishu_quoted_media import (
    _event,
    _manager,
    _prepare,
    _small_png_bytes,
)
from agent_py_agent.tests.test_feishu_quoted_text import _quote_facts, _text_parent


# LLM: 合成成员只携带 API 明确编号和 upper 关系；外部源 chat 与 sender 不升级当前身份。
# 函数用途: 生成同次转发查询的根或后代，可独立改类型、资源与关系做负例。
def _member(message_id, kind, content, upper=None):
    row = _text_parent()
    row.update(message_id=message_id, msg_type=kind, body={'content': content})
    if upper:
        row.update(upper_message_id=upper, chat_id='oc_original_private')
    return row


@pytest.mark.parametrize('kind,key,name', [
    ('image', 'image_key', ''), ('audio', 'file_key', ''), ('media', 'file_key', ''),
    ('file', 'file_key', 'repository.zip'), ('file', 'file_key', 'report.pdf'),
    ('file', 'file_key', 'table.xlsx'), ('file', 'file_key', 'new-format.future'),
])
def test_quoted_media_types_download_under_current_group_owner(tmp_path, monkeypatch, kind, key, name):
    content = {key: 'resource_key', 'file_name': name}
    parent = _member('om_file', kind, content)
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [parent]}}, text_enabled=True)
    payload = _prepare(manager, _event(content='分析引用资料'))
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    paths = list((owner.home_dir / 'media' / 'input').iterdir())
    if kind == 'image':
        ref, = payload['input_media']
        assert ref['media_type'] == 'image/png' and ref['path'].endswith('/' + ref['sha256'])
        assert len(paths) == 2 and all(path.read_bytes() == _small_png_bytes() for path in paths)
        assert ref['path'] in payload['prompt'] and all(path.parent == owner.home_dir / 'media' / 'input' for path in paths)
    else:
        assert len(paths) == 1 and paths[0].read_bytes() == b'own attachment'
        assert str(paths[0]) in payload['prompt'] and 'input_media' not in payload
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
    assert resources == [('om_file', 'resource_key', 'image' if kind == 'image' else 'file')]
    assert len(gets) == 1 and payload['metadata']['user_id'] == 'ou_user'
    assert _quote_facts(payload)['coverage'] == 'complete'
    assert not (host.home_paths.root / 'owners' / 'local' / 'main' / 'media').exists()


def test_card_visible_code_links_images_keep_hidden_callback_values_out(tmp_path, monkeypatch):
    card = {'header': {'title': {'tag': 'plain_text', 'content': '告警详情'}},
        'body': {'elements': [
            {'tag': 'markdown', 'content': '```python\n  print("alarm")\n```'},
            {'tag': 'button', 'text': {'content': '代码仓库'}, 'url': 'https://github.com/example/repo',
             'value': {'token': 'HIDDEN_CALLBACK_TOKEN'},
             'behaviors': [{'type': 'callback', 'value': {'text': 'HIDDEN_CALLBACK_BODY'}}]},
            {'tag': 'img', 'img_key': 'card_image'},
        ]}, 'config': {'text': 'HIDDEN_CONFIG_BODY'}, 'sender': {'text': 'HIDDEN_SENDER_BODY'}}
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [_member('om_file', 'interactive', json.dumps(card))]}}, text_enabled=True)
    payload = _prepare(manager, _event(content='分析引用的卡片'))
    facts = _quote_facts(payload)
    assert '告警详情' in facts['content'] and '  print("alarm")' in facts['content']
    assert 'https://github.com/example/repo' in facts['content']
    assert 'HIDDEN_' not in payload['prompt'] and 'card_image' not in facts['content']
    assert facts['coverage'] == 'visible_only' and resources == [('om_file', 'card_image', 'image')]
    assert len(payload['input_media']) == 1 and payload['input_media'][0]['media_type'] == 'image/png'
    assert len(gets) == 1  # 链接/代码仅资料，不在准备阶段 fetch/clone/execute。


def test_unknown_visible_protocol_and_resource_are_readable_without_full_claim(tmp_path, monkeypatch):
    body = {'title': '新协议资料', 'content': 'https://example.invalid/resource',
        'file_key': 'future_file', 'file_name': 'opaque.binary',
        'credentials': {'content': 'HIDDEN_SECRET'}}
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [_member('om_file', 'future_kind', body)]}}, text_enabled=True)
    payload = _prepare(manager, _event())
    facts = _quote_facts(payload)
    assert '新协议资料' in facts['content'] and 'example.invalid/resource' in facts['content']
    assert facts['coverage'] == 'visible_only' and 'HIDDEN_SECRET' not in payload['prompt']
    assert resources == [('om_file', 'future_file', 'file')]


@pytest.mark.parametrize('kind,body', [
    ('namecard', {'user_id': 'ou_secret'}), ('future_kind', {'credentials': {'text': 'SECRET'}}),
    ('merge_forward', {'message_ids': ['om_not_in_response']}), ('image', {}),
    ('future_kind', {'message_type': 'file', 'body': {'content': {'file_key': 'unproved'}}}),
])
def test_opaque_or_unproved_quote_is_explicitly_unavailable(tmp_path, monkeypatch, kind, body):
    manager, _, _, gets, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [_member('om_file', kind, body)]}}, text_enabled=True)
    payload = _prepare(manager, _event())
    assert 'FEISHU_QUOTED_MEDIA_NO_RESOURCE' in payload['prompt']
    assert '[引用消息资料]' not in payload['prompt'] and not resources and len(gets) == 1
    assert 'SECRET' not in payload['prompt'] and 'unproved' not in payload['prompt']


def test_merged_forward_downloads_each_proven_member_from_its_exact_id(tmp_path, monkeypatch):
    root = _member('om_file', 'merge_forward', {'message_ids': ['om_wrong_body_id']})
    text = _member('om_text', 'text', {'text': '告警与 https://github.com/example/repo'}, 'om_file')
    nested = _member('om_nested', 'merge_forward', {}, 'om_file')
    image = _member('om_image', 'image', {'image_key': 'quoted_image'}, 'om_nested')
    file = _member('om_zip', 'file', {'file_key': 'quoted_zip', 'file_name': 'repository.zip'}, 'om_file')
    foreign = _member('om_foreign', 'text', {'text': 'FOREIGN_MUST_NOT_LEAK'}, 'om_other')
    response = {'code': 0, 'data': {'items': [file, foreign, image, text, root, nested]}}
    manager, _, host, gets, resources = _manager(tmp_path, monkeypatch, response, text_enabled=True)
    payload = _prepare(manager, _event(content='分析这些资料'))
    facts = _quote_facts(payload)
    assert facts['member_count'] == 5 and facts['coverage'] == 'complete' and facts['unreadable_count'] == 0
    assert '告警与' in facts['content'] and 'FOREIGN' not in payload['prompt']
    assert resources == [('om_zip', 'quoted_zip', 'file'), ('om_image', 'quoted_image', 'image')]
    assert len(gets) == 1 and payload['metadata']['channel_chat_id'] == 'oc_team'
    assert payload['metadata']['user_id'] == 'ou_user'
    owner = resolve_owner_home(host.home_paths.root, OwnerIdentity.provider_group('feishu', 'oc_team'))
    paths = list((owner.home_dir / 'media' / 'input').iterdir())
    assert len(paths) == 3  # 原下载文件、原图片下载及其唯一内容寻址图片。
    assert len(payload['input_media']) == 1 and payload['input_media'][0]['media_type'] == 'image/png'
    assert all(path.parent == owner.home_dir / 'media' / 'input' for path in paths)
    assert sorted(path.read_bytes() for path in paths) == sorted([b'own attachment', _small_png_bytes(), _small_png_bytes()])
    assert not (host.home_paths.root / 'owners' / 'providers' / 'feishu' / 'groups' / 'oc_original_private').exists()


def test_merge_scope_and_resource_proof_failure_do_not_leak_any_member(tmp_path, monkeypatch):
    root = _member('om_file', 'merge_forward', {})
    root['chat_id'] = 'oc_other'
    child = _member('om_child', 'text', {'text': 'FOREIGN_MUST_NOT_LEAK'}, 'om_file')
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [root, child]}}, text_enabled=True)
    assert 'FOREIGN_MUST_NOT_LEAK' not in _prepare(manager, _event())['prompt'] and not resources


def test_related_duplicate_cannot_select_one_variant_or_partial_text(tmp_path, monkeypatch):
    root = _member('om_file', 'merge_forward', {})
    child = _member('om_child', 'text', {'text': 'PARTIAL_MUST_NOT_LEAK'}, 'om_file')
    duplicate = _member('om_child', 'text', {'text': 'FOREIGN'}, 'om_foreign')
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [root, child, duplicate]}}, text_enabled=True)
    payload = _prepare(manager, _event())
    assert 'FEISHU_QUOTED_MEDIA_ID_MISMATCH' in payload['prompt']
    assert 'PARTIAL_MUST_NOT_LEAK' not in payload['prompt'] and not resources


def test_merge_media_proof_does_not_allow_unselected_source_id(tmp_path, monkeypatch):
    root = _member('om_file', 'merge_forward', {})
    child = _member('om_child', 'file', {'file_key': 'key'}, 'om_file')
    _, adapter, _, _, _ = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [root, child]}}, text_enabled=True)
    msg = feishu_to_incoming(_event(), bot_open_id='ou_bot')
    proof = read_quoted_media(adapter, msg)
    assert quoted_media_references(msg, proof)['items'][0]['source_message_id'] == 'om_child'
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_INVALID'):
        quoted_media_references(msg, replace(proof, items=({'file_key': 'key', 'source_message_id': 'om_foreign'},)))


def test_unreadable_merged_child_reports_partial_coverage(tmp_path, monkeypatch):
    root = _member('om_file', 'merge_forward', {})
    text = _member('om_text', 'text', {'text': '已读资料'}, 'om_file')
    gone = _member('om_gone', 'text', {'text': 'GONE_MUST_NOT_LEAK'}, 'om_file')
    gone['deleted'] = True
    manager, _, _, _, _ = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [root, gone, text]}}, text_enabled=True)
    facts = _quote_facts(_prepare(manager, _event()))
    assert facts['coverage'] == 'visible_only' and facts['unreadable_count'] == 1
    assert facts['member_count'] == 2 and 'GONE_MUST_NOT_LEAK' not in facts['content']


def test_disabled_media_is_honest_partial_projection_without_download(tmp_path, monkeypatch):
    body = {'content': [[{'tag': 'text', 'text': '资料'}, {'tag': 'img', 'image_key': 'key'}]]}
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [_member('om_file', 'post', body)]}}, enabled=False, text_enabled=True)
    facts = _quote_facts(_prepare(manager, _event()))
    assert facts['media_omitted_count'] == 1 and facts['coverage'] == 'visible_only' and not resources


def test_disabled_text_does_not_expose_card_body_but_downloads_resource(tmp_path, monkeypatch):
    body = {'elements': [{'content': 'TEXT_MUST_STAY_OFF'}, {'tag': 'img', 'img_key': 'key'}]}
    manager, _, _, _, resources = _manager(tmp_path, monkeypatch,
        {'code': 0, 'data': {'items': [_member('om_file', 'interactive', body)]}}, text_enabled=False)
    payload = _prepare(manager, _event())
    facts = _quote_facts(payload)
    assert facts['text_omitted'] is True and facts['content'] == ''
    assert 'TEXT_MUST_STAY_OFF' not in payload['prompt'] and resources == [('om_file', 'key', 'image')]
    assert len(payload['input_media']) == 1 and payload['input_media'][0]['media_type'] == 'image/png'
