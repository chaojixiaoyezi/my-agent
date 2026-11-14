"""W12 #103：真实 owner 存储、F8 分页、原产物读写与 native 入口的定向验收。"""
from __future__ import annotations

import ast
import csv
import hashlib
import html
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces import v13_w12_reporting as reporting
from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.conversation.business_access import BusinessError
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.tests.test_mega_business_api_actions import native_request
from agent_py_agent.tests.test_v13_w0_harness import v13_call
from agent_py_agent.tests.test_v13_w0_harness import v13_host as v13_host
from agent_py_agent.tests.test_v13_w0_harness import v13_native as v13_native


# LLM: 注入固定 UTC 时钟，测试结果与执行机器的本地时区无关。
# 函数用途: 将测试时间转成 epoch 秒。
def _epoch(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# LLM: 所有测试使用发行的 BusinessCallContext，不用字典伪造身份。
# 函数用途: 创建调用真实领域函数的 F10 对象。
def _call(host, action, params, *, subject='analyst-a', key=None, revision=None, now='2026-11-03T12:00:00Z'):
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    if not host.home_paths.owner_id:
        host.home_paths = home_paths_with_owner(host.home_paths, resolve_owner_home(host.home_paths.root))
    context = api.bind_business_context(host, authorize=lambda action, facts: True, subject_id=subject)
    return V13Call(action, params, 'reporting-test', context.owner_id, subject, context,
        api.BusinessServices(host), Path(host.home_paths.owner_home_dir),
        key or 'reporting-' + uuid4().hex, revision, lambda: _epoch(now))


# LLM: 记录经原 V13Store 提交，测试夹具不替业务函数修改结果。
# 函数用途: 向指定 owner 的真实集合种入合成记录。
def _seed(call, collection, records):
    store = V13Store(call.owner_home, owner_id=call.owner_id)

    # LLM: 夹具显式提供主键，避免根据字段名猜测归属。
    # 函数用途: 原子写入测试记录。
    def mutate(rows):
        rows.update(records)
        return {'count': len(records)}

    return store.transaction(collection, mutate, idempotency_key='seed-' + uuid4().hex, params_digest=collection)


# LLM: 合成告警仍完整遵循 W8 冻结字段，不把 UI 假响应当作集成。
# 函数用途: 创建一个可供真实指标读取的告警。
def _alert(call, identity, created, *, status='resolved', source='monitor'):
    at = _epoch(created)
    return {'alert_id': identity, 'owner_id': call.owner_id, 'space_id': 'space-test',
        'title': '告警', 'description': '', 'severity': 'critical', 'status': status,
        'verdict': 'true_positive' if status == 'resolved' else None, 'ai_confidence': None,
        'disposition': None, 'rule_id': 'rule-test', 'source': source, 'external_ref': None,
        'entities': [{'type': 'domain', 'value': '<img src=x onerror="bad">'}], 'techniques': [],
        'assignee': 'analyst-a', 'case_id': None, 'tags': [], 'evidence_refs': [],
        'acknowledged_at': reporting._timestamp(at + 120) if status == 'resolved' else None,
        'resolved_at': reporting._timestamp(at + 3600) if status == 'resolved' else None,
        'revision': 1, 'created_at': created, 'updated_at': created}


# LLM: 案件样本关联使用结构化 alert_ids，不按标题识别。
# 函数用途: 创建一个完整的 W8 案件记录。
def _case(call, identity, created, *, alert_ids=None):
    return {'case_id': identity, 'owner_id': call.owner_id, 'space_id': 'space-test',
        'title': '<script>case & "value"</script>', 'description': '', 'severity': 'high',
        'status': 'closed', 'assignee': 'analyst-a', 'alert_ids': alert_ids or [], 'merged_into': None,
        'tags': [], 'custom_values': {}, 'closed_at': reporting._timestamp(_epoch(created) + 7200),
        'revision': 1, 'created_at': created, 'updated_at': created}


# LLM: 先种入正常、样本、边界和明显异常记录，聚合必须剔除后再统计。
# 函数用途: 准备前后两周的真实集合输入。
def _seed_weekly(call):
    rows = {
        'current': _alert(call, 'current', '2026-10-26T10:00:00Z'),
        'open': _alert(call, 'open', '2026-11-01T10:00:00Z', status='new'),
        'previous': _alert(call, 'previous', '2026-10-20T10:00:00Z'),
        'boundary': _alert(call, 'boundary', '2026-11-02T00:00:00Z'),
        'sample': _alert(call, 'sample', '2026-10-28T10:00:00Z', source='sample'),
        'invalid': _alert(call, 'invalid', '2026-10-28T10:00:00Z'),
    }
    rows['current']['entities'].append(dict(rows['current']['entities'][0]))
    rows['invalid']['acknowledged_at'] = '2026-10-27T10:00:00Z'
    _seed(call, 'soc_alerts', rows)
    _seed(call, 'soc_cases', {
        'case-current': _case(call, 'case-current', '2026-10-27T10:00:00Z'),
        'case-previous': _case(call, 'case-previous', '2026-10-21T10:00:00Z'),
        'case-sample': _case(call, 'case-sample', '2026-10-28T10:00:00Z', alert_ids=['sample'])})
    _seed(call, 'monitor_rules', {'rule-test': {'rule_id': 'rule-test', 'owner_id': call.owner_id,
        'space_id': 'space-test', 'name': '<svg onload="bad"> & rule', 'description': '', 'enabled': True,
        'severity': 'critical', 'query': {'language': 'sql', 'text': 'SELECT 1'}, 'schedule': None,
        'script_id': None, 'skill_ids': [], 'datasource_ids': [], 'mitre_techniques': [], 'tags': [],
        'exclusions': [], 'origin': 'manual', 'scheduler_job_id': None, 'last_run': None,
        'revision': 1, 'created_at': '2026-10-01T00:00:00Z', 'updated_at': '2026-10-01T00:00:00Z'}})


# LLM: 不只检查 HTML 存在；核对默认整周、同口径 KPI、样本/坏记录剔除和所有动态值转义。
# 函数用途: 验证周报 JSON 和可打印文档。
def test_weekly_digest_json_and_printable_html_escape_all_values(v13_host):
    from agent_py_agent.agent.business_spaces.v13_w12_metrics import dashboard

    call = _call(v13_host, 'reports.weekly_digest', {})
    _seed_weekly(call)
    result = reporting.weekly_digest(call)
    assert result['period'] == {'start': '2026-10-26T00:00:00+00:00', 'end': '2026-11-02T00:00:00+00:00'}
    assert result['html'] is None
    digest = result['digest']
    assert digest['kpis'] == dashboard(replace(call, params={'window': '7d'},
        clock=lambda: _epoch('2026-11-02T00:00:00Z')))['kpis']
    assert digest['previous_kpis'] == dashboard(replace(call, params={'window': '7d'},
        clock=lambda: _epoch('2026-10-26T00:00:00Z')))['kpis']
    assert digest['kpis']['alerts_total'] == 2 and digest['previous_kpis']['alerts_total'] == 1
    assert digest['kpis']['mttd_seconds'] == 120 and digest['kpis']['mttr_seconds'] == 3600
    assert [row['case_id'] for row in digest['notable_cases']] == ['case-current']
    assert digest['top_entities'][0]['count'] == 2
    assert digest['usage'] is None and 'usage' in digest['unknown'] and 'soc_records' in digest['unknown']
    rendered = reporting.weekly_digest(replace(call, params={'format': 'html'}))['html']
    assert rendered.startswith('<!doctype html>') and '@media print' in rendered
    for value in ('<script>case & "value"</script>', '<svg onload="bad"> & rule', '<img src=x onerror="bad">'):
        assert value not in rendered and html.escape(value, quote=True) in rendered
    assert '<script' not in rendered.lower()
    key = '<script>key</script>'
    assert html.escape(key) in reporting._html_value({key: key})


# LLM: 错误日期不得被静默当默认周；空来源也不能捏造耗时或用量。
# 函数用途: 验证非法日期与空周语义。
def test_weekly_invalid_date_and_empty_window(v13_host):
    call = _call(v13_host, 'reports.weekly_digest', {'week_start': '2026-02-30'})
    with pytest.raises(HostAPIError) as error:
        reporting.weekly_digest(call)
    assert error.value.code == 'INVALID_SCHEMA'
    result = reporting.weekly_digest(replace(call, params={'week_start': '2026-10-12'}))
    assert result['digest']['kpis']['alerts_total'] == 0
    assert result['digest']['kpis']['mttd_seconds'] is None and result['digest']['usage'] is None


# LLM: 固定样例参数直接调用真实 owner 存储，不返回手写空成功。
# 函数用途: 验证 saved_views.list 的原文样例。
def test_saved_views_list_example_params(v13_host):
    assert reporting.views_list(_call(v13_host, 'saved_views.list', {'surface': 'alerts'})) == {'items': []}


# LLM: 所有拒绝都检查实际结构化码，失败后仍检查存储未改变。
# 函数用途: 验证 CRUD、集合 CAS、回执冲突和单一默认值。
def test_saved_views_crud_cas_and_single_default(v13_host):
    first_call = _call(v13_host, 'saved_views.create',
        {'surface': 'alerts', 'name': '第一视图', 'filters': {'severity': 'high'}, 'is_default': True})
    first = reporting.views_create(first_call)['view']
    second = reporting.views_create(replace(first_call, idempotency_key='second-' + uuid4().hex,
        params={**first_call.params, 'name': '第二视图'}))['view']
    listed = reporting.views_list(_call(v13_host, 'saved_views.list', {'surface': 'alerts'}))['items']
    assert [row['view_id'] for row in listed if row['is_default']] == [second['view_id']]
    assert reporting.views_create(first_call)['view'] == first
    update_call = _call(v13_host, 'saved_views.update', {'view_id': first['view_id'], 'is_default': True,
        'shared': True, 'sort': {'at': 'desc'}}, revision=second['revision'])
    updated = reporting.views_update(update_call)['view']
    assert updated['revision'] > second['revision']
    before = V13Store(first_call.owner_home, owner_id=first_call.owner_id).list('saved_views')
    for modified, code in (
        (replace(update_call, idempotency_key='stale-' + uuid4().hex), 'REVISION_CONFLICT'),
        (replace(update_call, idempotency_key='rename-' + uuid4().hex, expected_revision=updated['revision'],
            params={'view_id': first['view_id'], 'name': '第二视图', 'is_default': False}), 'STATE_CONFLICT'),
        (replace(first_call, params={**first_call.params, 'name': '其它参数'}), 'IDEMPOTENCY_CONFLICT'),
        (replace(update_call, expected_revision=updated['revision']), 'IDEMPOTENCY_CONFLICT'),
    ):
        with pytest.raises((HostAPIError, BusinessError)) as error:
            reporting.views_update(modified) if modified.action == 'saved_views.update' else reporting.views_create(modified)
        assert error.value.code == code
    assert V13Store(first_call.owner_home, owner_id=first_call.owner_id).list('saved_views') == before
    delete_call = _call(v13_host, 'saved_views.delete', {'view_id': first['view_id']}, revision=updated['revision'])
    deleted = reporting.views_delete(delete_call)
    assert deleted == {'view_id': first['view_id'], 'deleted': True}
    assert reporting.views_delete(delete_call) == deleted
    assert reporting.views_update(update_call)['view'] == updated
    assert reporting.views_create(first_call)['view'] == first
    assert [row['view_id'] for row in reporting.views_list(
        _call(v13_host, 'saved_views.list', {'surface': 'alerts'}))['items']] == [second['view_id']]


# LLM: 同租户共享只读和跨租户隔离分别验证，不能从单一身份成功外推权限。
# 函数用途: 验证 creator 写隔离、共享可见性和 owner 边界。
def test_saved_views_creator_shared_owner_isolation(v13_host, tmp_path):
    from agent_py_agent.agent.user_space.home_layout import home_paths

    private = reporting.views_create(_call(v13_host, 'saved_views.create',
        {'surface': 'alerts', 'name': '私有', 'filters': {}, 'is_default': True}))['view']
    shared = reporting.views_create(_call(v13_host, 'saved_views.create',
        {'surface': 'alerts', 'name': '共享', 'filters': {}, 'shared': True}))['view']
    bob = _call(v13_host, 'saved_views.list', {'surface': 'alerts'}, subject='analyst-b')
    assert [row['view_id'] for row in reporting.views_list(bob)['items']] == [shared['view_id']]
    bob_view = reporting.views_create(_call(v13_host, 'saved_views.create',
        {'surface': 'alerts', 'name': '私有', 'filters': {}, 'is_default': True}, subject='analyst-b'))['view']
    own = reporting.views_list(_call(v13_host, 'saved_views.list', {'surface': 'alerts'}))['items']
    assert [row['view_id'] for row in own if row['is_default']] == [private['view_id']]
    assert bob_view['view_id'] not in [row['view_id'] for row in own]
    for action, handler in (('saved_views.update', reporting.views_update), ('saved_views.delete', reporting.views_delete)):
        with pytest.raises(HostAPIError) as denied:
            handler(_call(v13_host, action, {'view_id': shared['view_id']}, subject='analyst-b', revision=999))
        assert (denied.value.status, denied.value.code) == (404, 'RESOURCE_NOT_FOUND')
    other = SimpleNamespace(home_paths=home_paths(tmp_path / 'different-owner'), config=v13_host.config)
    assert reporting.views_list(_call(other, 'saved_views.list', {'surface': 'alerts'}))['items'] == []
    with pytest.raises(HostAPIError) as missing:
        reporting.views_delete(_call(other, 'saved_views.delete', {'view_id': private['view_id']}, revision=0))
    assert missing.value.code == 'RESOURCE_NOT_FOUND'


# LLM: 竞争写入通过真实 F2 目录锁交错，不用单线程顺序伪称并发安全。
# 函数用途: 验证并发重名、幂等与唯一默认不变量。
def test_saved_views_concurrent_name_idempotency_and_defaults(v13_host):
    first = _call(v13_host, 'saved_views.create',
        {'surface': 'alerts', 'name': '并发名称', 'filters': {}, 'is_default': True})

    # LLM: 返回结构化拒绝码便于比较，不吞未知异常或放宽断言。
    # 函数用途: 在线程池中执行一次真实事务。
    def execute(call):
        try:
            return reporting.views_create(call)
        except (HostAPIError, BusinessError) as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        same = list(pool.map(execute, [first, first]))
    assert same[0] == same[1] and isinstance(same[0], dict)
    with ThreadPoolExecutor(max_workers=2) as pool:
        conflicts = list(pool.map(execute, [replace(first, idempotency_key='conflict-a-' + uuid4().hex),
            replace(first, idempotency_key='conflict-b-' + uuid4().hex)]))
    assert conflicts == ['STATE_CONFLICT', 'STATE_CONFLICT']
    with ThreadPoolExecutor(max_workers=2) as pool:
        defaults = list(pool.map(execute, [replace(first, idempotency_key='default-a-' + uuid4().hex,
            params={**first.params, 'name': '默认 A'}), replace(first, idempotency_key='default-b-' + uuid4().hex,
            params={**first.params, 'name': '默认 B'})]))
    assert all(isinstance(row, dict) for row in defaults)
    rows = reporting.views_list(_call(v13_host, 'saved_views.list', {'surface': 'alerts'}))['items']
    assert len(rows) == 3 and sum(row['is_default'] for row in rows) == 1


# LLM: 大体积源夹具写的是 F8 权威集合的精确形状，不用替代 reader 生成业务响应。
# 函数用途: 种入可分页的合成审计来源。
def _seed_audit(call, count, *, target_positions=(), actor='analyst-a'):
    records = {}
    end = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    for index in range(count):
        identity = 'entry-' + str(index).zfill(6)
        at = (end - timedelta(seconds=index)).isoformat()
        records[identity] = {'entry_id': identity, 'owner_id': call.owner_id, 'at': at, 'subject_id': actor,
            'action': 'target.action' if index in target_positions else 'other.action', 'collection': 'saved_views',
            'record_ids': [], 'outcome': 'committed', 'revision': index + 1, 'created_at': at, 'updated_at': at}
    state = {'schema_version': 'business-v13.v1', 'owner_id': call.owner_id, 'collection': 'audit_log',
        'revision': count, 'records': records, 'receipts': {}}
    path = call.owner_home / 'data' / 'business_v13' / 'audit_log.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    return records


# LLM: 比较直接 F8 的原始结果，并验证 host 权限不受伪造角色影响。
# 函数用途: 验证 owner 审计、native admin 和 closed unavailable 投影。
def test_audit_list_owner_scope_and_host_scope_admin_only(v13_host):
    call = _call(v13_host, 'audit.list', {})
    _seed_audit(call, 3, target_positions=(1,))
    result = reporting.audit_list(call)
    original = reporting.audit_records(call.owner_home, call.owner_id, limit=50, host=call.context.host,
        admin_context=call)
    assert result['records'] == original['records']
    assert all(isinstance(item, str) for item in result['unavailable'])
    v13_host.business_native_admin = lambda: False
    v13_host.roles = ['admin']
    with pytest.raises(HostAPIError) as denied:
        reporting.audit_list(replace(call, params={'scope': 'host'}))
    assert (denied.value.status, denied.value.code) == (403, 'BUSINESS_ACCESS_DENIED')
    v13_host.business_native_admin = lambda: True
    assert reporting.audit_list(replace(call, params={'scope': 'host'}))['records'] == result['records']


# LLM: 匹配位于多页之后和扫描预算之外；最终顺序必须无遗漏、无重复。
# 函数用途: 验证前缀筛选、扫描界限以及篡改/跨条件游标失效。
def test_audit_prefix_paging_is_bounded_without_dropping_late_matches(v13_host):
    call = _call(v13_host, 'audit.list', {'action_prefix': 'target.', 'limit': 1})
    _seed_audit(call, 2007, target_positions=(3, 2006))
    first = reporting.audit_list(call)
    assert [row['record_id'] for row in first['records']] == ['entry-000003']
    cursor = first['next_cursor']
    assert cursor is not None and len(cursor) <= 2048
    for changed in ({'action_prefix': 'other.'}, {'actor': 'analyst-b'}, {'since': '2026-10-01T00:00:00Z'}):
        with pytest.raises(HostAPIError) as expired:
            reporting.audit_list(replace(call, params={**call.params, 'cursor': cursor, **changed}))
        assert expired.value.code == 'CURSOR_EXPIRED'
    with pytest.raises(HostAPIError) as tampered:
        reporting.audit_list(replace(call, params={**call.params, 'cursor': cursor[:-4] + 'AAAA'}))
    assert tampered.value.code == 'CURSOR_EXPIRED'
    second = reporting.audit_list(replace(call, params={**call.params, 'cursor': cursor}))
    assert second['records'] == [] and second['next_cursor'] is not None and second['coverage'] == 'partial'
    third = reporting.audit_list(replace(call, params={**call.params, 'cursor': second['next_cursor']}))
    assert [row['record_id'] for row in third['records']] == ['entry-002006'] and third['next_cursor'] is None


# LLM: 源内容变化后不能复用原签名游标，否则会跳过或重复合规记录。
# 函数用途: 验证 F8 快照版本变化被保留为 CURSOR_EXPIRED。
def test_audit_cursor_expires_when_source_changes(v13_host):
    call = _call(v13_host, 'audit.list', {'limit': 1})
    _seed_audit(call, 3)
    cursor = reporting.audit_list(call)['next_cursor']
    _seed_audit(call, 4)
    with pytest.raises((HostAPIError, BusinessError)) as expired:
        reporting.audit_list(replace(call, params={'limit': 1, 'cursor': cursor}))
    assert expired.value.code == 'CURSOR_EXPIRED'


# LLM: 不允许非前进分页制造无限循环，也不把缺来源伪装完整合规档案。
# 函数用途: 验证审计异常覆盖和无进展源的失败语义。
def test_audit_partial_sources_and_nonadvancing_reader_fail_closed(v13_host, monkeypatch):
    call = _call(v13_host, 'audit.export', {'format': 'csv'})

    # LLM: 仅隔离测试来源协议异常，成功产物测试仍使用真实 F8。
    # 函数用途: 提供来源不可用的结构化失败页。
    def unavailable(owner_home, owner_id, *, scope, since, until, actor, cursor, limit, admin_context, host):
        return {'records': [], 'next_cursor': None, 'coverage': 'partial',
            'unavailable': [{'source': 'request', 'reason_code': 'HOST_NOT_READY'}]}

    monkeypatch.setattr(reporting, 'audit_records', unavailable)
    with pytest.raises(HostAPIError) as source:
        reporting._export_records(call)
    assert source.value.code == 'HOST_NOT_READY'
    assert reporting.audit_list(replace(call, action='audit.list', params={}))['unavailable'] == ['request:HOST_NOT_READY']

    # LLM: 空页却给同一继续位置必须拒绝，而非重试至超时。
    # 函数用途: 提供无进展的坏源页。
    def stalled(owner_home, owner_id, *, scope, since, until, actor, cursor, limit, admin_context, host):
        return {'records': [], 'next_cursor': 'stalled', 'coverage': 'partial', 'unavailable': []}

    monkeypatch.setattr(reporting, 'audit_records', stalled)
    for handler, selected in ((reporting.audit_list, replace(call, action='audit.list', params={})),
                              (reporting._export_records, call)):
        with pytest.raises(HostAPIError) as source:
            handler(selected)
        assert source.value.code == 'HOST_NOT_READY'


# LLM: 密集匹配超过结果容量时必须恢复到原页中间，不能把页尾记录吞掉。
# 函数用途: 验证前缀分页的页内剩余记录不丢失。
def test_audit_dense_prefix_does_not_discard_page_remainder(v13_host):
    call = _call(v13_host, 'audit.list', {'action_prefix': 'target.', 'limit': 3})
    _seed_audit(call, 9, target_positions=tuple(range(9)))
    identities, cursor = [], None
    while True:
        selected = replace(call, params={**call.params, **({'cursor': cursor} if cursor else {})})
        page = reporting.audit_list(selected)
        identities.extend(row['record_id'] for row in page['records'])
        cursor = page['next_cursor']
        if cursor is None:
            break
    assert identities == ['entry-' + str(index).zfill(6) for index in range(9)]


# LLM: 所有公式前缀及前置空白分别断言；RFC4180 引号用真实 csv.reader 读回。
# 函数用途: 验证 CSV 公式防护与 HTML 表格转义。
@pytest.mark.parametrize('value', ['=SUM(1,2)', '+1', '-1', '@command', '  =formula', '\tformula', '\rformula'])
def test_audit_export_csv_formula_protection_and_html_escape(value):
    row = {'at': '2026-10-31T00:00:00Z', 'actor': value,
        'action': '<script>value & "quoted"</script>', 'outcome': 'committed', 'source': 'v13', 'record_id': 'one'}
    text = reporting._export_content([row], 'csv').decode()
    parsed = list(csv.reader(StringIO(text)))
    assert parsed[0] == ['at', 'actor', 'action', 'outcome', 'source', 'record_id']
    assert parsed[1][1] == "'" + value and parsed[1][2] == row['action'] and '\r\n' in text
    rendered = reporting._export_content([row], 'html').decode()
    assert row['action'] not in rendered and html.escape(row['action'], quote=True) in rendered


# LLM: 只在当前 pytest 临时根枚举少量已创建 owner，按实际空间 ID 定位，不猜身份路径。
# 函数用途: 找到真实 native 请求创建的空间所属 owner。
def _native_owner(native, space_id):
    from agent_py_agent.agent.user_space.owner_admin_controls import list_owner_home_paths

    _server, agent = native
    homes, truncated = list_owner_home_paths(agent.home_paths, limit=20)
    assert not truncated
    matches = []
    for owner_id, paths in homes:
        catalog = paths.owner_home_dir / 'data' / 'business' / 'catalog.json'
        if catalog.is_file() and space_id in json.loads(catalog.read_text(encoding='utf-8'))['spaces']:
            matches.append((owner_id, paths))
    assert len(matches) == 1
    owner_id, paths = matches[0]
    paths = replace(paths, owner_id=owner_id)
    return SimpleNamespace(home_paths=paths, config=agent.config)


# LLM: 原生端到端成功必须核对标准信封与注册 schema，不把构建通过当业务通过。
# 函数用途: 执行并验证一个真实 HTTP 业务请求。
def _native_data(native, action, params, *, key=None, revision=None):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec

    status, body = v13_call(native, action, params, user='business-alice',
        idempotency_key=key, expected_revision=revision)
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
        'allowed_actions', 'warnings', 'effect_outcome'}
    if action.startswith(('reports.', 'audit.', 'saved_views.')):
        assert v13_action_spec(action).validate_result(body['data']) == body['data']
    return body['data']


# LLM: 内容路由必须经过真实 HTTP 鉴权并按冻结 version_ref 读取，不在测试中补登记或补正文。
# 函数用途: 读回导出产物的真实媒体字节。
def _native_content(native, artifact):
    path = '/client/business/artifacts/' + artifact['artifact_id'] + '/content?version_ref=' + artifact['version_ref']
    status, headers, content = native_request(native, 'GET', path)
    assert status == 200 and headers['X-Content-Type-Options'] == 'nosniff'
    assert len(content) == artifact['size_bytes']
    assert hashlib.sha256(content).hexdigest() == artifact['content_sha256']
    foreign_status, _, foreign_content = native_request(native, 'GET', path, user='business-bob')
    # 新Owner尚无旧run完整覆盖证明；拒绝内容并如实返回未知，不把未发现冒充确定404。
    assert foreign_status == 503 and content not in foreign_content
    foreign = json.loads(foreign_content)
    assert set(foreign) == {'error'}
    assert foreign['error']['code'] == 'HOST_NOT_READY'
    assert foreign['error']['message'] == 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE'
    assert foreign['error']['outcome'] == 'not_started'
    return content


# LLM: 原 upload→冻结 artifacts.get→真实 content route 全链路；同键重放不产生重复产物。
# 函数用途: 验证 CSV/HTML 真实导出、分页、参数冲突与源变更后的幂等重放。
def test_audit_export_csv_html_artifact_and_row_limit(v13_native):
    space = _native_data(v13_native, 'spaces.create', {'name': '审计空间'}, key='reporting-space-' + uuid4().hex)
    host = _native_owner(v13_native, space['space_id'])
    seed_call = _call(host, 'audit.list', {})
    actor = '=SUM(1,2)<script>bad & "x"</script>'
    _seed_audit(seed_call, 205, actor=actor)
    params = {'format': 'csv', 'space_id': space['space_id'],
        'since': '2026-10-01T00:00:00Z', 'until': '2026-11-01T00:00:00Z'}
    key = 'reporting-csv-' + uuid4().hex
    exported = _native_data(v13_native, 'audit.export', params, key=key)
    assert exported['rows'] == 205
    artifact = exported['export']
    status, got = v13_call(v13_native, 'artifacts.get',
        {'artifact_id': artifact['artifact_id'], 'version_ref': artifact['version_ref']}, user='business-alice')
    assert status == 200, json.dumps(got, ensure_ascii=False)
    assert got['data']['artifact']['content_sha256'] == artifact['content_sha256'] and got['data']['available']
    content = _native_content(v13_native, artifact)
    parsed = list(csv.reader(StringIO(content.decode())))
    assert len(parsed) == 206 and parsed[1][1] == "'" + actor
    html_params = {**params, 'format': 'html'}
    rendered = _native_data(v13_native, 'audit.export', html_params, key='reporting-html-' + uuid4().hex)
    text = _native_content(v13_native, rendered['export']).decode()
    assert actor not in text and html.escape(actor, quote=True) in text and '<script>' not in text
    _seed_audit(seed_call, 206, actor=actor)
    assert _native_data(v13_native, 'audit.export', params, key=key) == exported
    status, conflict = v13_call(v13_native, 'audit.export', html_params, user='business-alice', idempotency_key=key)
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    status, listed = v13_call(v13_native, 'artifacts.list', {'limit': 100}, user='business-alice')
    assert status == 200, listed
    assert {row['artifact_id'] for row in listed['data']['items']} == {
        artifact['artifact_id'], rendered['export']['artifact_id']}


# LLM: 一万行是包含边界；超出必须在上传前失败，不能用已截断页伪装全部。
# 函数用途: 验证完整 F8 分页与硬上限。
def test_audit_export_exact_row_boundary_and_limit(v13_host):
    call = _call(v13_host, 'audit.export', {'format': 'csv',
        'since': '2026-10-01T00:00:00Z', 'until': '2026-11-01T00:00:00Z'})
    _seed_audit(call, 10000)
    rows = reporting._export_records(call)
    assert len(rows) == 10000 and rows[0]['record_id'] == 'entry-000000' and rows[-1]['record_id'] == 'entry-009999'
    _seed_audit(call, 10001)
    with pytest.raises(HostAPIError) as limit:
        reporting._export_records(call)
    assert (limit.value.status, limit.value.code) == (400, 'LIMIT_EXCEEDED')
    assert not (call.owner_home / 'artifacts' / 'business').exists()


# LLM: 零空间或多个 active 空间均不得任选；显式空间仍交原 W1 复核。
# 函数用途: 验证默认导出空间只允许唯一可见 active 空间。
def test_audit_export_default_space_requires_unique_active(v13_host):
    call = _call(v13_host, 'audit.export', {'format': 'csv'})
    with pytest.raises(HostAPIError) as empty:
        reporting._export_space(call)
    assert empty.value.code == 'INVALID_SCHEMA'
    actor = call.services.actor(call.context)
    first = call.services.spaces.create({'name': '空间一'}, actor=actor, idempotency_key='space-one-' + uuid4().hex)
    assert reporting._export_space(call) == first['space_id']
    call.services.spaces.create({'name': '空间二'}, actor=actor, idempotency_key='space-two-' + uuid4().hex)
    with pytest.raises(HostAPIError) as multiple:
        reporting._export_space(call)
    assert multiple.value.code == 'INVALID_SCHEMA'
    assert reporting._export_space(replace(call, params={'format': 'csv', 'space_id': first['space_id']})) == first['space_id']


# LLM: 真实 HTTP 序列覆盖全部七个动作和闭合错误，不能只展示空页欢迎信息。
# 函数用途: 验证正式入口信封、读写、CAS、删除回放与拒绝。
def test_envelopes(v13_native):
    _native_data(v13_native, 'reports.weekly_digest', {'format': 'html'})
    _native_data(v13_native, 'audit.list', {})
    assert _native_data(v13_native, 'saved_views.list', {'surface': 'alerts'}) == {'items': []}
    first = _native_data(v13_native, 'saved_views.create', {'surface': 'alerts', 'name': '原生视图', 'filters': {}},
        key='native-create-' + uuid4().hex)['view']
    updated = _native_data(v13_native, 'saved_views.update', {'view_id': first['view_id'], 'shared': True},
        key='native-update-' + uuid4().hex, revision=first['revision'])['view']
    key = 'native-delete-' + uuid4().hex
    deleted = _native_data(v13_native, 'saved_views.delete', {'view_id': first['view_id']},
        key=key, revision=updated['revision'])
    assert _native_data(v13_native, 'saved_views.delete', {'view_id': first['view_id']},
        key=key, revision=updated['revision']) == deleted
    for action, params, expected in (
        ('saved_views.list', {'surface': 'alerts', 'owner_id': 'forged'}, (400, 'INVALID_SCHEMA')),
        ('saved_views.create', {'surface': 'alerts', 'name': 'missing key', 'filters': {}}, (400, 'INVALID_SCHEMA')),
        ('saved_views.update', {'view_id': first['view_id']}, (400, 'INVALID_SCHEMA')),
    ):
        status, body = v13_call(v13_native, action, params, user='business-alice')
        assert (status, body['error']['code']) == expected and body['state'] == 'failed' and body['data'] == {}
    status, _headers, _body = native_request(v13_native, 'POST', '/client/business',
        {'action': 'saved_views.list', 'request_id': 'denied', 'params': {'surface': 'alerts'}},
        headers={'X-Gateway-Token': ''})
    assert status == 403


# LLM: 以 AST 防止再次引入注册器循环导入和锁外整快照覆盖，并验证任务限定的代码风格。
# 函数用途: 固化 reporting 的关键结构约束。
def test_reporting_structure_has_lazy_registry_imports_and_explicit_parameters():
    source = Path(reporting.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    assert ast.get_docstring(tree)
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            assert node.module != 'gateway_parts.business_api_v13'
        if isinstance(node, ast.Assign):
            assert not isinstance(node.value, ast.Constant) or type(node.value.value) not in (int, float)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.args.vararg is None and node.args.kwarg is None
            preceding = source.splitlines()[max(0, node.lineno - 5):node.lineno - 1]
            assert any('# LLM:' in line for line in preceding)
            assert any('# 函数用途:' in line for line in preceding)
    for name in ('views_create', 'views_update', 'views_delete'):
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
        mutators = [node for node in function.body if isinstance(node, ast.FunctionDef) and node.name == 'mutate']
        assert len(mutators) == 1 and mutators[0].args.args[0].arg == 'rows'


def test_audit_export_known_upload_source_replays_and_separates_subjects(v13_host):
    call = _call(v13_host, 'audit.export', {'format': 'csv'}, key='known-source-key')
    space = call.services.spaces.create({'name': 'known source'}, actor=call.services.actor(call.context),
        idempotency_key='known-source-space')
    call = replace(call, params={'format': 'csv', 'space_id': space['space_id']})
    _seed_audit(call, 2)
    first = reporting.audit_export(call)
    assert reporting.audit_export(call) == first
    other = _call(v13_host, 'audit.export', call.params, subject='analyst-b', key=call.idempotency_key)
    second = reporting.audit_export(other)
    assert second['export']['artifact_id'] != first['export']['artifact_id']
    assert reporting.audit_export(call) == first


def test_known_upload_lookup_does_not_treat_foreign_source_as_absent(v13_host):
    from agent_py_agent.agent.artifacts.registry import (
        ArtifactRegistration,
        register_observed_artifact,
    )

    call = _call(v13_host, 'audit.export', {'format': 'csv'}, key='existing-foreign-source')
    _, artifact_id = reporting._export_identity(call)
    root = call.owner_home / 'artifacts' / 'business'
    root.mkdir(parents=True)
    path = root / (artifact_id + '.blob')
    data = b'existing-other-source'
    path.write_bytes(data)
    register_observed_artifact(ArtifactRegistration(root, path, artifact_id=artifact_id, source='workflow', status='ready',
        metadata={'owner_id': call.owner_id}), observed_sha256=hashlib.sha256(data).hexdigest(),
        observed_size_bytes=len(data), atomic_publish=True)
    before = {p.name: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(HostAPIError) as conflict:
        reporting.audit_export(call)
    assert conflict.value.code == 'HOST_NOT_READY' and conflict.value.status == 503
    assert {p.name: p.read_bytes() for p in root.rglob('*') if p.is_file()} == before


def test_known_upload_lookup_enforces_primary_registry_read_budget(v13_host, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as media

    call = _call(v13_host, 'audit.export', {'format': 'csv'}, key='bounded-source-lookup')
    _, artifact_id = reporting._export_identity(call)
    observed = []
    def read(_owner, parts, *, max_bytes=None):
        observed.append((parts, max_bytes))
        return b'x' * max_bytes
    monkeypatch.setattr(media, 'read_bytes_beneath', read)
    with pytest.raises(HostAPIError) as limit:
        reporting._export_artifact(call, artifact_id)
    assert limit.value.code == 'HOST_NOT_READY' and '读取预算' in limit.value.message
    assert len(observed) == 1 and observed[0][1] == 2 * 1024 * 1024 + 1
    assert not (call.owner_home / 'artifacts' / 'business').exists()


def test_known_upload_absence_cannot_overwrite_unregistered_blob(v13_host):
    call = _call(v13_host, 'audit.export', {'format': 'csv'}, key='unregistered-upload-unknown')
    space = call.services.spaces.create({'name': 'unknown upload'}, actor=call.services.actor(call.context),
        idempotency_key='unknown-upload-space')
    call = replace(call, params={'format': 'csv', 'space_id': space['space_id']})
    _seed_audit(call, 1)
    _, artifact_id = reporting._export_identity(call)
    root = call.owner_home / 'artifacts' / 'business'
    root.mkdir(parents=True)
    path = root / (artifact_id + '.blob')
    path.write_bytes(b'original-unconfirmed-blob')
    stamp = path.stat().st_mtime_ns
    with pytest.raises(HostAPIError) as unknown:
        reporting.audit_export(call)
    assert (unknown.value.code, unknown.value.status, unknown.value.outcome) == ('OUTCOME_UNKNOWN', 503, 'unknown')
    assert path.read_bytes() == b'original-unconfirmed-blob' and path.stat().st_mtime_ns == stamp


@pytest.mark.parametrize('reported_outcome', ['not_started', 'committed'])
def test_real_upload_post_commit_lookup_failure_keeps_unknown_or_declared_commit(v13_host, monkeypatch, reported_outcome):
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as media

    call = _call(v13_host, 'audit.export', {'format': 'csv'}, key='post-upload-readback')
    space = call.services.spaces.create({'name': 'post upload'}, actor=call.services.actor(call.context),
        idempotency_key='post-upload-space')
    call = replace(call, params={'format': 'csv', 'space_id': space['space_id']})
    _seed_audit(call, 2)
    original = media.business_upload_artifact
    calls = []
    def lookup(*args):
        calls.append(args[0])
        if len(calls) == 2:
            raise HostAPIError('HOST_NOT_READY', 503, reported_outcome, message='QA_POST_UPLOAD_LOOKUP_UNAVAILABLE')
        return original(*args)
    monkeypatch.setattr(media, 'business_upload_artifact', lookup)
    with pytest.raises(HostAPIError) as failure:
        reporting.audit_export(call)
    expected = 'unknown' if reported_outcome == 'not_started' else 'committed'
    assert (failure.value.code, failure.value.status, failure.value.outcome) == ('HOST_NOT_READY', 503, expected)
    if expected == 'unknown':
        assert '勿自动重传' in failure.value.message and 'QA_POST_UPLOAD_LOOKUP_UNAVAILABLE' in failure.value.message
    artifact = original(calls[0], call.context, call.services)
    assert artifact is not None and artifact['available']
    path = call.owner_home / 'artifacts' / 'business' / (artifact['artifact_id'] + '.blob')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    monkeypatch.setattr(media, 'business_upload_artifact', original)
    replayed = reporting.audit_export(call)
    assert replayed['export']['artifact_id'] == artifact['artifact_id']
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_native_post_upload_readback_failure_reports_unknown_without_retransmission(v13_native, monkeypatch):
    from agent_py_agent.agent.gateway_parts import business_artifacts_http as media

    space = _native_data(v13_native, 'spaces.create', {'name': 'native post upload'}, key='native-post-upload-space')
    host = _native_owner(v13_native, space['space_id'])
    _seed_audit(_call(host, 'audit.list', {}), 2)
    original = media.business_upload_artifact
    reads = []
    def lookup(*args):
        reads.append(args)
        if len(reads) == 2:
            raise HostAPIError('HOST_NOT_READY', 503, message='QA_NATIVE_POST_UPLOAD_READBACK')
        return original(*args)
    monkeypatch.setattr(media, 'business_upload_artifact', lookup)
    status, body = v13_call(v13_native, 'audit.export', {'format': 'csv', 'space_id': space['space_id']},
        user='business-alice', idempotency_key='native-post-upload-key')
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    assert body['effect_outcome'] == 'unknown' and body['data'] == {}
    assert 'QA_NATIVE_POST_UPLOAD_READBACK' in body['error']['message']
    details = {item['key']: item['value'] for item in body['error']['details']}
    assert details['retry_same_request'] is False and '勿重发' in details['next_action']
    artifact = original(*reads[0])
    assert artifact is not None and artifact['available'] and len(reads) == 2
    path = reads[0][2].owner_home / 'artifacts' / 'business' / (artifact['artifact_id'] + '.blob')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact['content_sha256']
