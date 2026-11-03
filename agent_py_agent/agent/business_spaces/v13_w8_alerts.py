"""SOC 告警、受控反馈与实体风险；持久化和实时授权复用 v13/W1 边界。"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone

from ..conversation.business_access import BusinessError
from ..plugin_host.errors import HostAPIError


# LLM: 时间仅取本次可信时钟，测试不依赖机器日期。
# 函数用途: 生成 UTC 时间戳。
def w8_now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 日期按显式时区比较；无时间表示未知，不能冒充当前发生。
# 函数用途: 把持久时间戳解析为秒数。
def _epoch(value):
    if value is None:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


# LLM: 字段错误只返回安全的结构化位置，不包含任意参数正文。
# 函数用途: 拒绝不符合领域约束的输入。
def w8_schema_error(path):
    error = HostAPIError('INVALID_SCHEMA', 400)
    error.details = {'path': str(path), 'reason': 'invalid_value'}
    raise error


# LLM: 每次记录披露和提交前复用已发行上下文，owner 或空间不一致不泄露记录。
# 函数用途: 校验本次调用是否能访问一条记录。
def w8_access(call, row):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call, row)
    selected = call.params.get('space_id')
    if selected is not None and row.get('space_id') not in (None, selected):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return row


# LLM: 返回值只剔除 owner 私有字段；带其它持久字段的 DTO 由所属模块进一步投影。
# 函数用途: 从存储记录构造独立公开字典。
def w8_view(row):
    return json.loads(json.dumps({key: value for key, value in row.items() if key != 'owner_id'}, allow_nan=False))


# LLM: 不建立第二份记录状态，列表只读取同 owner 的 v13Store 并逐记录实时校验。
# 函数用途: 返回本次调用可见的集合记录。
def w8_rows(call, collection):
    from .v13_store import v13_store_for

    result = []
    for row in v13_store_for(call, 'owner').list(collection):
        try:
            result.append(w8_access(call, row))
        except HostAPIError as error:
            if error.status != 404:
                raise
    return result


# LLM: 不存在或其它 owner 统一 404，禁止空成功或先泄露归属。
# 函数用途: 取得准确集合记录。
def w8_get(call, collection, record_id):
    from .v13_store import v13_store_for

    row = v13_store_for(call, 'owner').get(collection, record_id)
    if row is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    return w8_access(call, row)


# LLM: 可选空间沿 W1 活跃空间解析；不存在按 W8 冻结契约返回 RESOURCE_NOT_FOUND，不泄露其它空间归属。
# 函数用途: 解析本次调用所用业务空间。
def w8_space(call):
    from ..gateway_parts.business_api_v13 import v13_call_frozen, v13_resolve_space

    if call.params.get('space_id'):
        try:
            return v13_resolve_space(call)['space_id']
        except (HostAPIError, BusinessError) as error:
            if error.code == 'SPACE_NOT_FOUND':
                raise HostAPIError('RESOURCE_NOT_FOUND', 404) from None
            raise
    page = v13_call_frozen(call, 'spaces.list', {'limit': 100})
    spaces = [row for row in page['items'] if row['state'] == 'active']
    if page['next_cursor'] is not None or len(spaces) != 1:
        w8_schema_error('$.space_id')
    return v13_resolve_space(call, spaces[0]['space_id'])['space_id']


# LLM: W8 的 CAS 是记录版本，不把它错当 W0 集合版本。
# 函数用途: 在锁内比较记录版本。
def w8_check_revision(call, row):
    if type(call.expected_revision) is not int or call.expected_revision != row['revision']:
        raise HostAPIError('REVISION_CONFLICT', 409)


# LLM: 只能由持有 v13 写事务的调用方使用，保证不覆盖锁外旧记录。
# 函数用途: 更新一条记录的版本及修改时间。
def w8_touch(call, row):
    row['revision'] += 1
    row['updated_at'] = w8_now(call)


# LLM: owner、subject、action、params、CAS 同时绑定回执；跨集合原子性由 W0 统一事务提供。
# 函数用途: 提交一次可幂等重放的领域变更。
def w8_transaction(call, collections, mutate):
    from .v13_store import v13_store_for

    digest = hashlib.sha256(json.dumps([call.owner_id, call.subject_id, call.action, call.params,
        call.expected_revision], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    store = v13_store_for(call, 'owner')
    if len(collections) == 1:
        # LLM: 回调在 W0 锁内拿到最新 records；保持单集合与多集合的同一调用面。
        # 函数用途: 给单集合变更包装集合名。
        def single(records):
            return mutate({collections[0]: records})
        return store.transaction(collections[0], single, idempotency_key=call.idempotency_key, params_digest=digest)
    return store.transaction_many(collections, mutate, idempotency_key=call.idempotency_key, params_digest=digest)


# LLM: 投影页沿原观察服务 HMAC 游标；绑定 owner/subject/action/filter 及完整结果代次，不能跨主体复用。
# 函数用途: 分页确定性派生记录，不保存第二份快照。
def w8_page(call, collection, items, scope):
    reader = call.services.observation
    raw = json.dumps([call.owner_id, call.subject_id, call.action, collection, scope],
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    identity = hashlib.sha256(raw).hexdigest()
    revision = hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    position = reader.cursor_position(call.params.get('cursor'), identity)
    offset = 0
    if position is not None:
        if (not isinstance(position, dict) or position.get('revision') != revision
                or type(position.get('offset')) is not int or not 0 <= position['offset'] <= len(items)):
            raise HostAPIError('CURSOR_EXPIRED', 503)
        offset = position['offset']
    limit = call.params.get('limit', 50)
    end = offset + limit
    cursor = reader.cursor(identity, {'revision': revision, 'offset': end}) if end < len(items) else None
    return {'items': items[offset:end], 'next_cursor': cursor, 'coverage': 'partial' if cursor else 'complete'}


# LLM: 新告警所有字段一次闭合，occurred_at 保留来源时间而非读取时间。
# 函数用途: 构造规范持久告警。
def _new_alert(call, space_id, value, alert_id, source):
    now = w8_now(call)
    return {'alert_id': alert_id, 'space_id': space_id, 'title': value['title'],
        'description': value.get('description', ''), 'severity': value['severity'], 'status': 'new',
        'verdict': None, 'ai_confidence': None, 'disposition': None, 'rule_id': value.get('rule_id'),
        'source': source, 'external_ref': value.get('external_ref'), 'entities': value.get('entities', []),
        'techniques': value.get('techniques', []), 'assignee': None, 'case_id': None,
        'tags': value.get('tags', []), 'evidence_refs': [], 'acknowledged_at': None, 'resolved_at': None,
        'revision': 0, 'created_at': value.get('occurred_at') or now, 'updated_at': now, 'owner_id': call.owner_id}


# LLM: 反馈只由分析师动作产生；未有原判定使用 unknown token，绝不伪造 AI 判定。
# 函数用途: 构造供其它切片消费的受控反馈记录。
def w8_feedback(call, alert, new_verdict, reason, source):
    now = w8_now(call)
    return {'override_id': 'override-' + uuid.uuid4().hex, 'alert_id': alert['alert_id'],
        'space_id': alert['space_id'], 'rule_id': alert['rule_id'],
        'original_verdict': alert['verdict'] or 'unknown', 'override_verdict': new_verdict,
        'reason': reason, 'analyst': call.subject_id, 'source': source,
        'created_at': now, 'owner_id': call.owner_id, 'revision': 0, 'updated_at': now}


# LLM: 首次确认/解决时间单调保留；回退状态不擦除已发生的事实。
# 函数用途: 在事务内应用告警字段变化。
def _update_alert(call, row, changes):
    if changes.get('status') in ('acknowledged', 'in_progress', 'resolved', 'closed') and row['acknowledged_at'] is None:
        row['acknowledged_at'] = w8_now(call)
    if changes.get('status') in ('resolved', 'closed') and row['resolved_at'] is None:
        row['resolved_at'] = w8_now(call)
    row.update(changes)
    w8_touch(call, row)


# LLM: 相同 external_ref 在同空间只创建一次，回执与记录写入由同事务决定。
# 函数用途: 接收外部规范告警。
def ingest_alerts(call):
    space_id = w8_space(call)

    # LLM: 仅在锁内按最新外部引用去重，并先验证所有可见目标。
    # 函数用途: 批量构造告警和逐项回执。
    def mutate(tables):
        records = tables['soc_alerts']
        indexed = {(row['space_id'], row['external_ref']): row for row in records.values()}
        result = []
        for value in call.params['alerts']:
            old = indexed.get((space_id, value['external_ref']))
            created = old is None
            if old is not None:
                w8_access(call, old)
            else:
                old = _new_alert(call, space_id, value, 'ALERT-' + uuid.uuid4().hex[:12].upper(), 'ingest')
                records[old['alert_id']] = old
                indexed[(space_id, value['external_ref'])] = old
            result.append({'external_ref': value['external_ref'], 'alert_id': old['alert_id'],
                'created': created})
        return {'results': result}
    return w8_transaction(call, ('soc_alerts',), mutate)


# LLM: 精确 ID 读只披露已经过 owner/live 校验的规范 DTO。
# 函数用途: 查询一条告警。
def get_alert(call):
    return {'alert': w8_view(w8_get(call, 'soc_alerts', call.params['alert_id']))}


# LLM: verdict 覆写和反馈不可半提交，CAS 与幂等均在持久事务内决定。
# 函数用途: 修改告警状态或归属并保存研判信号。
def update_alert(call):
    from .v13_w8_cases import _link, _record, _same_space

    fields = {key: value for key, value in call.params.items() if key != 'alert_id'}

    # LLM: 回调中读取最新 alert，任何关联校验失败均不提交。
    # 函数用途: 对一条告警执行记录 CAS。
    def mutate(tables):
        row = tables['soc_alerts'].get(call.params['alert_id'])
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        w8_access(call, row)
        w8_check_revision(call, row)
        if 'case_id' in fields and fields['case_id'] != row['case_id']:
            target = fields['case_id']
            if target is not None:
                case = _record(call, tables, 'soc_cases', target)
                _same_space(case, row)
                if case['merged_into']:
                    raise HostAPIError('STATE_CONFLICT', 409)
                _link(call, tables, case, [row['alert_id']], touch_alert=False)
                w8_touch(call, case)
            elif row['case_id'] is not None:
                case = _record(call, tables, 'soc_cases', row['case_id'])
                _same_space(case, row)
                case['alert_ids'] = [aid for aid in case['alert_ids'] if aid != row['alert_id']]
                w8_touch(call, case)
        if fields.get('verdict') is not None and fields['verdict'] != row['verdict']:
            feedback = w8_feedback(call, row, fields['verdict'], '', 'alert_update')
            tables['soc_feedback'][feedback['override_id']] = feedback
        _update_alert(call, row, fields)
        return {'alert': w8_view(row)}
    return w8_transaction(call, ('soc_alerts', 'soc_feedback', 'soc_cases'), mutate)


# LLM: 只读同 owner 原 SchedulerRepository，不从线程名称猜规则身份或遍历全部会话。
# 函数用途: 取得原生计划读口。
def _monitor_repository(call):
    from ..scheduler.repository import SchedulerRepository

    paths = call.context.host.home_paths
    return SchedulerRepository(call.owner_home / 'data' / 'scheduler',
        owner_provider=getattr(paths, 'owner_provider', 'local'),
        owner_kind=paths.owner_kind, owner_id=call.owner_id)


# LLM: 只投影携明确 delivery_event_key 的规范消息；坏来源明确失败，历史窗口有界且不冒充完整。
# 函数用途: 把可见监控规则的投递转换为稳定告警。
def sync_monitor_alerts(call):
    rules = w8_rows(call, 'monitor_rules')
    if not rules:
        return 'complete'
    conversation = getattr(call.context.host, 'conversation_store', None)
    if conversation is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    repository = _monitor_repository(call)
    projected, coverage = {}, 'complete'
    for rule in rules[:1000]:
        if not rule.get('scheduler_job_id'):
            continue
        job = repository.get_job(rule['scheduler_job_id'])
        if job['name'] != 'monitor:' + rule['rule_id']:
            raise HostAPIError('HOST_NOT_READY', 503)
        thread = conversation.threads.require(job['thread_id'])
        if thread.owner_id != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        w8_access(call, {'owner_id': thread.owner_id, 'space_id': rule['space_id'], 'agent_id': thread.thread_id})
        messages, errors = conversation.messages.recent_report(thread.thread_id, limit=1000)
        if errors:
            raise HostAPIError('HOST_NOT_READY', 503)
        if len(messages) == 1000:
            coverage = 'partial'
        for message in messages:
            key = message.metadata.get('scheduler_delivery_event_key')
            if not isinstance(key, str) or not key:
                continue
            identity = 'ALERT-' + hashlib.sha256(key.encode()).hexdigest()[:12].upper()
            value = {'external_ref': key, 'title': (message.content.splitlines() or ['监控投递'])[0][:300] or '监控投递',
                'description': message.content[:8000], 'severity': rule['severity'],
                'rule_id': rule['rule_id'], 'techniques': rule['mitre_techniques'][:50],
                'occurred_at': datetime.fromtimestamp(message.created_at, timezone.utc).isoformat()}
            row = _new_alert(call, rule['space_id'], value, identity, 'monitor')
            row['evidence_refs'] = message.metadata.get('evidence_refs', [])[:100]
            projected[identity] = row
    if len(rules) > 1000:
        coverage = 'partial'
    existing = {row['alert_id'] for row in w8_rows(call, 'soc_alerts')}
    fresh = {key: row for key, row in projected.items() if key not in existing}
    if fresh:
        projection_call = replace(call, idempotency_key='projection_' + hashlib.sha256(
            json.dumps(sorted(fresh), separators=(',', ':')).encode()).hexdigest(),
            params={'projection_ids': sorted(fresh)}, expected_revision=None)

        # LLM: 首次读取的投影可能同时到达；锁内 setdefault 保留已有人工分诊状态。
        # 函数用途: 幂等增加尚未映射的监控投递。
        def mutate(tables):
            for key, row in fresh.items():
                w8_access(call, row)
                tables['soc_alerts'].setdefault(key, row)
            return {}
        w8_transaction(projection_call, ('soc_alerts',), mutate)
    return coverage


# LLM: 过滤先于分页；时间按数值比较，cursor 同时绑定过滤条件与结果代次。
# 函数用途: 查询告警列表并同步最新监控投递。
def list_alerts(call):
    coverage = sync_monitor_alerts(call)
    filters = {key: value for key, value in call.params.items() if key not in ('cursor', 'limit')}
    items = []
    for row in w8_rows(call, 'soc_alerts'):
        if any(row.get(key) != value for key, value in filters.items() if key != 'since'):
            continue
        since = filters.get('since')
        if since is not None and (_epoch(row['created_at']) is None or _epoch(row['created_at']) < _epoch(since)):
            continue
        items.append(w8_view(row))
    items.sort(key=lambda row: (_epoch(row['created_at']) or 0, row['alert_id']), reverse=True)
    result = w8_page(call, 'soc_alerts', items, filters)
    if coverage != 'complete':
        result['coverage'] = coverage
    return result


# LLM: current verdict 比较和告警/反馈更新处在同一个事务，重复请求优先回放原回执。
# 函数用途: 保存分析师明确的研判覆写。
def submit_override(call):
    params = call.params

    # LLM: stale 原判定不得覆盖别人刚提交的研判；不在锁外使用旧快照。
    # 函数用途: 原子提交一条人工反馈及对应告警。
    def mutate(tables):
        row = tables['soc_alerts'].get(params['alert_id'])
        if row is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        w8_access(call, row)
        if row['verdict'] != params['original_verdict']:
            error = HostAPIError('STATE_CONFLICT', 409)
            error.details = {'path': '$.original_verdict', 'current_verdict': row['verdict']}
            raise error
        feedback = w8_feedback(call, row, params['override_verdict'], params['reason'], 'override')
        tables['soc_feedback'][feedback['override_id']] = feedback
        _update_alert(call, row, {'verdict': params['override_verdict'], 'disposition': params['override_verdict']})
        return {'override': _override_view(feedback), 'alert': w8_view(row)}
    return w8_transaction(call, ('soc_alerts', 'soc_feedback'), mutate)


# LLM: Override DTO 是冻结形状，内部 revision/updated_at 不泄露到公开结果。
# 函数用途: 投影公开反馈记录。
def _override_view(row):
    return {key: value for key, value in w8_view(row).items() if key not in ('revision', 'updated_at')}


# LLM: 相似性只由规则与主实体结构化比较，不解析告警标题或模型摘要。
# 函数用途: 对同类待处理告警执行受控重处置。
def apply_redisposition(call):
    dry_run = call.params.get('dry_run', False)

    # LLM: 候选与反馈在锁内重核，dry_run 仅返回计划，不改变告警或反馈集合。
    # 函数用途: 匹配并可选更新同空间的相似告警。
    def mutate(tables):
        feedback = tables['soc_feedback'].get(call.params['override_id'])
        if feedback is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        w8_access(call, feedback)
        original = tables['soc_alerts'].get(feedback['alert_id'])
        if original is None:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        w8_access(call, original)
        matched, applied, skipped = [], [], []
        primary = original['entities'][0] if original['entities'] else None
        candidates = sorted(tables['soc_alerts'].values(), key=lambda row: (_epoch(row['created_at']) or 0, row['alert_id']))
        for row in candidates:
            if (primary is None or row['alert_id'] == original['alert_id'] or row['space_id'] != original['space_id']
                    or row['rule_id'] != original['rule_id'] or primary not in row['entities']):
                continue
            try:
                w8_access(call, row)
            except HostAPIError as error:
                if error.status != 404:
                    raise
                continue
            if row['status'] not in ('new', 'acknowledged', 'in_progress'):
                skipped.append({'alert_id': row['alert_id'], 'reason': 'not_pending'})
                continue
            if len(matched) >= call.params.get('max_alerts', 100):
                break
            matched.append(row['alert_id'])
            if dry_run:
                continue
            record = w8_feedback(call, row, feedback['override_verdict'], feedback['reason'], 'redisposition')
            tables['soc_feedback'][record['override_id']] = record
            _update_alert(call, row, {'verdict': feedback['override_verdict'], 'disposition': feedback['override_verdict']})
            applied.append(row['alert_id'])
        return {'matched': matched, 'applied': applied, 'skipped': skipped[:500], 'dry_run': dry_run}
    return w8_transaction(call, ('soc_alerts', 'soc_feedback'), mutate)


# LLM: 列表按准确 analyst/alert/space/time 过滤，保留原反馈事实而非重算标签。
# 函数用途: 查询受控反馈记录。
def list_overrides(call):
    filters = {key: value for key, value in call.params.items() if key not in ('cursor', 'limit')}
    rows = [row for row in w8_rows(call, 'soc_feedback')
        if all(row.get(key) == value for key, value in filters.items() if key != 'since')
        and (filters.get('since') is None or _epoch(row['created_at']) >= _epoch(filters['since']))]
    rows.sort(key=lambda row: (_epoch(row['created_at']) or 0, row['override_id']), reverse=True)
    return w8_page(call, 'soc_feedback', [_override_view(row) for row in rows], filters)


# LLM: 原 AI 判定仅取该告警第一条反馈的 original，不把分析师连续修改当新 AI 预测。
# 函数用途: 汇总窗口内研判纠偏及可观测一致率。
def feedback_summary(call):
    days = call.params.get('window_days', 30)
    since = call.clock() - days * 86400
    records = [row for row in w8_rows(call, 'soc_feedback') if _epoch(row['created_at']) >= since]
    transitions = Counter((row['original_verdict'], row['override_verdict']) for row in records)
    rules = Counter(row['rule_id'] for row in records if row['rule_id'] is not None)
    grouped = defaultdict(list)
    for row in w8_rows(call, 'soc_feedback'):
        grouped[row['alert_id']].append(row)
    reviewed, confirmed = 0, 0
    alerts = {row['alert_id']: row for row in w8_rows(call, 'soc_alerts')}
    for alert_id, values in grouped.items():
        values.sort(key=lambda row: (_epoch(row['created_at']) or 0, row['override_id']))
        original, latest = values[0], values[-1]
        alert = alerts.get(alert_id)
        if (alert is not None and alert['ai_confidence'] is not None and original['original_verdict'] != 'unknown'
                and _epoch(latest['created_at']) >= since):
            reviewed += 1
            confirmed += latest['override_verdict'] == original['original_verdict']
    ranked = sorted(rules.items(), key=lambda item: (-item[1], item[0] or ''))
    return {'window_days': days, 'overrides_total': len(records),
        'by_transition': [{'from_verdict': source, 'to_verdict': target, 'count': count}
            for (source, target), count in sorted(transitions.items())][:100],
        'agreement_rate': confirmed / reviewed if reviewed else None,
        'top_rules': [{'rule_id': rule_id, 'overrides': count} for rule_id, count in ranked[:20]],
        'coverage': 'partial' if len(transitions) > 100 or len(ranked) > 20 else 'complete'}


# LLM: 同一实体在同一条告警中只计一次，未来时间和未知时间不进入当前风险窗口。
# 函数用途: 按用户、主机及 IP 汇总可见告警。
def _entity_alerts(call):
    grouped = defaultdict(list)
    now = call.clock()
    for row in w8_rows(call, 'soc_alerts'):
        stamp = _epoch(row['created_at'])
        if stamp is None or stamp > now:
            continue
        keys = {(entity['type'], entity['value']) for entity in row['entities']
            if entity['type'] in ('user', 'host', 'ip')}
        for key in keys:
            grouped[key].append(row)
    return grouped


# LLM: 权重与窗口来自本切片冻结契约，不解释标题或标签中的自然语言。
# 函数用途: 计算一个实体的确定性风险档案。
def _risk_profile(call, key, history):
    weights = {'info': 1, 'low': 5, 'medium': 15, 'high': 30, 'critical': 50}
    now = call.clock()
    current = [row for row in history if _epoch(row['created_at']) >= now - 30 * 86400]
    current.sort(key=lambda row: (_epoch(row['created_at']), row['alert_id']), reverse=True)
    total = sum(weights[row['severity']] * (0.5 if _epoch(row['created_at']) < now - 7 * 86400 else 1)
        for row in current)
    recent = sum(_epoch(row['created_at']) >= now - 7 * 86400 for row in history)
    previous = sum(now - 28 * 86400 <= _epoch(row['created_at']) < now - 7 * 86400 for row in history)
    mean = previous / 3
    deviation = ((recent - mean) / max(1, mean)
        if min(_epoch(row['created_at']) for row in history) <= now - 14 * 86400 else None)
    return {'entity_type': key[0], 'entity_value': key[1], 'risk_score': min(100, total),
        'baseline_deviation': deviation, 'contributing_alerts': [row['alert_id'] for row in current[:100]],
        'alert_count': len(current), 'top_severity': max(current, key=lambda row: weights[row['severity']])['severity']
            if current else 'info', 'last_seen_at': max(history, key=lambda row: _epoch(row['created_at']))['created_at']}


# LLM: 队列只含三十日内有证据的实体；较早历史仍可用于基线，但不凭空抬高当前得分。
# 函数用途: 查询按风险分数排序的实体队列。
def entity_risk_queue(call):
    rows = []
    for key, history in _entity_alerts(call).items():
        row = _risk_profile(call, key, history)
        if row['alert_count'] == 0:
            continue
        if call.params.get('entity_type') not in (None, key[0]) or row['risk_score'] < call.params.get('min_score', 0):
            continue
        rows.append(row)
    rows.sort(key=lambda row: (-row['risk_score'], row['entity_type'], row['entity_value']))
    scope = {key: value for key, value in call.params.items() if key not in ('cursor', 'limit')}
    return w8_page(call, 'entity_risk', rows, scope)


# LLM: 阈值按契约高>=70、中>=40，其余低；统计与队列使用同一评分函数。
# 函数用途: 汇总当前窗口内实体风险分布。
def entity_risk_stats(call):
    rows = [_risk_profile(call, key, history) for key, history in _entity_alerts(call).items()]
    rows = [row for row in rows if row['alert_count']]
    by_type = {key: sum(row['entity_type'] == key for row in rows) for key in ('user', 'host', 'ip')}
    high = sum(row['risk_score'] >= 70 for row in rows)
    medium = sum(40 <= row['risk_score'] < 70 for row in rows)
    return {'entities_total': len(rows), 'high': high, 'medium': medium, 'low': len(rows) - high - medium,
        'by_type': by_type, 'generated_at': w8_now(call), 'coverage': 'complete'}


# LLM: 历史按 UTC 日分组，超出九十日的事实不进入历史图，但已知实体不错误变成不存在。
# 函数用途: 查询一个实体的风险画像和每日证据。
def entity_risk_get(call):
    key = (call.params['entity_type'], call.params['entity_value'])
    history = _entity_alerts(call).get(key)
    if not history:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    weights = {'info': 1, 'low': 5, 'medium': 15, 'high': 30, 'critical': 50}
    days = defaultdict(list)
    since = call.clock() - 90 * 86400
    for row in history:
        stamp = _epoch(row['created_at'])
        if stamp >= since:
            day = datetime.fromtimestamp(stamp, timezone.utc).date().isoformat()
            days[day].append(row)
    result = [{'day': day, 'alert_count': len(values),
        'max_severity': max(values, key=lambda row: weights[row['severity']])['severity'],
        'score': min(100, sum(weights[row['severity']] for row in values))}
        for day, values in sorted(days.items())]
    return {'entity': _risk_profile(call, key, history), 'history': result[-90:],
        'coverage': 'partial' if len(result) > 90 else 'complete'}
