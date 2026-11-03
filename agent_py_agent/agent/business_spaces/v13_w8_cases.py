"""案件、实体关联与协同投影；存储、游标及活跃身份统一经过 W8/W0 边界。"""
from __future__ import annotations

import hashlib
import math
from datetime import date, datetime

from ..plugin_host.errors import HostAPIError
from .v13_w8_alerts import (
    _epoch,
    w8_access,
    w8_check_revision,
    w8_feedback,
    w8_get,
    w8_now,
    w8_page,
    w8_rows,
    w8_schema_error,
    w8_space,
    w8_touch,
    w8_transaction,
    w8_view,
)


# LLM: 写回必须读取锁内记录，不使用锁外快照覆盖。
# 函数用途: 在事务表中查找并核验所属身份与空间。
def _record(call, tables, collection, record_id):
    row = tables[collection].get(record_id)
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    w8_access(call, row)
    return row


# LLM: 关联不可跨空间，即使同一身份可见两个空间；错误保持 W8 的 RESOURCE_NOT_FOUND 合同。
# 函数用途: 核验两个已授权记录的空间一致。
def _same_space(left, right):
    if left['space_id'] != right['space_id']:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)


# LLM: 所有关联入口维护双向引用，旧案件不能残留移出的告警。
# 函数用途: 在同一事务内迁移告警关联并递增被影响案件版本。
def _link(call, tables, case, alert_ids, *, touch_alert=True):
    for alert_id in dict.fromkeys(alert_ids):
        alert = _record(call, tables, 'soc_alerts', alert_id)
        _same_space(case, alert)
        old_id = alert.get('case_id')
        if old_id and old_id != case['case_id']:
            old = _record(call, tables, 'soc_cases', old_id)
            _same_space(case, old)
            old['alert_ids'] = [item for item in old['alert_ids'] if item != alert_id]
            w8_touch(call, old)
        if alert_id not in case['alert_ids']:
            case['alert_ids'].append(alert_id)
        if alert.get('case_id') != case['case_id']:
            alert['case_id'] = case['case_id']
            if touch_alert:
                w8_touch(call, alert)
    if len(case['alert_ids']) > 1000:
        raise HostAPIError('LIMIT_EXCEEDED', 400)


# LLM: 年序号扫描包括关闭案件，避免重用历史编号。
# 函数用途: 从事务内当前所属身份记录生成案件并关联告警。
def _new_case(call, tables, space_id, title, severity, alert_ids):
    now = w8_now(call)
    prefix = 'CASE-' + now[:4] + '-'
    numbers = [int(key[len(prefix):]) for key, row in tables['soc_cases'].items()
               if row.get('owner_id') == call.owner_id and key.startswith(prefix)
               and key[len(prefix):].isdigit()]
    case_id = call.params.get('case_id') or prefix + str(max(numbers, default=0) + 1).zfill(3)
    if case_id in tables['soc_cases']:
        raise HostAPIError('STATE_CONFLICT', 409)
    row = {'case_id': case_id, 'owner_id': call.owner_id, 'space_id': space_id,
           'title': title, 'description': call.params.get('description', ''),
           'severity': severity, 'status': 'open', 'assignee': call.params.get('assignee'),
           'alert_ids': [], 'merged_into': None, 'tags': call.params.get('tags', []),
           'custom_values': {}, 'closed_at': None, 'revision': 0,
           'created_at': now, 'updated_at': now}
    tables['soc_cases'][case_id] = row
    _link(call, tables, row, alert_ids)
    return row


# LLM: 分页的过滤范围参与公共游标绑定。
# 函数用途: 列出所属身份可见案件并按结构化条件筛选。
def list_cases(call):
    scope = {key: value for key, value in call.params.items() if key not in {'cursor', 'limit'}}
    rows = [row for row in w8_rows(call, 'soc_cases')
            if all(row.get(key) == value for key, value in scope.items())]
    rows.sort(key=lambda row: (_epoch(row['created_at']) or 0, row['case_id']), reverse=True)
    return w8_page(call, 'soc_cases', [w8_view(row) for row in rows], scope)


# LLM: 创建与关联告警同事务提交，幂等由公共边界记录。
# 函数用途: 创建指定或年序号案件。
def create_case(call):
    space_id = w8_space(call)

    # LLM: 锁内生成序号避免并发覆盖。
    # 函数用途: 原子创建案件及其关联。
    def mutate(tables):
        row = _new_case(call, tables, space_id, call.params['title'],
                        call.params['severity'], call.params.get('alert_ids', []))
        return {'case': w8_view(row)}

    return w8_transaction(call, ('soc_cases', 'soc_alerts'), mutate)


# LLM: 不根据案件中的裸引用直接读取其它身份的告警。
# 函数用途: 返回案件与已核验关联告警，受响应上限约束。
def get_case(call):
    row = w8_get(call, 'soc_cases', call.params['case_id'])
    alerts = _case_alerts(call, row)
    return {'case': w8_view(row), 'alerts': [w8_view(alert) for alert in alerts[:200]]}


# LLM: CAS在锁内执行，返回当前写入后的版本。
# 函数用途: 更新案件属性并原子增删双向关联。
def update_case(call):
    # LLM: 所有校验失败都会由公共事务撤回整个变更。
    # 函数用途: 修改当前版本并维护告警引用。
    def mutate(tables):
        row = _record(call, tables, 'soc_cases', call.params['case_id'])
        w8_check_revision(call, row)
        if row.get('merged_into'):
            raise HostAPIError('STATE_CONFLICT', 409)
        for key in ('title', 'description', 'severity', 'status', 'assignee', 'tags'):
            if key in call.params:
                row[key] = call.params[key]
        for alert_id in dict.fromkeys(call.params.get('remove_alert_ids', [])):
            alert = _record(call, tables, 'soc_alerts', alert_id)
            _same_space(row, alert)
            row['alert_ids'] = [item for item in row['alert_ids'] if item != alert_id]
            if alert.get('case_id') == row['case_id']:
                alert['case_id'] = None
                w8_touch(call, alert)
        _link(call, tables, row, call.params.get('add_alert_ids', []))
        if row['status'] == 'closed' and not row['closed_at']:
            row['closed_at'] = w8_now(call)
        w8_touch(call, row)
        return {'case': w8_view(row)}

    return w8_transaction(call, ('soc_cases', 'soc_alerts'), mutate)


# LLM: 先过滤所属身份及空间，再组装关联投影。
# 函数用途: 按时间稳定排序返回案件可见关联告警。
def _case_alerts(call, case):
    rows = [row for row in w8_rows(call, 'soc_alerts')
            if row.get('case_id') == case['case_id'] and row['space_id'] == case['space_id']]
    return sorted(rows, key=lambda row: (_epoch(row['created_at']) or 0, row['alert_id']))


# LLM: 哈希包含结构化类型和关系，防止同值异类碰撞。
# 函数用途: 生成稳定且有界的图谱标识。
def _graph_id(kind, values):
    value = '\x00'.join((kind, *values))
    return kind + '-' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]


# LLM: 图谱不携带私有存储字段。
# 函数用途: 构造闭合节点投影。
def _node(kind, record_id, label, attributes):
    return {'node_id': _graph_id(kind, (record_id,)), 'kind': kind,
            'label': label, 'attributes': attributes}


# LLM: 边标识取决于端点与关系，不依赖读取顺序。
# 函数用途: 构造确定性边。
def _edge(source, target, relation, at):
    return {'edge_id': _graph_id('edge', (source, target, relation)),
            'source': source, 'target': target, 'relation': relation, 'at': at}


# LLM: 响应上限不隐瞒截断，也不返回悬空边。
# 函数用途: 对图谱去重、排序并报告真实覆盖范围。
def _bounded_graph(nodes, edges):
    node_rows = sorted(nodes.values(), key=lambda row: row['node_id'])
    edge_rows = sorted(edges.values(), key=lambda row: row['edge_id'])
    partial = len(node_rows) > 2000 or len(edge_rows) > 5000
    node_rows = node_rows[:2000]
    allowed = {row['node_id'] for row in node_rows}
    edge_rows = [row for row in edge_rows if row['source'] in allowed and row['target'] in allowed][:5000]
    return {'nodes': node_rows, 'edges': edge_rows, 'coverage': 'partial' if partial else 'complete'}


# LLM: 实体类型和值决定同一节点，所有来源先经过所属身份过滤。
# 函数用途: 投影告警、实体、技术与案件之间的结构关系。
def _alert_graph(alerts, cases):
    nodes, edges = {}, {}
    for case in cases:
        node = _node('case', case['case_id'], case['title'], {'case_id': case['case_id']})
        nodes[node['node_id']] = node
    case_index = {case['case_id']: case for case in cases}
    for alert in alerts:
        node = _node('alert', alert['alert_id'], alert['title'], {'alert_id': alert['alert_id']})
        nodes[node['node_id']] = node
        case = case_index.get(alert.get('case_id'))
        if case and case['space_id'] == alert['space_id']:
            edge = _edge(_graph_id('case', (case['case_id'],)), node['node_id'], 'contains', alert['created_at'])
            edges[edge['edge_id']] = edge
        entities = []
        for entity in alert.get('entities', []):
            entity_node = _node('entity', entity['type'] + ':' + entity['value'], entity['value'], dict(entity))
            nodes[entity_node['node_id']] = entity_node
            entities.append(entity_node['node_id'])
            edge = _edge(node['node_id'], entity_node['node_id'], 'involves', alert['created_at'])
            edges[edge['edge_id']] = edge
        for left, right in zip(entities, entities[1:]):
            edge = _edge(left, right, 'observed_with', alert['created_at'])
            edges[edge['edge_id']] = edge
        for technique in alert.get('techniques', []):
            technique_node = _node('technique', technique, technique, {'technique_id': technique})
            nodes[technique_node['node_id']] = technique_node
            edge = _edge(node['node_id'], technique_node['node_id'], 'maps_to', alert['created_at'])
            edges[edge['edge_id']] = edge
    return nodes, edges


# LLM: 调查节点只纳入当前案件和同一空间的可见记录。
# 函数用途: 构建案件、告警、实体、调查和技术全图。
def case_graph(call):
    case = w8_get(call, 'soc_cases', call.params['case_id'])
    nodes, edges = _alert_graph(_case_alerts(call, case), [case])
    for run in w8_rows(call, 'soc_investigations'):
        if run.get('case_id') != case['case_id'] or run['space_id'] != case['space_id']:
            continue
        node = _node('investigation', run['run_id'], run['run_id'], {'run_id': run['run_id']})
        nodes[node['node_id']] = node
        edge = _edge(_graph_id('case', (case['case_id'],)), node['node_id'], 'investigated_by', run['created_at'])
        edges[edge['edge_id']] = edge
    return _bounded_graph(nodes, edges)


# LLM: 首次出现时间来自已排序的告警，重复实体只计一次。
# 函数用途: 返回按实体首次出现时间排序的路径。
def case_path(call):
    case = w8_get(call, 'soc_cases', call.params['case_id'])
    nodes = {}
    for alert in _case_alerts(call, case):
        for entity in alert['entities']:
            node = _node('entity', entity['type'] + ':' + entity['value'], entity['value'], dict(entity))
            nodes.setdefault(node['node_id'], (node, alert['created_at']))
    path = list(nodes.values())
    edges = [_edge(left[0]['node_id'], right[0]['node_id'], 'observed_with', right[1])
             for left, right in zip(path, path[1:])]
    return {'path': [node for node, at in path[:500]], 'edges': edges[:499],
            'coverage': 'partial' if len(path) > 500 else 'complete'}


# LLM: 未知技术保持空值，不根据文本推断战术。
# 函数用途: 按技术分组并以首次告警时间排序阶段。
def case_chain(call):
    case = w8_get(call, 'soc_cases', call.params['case_id'])
    groups = {}
    for alert in _case_alerts(call, case):
        for technique in dict.fromkeys(alert['techniques'] or [None]):
            groups.setdefault(technique, []).append(alert)
    ordered = sorted(groups.items(), key=lambda item: (_epoch(item[1][0]['created_at']) or 0, item[0] or ''))
    stages = [{'stage': index, 'technique_id': technique, 'tactic': None,
               'alert_ids': [alert['alert_id'] for alert in alerts[:200]],
               'first_seen_at': alerts[0]['created_at'], 'last_seen_at': alerts[-1]['created_at']}
              for index, (technique, alerts) in enumerate(ordered[:200])]
    partial = len(ordered) > 200 or any(len(alerts) > 200 for alerts in groups.values())
    return {'stages': stages, 'coverage': 'partial' if partial else 'complete'}


# LLM: 深度按共享实体扩展一跳，不将另一个空间作为邻居。
# 函数用途: 返回指定实体的一至二层告警关联图。
def entity_graph(call):
    entity = call.params['entity']
    alerts = w8_rows(call, 'soc_alerts')
    selected = [row for row in alerts if entity in row['entities']]
    if not selected:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if call.params.get('depth', 1) == 2:
        neighbors = {(row['space_id'], item['type'], item['value'])
                     for row in selected for item in row['entities']}
        selected = [row for row in alerts if any((row['space_id'], item['type'], item['value']) in neighbors
                                                for item in row['entities'])]
    case_ids = {(row['space_id'], row.get('case_id')) for row in selected}
    cases = [row for row in w8_rows(call, 'soc_cases') if (row['space_id'], row['case_id']) in case_ids]
    return _bounded_graph(*_alert_graph(selected, cases))


# LLM: 单条缺失与CAS失败分别回执，其它成功项仍在同一事务提交。
# 函数用途: 批量分诊并原子记录每次判定变更。
def bulk_triage(call):
    revisions = call.params.get('expected_revisions', {})
    if any(type(value) is not int or value < 0 for value in revisions.values()):
        w8_schema_error('params.expected_revisions')

    # LLM: 逐条校验之后才应用属性与双向关联。
    # 函数用途: 在一个锁内处理分诊结果和反馈。
    def mutate(tables):
        results = []
        for alert_id in call.params['alert_ids']:
            row = tables['soc_alerts'].get(alert_id)
            if row is None or row.get('owner_id') != call.owner_id:
                results.append({'alert_id': alert_id, 'revision': None, 'error_code': 'RESOURCE_NOT_FOUND'})
                continue
            try:
                w8_access(call, row)
            except HostAPIError as error:
                if error.status != 404:
                    raise
                results.append({'alert_id': alert_id, 'revision': None, 'error_code': 'RESOURCE_NOT_FOUND'})
                continue
            if alert_id in revisions and revisions[alert_id] != row['revision']:
                results.append({'alert_id': alert_id, 'revision': row['revision'], 'error_code': 'REVISION_CONFLICT'})
                continue
            original_revision = row['revision']
            if 'case_id' in call.params:
                case = _record(call, tables, 'soc_cases', call.params['case_id'])
                _same_space(case, row)
                if row.get('case_id') != case['case_id']:
                    _link(call, tables, case, [alert_id])
                    w8_touch(call, case)
            verdict = call.params.get('verdict')
            if verdict is not None and verdict != row['verdict']:
                feedback = w8_feedback(call, row, verdict, call.params.get('note', ''), 'bulk_triage')
                tables['soc_feedback'][feedback['override_id']] = feedback
                row['disposition'] = verdict
            status = call.params.get('status')
            if status in ('acknowledged', 'in_progress', 'resolved', 'closed') and not row['acknowledged_at']:
                row['acknowledged_at'] = w8_now(call)
            if status in ('resolved', 'closed') and not row['resolved_at']:
                row['resolved_at'] = w8_now(call)
            for key in ('status', 'verdict', 'severity', 'assignee'):
                if key in call.params:
                    row[key] = call.params[key]
            if row['revision'] == original_revision:
                w8_touch(call, row)
            results.append({'alert_id': alert_id, 'revision': row['revision'], 'error_code': None})
        return {'results': results}

    return w8_transaction(call, ('soc_alerts', 'soc_feedback', 'soc_cases'), mutate)


# LLM: 新建案件至少需要两个不同告警；严重度来自最严重的来源记录。
# 函数用途: 将告警合并到新案件或现有案件。
def merge_alerts(call):
    alert_ids = list(dict.fromkeys(call.params['alert_ids']))
    if not call.params.get('case_id') and len(alert_ids) < 2:
        w8_schema_error('params.alert_ids')
    space_id = w8_space(call) if not call.params.get('case_id') else None

    # LLM: 所有来源与目标先在锁内验证。
    # 函数用途: 原子迁移关联，必要时创建新案件。
    def mutate(tables):
        alerts = [_record(call, tables, 'soc_alerts', item) for item in alert_ids]
        if call.params.get('case_id'):
            row = _record(call, tables, 'soc_cases', call.params['case_id'])
            if row.get('merged_into'):
                raise HostAPIError('STATE_CONFLICT', 409)
            _link(call, tables, row, alert_ids)
            w8_touch(call, row)
        else:
            severity_order = ('info', 'low', 'medium', 'high', 'critical')
            primary = max(alerts, key=lambda alert: severity_order.index(alert['severity']))
            row = _new_case(call, tables, space_id, call.params.get('title', primary['title']),
                            primary['severity'], alert_ids)
        return {'case': w8_view(row)}

    return w8_transaction(call, ('soc_cases', 'soc_alerts'), mutate)


# LLM: 目标CAS与来源关闭、关联迁移必须同事务提交。
# 函数用途: 合并案件，保留来源的关闭时间及归并指向。
def merge_cases(call):
    # LLM: 来源已归并不可再次归并，防止环或多目标覆盖。
    # 函数用途: 核验目标版本并原子归并来源。
    def mutate(tables):
        target_id = call.params['target_case_id']
        source_ids = list(dict.fromkeys(call.params['source_case_ids']))
        if target_id in source_ids:
            raise HostAPIError('STATE_CONFLICT', 409)
        target = _record(call, tables, 'soc_cases', target_id)
        w8_check_revision(call, target)
        if target.get('merged_into'):
            raise HostAPIError('STATE_CONFLICT', 409)
        sources = [_record(call, tables, 'soc_cases', item) for item in source_ids]
        for source in sources:
            _same_space(target, source)
            if source.get('merged_into'):
                raise HostAPIError('STATE_CONFLICT', 409)
        for source in sources:
            linked = list(dict.fromkeys(source['alert_ids'] + [row['alert_id'] for row in tables['soc_alerts'].values()
                            if row.get('owner_id') == call.owner_id and row.get('case_id') == source['case_id']]))
            _link(call, tables, target, linked)
            source['alert_ids'] = []
            source['status'] = 'closed'
            source['merged_into'] = target_id
            source['closed_at'] = source['closed_at'] or w8_now(call)
            w8_touch(call, source)
        w8_touch(call, target)
        return {'case': w8_view(target), 'merged': source_ids}

    return w8_transaction(call, ('soc_cases', 'soc_alerts'), mutate)


# LLM: SLA未知时保留空值，不把缺少策略当成零违约。
# 函数用途: 汇总分析师在办案件和告警负载。
def workload(call):
    cases = [row for row in w8_rows(call, 'soc_cases') if row['status'] not in ('resolved', 'closed')]
    alerts = [row for row in w8_rows(call, 'soc_alerts') if row['status'] not in ('resolved', 'closed')]
    policies = {row['space_id']: row['policies'] for row in w8_rows(call, 'monitor_sla_policies')}
    selected = call.params.get('assignee')
    groups = {None: {'assignee': None, 'open_cases': 0, 'investigating_cases': 0,
                      'open_alerts': 0, 'oldest_open_at': None, 'sla_breaches': None}}
    now = datetime.fromisoformat(w8_now(call)).timestamp()
    for row in cases + alerts:
        assignee = row['assignee']
        if selected is not None and assignee != selected:
            continue
        load = groups.setdefault(assignee, {'assignee': assignee, 'open_cases': 0,
                    'investigating_cases': 0, 'open_alerts': 0, 'oldest_open_at': None, 'sla_breaches': None})
        field = 'open_alerts' if 'alert_id' in row else 'open_cases'
        load[field] += 1
        if row['status'] == 'investigating':
            load['investigating_cases'] += 1
        at = row['created_at']
        if at and (load['oldest_open_at'] is None or _epoch(at) < _epoch(load['oldest_open_at'])):
            load['oldest_open_at'] = at
        matching = [policy for policy in policies.get(row['space_id'], []) if policy['severity'] == row['severity']]
        if matching and at:
            policy = matching[0]
            load['sla_breaches'] = load['sla_breaches'] or 0
            elapsed = (now - datetime.fromisoformat(at.replace('Z', '+00:00')).timestamp()) / 60
            ack_pending = 'alert_id' in row and row['acknowledged_at'] is None
            if elapsed > policy['resolve_minutes'] or (ack_pending and elapsed > policy['ack_minutes']):
                load['sla_breaches'] += 1
    analysts = [groups[key] for key in sorted(key for key in groups if key is not None)]
    return {'analysts': analysts[:500], 'unassigned': groups[None],
            'coverage': 'partial' if len(analysts) > 500 else 'complete'}


# LLM: 字段定义归属于空间，空定义的初始版本明确为零。
# 函数用途: 查询空间的案件扩展字段。
def list_custom_fields(call):
    space_id = w8_space(call)
    rows = [row for row in w8_rows(call, 'soc_case_custom_fields') if row['space_id'] == space_id]
    if not rows:
        return {'fields': [], 'revision': 0}
    return {'fields': rows[0]['fields'], 'revision': rows[0]['revision']}


# LLM: 不接受重复标识，以免同一值按不同类型解释。
# 函数用途: 按版本替换空间字段定义。
def upsert_custom_fields(call):
    space_id = w8_space(call)
    fields = call.params['fields']
    if len({field['field_id'] for field in fields}) != len(fields):
        w8_schema_error('params.fields')

    # LLM: 首次写也比较零版本，避免并发初始创建覆盖。
    # 函数用途: 原子保存定义及其版本。
    def mutate(tables):
        row = tables['soc_case_custom_fields'].get(space_id)
        if row is None:
            row = {'space_id': space_id, 'owner_id': call.owner_id, 'fields': [],
                   'revision': 0, 'created_at': w8_now(call), 'updated_at': w8_now(call)}
            tables['soc_case_custom_fields'][space_id] = row
        else:
            w8_access(call, row)
        w8_check_revision(call, row)
        row['fields'] = fields
        w8_touch(call, row)
        return {'fields': fields, 'revision': row['revision']}

    return w8_transaction(call, ('soc_case_custom_fields',), mutate)


# LLM: 扩展值使用案件版本，不另建第二套版本账。
# 函数用途: 查询一宗案件的扩展值及版本。
def get_custom_values(call):
    row = w8_get(call, 'soc_cases', call.params['case_id'])
    return {'case_id': row['case_id'], 'values': row['custom_values'], 'revision': row['revision']}


# LLM: 布尔值不是数值；日期必须是真实日历日期。
# 函数用途: 按定义验证单个扩展字段值。
def _valid_custom_value(field, value):
    kind = field['type']
    if kind == 'boolean':
        return type(value) is bool
    if kind == 'number':
        return type(value) is int or (type(value) is float and math.isfinite(value))
    if kind in ('string', 'user'):
        return isinstance(value, str)
    if kind == 'enum':
        return isinstance(value, str) and value in field['options']
    if kind == 'date':
        if not isinstance(value, str):
            return False
        try:
            return date.fromisoformat(value).isoformat() == value
        except ValueError:
            return False
    return False


# LLM: 锁内同时读取定义与案件，防止校验后定义被并发替换。
# 函数用途: 验证并按CAS替换案件扩展值。
def set_custom_values(call):
    values = call.params['values']

    # LLM: 未知、缺必填和类型错误都定位到具体字段。
    # 函数用途: 在同一事务中验证定义并保存值。
    def mutate(tables):
        row = _record(call, tables, 'soc_cases', call.params['case_id'])
        w8_check_revision(call, row)
        definition = tables['soc_case_custom_fields'].get(row['space_id'])
        if definition:
            w8_access(call, definition)
        fields = {field['field_id']: field for field in definition['fields']} if definition else {}
        for field_id, value in values.items():
            if field_id not in fields or not _valid_custom_value(fields[field_id], value):
                w8_schema_error('params.values.' + field_id)
        for field_id, field in fields.items():
            if field['required'] and field_id not in values:
                w8_schema_error('params.values.' + field_id)
        row['custom_values'] = values
        w8_touch(call, row)
        return {'case_id': row['case_id'], 'values': values, 'revision': row['revision']}

    return w8_transaction(call, ('soc_cases', 'soc_case_custom_fields'), mutate)


# LLM: 房间DTO不暴露存储版本或空间私有字段。
# 函数用途: 投影正式作战室响应。
def _room_view(row):
    return {key: row[key] for key in ('war_room_id', 'case_id', 'status', 'agent_id',
            'thread_id', 'participants', 'opened_by', 'opened_at')}


# LLM: 先确认案件可见，未知案件不能用空房间成功掩盖。
# 函数用途: 查询案件的协作作战室。
def get_war_room(call):
    case = w8_get(call, 'soc_cases', call.params['case_id'])
    rows = [row for row in w8_rows(call, 'soc_war_rooms') if row['case_id'] == case['case_id']]
    if not rows:
        return {'war_room': None}
    _same_space(case, rows[0])
    return {'war_room': _room_view(rows[0])}


# LLM: 同一案件只创建一个原生协作线程，重放不再次创建代理。
# 函数用途: 原子登记案件作战室并复用原生消息线程。
def open_war_room(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen

    # LLM: 锁内查重；原生创建使用案件绑定幂等键，写入失败重试也不重复创建。
    # 函数用途: 创建或返回唯一房间。
    def mutate(tables):
        case = _record(call, tables, 'soc_cases', call.params['case_id'])
        existing = tables['soc_war_rooms'].get(case['case_id'])
        if existing:
            w8_access(call, existing)
            _same_space(case, existing)
            return {'war_room': _room_view(existing)}
        key = 'warroom-' + hashlib.sha256((call.owner_id + '\x00' + case['case_id']).encode()).hexdigest()
        result = v13_call_frozen(call, 'background_agents.create', {
            'space_id': case['space_id'], 'name': ('war-room ' + case['case_id'])[:120],
            'purpose': '案件协同讨论', 'after_task': 'reuse'}, idempotency_key=key)
        if not isinstance(result, dict) or not isinstance(result.get('agent'), dict):
            raise HostAPIError('HOST_NOT_READY', 503)
        agent = result['agent']
        if not agent.get('agent_id') or not agent.get('thread_id'):
            raise HostAPIError('HOST_NOT_READY', 503)
        now = w8_now(call)
        row = {'case_id': case['case_id'], 'owner_id': call.owner_id,
               'war_room_id': 'ROOM-' + hashlib.sha256(key.encode()).hexdigest()[:24],
               'space_id': case['space_id'], 'status': 'open', 'agent_id': agent['agent_id'],
               'thread_id': agent['thread_id'], 'participants': list(dict.fromkeys(call.params.get('participants', []))),
               'opened_by': call.subject_id, 'opened_at': now, 'revision': 0,
               'created_at': now, 'updated_at': now}
        tables['soc_war_rooms'][case['case_id']] = row
        return {'war_room': _room_view(row)}

    return w8_transaction(call, ('soc_cases', 'soc_war_rooms'), mutate)
