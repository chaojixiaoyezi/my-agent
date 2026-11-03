"""案件与协同的真实网关往返；纯函数测试补充边界值，不替代原生入口。"""
from __future__ import annotations

import errno
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent_py_agent.agent.business_spaces import v13_w8_cases as cases
from agent_py_agent.tests import test_v13_w0_harness

native = test_v13_w0_harness.v13_native


# LLM: 每个写请求有独立合法幂等键，需要重放的测试显式复用。
# 函数用途: 经W0真实HTTP助手发起请求。
def wire(server, action, params, key=None, revision=None, user='business-alice'):
    from agent_py_agent.agent.gateway_parts.business_api_v13 import v13_action_spec
    from agent_py_agent.agent.plugin_host.business_action_specs import business_action_spec
    from agent_py_agent.agent.plugin_host.errors import HostAPIError
    from agent_py_agent.tests.test_v13_w0_harness import v13_call

    try:
        spec = business_action_spec(action)
    except HostAPIError:
        spec = v13_action_spec(action)
    if key is None and spec.requires_idempotency:
        key = uuid.uuid4().hex
    return v13_call(server, action, params, user=user, idempotency_key=key, expected_revision=revision)


# LLM: 成功不仅检查状态码，同时核对标准信封。
# 函数用途: 取得完成响应的业务投影。
def completed(response):
    status, body = response
    assert status == 200, body
    assert body['state'] == 'completed', body
    assert body['error'] is None, body
    return body['data']


# LLM: 失败不得被空成功掩盖。
# 函数用途: 核对准确错误状态和错误码。
def rejected(response, status, code):
    actual, body = response
    assert actual == status, body
    assert body['state'] == 'failed', body
    assert body['data'] == {}, body
    assert body['error']['code'] == code, body
    return body


# LLM: 测试空间来自真实W1，不硬编码宿主空间。
# 函数用途: 解析当前原生空间。
def space(server):
    from agent_py_agent.tests.test_v13_w0_harness import v13_space

    host = server[0] if isinstance(server, tuple) else server
    if not hasattr(host, '_w8_space_id'):
        host._w8_space_id = v13_space(server)
    return host._w8_space_id


# LLM: 告警通过正式写入动作产生，不直接写产品集合。
# 函数用途: 创建可关联告警并取回规范记录。
def ingest(server, count=2, selected_space=None):
    space_id = selected_space or space(server)
    values = [{'external_ref': uuid.uuid4().hex, 'title': 'event-' + str(index),
               'severity': ('medium', 'critical', 'high')[index % 3],
               'entities': [{'type': 'host', 'value': 'node-' + str(index)},
                            {'type': 'user', 'value': 'analyst-fixture'}],
               'techniques': ['T100' + str(index % 2)],
               'occurred_at': '2026-10-' + str(10 + index).zfill(2) + 'T00:00:00+00:00'}
              for index in range(count)]
    result = completed(wire(server, 'soc.alerts.ingest', {'space_id': space_id, 'alerts': values}))
    return [completed(wire(server, 'soc.alerts.get', {'alert_id': item['alert_id']}))['alert']
            for item in result['results']]


# LLM: 案件通过真实入口创建，后续CAS读取其返回版本。
# 函数用途: 创建测试案件。
def create(server, alerts=None, title='协同案件', severity='high'):
    params = {'space_id': space(server), 'title': title, 'severity': severity,
              'alert_ids': [row['alert_id'] for row in alerts or []]}
    return completed(wire(server, 'soc.cases.create', params))['case']


# LLM: 序号、关联与幂等重放一起验证。
# 函数用途: 验证案件创建及重复请求语义。
def test_create_generates_case_year_sequence_id_and_links_alerts(native):
    alerts = ingest(native)
    key = uuid.uuid4().hex
    params = {'space_id': space(native), 'title': '新案件', 'severity': 'high',
              'alert_ids': [row['alert_id'] for row in alerts]}
    first = completed(wire(native, 'soc.cases.create', params, key))['case']
    assert first['case_id'].startswith('CASE-')
    assert first['case_id'].rsplit('-', 1)[1].isdigit()
    assert completed(wire(native, 'soc.cases.create', params, key))['case'] == first
    for alert in alerts:
        current = completed(wire(native, 'soc.alerts.get', {'alert_id': alert['alert_id']}))['alert']
        assert current['case_id'] == first['case_id']
    rejected(wire(native, 'soc.cases.create', dict(params, title='改过参数'), key), 409, 'IDEMPOTENCY_CONFLICT')
    rejected(wire(native, 'soc.cases.create', dict(params, case_id=first['case_id'])), 409, 'STATE_CONFLICT')
    second = create(native)
    assert int(second['case_id'].rsplit('-', 1)[1]) > int(first['case_id'].rsplit('-', 1)[1])


# LLM: 查询闭合记录，不泄露私有归属字段。
# 函数用途: 验证详情和不存在记录的精确错误。
def test_get_includes_linked_alerts(native):
    alerts = ingest(native)
    case = create(native, alerts)
    result = completed(wire(native, 'soc.cases.get', {'case_id': case['case_id']}))
    assert result['case'] == case
    assert {row['alert_id'] for row in result['alerts']} == set(case['alert_ids'])
    assert 'owner_id' not in str(result)
    rejected(wire(native, 'soc.cases.get', {'case_id': 'missing-case'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 游标绑定过滤、身份和结果版本，不能跨范围复用。
# 函数用途: 验证列表筛选、分页、篡改及失效。
def test_cases_list_filters(native):
    create(native, title='medium-case', severity='medium')
    create(native, title='critical-one', severity='critical')
    create(native, title='critical-two', severity='critical')
    page = completed(wire(native, 'soc.cases.list', {'severity': 'critical', 'limit': 1}))
    assert len(page['items']) == 1
    assert page['items'][0]['severity'] == 'critical'
    assert page['next_cursor']
    following = completed(wire(native, 'soc.cases.list', {'severity': 'critical', 'limit': 1, 'cursor': page['next_cursor']}))
    assert following['items'][0]['case_id'] != page['items'][0]['case_id']
    rejected(wire(native, 'soc.cases.list', {'severity': 'medium', 'cursor': page['next_cursor']}), 503, 'CURSOR_EXPIRED')
    rejected(wire(native, 'soc.cases.list', {'cursor': page['next_cursor'] + 'x'}), 503, 'CURSOR_EXPIRED')
    create(native, severity='critical')
    rejected(wire(native, 'soc.cases.list', {'severity': 'critical', 'cursor': page['next_cursor']}), 503, 'CURSOR_EXPIRED')
    filtered = completed(wire(native, 'soc.cases.list', {'severity': 'medium', 'status': 'open'}))
    assert [row['title'] for row in filtered['items']] == ['medium-case']


# LLM: 测试当前版本而非旧快照；失败后检查未半提交。
# 函数用途: 验证CAS、关联迁移、移除和关闭时间。
def test_update_cas_and_alert_linking(native):
    alerts = ingest(native)
    old = create(native, alerts[:1])
    target = create(native)
    params = {'case_id': target['case_id'], 'add_alert_ids': [row['alert_id'] for row in alerts], 'assignee': 'lin'}
    key = uuid.uuid4().hex
    updated = completed(wire(native, 'soc.cases.update', params, key, target['revision']))['case']
    assert updated['assignee'] == 'lin'
    assert set(updated['alert_ids']) == {row['alert_id'] for row in alerts}
    assert completed(wire(native, 'soc.cases.update', params, key, target['revision']))['case'] == updated
    assert completed(wire(native, 'soc.cases.get', {'case_id': old['case_id']}))['case']['alert_ids'] == []
    rejected(wire(native, 'soc.cases.update', params, revision=target['revision']), 409, 'REVISION_CONFLICT')
    rejected(wire(native, 'soc.cases.update', {'case_id': target['case_id'], 'title': '不可提交',
        'add_alert_ids': ['missing-alert']}, revision=updated['revision']), 404, 'RESOURCE_NOT_FOUND')
    assert completed(wire(native, 'soc.cases.get', {'case_id': target['case_id']}))['case']['title'] == target['title']
    final = completed(wire(native, 'soc.cases.update', {'case_id': target['case_id'], 'status': 'closed',
        'remove_alert_ids': [alerts[0]['alert_id']]}, revision=updated['revision']))['case']
    assert final['closed_at']
    assert completed(wire(native, 'soc.alerts.get', {'alert_id': alerts[0]['alert_id']}))['alert']['case_id'] is None


# LLM: 重复读取必须稳定，边不能指向响应中不存在的节点。
# 函数用途: 验证全图节点、关系和确定性标识。
def test_case_graph_nodes_edges_deterministic(native):
    case = create(native, ingest(native, 3))
    params = {'case_id': case['case_id']}
    graph = completed(wire(native, 'soc.graph.case_graph.get', params))
    assert graph == completed(wire(native, 'soc.graph.case_graph.get', params))
    assert {node['kind'] for node in graph['nodes']} == {'case', 'alert', 'entity', 'technique'}
    ids = {node['node_id'] for node in graph['nodes']}
    assert len(ids) == len(graph['nodes'])
    assert all(edge['source'] in ids and edge['target'] in ids for edge in graph['edges'])
    assert {edge['relation'] for edge in graph['edges']} >= {'contains', 'involves', 'observed_with', 'maps_to'}
    rejected(wire(native, 'soc.graph.case_graph.get', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 输入关联顺序故意与时间相反，不能拿数组顺序冒充时间。
# 函数用途: 验证实体首次出现路径。
def test_case_path_orders_entities_by_first_seen(native):
    alerts = ingest(native, 3)
    case = create(native, list(reversed(alerts)))
    result = completed(wire(native, 'soc.graph.case_path.get', {'case_id': case['case_id']}))
    labels = [node['label'] for node in result['path']]
    assert labels == ['node-0', 'analyst-fixture', 'node-1', 'node-2']
    assert len(result['edges']) == len(result['path']) - 1
    rejected(wire(native, 'soc.graph.case_path.get', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 相同技术的多个告警应归于一组，未知战术保持空值。
# 函数用途: 验证技术阶段分组和顺序。
def test_case_chain_groups_alerts_by_technique_stage(native):
    alerts = ingest(native, 3)
    case = create(native, alerts)
    stages = completed(wire(native, 'soc.graph.case_chain.get', {'case_id': case['case_id']}))['stages']
    assert [row['technique_id'] for row in stages] == ['T1000', 'T1001']
    assert stages[0]['alert_ids'] == [alerts[0]['alert_id'], alerts[2]['alert_id']]
    assert all(row['tactic'] is None for row in stages)
    rejected(wire(native, 'soc.graph.case_chain.get', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 深度扩展必须依据共享实体，不因同空间而纳入无关告警。
# 函数用途: 验证实体一跳与二跳邻域。
def test_entity_graph_neighbourhood(native):
    alerts = ingest(native, 3)
    create(native, alerts)
    params = {'space_id': space(native), 'entity': {'type': 'host', 'value': 'node-0'}}
    first = completed(wire(native, 'soc.graph.entity.get', dict(params, depth=1)))
    second = completed(wire(native, 'soc.graph.entity.get', dict(params, depth=2)))
    assert sum(node['kind'] == 'alert' for node in first['nodes']) == 1
    assert sum(node['kind'] == 'alert' for node in second['nodes']) == 3
    rejected(wire(native, 'soc.graph.entity.get', {'entity': {'type': 'host', 'value': 'not-seen'}}),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 部分失败必须逐条出现；成功判定同时写反馈。
# 函数用途: 验证批量分诊CAS、缺失、时间戳及幂等。
def test_bulk_triage_per_alert_results_and_optional_cas(native):
    alerts = ingest(native, 3)
    params = {'alert_ids': [row['alert_id'] for row in alerts] + ['missing-alert'],
              'status': 'resolved', 'verdict': 'benign', 'note': '人工复核',
              'expected_revisions': {alerts[1]['alert_id']: alerts[1]['revision'] + 5}}
    key = uuid.uuid4().hex
    data = completed(wire(native, 'cases.bulk_triage', params, key))
    assert [row['error_code'] for row in data['results']] == [None, 'REVISION_CONFLICT', None, 'RESOURCE_NOT_FOUND']
    assert completed(wire(native, 'cases.bulk_triage', params, key)) == data
    rejected(wire(native, 'cases.bulk_triage', dict(params, note='不同参数'), key), 409, 'IDEMPOTENCY_CONFLICT')
    for index in (0, 2):
        current = completed(wire(native, 'soc.alerts.get', {'alert_id': alerts[index]['alert_id']}))['alert']
        assert current['verdict'] == 'benign'
        assert current['acknowledged_at'] and current['resolved_at']
    unchanged = completed(wire(native, 'soc.alerts.get', {'alert_id': alerts[1]['alert_id']}))['alert']
    assert unchanged['verdict'] is None
    overrides = completed(wire(native, 'feedback.overrides.list', {}))['items']
    matching = [row for row in overrides if row['alert_id'] in {alerts[0]['alert_id'], alerts[2]['alert_id']}]
    assert len(matching) == 2
    assert all(row['source'] == 'bulk_triage' and row['original_verdict'] == 'unknown' for row in matching)
    rejected(wire(native, 'cases.bulk_triage', {'alert_ids': [], 'expected_revisions': {'x': True}}),
             400, 'INVALID_SCHEMA')


# LLM: 最严重告警决定默认标题，而非传入数组的首项。
# 函数用途: 验证新建与已有案件的告警合并。
def test_merge_alerts_into_new_or_existing_case(native):
    alerts = ingest(native, 3)
    rejected(wire(native, 'cases.merge_alerts', {'alert_ids': [alerts[0]['alert_id']]}), 400, 'INVALID_SCHEMA')
    rejected(wire(native, 'cases.merge_alerts', {'alert_ids': [alerts[0]['alert_id'], alerts[0]['alert_id']]}),
             400, 'INVALID_SCHEMA')
    params = {'space_id': space(native), 'alert_ids': [row['alert_id'] for row in alerts[:2]]}
    key = uuid.uuid4().hex
    case = completed(wire(native, 'cases.merge_alerts', params, key))['case']
    assert case['title'] == alerts[1]['title']
    assert case['severity'] == 'critical'
    assert completed(wire(native, 'cases.merge_alerts', params, key))['case'] == case
    merged = completed(wire(native, 'cases.merge_alerts', {'case_id': case['case_id'],
        'alert_ids': [alerts[2]['alert_id']]}))['case']
    assert set(merged['alert_ids']) == {row['alert_id'] for row in alerts}
    rejected(wire(native, 'cases.merge_alerts', {'case_id': case['case_id'], 'alert_ids': ['missing']}),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 归并后的来源状态与每条告警都必须一致。
# 函数用途: 验证案件归并、CAS与冲突分支。
def test_merge_cases_moves_alerts_and_closes_sources(native):
    alerts = ingest(native)
    source = create(native, alerts)
    target = create(native)
    params = {'source_case_ids': [source['case_id']], 'target_case_id': target['case_id']}
    rejected(wire(native, 'cases.merge', params, revision=target['revision'] + 9), 409, 'REVISION_CONFLICT')
    rejected(wire(native, 'cases.merge', {'source_case_ids': [target['case_id']],
        'target_case_id': target['case_id']}, revision=target['revision']), 409, 'STATE_CONFLICT')
    rejected(wire(native, 'cases.merge', {'source_case_ids': ['missing'],
        'target_case_id': target['case_id']}, revision=target['revision']), 404, 'RESOURCE_NOT_FOUND')
    key = uuid.uuid4().hex
    result = completed(wire(native, 'cases.merge', params, key, target['revision']))
    assert result['merged'] == [source['case_id']]
    assert set(result['case']['alert_ids']) == set(source['alert_ids'])
    assert completed(wire(native, 'cases.merge', params, key, target['revision'])) == result
    current = completed(wire(native, 'soc.cases.get', {'case_id': source['case_id']}))['case']
    assert current['status'] == 'closed' and current['closed_at']
    assert current['merged_into'] == target['case_id'] and current['alert_ids'] == []
    for alert in alerts:
        row = completed(wire(native, 'soc.alerts.get', {'alert_id': alert['alert_id']}))['alert']
        assert row['case_id'] == target['case_id']
    rejected(wire(native, 'cases.merge', params, revision=result['case']['revision']), 409, 'STATE_CONFLICT')


# LLM: 示例空参数也必须有完整负载投影；没有SLA不能报零违约。
# 函数用途: 验证负载、归属筛选和未知SLA。
def test_workload_example_empty_params(native):
    alerts = ingest(native)
    case = create(native, alerts)
    completed(wire(native, 'soc.cases.update', {'case_id': case['case_id'], 'status': 'investigating',
        'assignee': 'lin'}, revision=case['revision']))
    completed(wire(native, 'cases.bulk_triage', {'alert_ids': [alerts[0]['alert_id']], 'assignee': 'lin'}))
    result = completed(wire(native, 'cases.workload', {}))
    lin = next(row for row in result['analysts'] if row['assignee'] == 'lin')
    assert lin['open_cases'] == 1 and lin['investigating_cases'] == 1 and lin['open_alerts'] == 1
    assert lin['oldest_open_at'] and lin['sla_breaches'] is None
    assert result['unassigned']['open_alerts'] == 1
    assert completed(wire(native, 'cases.workload', {'assignee': 'other'}))['analysts'] == []


# LLM: 字段定义完整覆盖所有正式类型，初始CAS为零。
# 函数用途: 构建空间字段定义。
def definitions():
    return [{'field_id': name, 'label': name, 'type': kind, 'options': options, 'required': required}
            for name, kind, options, required in (
                ('summary', 'string', [], True), ('score', 'number', [], False),
                ('approved', 'boolean', [], False), ('due', 'date', [], False),
                ('category', 'enum', ['a', 'b'], False), ('reviewer', 'user', [], False))]


# LLM: 更新定义要比较当前版本，幂等重放不能再次递增。
# 函数用途: 验证扩展字段定义读写及错误分支。
def test_custom_fields_roundtrip(native):
    space(native)
    initial = completed(wire(native, 'cases.custom_fields.list', {}))
    assert initial == {'fields': [], 'revision': 0}
    params = {'space_id': space(native), 'fields': definitions()}
    key = uuid.uuid4().hex
    updated = completed(wire(native, 'cases.custom_fields.upsert', params, key, 0))
    assert updated['fields'] == definitions() and updated['revision'] > 0
    assert completed(wire(native, 'cases.custom_fields.upsert', params, key, 0)) == updated
    assert completed(wire(native, 'cases.custom_fields.list', {})) == updated
    rejected(wire(native, 'cases.custom_fields.upsert', params, revision=0), 409, 'REVISION_CONFLICT')
    rejected(wire(native, 'cases.custom_fields.upsert', dict(params, fields=[]), key, 0),
             409, 'IDEMPOTENCY_CONFLICT')
    repeated = {'fields': definitions() + [definitions()[0]]}
    rejected(wire(native, 'cases.custom_fields.upsert', repeated, revision=updated['revision']),
             400, 'INVALID_SCHEMA')


# LLM: 布尔不能被当成数值，缺必填和未知字段必须精确拒绝。
# 函数用途: 验证案件扩展值的正式类型、CAS和逐字段错误。
def test_custom_values_validated_against_definitions(native):
    case = create(native)
    completed(wire(native, 'cases.custom_fields.upsert', {'fields': definitions()}, revision=0))
    initial = completed(wire(native, 'cases.custom_values.get', {'case_id': case['case_id']}))
    assert initial['values'] == {} and initial['revision'] == case['revision']
    values = {'summary': '复核完成', 'score': 12.5, 'approved': True, 'due': '2026-12-01',
              'category': 'a', 'reviewer': 'lin'}
    for bad in ({}, {'summary': 'x', 'unknown': 1}, dict(values, score=True),
                dict(values, category='outside'), dict(values, due='2026-02-30'),
                dict(values, approved='yes'), dict(values, reviewer=12)):
        body = rejected(wire(native, 'cases.custom_values.set', {'case_id': case['case_id'], 'values': bad},
                             revision=case['revision']), 400, 'INVALID_SCHEMA')
        assert 'path' in str(body)
    key = uuid.uuid4().hex
    params = {'case_id': case['case_id'], 'values': values}
    updated = completed(wire(native, 'cases.custom_values.set', params, key, case['revision']))
    assert updated['values'] == values and updated['revision'] > case['revision']
    assert completed(wire(native, 'cases.custom_values.get', {'case_id': case['case_id']})) == updated
    assert completed(wire(native, 'cases.custom_values.set', params, key, case['revision'])) == updated
    rejected(wire(native, 'cases.custom_values.set', params, revision=case['revision']), 409, 'REVISION_CONFLICT')
    rejected(wire(native, 'cases.custom_values.get', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')
    rejected(wire(native, 'cases.custom_values.set', {'case_id': 'missing', 'values': values}, revision=0),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 第二次打开即使使用新幂等键，也不得再建代理。
# 函数用途: 验证房间唯一性和真实后台线程可读取。
def test_war_room_open_idempotent_then_get(native):
    case = create(native)
    params = {'case_id': case['case_id'], 'participants': ['lin', 'mo']}
    assert completed(wire(native, 'war_rooms.get', {'case_id': case['case_id']}))['war_room'] is None
    key = uuid.uuid4().hex
    first = completed(wire(native, 'war_rooms.open', params, key))['war_room']
    assert first['participants'] == ['lin', 'mo'] and first['status'] == 'open'
    assert first['agent_id'] and first['thread_id'] and first['opened_at']
    assert completed(wire(native, 'war_rooms.open', params, key))['war_room'] == first
    assert completed(wire(native, 'war_rooms.open', params))['war_room'] == first
    assert completed(wire(native, 'war_rooms.get', {'case_id': case['case_id']}))['war_room'] == first
    agent = completed(wire(native, 'background_agents.get', {'agent_id': first['agent_id']}))['agent']
    assert agent['thread_id'] == first['thread_id']
    rejected(wire(native, 'war_rooms.open', dict(params, participants=['different']), key), 409, 'IDEMPOTENCY_CONFLICT')
    rejected(wire(native, 'war_rooms.get', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')
    rejected(wire(native, 'war_rooms.open', {'case_id': 'missing'}), 404, 'RESOURCE_NOT_FOUND')


# LLM: 其它身份即使知道准确ID，也不能读取或修改记录。
# 函数用途: 验证案件、图谱与协同入口的所属身份隔离。
def test_cross_owner_404(native):
    case = create(native, ingest(native))
    for action in ('soc.cases.get', 'soc.graph.case_graph.get', 'soc.graph.case_path.get',
                   'soc.graph.case_chain.get', 'cases.custom_values.get', 'war_rooms.get'):
        rejected(wire(native, action, {'case_id': case['case_id']}, user='other-owner'),
                 404, 'RESOURCE_NOT_FOUND')
    rejected(wire(native, 'soc.cases.update', {'case_id': case['case_id'], 'title': '禁止修改'},
                  revision=case['revision'], user='other-owner'), 404, 'RESOURCE_NOT_FOUND')
    rejected(wire(native, 'war_rooms.open', {'case_id': case['case_id']}, user='other-owner'),
             404, 'RESOURCE_NOT_FOUND')


# LLM: 严格结构错误在进入领域写回前拒绝。
# 函数用途: 验证闭合参数与缺少CAS信封。
def test_envelopes(native):
    rejected(wire(native, 'soc.cases.create', {'title': 'missing severity'}), 400, 'INVALID_SCHEMA')
    rejected(wire(native, 'soc.cases.list', {'owner_id': 'not-issued'}), 400, 'INVALID_SCHEMA')
    rejected(wire(native, 'soc.cases.update', {'case_id': 'x'}), 400, 'INVALID_SCHEMA')


# LLM: 纯函数边界补测不替代真实网关测试。
# 函数用途: 验证扩展字段严格类型与真实日期。
@pytest.mark.parametrize('kind,value,options,valid', [
    ('number', True, [], False), ('number', 2.5, [], True), ('number', float('nan'), [], False),
    ('boolean', 1, [], False), ('boolean', False, [], True), ('date', '2026-02-30', [], False),
    ('date', '2026-02-28', [], True), ('date', '20260228', [], False),
    ('enum', 'a', ['a'], True), ('enum', 'b', ['a'], False), ('user', 123, [], False),
])
def test_custom_value_type_edges(kind, value, options, valid):
    assert cases._valid_custom_value({'type': kind, 'options': options}, value) is valid


# LLM: 超过正式上限时必须明确部分覆盖，边不能悬空。
# 函数用途: 验证图谱裁剪及确定性哈希。
def test_graph_bounds_and_stable_identifiers():
    nodes = {str(index): cases._node('alert', str(index), str(index), {}) for index in range(2001)}
    ordered = list(nodes.values())
    edges = {}
    for left, right in zip(ordered, ordered[1:]):
        edge = cases._edge(left['node_id'], right['node_id'], 'contains', None)
        edges[edge['edge_id']] = edge
    bounded = cases._bounded_graph(nodes, edges)
    assert len(bounded['nodes']) == 2000 and bounded['coverage'] == 'partial'
    ids = {node['node_id'] for node in bounded['nodes']}
    assert all(edge['source'] in ids and edge['target'] in ids for edge in bounded['edges'])
    assert cases._graph_id('entity', ('host:x',)) == cases._graph_id('entity', ('host:x',))
    assert cases._graph_id('entity', ('host:x',)) != cases._graph_id('entity', ('user:x',))


# LLM: 可见两个空间不等于允许把它们的记录互相关联。
# 函数用途: 验证跨空间关联失败时整笔事务回滚。
def test_space_boundaries_and_atomic_rollback(native):
    second_space = completed(wire(native, 'spaces.create', {'name': '隔离空间'}))['space_id']
    alerts = ingest(native, selected_space=second_space)
    target = create(native)
    rejected(wire(native, 'soc.cases.update', {'case_id': target['case_id'],
        'title': '不可提交', 'add_alert_ids': [alerts[0]['alert_id']]}, revision=target['revision']),
        404, 'RESOURCE_NOT_FOUND')
    assert completed(wire(native, 'soc.cases.get', {'case_id': target['case_id']}))['case'] == target
    rejected(wire(native, 'soc.cases.create', {'space_id': space(native), 'title': '不能跨空间',
        'severity': 'high', 'alert_ids': [alerts[0]['alert_id']]}), 404, 'RESOURCE_NOT_FOUND')
    assert completed(wire(native, 'soc.alerts.get', {'alert_id': alerts[0]['alert_id']}))['alert']['case_id'] is None
    rejected(wire(native, 'cases.bulk_triage', {'alert_ids': [alerts[0]['alert_id']],
        'case_id': target['case_id']}), 404, 'RESOURCE_NOT_FOUND')
    other = completed(wire(native, 'soc.cases.create', {'space_id': second_space,
        'title': '另一空间案件', 'severity': 'high'}))['case']
    rejected(wire(native, 'cases.merge', {'source_case_ids': [other['case_id']],
        'target_case_id': target['case_id']}, revision=target['revision']), 404, 'RESOURCE_NOT_FOUND')
    result = completed(wire(native, 'soc.cases.list', {'space_id': second_space}))
    assert [row['case_id'] for row in result['items']] == [other['case_id']]


# LLM: 时区偏移是协议合法输入，按字符串排序会把先发生的证据放到后面。
# 函数用途: 回归路径和阶段的真实时间顺序。
def test_case_path_compares_timezone_offsets(native):
    values = [{'external_ref': 'offset-early', 'title': '先发生', 'severity': 'medium',
        'entities': [{'type': 'host', 'value': 'early'}], 'techniques': ['T1000'],
        'occurred_at': '2026-11-02T18:00:00+08:00'},
        {'external_ref': 'utc-later', 'title': '后发生', 'severity': 'high',
        'entities': [{'type': 'host', 'value': 'later'}], 'techniques': ['T1001'],
        'occurred_at': '2026-11-02T11:00:00Z'}]
    items = completed(wire(native, 'soc.alerts.ingest', {'space_id': space(native), 'alerts': values}))['results']
    case = create(native, [{'alert_id': row['alert_id']} for row in reversed(items)])
    params = {'case_id': case['case_id']}
    path = completed(wire(native, 'soc.graph.case_path.get', params))
    assert [row['label'] for row in path['path']] == ['early', 'later']
    chain = completed(wire(native, 'soc.graph.case_chain.get', params))
    assert [row['technique_id'] for row in chain['stages']] == ['T1000', 'T1001']


# LLM: 只通过公开写入口建立普通记录，保留真实 owner、空间、版本和确认时间。
# 函数用途: 创建负载测试用的案件或待确认、已确认告警。
def _workload_record(server, kind, *, assignee=None, selected_space=None):
    sid = selected_space or space(server)
    if kind == 'case':
        params = {'space_id': sid, 'title': 'SLA案件', 'severity': 'high'}
        if assignee is not None:
            params['assignee'] = assignee
        return completed(wire(server, 'soc.cases.create', params))['case']
    result = completed(wire(server, 'soc.alerts.ingest', {'space_id': sid, 'alerts': [{
        'external_ref': uuid.uuid4().hex, 'title': 'SLA告警', 'severity': 'high'}]}))
    row = completed(wire(server, 'soc.alerts.get', {
        'alert_id': result['results'][0]['alert_id']}))['alert']
    changes = {}
    if assignee is not None:
        changes['assignee'] = assignee
    if kind == 'acknowledged':
        changes['status'] = 'acknowledged'
    if changes:
        row = completed(wire(server, 'soc.alerts.update', {
            'alert_id': row['alert_id'], **changes}, revision=row['revision']))['alert']
    return row


# LLM: 每次请求均走真实 W0 HTTP；检查完整信封及逐字段业务结果，不只检查状态码。
# 函数用途: 精确核对负载查询的成功回执。
def _assert_workload(server, params, expected, *, user='business-alice'):
    response = wire(server, 'cases.workload', params, user=user)
    data = completed(response)
    body = response[1]
    assert set(body) == {'request_id', 'operation_id', 'state', 'data', 'error',
                         'allowed_actions', 'warnings', 'effect_outcome'}
    assert body['request_id'].startswith('v13-')
    assert body['operation_id'] is None and body['effect_outcome'] == 'none'
    assert body['allowed_actions'] == [] and body['warnings'] == []
    assert data == expected
    return data


# LLM: 空分组与无可评估阶段都保留 null；数字零只用于真实计数。
# 函数用途: 构造完整的预期分析师负载投影。
def _expected_workload_load(*, kind=None, row=None, breaches=None, assignee=None):
    return {'assignee': assignee, 'open_cases': int(kind == 'case'), 'investigating_cases': 0,
            'open_alerts': int(kind in ('pending', 'acknowledged')),
            'oldest_open_at': row['created_at'] if row else None, 'sla_breaches': breaches}


# LLM: null 与数字零分别覆盖；不同阶段独立评估，两阶段同时超时也只计一条记录。
# 函数用途: 经真实策略写入和独立负载请求验证可空 SLA 全组合。
@pytest.mark.parametrize('kind,index', [('pending', 0), ('acknowledged', 1), ('case', 2)])
@pytest.mark.parametrize('ack,resolve,elapsed,expected', [
    (5, 5, 600, (1, 1, 1)),
    (5, 5, 0, (0, 0, 0)),
    (5, None, 600, (1, None, None)),
    (5, None, 0, (0, None, None)),
    (None, 5, 600, (1, 1, 1)),
    (None, 5, 0, (0, 0, 0)),
    (None, None, 600, (None, None, None)),
    (None, None, 0, (None, None, None)),
    (5, 20, 600, (1, 0, 0)),
    (20, 5, 600, (1, 1, 1)),
    (0, 0, 0, (0, 0, 0)),
    (0, 0, 1, (1, 1, 1)),
    (5, 5, 300, (0, 0, 0)),
])
def test_workload_nullable_sla_http(native, monkeypatch, kind, index, ack, resolve, elapsed, expected):
    row = _workload_record(native, kind)
    policy = {'space_id': row['space_id'], 'policies': [
        {'severity': 'high', 'ack_minutes': ack, 'resolve_minutes': resolve}]}
    key = uuid.uuid4().hex
    saved = completed(wire(native, 'monitors.sla.set', policy, key, 0))
    assert saved == {'policies': policy['policies'], 'revision': 1}
    assert completed(wire(native, 'monitors.sla.set', policy, key, 0)) == saved
    # 只固定聚合的观察时刻；HTTP 身份、领域写入及原生存储均保持真实入口。
    observed_at = datetime.fromisoformat(row['created_at']) + timedelta(seconds=elapsed)
    monkeypatch.setattr(cases, 'w8_now', lambda call: observed_at.isoformat())
    _assert_workload(native, {'space_id': row['space_id']}, {
        'analysts': [], 'unassigned': _expected_workload_load(
            kind=kind, row=row, breaches=expected[index]), 'coverage': 'complete'})
    current = completed(wire(native, 'monitors.sla.get', {'space_id': row['space_id']}))
    assert current['policies'] == policy['policies']


# LLM: 其它严重度或空间的策略不授予当前记录 SLA 目标。
# 函数用途: 验证无策略和不匹配策略继续返回未知。
@pytest.mark.parametrize('policy_scope', ['missing', 'different_severity', 'different_space'])
def test_workload_without_applicable_policy_http(native, policy_scope):
    row = _workload_record(native, 'case')
    if policy_scope != 'missing':
        sid = row['space_id']
        severity = 'critical'
        if policy_scope == 'different_space':
            sid = completed(wire(native, 'spaces.create', {'name': '另一策略空间'}))['space_id']
            severity = 'high'
        completed(wire(native, 'monitors.sla.set', {'space_id': sid, 'policies': [
            {'severity': severity, 'ack_minutes': 0, 'resolve_minutes': 0}]}, revision=0))
    _assert_workload(native, {'space_id': row['space_id']}, {
        'analysts': [], 'unassigned': _expected_workload_load(kind='case', row=row), 'coverage': 'complete'})


# LLM: 同组部分记录可评估时返回实际计数；关闭记录不计入，也不能被只读查询回写。
# 函数用途: 验证混合阶段、稳定排序、归属筛选及终态保留。
def test_workload_mixed_stages_and_closed_records_http(native, monkeypatch):
    case = _workload_record(native, 'case', assignee='zeta')
    case = completed(wire(native, 'soc.cases.update', {
        'case_id': case['case_id'], 'status': 'investigating'}, revision=case['revision']))['case']
    _workload_record(native, 'pending', assignee='zeta')
    acknowledged = _workload_record(native, 'acknowledged', assignee='alpha')
    unassigned = _workload_record(native, 'case')
    closed = _workload_record(native, 'case', assignee='zeta')
    closed = completed(wire(native, 'soc.cases.update', {
        'case_id': closed['case_id'], 'status': 'closed'}, revision=closed['revision']))['case']
    resolved = _workload_record(native, 'pending', assignee='zeta')
    resolved = completed(wire(native, 'soc.alerts.update', {
        'alert_id': resolved['alert_id'], 'status': 'resolved'}, revision=resolved['revision']))['alert']
    completed(wire(native, 'monitors.sla.set', {'space_id': case['space_id'], 'policies': [
        {'severity': 'high', 'ack_minutes': 5, 'resolve_minutes': None}]}, revision=0))
    observed_at = datetime.fromisoformat(case['created_at']) + timedelta(minutes=10)
    monkeypatch.setattr(cases, 'w8_now', lambda call: observed_at.isoformat())
    alpha = _expected_workload_load(kind='acknowledged', row=acknowledged, assignee='alpha')
    zeta = {**_expected_workload_load(kind='case', row=case, breaches=1, assignee='zeta'),
            'investigating_cases': 1, 'open_alerts': 1}
    _assert_workload(native, {'space_id': case['space_id']}, {
        'analysts': [alpha, zeta], 'unassigned': _expected_workload_load(kind='case', row=unassigned),
        'coverage': 'complete'})
    _assert_workload(native, {'space_id': case['space_id'], 'assignee': 'alpha'}, {
        'analysts': [alpha], 'unassigned': _expected_workload_load(), 'coverage': 'complete'})
    _assert_workload(native, {'space_id': case['space_id'], 'assignee': 'missing'}, {
        'analysts': [], 'unassigned': _expected_workload_load(), 'coverage': 'complete'})
    assert completed(wire(native, 'soc.cases.get', {'case_id': closed['case_id']}))['case'] == closed
    assert completed(wire(native, 'soc.alerts.get', {'alert_id': resolved['alert_id']}))['alert'] == resolved


# LLM: 策略 authority 来自当前 owner 和耐久修订，旧回执只能重放，不能覆盖新策略。
# 函数用途: 验证策略 CAS、幂等冲突、跨身份拒绝及新修订读取。
def test_workload_policy_revision_and_owner_controls_http(native, monkeypatch):
    row = _workload_record(native, 'case')
    initial = {'space_id': row['space_id'], 'policies': [
        {'severity': 'high', 'ack_minutes': None, 'resolve_minutes': None}]}
    current = {'space_id': row['space_id'], 'policies': [
        {'severity': 'high', 'ack_minutes': None, 'resolve_minutes': 5}]}
    old_key = uuid.uuid4().hex
    saved = completed(wire(native, 'monitors.sla.set', initial, old_key, 0))
    assert saved == {'policies': initial['policies'], 'revision': 1}
    rejected(wire(native, 'monitors.sla.set', current, revision=0), 409, 'REVISION_CONFLICT')
    rejected(wire(native, 'monitors.sla.set', current, old_key, 0), 409, 'IDEMPOTENCY_CONFLICT')
    assert completed(wire(native, 'monitors.sla.set', current, revision=1)) == {
        'policies': current['policies'], 'revision': 2}
    assert completed(wire(native, 'monitors.sla.set', initial, old_key, 0)) == saved
    observed_at = datetime.fromisoformat(row['created_at']) + timedelta(minutes=10)
    monkeypatch.setattr(cases, 'w8_now', lambda call: observed_at.isoformat())
    _assert_workload(native, {'space_id': row['space_id']}, {
        'analysts': [], 'unassigned': _expected_workload_load(kind='case', row=row, breaches=1),
        'coverage': 'complete'})
    assert completed(wire(native, 'monitors.sla.get', {'space_id': row['space_id']}))['policies'] == current['policies']
    rejected(wire(native, 'soc.cases.get', {'case_id': row['case_id']}, user='other-owner'),
             404, 'RESOURCE_NOT_FOUND')
    _assert_workload(native, {}, {
        'analysts': [], 'unassigned': _expected_workload_load(), 'coverage': 'complete'}, user='other-owner')


# LLM: 枚举结束再 fstat，排除采样器自己的目录 FD；只容忍已关闭的描述符。
# 函数用途: 取得真实打开的 FD 及其文件身份。
def _workload_fd_snapshot():
    directory = next(path for path in (Path('/proc/self/fd'), Path('/dev/fd')) if path.is_dir())
    with os.scandir(directory) as entries:
        numbers = [int(entry.name) for entry in entries if entry.name.isdecimal()]
    result = {}
    for number in numbers:
        try:
            status = os.fstat(number)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
        else:
            result[number] = (status.st_dev, status.st_ino, status.st_mode)
    return result


# LLM: 真实 FD 已打开后单次注入 EIO；观察器始终执行原集合读口，不伪造实现或响应。
# 函数用途: 验证预热成功、真实读取失败和重试均无资源泄漏。
@pytest.mark.parametrize('fail_read', [False, True])
def test_workload_warmed_http_fd_cleanup(native, monkeypatch, fail_read):
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    row = _workload_record(native, 'pending')
    completed(wire(native, 'monitors.sla.set', {'space_id': row['space_id'], 'policies': [
        {'severity': 'high', 'ack_minutes': 5, 'resolve_minutes': 5}]}, revision=0))
    params = {'space_id': row['space_id']}
    expected = {'analysts': [], 'unassigned': _expected_workload_load(
        kind='pending', row=row, breaches=0), 'coverage': 'complete'}
    _assert_workload(native, params, expected)
    owner = resolve_owner_home(native[1].home_paths.root, OwnerIdentity.provider_user('feishu', 'business-alice'))
    target = owner.home_dir / 'data' / 'business_v13' / 'soc_alerts.json'
    source = target.stat()
    original_bytes = target.read_bytes()
    original_read = os.read
    original_rows = cases.w8_rows
    opened, faults, deltas, collections = [], [], [], []

    # LLM: 只匹配已授权测试集合的实际 inode，其余读取保持原生行为。
    # 函数用途: 在原生读取点记录 FD，并按参数触发一次 I/O 故障。
    def read(descriptor, size):
        status = os.fstat(descriptor)
        if (status.st_dev, status.st_ino) == (source.st_dev, source.st_ino):
            opened.append((descriptor, (status.st_dev, status.st_ino, status.st_mode)))
            if fail_read and not faults:
                faults.append(descriptor)
                raise OSError(errno.EIO, 'injected workload collection read failure')
        return original_read(descriptor, size)

    # LLM: 在实际资源获取边界观察原调用，已有 HTTP 工作线程也必须被覆盖。
    # 函数用途: 记录每次原生集合读取成功或失败后的 FD 差额。
    def rows(call, collection):
        before = _workload_fd_snapshot()
        try:
            return original_rows(call, collection)
        finally:
            deltas.append(len(_workload_fd_snapshot()) - len(before))
            collections.append(collection)

    with monkeypatch.context() as observer:
        observer.setattr(cases, 'w8_rows', rows)
        observer.setattr(os, 'read', read)
        if fail_read:
            failed = rejected(wire(native, 'cases.workload', params), 503, 'HOST_NOT_READY')
            assert failed['effect_outcome'] == 'not_started' and failed['operation_id'] is None
        _assert_workload(native, params, expected)
    assert len(faults) == int(fail_read)
    sequence = ['soc_cases', 'soc_alerts', 'monitor_sla_policies']
    assert collections == (sequence[:2] if fail_read else []) + sequence
    assert len(deltas) == len(collections)
    assert all(delta == 0 for delta in deltas), deltas
    assert opened, '未进入目标集合的真实 FD 读取点'
    after = _workload_fd_snapshot()
    assert all(after.get(number) != identity for number, identity in opened)
    assert target.read_bytes() == original_bytes
    print('workload FD evidence:', {'faults': len(faults), 'fd_delta': deltas,
                                    'source_handles_closed': True})
