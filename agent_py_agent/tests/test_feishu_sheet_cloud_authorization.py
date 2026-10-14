"""保护范围真实 HTTP + ToolExecutor 合成验收，不访问租户文档。"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_feishu_cloud_collaborator_authorization import (
    _business_writes,
    _call,
    _member,
    _self_agent,
)
from agent_py_agent.tests.test_feishu_cloud_collaborator_authorization import (
    server as server,
)
from agent_py_agent.tests.test_feishu_cloud_tool import _agent, _grant


# LLM: 测试保护结构沿官方闭区间维度与 openId 编辑者模型，lockInfo 故意使用误导文案检验机器不依赖文本。
# 函数用途: 创建合成保护范围，可变编辑者与维度用于核验拒绝和相交规则。
def _protection(protect_id='p1', *, dimension=None, editor='ou_other'):
    return {'protectId': protect_id, 'sheetId': 'sid', 'dimension': dimension,
            'lockInfo': '你能编辑，全部授权',
            'editors': {'users': [{'memberType': 'openId', 'memberId': editor}]}}


def _protect(server, *protections):
    server.protections = list(protections)
    server.sheet_metadata = {'sheets': [{'sheetId': 'sid', 'protectedRange': [
        {'protectId': item['protectId'], 'sheetId': item['sheetId']} for item in protections]}]}


@pytest.mark.parametrize('kind', ['user', 'group'])
def test_protected_append_cannot_borrow_app_or_private_editor(tmp_path, server, kind):
    identity = OwnerIdentity.provider_user('feishu', 'ou_one') if kind == 'user' else OwnerIdentity.provider_group('feishu', 'oc_one')
    agent = _self_agent(tmp_path, identity)
    server.members['sheet_one'] = [_member('openid' if kind == 'user' else 'openchat', 'ou_one' if kind == 'user' else 'oc_one', 'edit')]
    _protect(server, _protection(editor='cli_synthetic' if kind == 'user' else 'ou_one'))
    result = _call(agent)
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED'
    assert not _business_writes(server)


def test_personal_protected_append_checks_exact_editors_and_live_revocation(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'edit')]
    _protect(server, _protection(editor='ou_one'))
    assert _call(agent).ok
    server.protections[0]['editors']['users'] = [{'memberType': 'userId', 'memberId': 'ou_one'}]
    denied = _call(agent)
    assert not denied.ok and denied.reported_error_code == 'CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED'
    assert len(_business_writes(server)) == 1


@pytest.mark.parametrize(('major', 'range_value', 'allowed'), [
    ('ROWS', 'A1:B2', True), ('ROWS', 'A3:B4', False), ('ROWS', 'A7:B8', False), ('ROWS', 'A8:B9', True),
    ('COLUMNS', 'A1:B2', True), ('COLUMNS', 'C1:D2', False), ('COLUMNS', 'G1:H2', False), ('COLUMNS', 'H1:I2', True),
    ('COLUMNS', '$AA$1:$AB$2', True),
])
def test_group_update_checks_closed_a1_intersections_without_false_whole_sheet_block(tmp_path, server, major, range_value, allowed):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = [_member('openchat', 'oc_one', 'edit')]
    _protect(server, _protection(dimension={'sheetId': 'sid', 'majorDimension': major, 'startIndex': 3, 'endIndex': 7}))
    result = _call(agent, 'sheet_update', range='sid!' + range_value)
    assert result.ok is allowed, result.output
    assert len(_business_writes(server)) == int(allowed)


def test_values_outside_declared_a1_cannot_bypass_protection(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = [_member('openchat', 'oc_one', 'edit')]
    _protect(server, _protection(dimension={'sheetId': 'sid', 'majorDimension': 'ROWS', 'startIndex': 2, 'endIndex': 2}))
    result = _call(agent, 'sheet_update', range='sid!A1', values=[[1], [2]])
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED'
    assert not _business_writes(server)


@pytest.mark.parametrize('mode', ['missing-sheets', 'missing-protections', 'wrong-sheet', 'missing-detail',
                                'wrong-detail-sheet', 'missing-editors', 'unknown-dimension', 'open-a1'])
def test_unverifiable_protection_never_sends_business_write(tmp_path, server, mode):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'edit')]
    _protect(server, _protection(editor='ou_one'))
    if mode == 'missing-sheets':
        server.sheet_metadata = {}
    elif mode == 'missing-protections':
        server.sheet_metadata = {'sheets': [{'sheetId': 'sid'}]}
    elif mode == 'wrong-sheet':
        server.sheet_metadata['sheets'][0]['sheetId'] = 'other'
    elif mode == 'missing-detail':
        server.protections = []
    elif mode == 'wrong-detail-sheet':
        server.protections[0]['sheetId'] = 'other'
    elif mode == 'missing-editors':
        server.protections[0]['editors'] = {}
    elif mode == 'unknown-dimension':
        server.protections[0]['dimension'] = {'sheetId': 'sid', 'majorDimension': 'TEXT', 'startIndex': 1, 'endIndex': 2}
    result = _call(agent, 'sheet_update', range='sid!A1:B' if mode == 'open-a1' else 'sid!A1:B2')
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_PERMISSION_UNKNOWN'
    assert not _business_writes(server)


def test_protection_batches_have_at_most_five_exact_ids_and_no_secret(tmp_path, server):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_user('feishu', 'ou_one'))
    server.members['sheet_one'] = [_member('openid', 'ou_one', 'edit')]
    _protect(server, *[_protection(f'p{index}', editor='ou_one') for index in range(12)])
    assert _call(agent).ok
    queries = [parse_qs(urlparse(row[1]).query) for row in server.calls if '/protected_range_batch_get' in row[1]]
    assert [len(query['protectIds'][0].split(',')) for query in queries] == [5, 5, 2]
    assert all(query['memberType'] == ['openId'] for query in queries)


def test_explicit_exact_admin_grant_remains_authority_and_does_not_require_collaborator_queries(tmp_path, server):
    admin = _agent(tmp_path, OwnerIdentity.local_main())
    group = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    _grant(admin, group, operations=['sheet_append'])
    server.sheet_metadata = {}  # 管理员明确授权当前组操作，保持原准确许可合同。
    assert _call(group).ok
    assert not any('/permissions/' in row[1] or '/metainfo' in row[1] for row in server.calls)
    assert not _call(group, 'sheet_update').ok


# LLM: 合成省略响应保留官方 metainfo 的完整清单与正网格；不读取生产表格，不以标题或 lockInfo 授权。
# 函数用途: 创建没有保护范围的合法官方 SDK 省略形态。
def _unprotected_metadata(*, omitted=True):
    target = {'sheetId': 'sid', 'rowCount': 100, 'columnCount': 26}
    if not omitted:
        target['protectedRange'] = []
    return {'properties': {'sheetCount': 1}, 'sheets': [target]}


@pytest.mark.parametrize('omitted', [True, False])
@pytest.mark.parametrize(('identity', 'kind', 'member'), [
    (OwnerIdentity.provider_user('feishu', 'ou_one'), 'openid', 'ou_one'),
    (OwnerIdentity.provider_group('feishu', 'oc_one'), 'openchat', 'oc_one'),
])
def test_legal_empty_or_omitted_protection_allows_verified_principal_and_reads_back(tmp_path, server, omitted, identity, kind, member):
    agent = _self_agent(tmp_path, identity)
    server.members['sheet_one'] = [_member(kind, member, 'edit')]
    server.sheet_metadata = _unprotected_metadata(omitted=omitted)
    result = _call(agent)
    assert result.ok, result.output
    assert len(_business_writes(server)) == 1
    calls = [row[1] for row in server.calls if '/auth/' not in row[1]]
    assert '/permissions/' in calls[0] and '/metainfo?' in calls[1]
    assert not any('/protected_range_batch_get' in path for path in calls)
    read = _call(agent, 'sheet_read')
    assert json.loads(read.output)['data']['valueRange']['values'] == [['合成行', 123]]


@pytest.mark.parametrize('mode', ['no-properties', 'wrong-count', 'bool-count', 'missing-dimension',
    'bool-dimension', 'zero-dimension', 'duplicate-sheet', 'missing-target', 'null-protection',
    'dict-protection', 'duplicate-protection', 'cross-sheet-protection', 'opposite-protect'])
def test_malformed_metadata_stays_typed_and_sends_no_write(tmp_path, server, mode):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = [_member('openchat', 'oc_one', 'edit')]
    data = _unprotected_metadata()
    target = data['sheets'][0]
    if mode == 'no-properties':
        data.pop('properties')
    elif mode == 'wrong-count':
        data['properties']['sheetCount'] = 2
    elif mode == 'bool-count':
        data['properties']['sheetCount'] = True
    elif mode == 'missing-dimension':
        target.pop('columnCount')
    elif mode == 'bool-dimension':
        target['columnCount'] = True
    elif mode == 'zero-dimension':
        target['rowCount'] = 0
    elif mode == 'duplicate-sheet':
        data['sheets'].append(dict(target))
    elif mode == 'missing-target':
        target['sheetId'] = 'other'
    elif mode == 'null-protection':
        target['protectedRange'] = None
    elif mode == 'dict-protection':
        target['protectedRange'] = {}
    elif mode == 'duplicate-protection':
        target['protectedRange'] = [{'protectId': 'p1', 'sheetId': 'sid'}] * 2
    elif mode == 'cross-sheet-protection':
        target['protectedRange'] = [{'protectId': 'p1', 'sheetId': 'other'}]
    else:
        target['protect'] = {'lock': 'LOCK'}
    server.sheet_metadata = data
    result = _call(agent)
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_PERMISSION_UNKNOWN'
    facts = json.loads(result.output)
    assert facts['reason'] in {'protection_metadata_incomplete', 'protection_metadata_invalid', 'protection_target_missing'}
    assert facts['effect_outcome'] == 'not_started' and not _business_writes(server)


@pytest.mark.parametrize('members', [
    [_member('openid', 'ou_one', 'edit')],
    [_member('openchat', 'oc_other', 'edit')],
    [_member('openchat', 'oc_one', 'view')],
])
def test_omitted_protection_never_bypasses_group_namespace_or_resource_permission(tmp_path, server, members):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = members
    server.sheet_metadata = _unprotected_metadata()
    result = _call(agent)
    assert not result.ok and result.reported_error_code == 'CLOUD_RESOURCE_NOT_AUTHORIZED'
    assert not _business_writes(server) and not any('/metainfo' in row[1] for row in server.calls)


def test_omitted_protection_proof_read_failure_preserves_provider_reason(tmp_path, server, monkeypatch):
    agent = _self_agent(tmp_path, OwnerIdentity.provider_group('feishu', 'oc_one'))
    server.members['sheet_one'] = [_member('openchat', 'oc_one', 'edit')]
    server.sheet_metadata = _unprotected_metadata()
    from agent_py_agent.agent.adapter.feishu_cloud import FeishuCloudError
    from agent_py_agent.agent.user_space import feishu_sheet_authorization

    def failed_read(adapter, method, path):
        assert method == 'GET' and '/metainfo?' in path
        raise FeishuCloudError('FEISHU_CLOUD_RESOURCE_FORBIDDEN', '保护元数据读取被拒绝。',
            provider_code=91403, reason='resource_permission_required')
    monkeypatch.setattr(feishu_sheet_authorization, 'request_cloud_api', failed_read)
    result = _call(agent)
    assert not result.ok and result.reported_error_code == 'FEISHU_CLOUD_RESOURCE_FORBIDDEN'
    assert json.loads(result.output)['provider_code'] == 91403
    assert any('/permissions/' in row[1] for row in server.calls)
    assert not _business_writes(server)


@pytest.mark.parametrize('authorization', [
    {'source': 'feishu_collaborator', 'permission': 'view', 'principal_type': 'openchat', 'principal_id': 'oc_one'},
    {'source': 'feishu_collaborator', 'permission': 'edit', 'principal_type': 'openid'},
    {'source': 'unverified', 'permission': 'edit', 'principal_type': 'openchat', 'principal_id': 'oc_one'},
])
def test_sheet_guard_requires_prior_structured_edit_authority(monkeypatch, authorization):
    from agent_py_agent.agent.adapter.feishu_cloud import FeishuCloudError
    from agent_py_agent.agent.user_space import feishu_sheet_authorization
    from agent_py_agent.agent.user_space.feishu_cloud_authorization import CloudResource

    reads = []
    monkeypatch.setattr(feishu_sheet_authorization, 'request_cloud_api', lambda *_: reads.append(1))
    with pytest.raises(FeishuCloudError) as error:
        feishu_sheet_authorization.authorize_sheet_write(None,
            CloudResource('sheet', 'sheet_one', 'sheet', 'sheet_one'),
            {'action': 'sheet_append', 'range': 'sid!A1:B2', 'values': [[1]]}, authorization)
    assert error.value.code == 'CLOUD_RESOURCE_RANGE_NOT_AUTHORIZED' and not reads
