"""引用内容纯解析回归：可见树、原格式、不可读事实与冻结资源，不发送网络或读取生产数据。"""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from agent_py_agent.agent.adapter import feishu_quoted_content as module
from agent_py_agent.agent.adapter.feishu_media import extract_feishu_content
from agent_py_agent.agent.adapter.feishu_quoted_content import extract_quoted_content


@pytest.mark.parametrize('raw', [{'text': '普通正文'}, '{"text":"普通正文"}', '普通正文'])
def test_known_text_uses_original_projection(raw):
    result = extract_quoted_content('text', raw)
    assert result.text == '普通正文'
    assert result.media == {} and result.coverage == 'complete' and result.unreadable_count == 0


@pytest.mark.parametrize('raw', ['123', 'null', 'true', '1.25'])
@pytest.mark.parametrize('msg_type', ['text', 'future'])
def test_direct_scalar_like_text_remains_public_material(msg_type, raw):
    result = extract_quoted_content(msg_type, raw)
    assert result.text == raw and result.media == {} and result.unreadable_count == 0
    assert result.coverage == ('complete' if msg_type == 'text' else 'visible_only')


@pytest.mark.parametrize('msg_type,raw,media', [
    ('image', {'image_key': 'img-one'}, {'image_key': 'img-one'}),
    ('image', {'img_key': 'img-one'}, {'image_key': 'img-one'}),
    ('file', {'file_key': 'file-one', 'file_name': '原样.未知格式'}, {'file_key': 'file-one', 'file_name': '原样.未知格式'}),
    ('audio', {'file_key': 'audio-one'}, {'file_key': 'audio-one'}),
    ('media', {'file_key': 'video-one'}, {'file_key': 'video-one'}),
])
def test_known_media_remains_original_items_shape(msg_type, raw, media):
    result = extract_quoted_content(msg_type, raw)
    assert result.media == {'items': [media]}
    assert result.coverage == 'complete' and result.unreadable_count == 0


@pytest.mark.parametrize('kind,raw', [
    ('image', {'file_key': 'wrong-resource-kind'}),
    ('file', {'image_key': 'wrong-resource-kind'}),
    ('audio', {'image_key': 'wrong-resource-kind'}),
    ('media', {'image_key': 'wrong-resource-kind'}),
])
def test_known_media_requires_its_own_protocol_resource_key(kind, raw):
    result = extract_quoted_content(kind, raw)
    assert result.coverage == 'unavailable' and result.media == {} and result.text == ''


@pytest.mark.parametrize('kind,raw', [
    ('interactive', {'elements': [{'tag': 'img', 'alt': {'content': '图片说明'}}]}),
    ('post', {'content': [[{'tag': 'img', 'text': '图片说明'}]]}),
])
def test_media_caption_does_not_hide_missing_resource(kind, raw):
    result = extract_quoted_content(kind, raw)
    assert result.text == '图片说明' and result.media == {}
    assert result.coverage == 'visible_only' and result.unreadable_count == 1


def test_exact_node_budget_does_not_report_nonexistent_missing_material(monkeypatch):
    monkeypatch.setattr(module, 'MAX_VISIBLE_NODES', 2)
    result = extract_quoted_content('post', {'content': [[{'tag': 'text', 'text': '完整一行'}]]})
    assert result.text == '完整一行' and result.coverage == 'complete' and result.unreadable_count == 0


def test_card_image_list_and_opaque_post_mention_do_not_expand_identity():
    card = extract_quoted_content('interactive', {'elements': [{'tag': 'img_combination', 'img_list': [
        {'img_key': 'one'}, {'img_key': 'two'}]}]})
    assert card.media == {'items': [{'image_key': 'one'}, {'image_key': 'two'}]}
    post = extract_quoted_content('post', {'content': [[{'tag': 'at', 'user_id': 'opaque-person', 'text': '显示标签'}]]})
    assert post.text == '显示标签' and 'opaque-person' not in post.text


@pytest.mark.parametrize('shape', ['direct', 'localized', 'wrapped', 'future-locale'])
def test_post_reuses_original_code_links_and_row_format(shape):
    code = 'def main():\n    print("```token 是公开代码")\n\nmain()'
    post = {'title': '引用代码', 'content': [
        [{'tag': 'text', 'text': '看这些：'}],
        [{'tag': 'code_block', 'language': 'python', 'text': code}],
        [{'tag': 'a', 'text': '代码库', 'href': 'https://example.invalid/repository'}],
        [{'tag': 'img', 'image_key': 'img-one'}, {'tag': 'file', 'file_key': 'file-one', 'file_name': '样本.未来'}],
    ]}
    raw = post if shape == 'direct' else {'future_locale' if shape == 'future-locale' else 'zh_cn': post}
    raw = {'post': raw} if shape == 'wrapped' else raw
    result = extract_quoted_content('post', raw)
    assert result.text == extract_feishu_content('post', raw)[0]
    assert code in result.text and '````python' in result.text
    assert result.coverage == 'complete' and result.unreadable_count == 0
    assert result.media == {'items': [{'image_key': 'img-one'}, {'file_key': 'file-one', 'file_name': '样本.未来'}]}


@pytest.mark.parametrize('schema', ['legacy', '2.0'])
def test_cards_visible_header_body_code_url_and_image(schema):
    elements = [
        {'tag': 'div', 'text': {'tag': 'lark_md', 'content': '正文 token: 公开示例'}},
        {'tag': 'markdown', 'content': '```python\nprint("可读代码")\n```'},
        {'tag': 'a', 'text': '仓库地址', 'href': 'https://example.invalid/repo'},
        {'tag': 'img', 'img_key': 'img-card', 'alt': {'content': '示意图'}},
    ]
    card = {'header': {'title': {'tag': 'plain_text', 'content': '标题'}}}
    card.update({'schema': '2.0', 'body': {'elements': elements}} if schema == '2.0' else {'elements': elements})
    result = extract_quoted_content('interactive', card)
    assert all(value in result.text for value in ['标题', '正文 token: 公开示例', 'print("可读代码")', '仓库地址', 'https://example.invalid/repo', '示意图'])
    assert result.media == {'items': [{'image_key': 'img-card'}]}
    assert result.coverage == 'visible_only' and result.unreadable_count == 0


def test_localized_card_display_containers_and_columns():
    result = extract_quoted_content('interactive', {'i18n_header': {'ja_jp': {'title': {'content': '見出し'}}},
        'i18n_elements': {'ja_jp': [{'tag': 'column_set', 'columns': [{'elements': [{'text': {'content': '本文'}}]}]}]}})
    assert '見出し' in result.text and '本文' in result.text
    assert result.coverage == 'visible_only'


@pytest.mark.parametrize('hidden', ['sender', 'config', 'value', 'behaviors', 'actions', 'params', 'credentials', 'metadata'])
def test_hidden_trees_do_not_leak_text_urls_or_resources(hidden):
    raw = {'elements': [{'text': {'content': '公开资料'}, hidden: {'text': 'PRIVATE-MARKER',
        'url': 'https://private.invalid', 'file_key': 'private-file', 'img_key': 'private-image'}}]}
    result = extract_quoted_content('interactive', raw)
    assert result.text == '公开资料' and result.media == {}
    assert 'PRIVATE-MARKER' not in result.text and 'private.invalid' not in result.text
    assert result.unreadable_count == (1 if hidden == 'actions' else 0)


def test_post_cannot_select_hidden_config_or_credentials_as_locale():
    hidden = {'content': [[{'tag': 'text', 'text': 'PRIVATE-MARKER', 'file_key': 'private-file'}]]}
    result = extract_quoted_content('post', {'credentials': hidden, 'config': hidden})
    assert result.text == '' and result.media == {} and result.coverage == 'unavailable'


@pytest.mark.parametrize('kind,raw', [
    ('share_user', {'user_id': 'opaque-person'}),
    ('share_chat', {'chat_id': 'opaque-chat'}),
    ('merge_forward', {'message_ids': ['opaque-child']}),
    ('future', {'credential_id': 'opaque-credential', 'sender': {'text': 'HIDDEN'}}),
    ('future', None), ('future', {}), ('image', {}), ('audio', {}), ('media', {}), ('post', {}),
])
def test_no_readable_material_does_not_promote_placeholder(kind, raw):
    result = extract_quoted_content(kind, raw)
    assert result.text == '' and result.media == {}
    assert result.coverage == 'unavailable' and result.unreadable_count >= 1


def test_merged_child_cannot_be_read_without_its_own_source_scope():
    result = extract_quoted_content('merge_forward', {'title': '转发摘要', 'content': [
        {'message_id': 'opaque-child', 'body': {'content': 'UNPROVEN-CHILD', 'image_key': 'private-image'}}]})
    assert result.text == '转发摘要' and result.media == {}
    assert result.coverage == 'visible_only' and result.unreadable_count == 1


@pytest.mark.parametrize('field', ['message_id', 'msg_type', 'message_type', 'upper_message_id'])
@pytest.mark.parametrize('shape', ['root-envelope', 'nested-envelope'])
def test_message_envelope_needs_its_own_scope_even_at_content_root(field, shape):
    envelope = {field: 'opaque-child-reference', 'body': {'content': {'text': 'UNPROVEN-CHILD', 'file_key': 'unproven-file'}}}
    raw = envelope if shape == 'root-envelope' else {'content': [envelope]}
    result = extract_quoted_content('merge_forward', raw)
    assert result.coverage == 'unavailable' and result.text == '' and result.media == {}
    assert result.unreadable_count >= 1


def test_envelope_field_words_inside_visible_text_do_not_trigger_scope_filter():
    text = 'message_type / upper_message_id / message_id 都是公开接口字段'
    result = extract_quoted_content('future', {'text': text})
    assert result.text == text and result.coverage == 'visible_only' and result.unreadable_count == 0


@pytest.mark.parametrize('field', ['message_id', 'msg_type', 'message_type', 'upper_message_id'])
def test_post_nodes_share_same_unproven_message_envelope_guard(field):
    node = {field: 'opaque-child', 'content': 'UNPROVEN-CHILD', 'file_key': 'unproven-file'}
    result = extract_quoted_content('post', {'content': [[node]]})
    assert result.text == '' and result.media == {} and result.coverage == 'unavailable'
    assert result.unreadable_count == 1


def test_post_body_envelope_cannot_promote_sibling_text_or_resource():
    node = {'message_type': 'file', 'body': {'content': 'opaque-content'},
        'text': 'UNPROVEN-SIBLING', 'file_key': 'unproven-file'}
    result = extract_quoted_content('post', {'content': [[node]]})
    assert result.text == '' and result.media == {} and result.coverage == 'unavailable'
    assert result.unreadable_count == 1


@pytest.mark.parametrize('wrapper', ['direct', 'post', 'locale'])
def test_post_root_or_locale_message_envelope_is_not_a_display_container(wrapper):
    envelope = {'message_id': 'opaque-child', 'content': [[{'tag': 'text', 'text': 'UNPROVEN-ROOT'}]]}
    raw = envelope if wrapper == 'direct' else {('post' if wrapper == 'post' else 'ja_jp'): envelope}
    result = extract_quoted_content('post', raw)
    assert result.text == '' and result.media == {} and result.coverage == 'unavailable'
    assert result.unreadable_count == 1


def test_id_only_post_metadata_is_not_mistaken_for_a_message_envelope():
    node = {'tag': 'future_node', 'message_id': 'metadata-only', 'text': '当前节点正文', 'file_key': 'parent-resource'}
    result = extract_quoted_content('post', {'content': [[node]]})
    assert result.text == '当前节点正文'
    assert result.media == {'items': [{'file_key': 'parent-resource'}]}
    assert result.coverage == 'visible_only' and result.unreadable_count == 0


@pytest.mark.parametrize('raw,expected', [
    ('未来类型直接正文 token 不删', '未来类型直接正文 token 不删'),
    ({'text': '未来直接正文'}, '未来直接正文'),
    ({'body': {'content': '未来显示树'}}, '未来显示树'),
    ('{"text":"未来 JSON 内正文"}', '未来 JSON 内正文'),
])
def test_open_world_visible_text_is_retained_without_claiming_complete(raw, expected):
    result = extract_quoted_content('future-open-world', raw)
    assert result.text == expected and result.coverage == 'visible_only' and result.unreadable_count == 0


def test_unknown_root_file_and_visible_images_are_open_world_and_deduplicated():
    result = extract_quoted_content('future', {'file_key': 'f-one', 'file_name': '新协议.未知', 'elements': [
        {'img_key': 'img-one'}, {'image_key': 'img-one'}, {'file_key': 'f-one'}]})
    assert result.media == {'items': [{'file_key': 'f-one', 'file_name': '新协议.未知'}, {'image_key': 'img-one'}]}
    assert result.coverage == 'visible_only'


@pytest.mark.parametrize('tag', [{}, [], {'content': 'HIDDEN-TAG'}, 7, None])
def test_malformed_tag_is_data_not_a_parser_crash_or_visible_metadata(tag):
    result = extract_quoted_content('interactive', {'elements': [{'tag': tag, 'text': '可见正文'}]})
    assert result.text == '可见正文' and result.media == {} and result.coverage == 'visible_only'


def test_projection_and_media_snapshot_are_immutable():
    raw = {'file_key': 'original', 'file_name': 'original.future'}
    result = extract_quoted_content('file', raw)
    raw['file_key'] = 'mutated-input'
    result.media['items'][0]['file_key'] = 'mutated-output'
    assert result.media['items'][0]['file_key'] == 'original'
    with pytest.raises(FrozenInstanceError):
        result.text = 'mutated'


def test_unknown_post_node_retains_visible_content_but_marks_partial():
    result = extract_quoted_content('post', {'content': [[{'tag': 'future_node', 'text': '未来节点可读文字'}, {'tag': 'opaque'}]]})
    assert result.text == '未来节点可读文字'
    assert result.coverage == 'visible_only' and result.unreadable_count == 1


@pytest.mark.parametrize('raw', ['{"credentials":{"text":"PRIVATE"}', '[{"text":"PRIVATE"}', '"PRIVATE'])
def test_malformed_json_body_is_not_dumped_as_quoted_text(raw):
    result = extract_quoted_content('future', raw)
    assert result.coverage == 'unavailable' and result.text == '' and result.media == {}


def test_depth_and_cycle_budget_marks_visible_material_partial(monkeypatch):
    monkeypatch.setattr(module, 'MAX_VISIBLE_DEPTH', 5)
    nested = {'content': 'not-reached'}
    for _ in range(20):
        nested = {'body': nested}
    cycle = {'text': 'cycle-visible'}
    cycle['body'] = cycle
    result = extract_quoted_content('future', {'title': '预算前材料', 'elements': [nested, cycle]})
    assert '预算前材料' in result.text and 'not-reached' not in result.text
    assert result.coverage == 'visible_only' and result.unreadable_count >= 2


def test_node_budget_does_not_claim_complete(monkeypatch):
    monkeypatch.setattr(module, 'MAX_VISIBLE_NODES', 12)
    result = extract_quoted_content('post', {'content': [[{'tag': 'text', 'text': str(i)}] for i in range(100)]})
    assert result.text and '99' not in result.text
    assert result.coverage == 'visible_only' and result.unreadable_count >= 1


def test_helper_does_not_add_second_character_truncation_authority():
    text = '可见 token 文本\n' * 3000
    result = extract_quoted_content('text', json.dumps({'text': text}))
    assert result.text == text and len(result.text) > 16000 and result.coverage == 'complete'
