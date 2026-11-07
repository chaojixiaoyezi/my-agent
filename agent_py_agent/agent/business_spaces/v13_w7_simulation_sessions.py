"""检测覆盖快照与桌面推演记录；只保存结构化事实，不执行外部动作。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

from ..plugin_host.errors import HostAPIError
from .v13_store import v13_store_for


# LLM: 缺省只选择当前 owner 唯一的活动空间；多个候选不得凭名称或排序猜测。
# 函数用途: 为可选空间参数补上有界、只读的唯一空间解析。
def resolve_space(call, space_id=None):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_resolve_space

    selected = space_id or call.params.get('space_id')
    if selected is None:
        page = v13_call_frozen(call, 'spaces.list', {'state': 'active', 'limit': 2})
        if not page['items']:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if len(page['items']) != 1 or page['next_cursor'] is not None:
            raise HostAPIError('STATE_CONFLICT', 409)
        selected = page['items'][0]['space_id']
    return v13_resolve_space(call, selected)


# LLM: 时间仅来自调用的可信时钟，所有记录使用同一 UTC 格式。
# 函数用途: 返回当前记录时间。
def _now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 回执摘要包括动作、参数和记录 CAS，跨实例重放不会重复变更。
# 函数用途: 计算本次请求摘要。
def _digest(call):
    payload = [call.action, call.params, call.expected_revision]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


# LLM: owner 字段只用于权限核验，不属于前端闭合结果。
# 函数用途: 投影公开业务字段。
def _view(row):
    return {key: value for key, value in row.items() if key != 'owner_id'}


# LLM: 缺失或其他 owner 必须是 404；在读取结果前核当前授权。
# 函数用途: 读取并授权一条真实记录。
def _get(call, store, collection, record_id):
    from ..gateway_parts.business_api_v13 import v13_require_access

    row = store.get(collection, record_id)
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    v13_require_access(call, row)
    return row


# LLM: CAS 在集合锁内比较记录版本，不混用 Store 的集合级版本。
# 函数用途: 检查本次写入的记录版本。
def _cas(call, row):
    if type(call.expected_revision) is not int or call.expected_revision != row['revision']:
        raise HostAPIError('REVISION_CONFLICT', 409)


# LLM: 所有引用必须在同一真实空间，owner 合法不代表跨空间合法。
# 函数用途: 校验可选规则或告警引用。
def _reference(call, store, collection, record_id, space_id):
    if record_id is None:
        return
    row = _get(call, store, collection, record_id)
    if row['space_id'] != space_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)


# LLM: Store 回执先于 mutate 返回；授权回调必须在首次与重放路径都携带真实记录。
# 函数用途: 为记录写入绑定实时空间权限复核。
def _record_store(call, collection, record_id):
    store = v13_store_for(call)

    # LLM: Store 会在锁内和提交前调用本回调，读操作不取得第二把写锁。
    # 函数用途: 拒绝已撤权记录的回执重放。
    def authorize():
        row = _get(call, store, collection, record_id)
        if collection == 'simulation_executions':
            _get(call, store, 'simulation_sessions', row['session_id'])
        return True

    store.authorize = authorize
    return store


# LLM: 同一空间前后快照在同一集合锁内选择；没有技术目录不编造覆盖分母。
# 函数用途: 保存当前规则覆盖与上一快照的差异。
def drift_snapshot(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    store = v13_store_for(call)
    space_id = resolve_space(call)['space_id']
    rules = [row for row in store.list('monitor_rules')
             if row.get('owner_id') == call.owner_id and row.get('space_id') == space_id]
    for row in rules:
        v13_require_access(call, row)
    enabled = [row for row in rules if row['enabled']]
    techniques = sorted({item for row in enabled for item in row['mitre_techniques']})
    if len(techniques) > 1000:
        raise HostAPIError('LIMIT_EXCEEDED')

    # LLM: 快照回执和差异一起提交，重放不会新建第二份快照。
    # 函数用途: 在当前集合视图上追加快照。
    def mutate(records):
        previous = sorted((row for row in records.values()
                           if row.get('owner_id') == call.owner_id and row.get('space_id') == space_id),
                          key=lambda row: row['revision'])
        delta = None
        if previous:
            old = previous[-1]
            delta = {'rules_total': len(rules) - old['rules_total'],
                     'rules_enabled': len(enabled) - old['rules_enabled'],
                     'techniques_added': sorted(set(techniques) - set(old['techniques_covered'])),
                     'techniques_removed': sorted(set(old['techniques_covered']) - set(techniques))}
        at, snapshot_id = _now(call), 'snapshot-' + uuid.uuid4().hex
        row = {'snapshot_id': snapshot_id, 'owner_id': call.owner_id, 'space_id': space_id,
               'rules_total': len(rules), 'rules_enabled': len(enabled),
               'techniques_covered': techniques, 'coverage_ratio': None,
               'delta_since_previous': delta, 'revision': previous[-1]['revision'] + 1 if previous else 1,
               'created_at': at, 'updated_at': at}
        records[snapshot_id] = row
        return {'snapshot': {key: row[key] for key in (
            'snapshot_id', 'space_id', 'rules_total', 'rules_enabled', 'techniques_covered',
            'coverage_ratio', 'delta_since_previous', 'created_at')}}

    return store.transaction('detection_drift_snapshots', mutate,
                             idempotency_key=call.idempotency_key, params_digest=_digest(call))


# LLM: 会话归属由真实空间和实时身份决定，调用 JSON 不能指定 owner。
# 函数用途: 创建一个未完成的桌面推演。
def create_session(call):
    store = v13_store_for(call)
    space_id = resolve_space(call)['space_id']

    # LLM: 首次响应与持久回执指向同一会话编号。
    # 函数用途: 原子创建会话记录。
    def mutate(records):
        at, session_id = _now(call), 'simulation-' + uuid.uuid4().hex
        row = {'session_id': session_id, 'space_id': space_id, 'owner_id': call.owner_id,
               'scenario': call.params['scenario'], 'title': call.params['title'],
               'objectives': call.params.get('objectives', []),
               'participants': call.params.get('participants', []), 'status': 'active',
               'findings': [], 'summary': None, 'completed_at': None,
               'revision': 1, 'created_at': at, 'updated_at': at}
        records[session_id] = row
        return {'session': _view(row)}

    return store.transaction('simulation_sessions', mutate,
                             idempotency_key=call.idempotency_key, params_digest=_digest(call))


# LLM: 会话、执行和回执沿 W0 多集合事务同笔提交，不暴露半条关联。
# 函数用途: 登记发现并按需创建技术验证执行。
def add_finding(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    session_id = call.params['session_id']
    store = _record_store(call, 'simulation_sessions', session_id)

    # LLM: 终态和 CAS 在锁内读取，禁止旧快照复活已归档会话。
    # 函数用途: 追加一个有界发现和关联执行快照。
    def mutate(tables):
        row = tables['simulation_sessions'].get(session_id)
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call, row)
        _cas(call, row)
        if row['status'] != 'active':
            raise HostAPIError('STATE_CONFLICT', 409)
        if len(row['findings']) >= 500:
            raise HostAPIError('LIMIT_EXCEEDED')
        _reference(call, store, 'monitor_rules', call.params.get('detection_rule_id'), row['space_id'])
        at, technique_id = _now(call), call.params.get('technique_id')
        execution_id = 'simulation-exec-' + uuid.uuid4().hex if technique_id else None
        finding = {'finding_id': 'finding-' + uuid.uuid4().hex,
                   'title': call.params['title'], 'description': call.params.get('description', ''),
                   'severity': call.params['severity'], 'technique_id': technique_id,
                   'detection_rule_id': call.params.get('detection_rule_id'),
                   'execution_id': execution_id, 'created_by': call.subject_id, 'created_at': at}
        execution = None if execution_id is None else {
            'execution_id': execution_id, 'session_id': session_id, 'technique_id': technique_id,
            'detected': call.params.get('detected'), 'detection_rule_id': call.params.get('detection_rule_id'),
            'alert_id': None, 'notes': None, 'revision': 1, 'created_at': at, 'updated_at': at}
        row['findings'].append(finding)
        row.update(revision=row['revision'] + 1, updated_at=at)
        if execution is not None:
            tables['simulation_executions'][execution_id] = {**execution, 'owner_id': call.owner_id}
        return {'session': _view(row), 'execution': execution}

    return store.transaction_many(('simulation_sessions', 'simulation_executions'), mutate,
                                  idempotency_key=call.idempotency_key, params_digest=_digest(call))


# LLM: 读取只返回真实 execution；不同观察版本明确未就绪，不捏造完整关联。
# 函数用途: 读取会话及其关联验证执行。
def get_session(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    store = v13_store_for(call)
    row = _get(call, store, 'simulation_sessions', call.params['session_id'])
    expected = {finding['execution_id'] for finding in row['findings'] if finding['execution_id']}
    executions = [item for item in store.list('simulation_executions')
                  if item.get('owner_id') == call.owner_id and item['session_id'] == row['session_id']]
    for item in executions:
        v13_require_access(call, item)
    if expected != {item['execution_id'] for item in executions}:
        raise HostAPIError('HOST_NOT_READY', 503)
    return {'session': _view(row), 'executions': [_view(item) for item in executions]}


# LLM: completed 为不可逆终态；同键回放仍取首次成功，异键重做必须冲突。
# 函数用途: 完成并归档桌面推演。
def complete(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    store = _record_store(call, 'simulation_sessions', call.params['session_id'])

    # LLM: 锁内取得最新会话再验证版本，不用外部旧快照覆盖 findings。
    # 函数用途: 原子推进会话终态。
    def mutate(records):
        row = records.get(call.params['session_id'])
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call, row)
        _cas(call, row)
        if row['status'] != 'active':
            raise HostAPIError('STATE_CONFLICT', 409)
        at = _now(call)
        row.update(status='completed', summary=call.params.get('summary'), completed_at=at,
                   updated_at=at, revision=row['revision'] + 1)
        return {'session': _view(row)}

    return store.transaction('simulation_sessions', mutate,
                             idempotency_key=call.idempotency_key, params_digest=_digest(call))


# LLM: 规则和告警必须属于同一会话空间；记录 CAS 在 execution 集合锁内检查。
# 函数用途: 更新一次技术验证的实际检测证据。
def update_detection(call):
    from ..gateway_parts.business_api_v13 import v13_require_access

    store = _record_store(call, 'simulation_executions', call.params['execution_id'])

    # LLM: 不改会话归档状态，后续验证只能修改本次执行的显式字段。
    # 函数用途: 保存经过引用校验的执行证据。
    def mutate(records):
        row = records.get(call.params['execution_id'])
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        v13_require_access(call, row)
        _cas(call, row)
        session = _get(call, store, 'simulation_sessions', row['session_id'])
        _reference(call, store, 'monitor_rules', call.params.get('detection_rule_id'), session['space_id'])
        _reference(call, store, 'soc_alerts', call.params.get('alert_id'), session['space_id'])
        for key in ('detected', 'detection_rule_id', 'alert_id', 'notes'):
            if key in call.params:
                row[key] = call.params[key]
        row.update(revision=row['revision'] + 1, updated_at=_now(call))
        return {'execution': _view(row)}

    return store.transaction('simulation_executions', mutate,
                             idempotency_key=call.idempotency_key, params_digest=_digest(call))
