"""同次已验证引用响应的转发成员闭包，不解析正文或把来源身份变成权限。"""
from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from agent_py_agent.agent.adapter.feishu_quoted_media import FeishuQuotedMediaError
from agent_py_agent.agent.adapter.feishu_quoted_members import validated_quote_members


# LLM: 合成关系记录只为检验消息来源图；不同 chat/sender 不参与授权，正文标记不能影响选择。
# 函数用途: 创建带准确编号、上级编号与撤回状态的供应商消息，避免自然语言决定关系。
def _message(message_id, upper=None, *, deleted=False, kind='text', chat='oc_source'):
    row = {'message_id': message_id, 'msg_type': kind, 'chat_id': chat,
           'deleted': deleted, 'sender': {'id': 'ou_original'},
           'body': {'content': {'text': f'SYNTHETIC_{message_id}'}}}
    if upper is not None:
        row['upper_message_id'] = upper
    return row


# LLM: root 是调用方已验证的准确 parent；本测试不把这个 helper 当作同群授权或 HTTP 入口。
# 函数用途: 按原成功 API shape 包装同次响应，成员顺序可与父子关系不同。
def _response(root, *members):
    return {'code': 0, 'data': {'items': [root, *members]}}


def test_non_merge_keeps_only_exact_root_and_does_not_expand_body_ids():
    root = _message('root')
    root['body']['content']['message_ids'] = ['child']
    result = validated_quote_members(_response(root, _message('child', 'root')), root)
    assert result.members == (root,)
    assert result.unreadable_count == 0


def test_merge_selects_root_child_and_grandchild_from_same_response_in_parent_order():
    root = _message('root', kind='merge_forward', chat='oc_current')
    child = _message('child', 'root', chat='oc_original_private')
    grand = _message('grand', 'child', chat='oc_other_source')
    response = {'code': 0, 'data': {'items': [grand, child, root]}}
    before = deepcopy(response)
    result = validated_quote_members(response, root)
    assert [row['message_id'] for row in result.members] == ['root', 'child', 'grand']
    assert result.members[1]['chat_id'] == 'oc_original_private'
    assert result.unreadable_count == 0 and response == before


def test_unrelated_items_missing_ancestors_and_disconnected_cycles_are_not_guessed():
    root = _message('root', kind='merge_forward')
    response = _response(root, _message('own', 'root'), _message('foreign', 'other-root'),
                         _message('orphan', 'absent'), _message('a', 'b'), _message('b', 'a'))
    result = validated_quote_members(response, root)
    assert [row['message_id'] for row in result.members] == ['root', 'own']
    assert result.unreadable_count == 0
    assert 'SYNTHETIC_foreign' not in repr(result.members)


@pytest.mark.parametrize('deleted', [True, None, 0, 'false', 'unknown'])
def test_unreadable_ancestor_blocks_its_descendants_but_counts_proven_members(deleted):
    root = _message('root', kind='merge_forward')
    response = _response(root, _message('bad', 'root', deleted=deleted),
                         _message('grand', 'bad'), _message('good', 'root'))
    result = validated_quote_members(response, root)
    assert [row['message_id'] for row in result.members] == ['root', 'good']
    assert result.unreadable_count == 2


@pytest.mark.parametrize('kind', ['text', 'merge_forward'])
def test_duplicate_root_is_rejected_even_when_second_row_claims_another_chat(kind):
    root = _message('root', kind=kind)
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_ID_MISMATCH'):
        validated_quote_members(_response(root, _message('root', 'other', chat='oc_other')), root)


def test_duplicate_related_id_is_rejected_without_selecting_one_variant():
    root = _message('root', kind='merge_forward')
    response = _response(root, _message('child', 'root'), _message('child', 'foreign'),
                         _message('grand', 'child'))
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_ID_MISMATCH'):
        validated_quote_members(response, root)


def test_unrelated_duplicate_ids_do_not_pollute_the_valid_closure():
    root = _message('root', kind='merge_forward')
    result = validated_quote_members(_response(root, _message('own', 'root'),
        _message('foreign', 'other-root'), _message('foreign', 'elsewhere')), root)
    assert [row['message_id'] for row in result.members] == ['root', 'own']
    assert result.unreadable_count == 0


@pytest.mark.parametrize('children,unreadable', [(63, 0), (64, 1), (70, 7)])
def test_member_limit_includes_root_and_reports_only_proven_overflow(children, unreadable):
    root = _message('root', kind='merge_forward')
    rows = [_message(f'child-{i}', 'root') for i in range(children)]
    result = validated_quote_members(_response(root, *rows), root)
    assert len(result.members) == 64 and result.members[0]['message_id'] == 'root'
    assert result.unreadable_count == unreadable


def test_depth_limit_is_sixteen_upper_edges_and_does_not_restart_at_a_descendant():
    root = _message('root', kind='merge_forward')
    rows = [_message(f'child-{i}', 'root' if i == 1 else f'child-{i - 1}') for i in range(1, 21)]
    result = validated_quote_members(_response(root, *reversed(rows)), root)
    assert [row['message_id'] for row in result.members] == ['root', *[f'child-{i}' for i in range(1, 17)]]
    assert result.unreadable_count == 4


def test_related_duplicate_beyond_output_limit_still_fails_closed():
    root = _message('root', kind='merge_forward')
    rows = [_message(f'child-{i}', 'root') for i in range(70)]
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_ID_MISMATCH'):
        validated_quote_members(_response(root, *rows, _message('child-69', 'foreign')), root)


@pytest.mark.parametrize('message_id', ['', None, 123, ['child']])
def test_related_record_without_an_exact_id_is_unreadable_and_not_a_new_anchor(message_id):
    root = _message('root', kind='merge_forward')
    response = _response(root, _message(message_id, 'root'), _message('grand', '123'))
    result = validated_quote_members(response, root)
    assert result.members == (root,) and result.unreadable_count == 1


def test_selected_records_are_detached_copies_and_result_is_frozen():
    root = _message('root', kind='merge_forward')
    child = _message('child', 'root')
    result = validated_quote_members(_response(root, child), root)
    child['body']['content']['text'] = 'changed input'
    assert result.members[1]['body']['content']['text'] == 'SYNTHETIC_child'
    result.members[0]['body']['content']['text'] = 'changed output'
    assert root['body']['content']['text'] == 'SYNTHETIC_root'
    with pytest.raises(FrozenInstanceError):
        result.unreadable_count = 99


@pytest.mark.parametrize('response', [None, {}, {'code': True}, {'code': 0, 'data': {}},
                                    {'code': 0, 'data': {'items': 'invalid'}}])
def test_invalid_success_response_does_not_create_a_source(response):
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_INVALID'):
        validated_quote_members(response, _message('root', kind='merge_forward'))


def test_absent_root_cannot_be_recreated_from_the_caller_argument():
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_ID_MISMATCH'):
        validated_quote_members({'code': 0, 'data': {'items': [_message('foreign')]}}, _message('root'))


@pytest.mark.parametrize('changes', [{'chat_id': 'oc_forged'}, {'msg_type': 'text'}, {'deleted': True}])
def test_caller_argument_cannot_rewrite_the_same_response_root(changes):
    root = _message('root', kind='merge_forward')
    claimed = {**root, **changes}
    with pytest.raises(FeishuQuotedMediaError, match='FEISHU_QUOTED_MEDIA_INVALID'):
        validated_quote_members(_response(root, _message('child', 'root')), claimed)


@pytest.mark.parametrize('deleted,code', [(True, 'FEISHU_QUOTED_MEDIA_DELETED'),
                                       (None, 'FEISHU_QUOTED_MEDIA_INVALID')])
def test_invalid_root_never_becomes_an_independent_authorization(deleted, code):
    root = _message('root', deleted=deleted, kind='merge_forward')
    with pytest.raises(FeishuQuotedMediaError, match=code):
        validated_quote_members(_response(root, _message('child', 'root')), root)
