"""自治建议与企业上下文规则；仅存建议，不替代原生审批执行门。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from ..conversation.business_access import BusinessError
from ..gateway_parts.business_api_v13 import (
    v13_require_access,
    v13_require_admin,
    v13_resolve_space,
)
from .v13_store import v13_store_for


# LLM: 时间只来自本次调用注入时钟，所有持久时间统一UTC。
# 函数用途: 生成记录更新时间。
def _now(call):
    return datetime.fromtimestamp(call.clock(), timezone.utc).isoformat()


# LLM: 参数、动作、主体和信封CAS参与摘要，不能用相同键悄悄换操作。
# 函数用途: 为当前动作生成稳定事务指纹。
def _digest(call):
    value = [call.action, call.owner_id, call.subject_id, call.params, call.expected_revision]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


# LLM: 隔离不只靠文件位置，每条记录继续核owner和原生即时授权。
# 函数用途: 读取当前用户可见的规范集合记录。
def _rows(call, collection):
    rows = []
    for row in v13_store_for(call).list(collection):
        if row.get('owner_id') != call.owner_id:
            continue
        v13_require_access(call, row)
        rows.append(row)
    return rows


# LLM: 回执与业务记录仍同笔写在原V13Store，闭包必须在锁内读取当前值。
# 函数用途: 执行一个有幂等回执和实时末核的业务事务。
def _transaction(call, collection, mutate):
    store = v13_store_for(call)

    # LLM: 原Store提交日志前再次调用此口；普通access不能替代管理员资格。
    # 函数用途: 在事务的所有授权观察点重核当前原生权限。
    def authorize():
        if call.action.startswith('autonomy_policy.'):
            return v13_require_admin(call)
        return v13_require_access(call)

    store.authorize = authorize
    return store.transaction(collection, mutate, idempotency_key=call.idempotency_key,
                             params_digest=_digest(call))


# LLM: policy总版本取所有规范记录的最高版本；重置保留默认值，避免删除引起版本回退。
# 函数用途: 计算自治建议列表的当前CAS版本。
def _revision(rows):
    return max((row['revision'] for row in rows), default=0)


# LLM: 比较发生在原Store锁内，不使用进锁前的旧快照。
# 函数用途: 检查列表级自治策略版本。
def _check_revision(call, rows):
    if call.expected_revision != _revision(rows):
        raise BusinessError('REVISION_CONFLICT')


# LLM: 管理员能力仍由W0原生身份判断；只复用原入口，不把业务角色当权限。
# 函数用途: 在策略变更前重新核验管理员。
def _admin(call):
    v13_require_admin(call)


# LLM: DTO仅暴露契约声明字段，不把内部owner、kind和policy_key传到前端。
# 函数用途: 投影动作等级策略。
def _policy_view(row):
    return {key: row['value'][key] for key in ('action', 'level', 'updated_by', 'updated_at')}


# LLM: enforcement明确为advisory，原审批模式继续是实际执行门。
# 函数用途: 读取自治建议及其版本。
def list_policies(call):
    rows = _rows(call, 'autonomy_policies')
    policies = [_policy_view(row) for row in rows if row['kind'] == 'action_level']
    if len(policies) > 200:
        raise BusinessError('LIMIT_EXCEEDED')
    return {'policies': policies, 'default_level': 'manual_approval',
            'enforcement': 'advisory', 'revision': _revision(rows)}


# LLM: 管理员与CAS在事务内重核，默认值也持久化以保持版本单调。
# 函数用途: 更新或重置单个动作的自治建议等级。
def _set_policy(call, level):
    _admin(call)

    # LLM: 只修改同owner记录，事务回执处理重放，撤权在写入前再次核验。
    # 函数用途: 在策略集合锁内保存一个新版本。
    def mutate(records):
        _admin(call)
        rows = [row for row in records.values() if row.get('owner_id') == call.owner_id]
        _check_revision(call, rows)
        key = 'action:' + call.params['action']
        previous = records.get(key)
        if previous is not None:
            v13_require_access(call, previous)
        elif sum(row['kind'] == 'action_level' for row in rows) >= 200:
            raise BusinessError('LIMIT_EXCEEDED')
        now, revision = _now(call), _revision(rows) + 1
        value = {'action': call.params['action'], 'level': level,
                 'updated_by': call.subject_id, 'updated_at': now}
        records[key] = {'policy_key': key, 'owner_id': call.owner_id, 'kind': 'action_level',
                        'value': value, 'revision': revision,
                        'created_at': previous['created_at'] if previous else now, 'updated_at': now}
        if call.action == 'autonomy_policy.reset':
            return {'action': call.params['action'], 'level': level, 'revision': revision}
        return {'policy': value, 'revision': revision}

    return _transaction(call, 'autonomy_policies', mutate)


# LLM: 自治等级只记建议，无脚本运行、批准票据或执行副作用。
# 函数用途: 保存管理员选择的动作等级。
def update_policy(call):
    return _set_policy(call, call.params['level'])


# LLM: 重置回到人工审批建议，并保留单调CAS历史。
# 函数用途: 恢复某动作的默认建议等级。
def reset_policy(call):
    return _set_policy(call, 'manual_approval')


# LLM: 影子分类和动作策略共用原集合最高版本，不推导真实执行状态。
# 函数用途: 读取各告警分类的影子建议开关。
def shadow_get(call):
    rows = _rows(call, 'autonomy_policies')
    classes = [dict(row['value']) for row in rows if row['kind'] == 'shadow_class']
    if len(classes) > 200:
        raise BusinessError('LIMIT_EXCEEDED')
    return {'classes': classes, 'revision': _revision(rows)}


# LLM: 管理员及CAS保护设置，不改原执行审批策略。
# 函数用途: 保存一个告警分类的影子建议。
def shadow_set(call):
    _admin(call)

    # LLM: 以锁内版本生成更新，不覆盖并发其它分类的记录。
    # 函数用途: 在原策略集合中提交影子开关。
    def mutate(records):
        _admin(call)
        rows = [row for row in records.values() if row.get('owner_id') == call.owner_id]
        _check_revision(call, rows)
        key = 'shadow:' + call.params['alert_class']
        previous = records.get(key)
        if previous is not None:
            v13_require_access(call, previous)
        elif sum(row['kind'] == 'shadow_class' for row in rows) >= 200:
            raise BusinessError('LIMIT_EXCEEDED')
        now, revision = _now(call), _revision(rows) + 1
        value = {'alert_class': call.params['alert_class'], 'shadow': call.params['shadow'], 'updated_at': now}
        records[key] = {'policy_key': key, 'owner_id': call.owner_id, 'kind': 'shadow_class',
                        'value': value, 'revision': revision,
                        'created_at': previous['created_at'] if previous else now, 'updated_at': now}
        return {'class': value, 'revision': revision}

    return _transaction(call, 'autonomy_policies', mutate)


# LLM: 未设置时间或坏时间不能被当成当前有效窗口中的事实。
# 函数用途: 解析UTC记录时间；缺失或无时区返回未知。
def _epoch(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


# LLM: 撤销记录始终隐藏；include_expired只扩大已到期记录，不恢复撤销授权。
# 函数用途: 列出仍存在的自治建议授权。
def grants_list(call):
    rows = []
    for row in _rows(call, 'autonomy_grants'):
        if row['revoked']:
            continue
        expires = _epoch(row['expires_at'])
        if row['expires_at'] is not None and expires is None:
            raise BusinessError('STORE_UNAVAILABLE')
        if not call.params.get('include_expired', False) and expires is not None and expires <= call.clock():
            continue
        rows.append({key: row[key] for key in ('grant_id', 'action', 'subject', 'level', 'expires_at',
                                              'granted_by', 'created_at')})
    if len(rows) > 500:
        raise BusinessError('LIMIT_EXCEEDED')
    return {'items': rows}


# LLM: 建议授权仅保存在本owner，不能签发原生权限或批准真实动作。
# 函数用途: 创建管理员指定的有界或长期自治建议授权。
def grants_create(call):
    _admin(call)

    # LLM: 同键重放由原回执返回原ID，不能重复生成授权。
    # 函数用途: 在授权集合锁内创建记录。
    def mutate(records):
        _admin(call)
        if sum(row.get('owner_id') == call.owner_id and not row['revoked'] for row in records.values()) >= 500:
            raise BusinessError('LIMIT_EXCEEDED')
        now, grant_id = _now(call), 'grant-' + uuid.uuid4().hex
        hours = call.params.get('expires_in_hours')
        expires = None if hours is None else datetime.fromtimestamp(call.clock() + hours * 3600, timezone.utc).isoformat()
        view = {'grant_id': grant_id, 'action': call.params['action'], 'subject': call.params.get('subject'),
                'level': call.params['level'], 'expires_at': expires, 'granted_by': call.subject_id, 'created_at': now}
        records[grant_id] = {**view, 'owner_id': call.owner_id, 'revoked': False,
                             'revision': 0, 'updated_at': now}
        return {'grant': view}

    return _transaction(call, 'autonomy_grants', mutate)


# LLM: 撤销只改变原记录，重复调用不使已撤销授权复活。
# 函数用途: 撤销一条自治建议授权。
def grants_revoke(call):
    _admin(call)

    # LLM: 查找与状态变更在同一把原Store锁内完成。
    # 函数用途: 原子标记授权已撤销。
    def mutate(records):
        _admin(call)
        row = records.get(call.params['grant_id'])
        if row is None or row.get('owner_id') != call.owner_id:
            raise BusinessError('RESOURCE_NOT_FOUND')
        v13_require_access(call, row)
        if not row['revoked']:
            row.update(revoked=True, revision=row['revision'] + 1, updated_at=_now(call))
        return {'grant_id': row['grant_id'], 'revoked': True}

    return _transaction(call, 'autonomy_grants', mutate)


# LLM: 窗口格式由契约规定，函数仍独立拒绝不合法值，避免意外扩大时间范围。
# 函数用途: 计算m、h、d窗口的起止时间。
def _window(call, window):
    parsed = re.fullmatch(r'([1-9][0-9]{0,3})([mhd])', window)
    if parsed is None:
        raise BusinessError('INVALID_SCHEMA')
    seconds = int(parsed.group(1)) * {'m': 60, 'h': 3600, 'd': 86400}[parsed.group(2)]
    return call.clock() - seconds, call.clock()


# LLM: 显式空间必须来自原W1；缺失时间单列partial，不把未知记录塞入窗口。
# 函数用途: 读取窗口内当前空间的规范事实记录。
def _window_rows(call, collection, window):
    space_id = call.params.get('space_id')
    if space_id is not None:
        v13_resolve_space(call, space_id)
    start, end = _window(call, window)
    result, coverage = [], 'complete'
    for row in _rows(call, collection):
        if space_id is not None and row.get('space_id') != space_id:
            continue
        timestamp = _epoch(row.get('created_at'))
        if timestamp is None:
            coverage = 'partial'
        elif start <= timestamp <= end:
            result.append(row)
    return result, coverage


# LLM: 审核窗口取反馈时刻而非告警创建时刻；原建议取首次反馈，最终反馈按UTC时刻选择。
# 函数用途: 依据告警与分析师反馈计算窗口内人机一致率。
def agreement(call):
    space_id = call.params.get('space_id')
    if space_id is not None:
        v13_resolve_space(call, space_id)
    start, end = _window(call, call.params['window'])
    alerts = {row['alert_id']: row for row in _rows(call, 'soc_alerts')
              if space_id is None or row['space_id'] == space_id}
    coverage = 'complete'
    by_alert = {}
    reviewed_ids = set()
    for row in _rows(call, 'soc_feedback'):
        if space_id is not None and row['space_id'] != space_id:
            continue
        timestamp = _epoch(row['created_at'])
        alert = alerts.get(row['alert_id'])
        if timestamp is None or alert is None or row['space_id'] != alert['space_id']:
            coverage = 'partial'
            continue
        if timestamp > end:
            continue
        by_alert.setdefault(row['alert_id'], []).append(row)
        if timestamp >= start:
            reviewed_ids.add(row['alert_id'])
    grouped = {}
    for alert_id, alert in alerts.items():
        timestamp = _epoch(alert['created_at'])
        if timestamp is None:
            coverage = 'partial'
        if alert_id not in reviewed_ids and (timestamp is None or not start <= timestamp <= end):
            continue
        history = sorted(by_alert.get(alert_id, []), key=lambda row: (_epoch(row['created_at']), row['override_id']))
        # 相同时刻不存在可靠先后时不凭随机ID猜最终反馈。
        if history and len({row['override_verdict'] for row in history
                            if _epoch(row['created_at']) == _epoch(history[-1]['created_at'])}) > 1:
            coverage = 'partial'
            continue
        original = history[0]['original_verdict'] if history else alert.get('verdict')
        if original is None:
            continue
        action = alert.get('disposition') or 'triage'
        counts = grouped.setdefault(action, Counter())
        counts['suggested'] += 1
        if alert_id in reviewed_ids:
            counts['reviewed'] += 1
            counts['agreed'] += int(history[-1]['override_verdict'] == original)
    by_action = [{'action': action, 'suggested': counts['suggested'], 'reviewed': counts['reviewed'],
                  'agreed': counts['agreed'],
                  'agreement_rate': counts['agreed'] / counts['reviewed'] if counts['reviewed'] else None}
                 for action, counts in sorted(grouped.items())]
    if len(by_action) > 200:
        raise BusinessError('LIMIT_EXCEEDED')
    suggestions = sum(row['suggested'] for row in by_action)
    reviewed = sum(row['reviewed'] for row in by_action)
    agreed = sum(row['agreed'] for row in by_action)
    return {'window': call.params['window'], 'ai_suggestions': suggestions, 'human_reviewed': reviewed,
            'agreed': agreed, 'agreement_rate': agreed / reviewed if reviewed else None, 'by_action': by_action,
            'coverage': coverage}


# LLM: 内部owner不属于公开DTO，返回契约声明字段的独立浅投影。
# 函数用途: 投影企业上下文规则。
def _rule_view(row):
    return {key: row[key] for key in ('rule_id', 'name', 'enabled', 'match', 'effect', 'revision',
                                     'created_at', 'updated_at')}


# LLM: 规则列表不执行匹配、模型请求或状态修改；过滤仅使用显式enabled布尔值。
# 函数用途: 读取已保存的业务上下文加权规则。
def rules_list(call):
    rows = [_rule_view(row) for row in _rows(call, 'business_context_rules')
            if 'enabled' not in call.params or row['enabled'] is call.params['enabled']]
    if len(rows) > 500:
        raise BusinessError('LIMIT_EXCEEDED')
    return {'items': rows}


# LLM: 规则是建议数据，权重只用于预览，不把规则写进运行时授权或真实告警。
# 函数用途: 创建企业上下文匹配和加权规则。
def rules_create(call):
    # LLM: 记录和幂等回执同笔提交，数量检查在锁内读取当前集合。
    # 函数用途: 保存一条归属于当前owner的规则。
    def mutate(records):
        if sum(row.get('owner_id') == call.owner_id for row in records.values()) >= 500:
            raise BusinessError('LIMIT_EXCEEDED')
        now, rule_id = _now(call), 'context-rule-' + uuid.uuid4().hex
        row = {'rule_id': rule_id, 'name': call.params['name'], 'enabled': call.params.get('enabled', True),
               'match': call.params['match'], 'effect': call.params['effect'], 'revision': 0,
               'created_at': now, 'updated_at': now, 'owner_id': call.owner_id}
        records[rule_id] = row
        return {'rule': _rule_view(row)}

    return _transaction(call, 'business_context_rules', mutate)


# LLM: 使用fnmatch的显式glob语义，不解析自然语言；tags条件与实体类型同时满足。
# 函数用途: 找到一个告警中匹配规则的实体。
def _matched_entities(alert, match):
    if not set(match.get('tags', [])) <= set(alert.get('tags', [])):
        return []
    return [entity for entity in alert['entities']
            if (match['entity_type'] == 'any' or entity['type'] == match['entity_type'])
            and fnmatchcase(entity['value'], match['pattern'])]


# LLM: 必须且只能提供rule_id或内联rule；只读投影不能改变告警或产生回执写入。
# 函数用途: 在真实窗口告警上预览上下文规则影响。
def rules_preview(call):
    if ('rule_id' in call.params) == ('rule' in call.params):
        raise BusinessError('INVALID_SCHEMA')
    if 'rule_id' in call.params:
        rule = v13_store_for(call).get('business_context_rules', call.params['rule_id'])
        if rule is None or rule.get('owner_id') != call.owner_id:
            raise BusinessError('RESOURCE_NOT_FOUND')
        v13_require_access(call, rule)
    else:
        rule = call.params['rule']
    alerts, coverage = _window_rows(call, 'soc_alerts', call.params.get('window', '7d'))
    severities = ('info', 'low', 'medium', 'high', 'critical')
    matched, entities, changes, sample = 0, set(), Counter(), []
    for alert in sorted(alerts, key=lambda row: row['alert_id']):
        selected = _matched_entities(alert, rule['match'])
        if not selected:
            continue
        matched += 1
        before = alert['severity']
        after = severities[max(0, min(len(severities) - 1, severities.index(before) + rule['effect']['severity_boost']))]
        changes[before + '->' + after] += 1
        for entity in selected:
            entities.add((entity['type'], entity['value']))
            if len(sample) < 50:
                sample.append({'alert_id': alert['alert_id'], 'entity': entity['value'],
                               'before_severity': before, 'after_severity': after})
    return {'matched_alerts': matched, 'affected_entities': len(entities),
            'severity_changes': dict(sorted(changes.items())), 'sample': sample, 'coverage': coverage}
