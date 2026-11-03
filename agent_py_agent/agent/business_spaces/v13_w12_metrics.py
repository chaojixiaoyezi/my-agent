"""SOC 指标与成本只读聚合：真实 owner 账本、UTC 半开窗口及明确的未知值。

指标不执行模型，不为读操作补写数据；成本只消费冻结 usage.get，模型账本仅提供尝试次数。
公开纯聚合函数的输入须先完成 owner/space 授权与样本排除。
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from datetime import datetime, timezone

from ..conversation.business_access import BusinessError
from ..plugin_host.contracts import DomainResult
from ..plugin_host.errors import HostAPIError
from .v13_store import v13_store_for


# LLM: 不将坏时间或无时区字符串推测为 UTC，数字仅接受有限 epoch 秒。
# 函数用途: 将账本时间转成可比较的秒，未知返回空值。
def _epoch(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


# LLM: 输出统一 UTC，不受宿主本地时区影响。
# 函数用途: 生成契约时间戳。
def _iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


# LLM: 参数经过运输校验；纯调用也拒绝非法窗口，时钟每请求只取一次。
# 函数用途: 解析分钟、小时或天窗口并返回半开范围。
def _period(call):
    window = call.params.get('window', '24h')
    if not isinstance(window, str) or re.fullmatch(r'[1-9][0-9]{0,3}[mhd]', window) is None:
        raise HostAPIError('INVALID_SCHEMA')
    until = float(call.clock())
    seconds = int(window[:-1]) * {'m': 60, 'h': 3600, 'd': 86400}[window[-1]]
    return window, until - seconds, until


# LLM: 窗口共用左闭右开，恰好落在上界的事件属于下一窗口。
# 函数用途: 判断时间字段是否落入范围。
def _within(row, field, since, until):
    value = _epoch(row.get(field))
    return value is not None and since <= value < until


# LLM: 只依据结构化样本标志，不按标题等文案推断记录性质。
# 函数用途: 判断显式样本记录。
def _sample(row):
    return (row.get('source') == 'sample' or row.get('origin') == 'sample'
            or (isinstance(row.get('tags'), list) and 'sample' in row['tags']))


# LLM: 集合主键只有一个规范字段，不能根据名称后缀猜测。
# 函数用途: 返回本模块读取集合的主键。
def _identity(collection):
    return {'soc_alerts': 'alert_id', 'soc_cases': 'case_id', 'soc_feedback': 'override_id',
        'monitor_rules': 'rule_id', 'monitor_sla_policies': 'space_id',
        'soc_investigations': 'run_id', 'threat_indicators': 'indicator_id'}[collection]


# LLM: 缺关键字段、坏枚举和负向生命周期会污染排名，先剔除且标记部分覆盖。
# 函数用途: 校验聚合需要的最小账本结构，不捏造缺失值。
def _valid_record(row, collection):
    identity = _identity(collection)
    if not isinstance(row.get(identity), str) or not row[identity] or _epoch(row.get('created_at')) is None:
        return False
    if collection != 'threat_indicators' and not isinstance(row.get('space_id'), str):
        return False
    if collection in {'soc_alerts', 'soc_cases'}:
        if row.get('severity') not in {'info', 'low', 'medium', 'high', 'critical'}:
            return False
        statuses = ({'new', 'acknowledged', 'in_progress', 'resolved', 'closed'}
            if collection == 'soc_alerts' else {'open', 'investigating', 'contained', 'resolved', 'closed'})
        if row.get('status') not in statuses:
            return False
        fields = ('acknowledged_at', 'resolved_at') if collection == 'soc_alerts' else ('closed_at',)
        for field in fields:
            value = row.get(field)
            if value is not None and (_epoch(value) is None or _epoch(value) < _epoch(row['created_at'])):
                return False
    if collection == 'soc_alerts':
        if row.get('source') not in {'monitor', 'ingest', 'sample'} or not isinstance(row.get('entities'), list):
            return False
        if any(row.get(key) is not None and not isinstance(row[key], str)
                for key in ('rule_id', 'assignee', 'verdict', 'case_id')):
            return False
        if any(not isinstance(item, dict) or not isinstance(item.get('type'), str)
                or not isinstance(item.get('value'), str) or not item['type'] or not item['value']
                for item in row['entities']):
            return False
        ack, resolved = _epoch(row.get('acknowledged_at')), _epoch(row.get('resolved_at'))
        return not (ack is not None and resolved is not None and ack > resolved)
    if collection == 'soc_feedback':
        return isinstance(row.get('alert_id'), str) and isinstance(row.get('override_verdict'), str)
    if collection == 'soc_investigations':
        return _epoch(row.get('started_at')) is not None
    return True


# LLM: 工厂复验活跃上下文，精确过滤 owner/space；重复主键只保留第一次确定性读取。
# 函数用途: 读取集合并同时返回数据质量覆盖标志。
def _records(call, collection):
    from ..gateway_parts.business_api_v13 import v13_require_access
    store = v13_store_for(call)
    _resolve_scope(call)
    rows, seen, clean = [], set(), True
    identity = _identity(collection)
    for row in store.list(collection):
        if row.get('owner_id') != call.owner_id:
            continue
        if collection != 'threat_indicators' and call.params.get('space_id') and row.get('space_id') != call.params['space_id']:
            continue
        if _sample(row):
            continue
        if not _valid_record(row, collection) or row[identity] in seen:
            clean = False
            continue
        v13_require_access(call, row)
        seen.add(row[identity])
        rows.append(row)
    return rows, clean


# LLM: 仍走原生 spaces.get 授权；聚合动作把缺空间规范成既定资源不存在错误。
# 函数用途: 校验可选空间，不让不存在的空间返回空成功。
def _resolve_scope(call):
    from ..gateway_parts.business_api_v13 import v13_resolve_space
    if call.params.get('space_id'):
        try:
            v13_resolve_space(call, call.params['space_id'])
        except (HostAPIError, BusinessError) as error:
            if error.code == 'SPACE_NOT_FOUND':
                raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
            raise


# LLM: W8 case 没有 source；关联样本告警的 case 和反馈也不能计入生产指标。
# 函数用途: 排除关联到样本告警的记录。
def _without_sample_links(call, rows):
    sample_ids = {row.get('alert_id') for row in v13_store_for(call).list('soc_alerts')
        if row.get('owner_id') == call.owner_id and _sample(row)}
    return [row for row in rows if row.get('alert_id') not in sample_ids
        and not set(row.get('alert_ids', [])).intersection(sample_ids)]


# LLM: 对外 helper 不写账本，输入调用身份仍经过原生授权。
# 函数用途: 取得按 owner、space、样本过滤的集合记录。
def load_soc_records(call, collection):
    rows, _clean = _records(call, collection)
    return _without_sample_links(call, rows) if collection in {'soc_cases', 'soc_feedback'} else rows


# LLM: 反馈按时间与主键确定性选最新，未来反馈不改变历史统计。
# 函数用途: 得到终点前每条告警的最终人工裁定。
def effective_verdicts(feedback, *, until):
    selected = {}
    for row in sorted(feedback, key=lambda item: (_epoch(item.get('created_at')) or 0, item.get('override_id', ''))):
        at = _epoch(row.get('created_at'))
        if at is not None and at < until:
            selected[row['alert_id']] = row['override_verdict']
    return selected


# LLM: 均值只使用完整非负生命周期，ack/resolved 窗口不受创建窗口限制。
# 函数用途: 计算窗口内完成事件的平均耗时。
def _mean_duration(rows, field, since, until):
    durations = [_epoch(row[field]) - _epoch(row['created_at']) for row in rows
        if _within(row, field, since, until) and _epoch(row.get('created_at')) is not None
        and _epoch(row[field]) >= _epoch(row['created_at'])]
    return sum(durations) / len(durations) if durations else None


# LLM: SLA 按空间和严重度精确匹配，不借其它空间策略填空。
# 函数用途: 为真实策略建立查询表。
def _policy_map(policies):
    result = {}
    for row in policies:
        for policy in row.get('policies', []):
            result[(row['space_id'], policy.get('severity'))] = policy
    return result


# LLM: 未配置、目标为空或部分严重度缺策略时返回未知，不当作零违约。
# 函数用途: 计算确认或解决超期的去重数量。
def _sla_breaches(alerts, policies, until):
    lookup = _policy_map(policies)
    if not lookup:
        return None
    count = 0
    for alert in alerts:
        policy = lookup.get((alert['space_id'], alert['severity']))
        if policy is None:
            return None
        known, breached = False, False
        for field, target in (('acknowledged_at', 'ack_minutes'), ('resolved_at', 'resolve_minutes')):
            minutes = policy.get(target)
            if type(minutes) is not int or minutes < 0:
                continue
            known = True
            finish = _epoch(alert.get(field))
            elapsed = min(finish, until) if finish is not None else until
            breached = breached or elapsed - _epoch(alert['created_at']) > minutes * 60
        if not known:
            return None
        count += int(breached)
    return count


# LLM: 此纯函数供报告复用，输入须已授权、去样本；计数按创建窗口，均值按完成事件。
# 函数用途: 计算仪表盘与周报共用的 SOC 指标。
def soc_kpis(alerts, cases, feedback, sla_policies, *, since, until):
    current = [row for row in alerts if _within(row, 'created_at', since, until)]
    current_cases = [row for row in cases if _within(row, 'created_at', since, until)]
    opened = [row for row in current if row['status'] in {'new', 'acknowledged', 'in_progress'}]
    ai = [row for row in current if row.get('verdict') is not None]
    overrides = effective_verdicts(feedback, until=until)
    return {'alerts_total': len(current), 'alerts_open': len(opened),
        'alerts_critical_open': sum(row['severity'] == 'critical' for row in opened),
        'cases_open': sum(row['status'] in {'open', 'investigating', 'contained'} for row in current_cases),
        'cases_closed': sum(_within(row, 'closed_at', since, until) for row in cases),
        'mttd_seconds': _mean_duration(alerts, 'acknowledged_at', since, until),
        'mttr_seconds': _mean_duration(alerts, 'resolved_at', since, until),
        'auto_triage_ratio': sum(row['alert_id'] not in overrides for row in ai) / len(ai) if ai else None,
        'sla_breaches': _sla_breaches(current, sla_policies, until)}


# LLM: 固定返回 schema 以 coverage 表达异常剔除，不添加未声明字段。
# 函数用途: 读取动作所需集合并聚合覆盖状态。
def _snapshot(call, collections):
    result, clean = {}, True
    for name in collections:
        rows, valid = _records(call, name)
        result[name] = _without_sample_links(call, rows) if name in {'soc_cases', 'soc_feedback'} else rows
        clean = clean and valid
    return result, 'complete' if clean else 'partial'


# LLM: 同次数用规则 ID 排序，名称只从同范围真实规则取得，缺失名称保持空。
# 函数用途: 返回确定性规则排名。
def _top_rules(alerts, rules):
    names = {row['rule_id']: row.get('name') for row in rules}
    counts = Counter(row.get('rule_id') for row in alerts)
    return [{'rule_id': key, 'name': names.get(key), 'count': count}
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0] or ''))[:10]]


# LLM: 同一实体在一条告警中只计一次，重复数组元素不增加排名。
# 函数用途: 统计真实告警的实体出现次数。
def _entities(alerts):
    counts = Counter()
    for row in alerts:
        counts.update({(entity['type'], entity['value']) for entity in row['entities']
            if isinstance(entity, dict) and isinstance(entity.get('type'), str)
            and isinstance(entity.get('value'), str) and entity['type'] and entity['value']})
    return counts


# LLM: UTC 桶覆盖整个请求窗口；超出固定输出容量时显式拒绝，不截断或换口径。
# 函数用途: 创建包含空桶的稳定趋势点。
def _points(alerts, since, until, bucket):
    step = 3600 if bucket == 'hour' else 86400
    start = math.floor(since / step) * step
    size = math.ceil((until - start) / step)
    if size > 744:
        raise HostAPIError('LIMIT_EXCEEDED')
    counts = Counter(math.floor(_epoch(row['created_at']) / step) * step for row in alerts
        if _within(row, 'created_at', since, until))
    return [{'at': _iso(start + index * step), 'count': counts[start + index * step]} for index in range(size)]


# LLM: 指标与报告共用同一 KPI 定义，样本从输入层排除。
# 函数用途: 提供主仪表盘指标、趋势与规则排名。
def dashboard(call):
    window, since, until = _period(call)
    rows, coverage = _snapshot(call, ('soc_alerts', 'soc_cases', 'soc_feedback', 'monitor_sla_policies', 'monitor_rules'))
    alerts = [row for row in rows['soc_alerts'] if _within(row, 'created_at', since, until)]
    return {'window': window,
        'kpis': soc_kpis(rows['soc_alerts'], rows['soc_cases'], rows['soc_feedback'], rows['monitor_sla_policies'],
            since=since, until=until),
        'trend': _points(alerts, since, until, 'hour' if until - since <= 48 * 3600 else 'day'),
        'top_rules': _top_rules(alerts, rows['monitor_rules']), 'coverage': coverage}


# LLM: 长规则 ID 使用稳定摘要，保持分组区别且符合冻结 key 长度，避免截断碰撞。
# 函数用途: 为趋势生成不会撞上 other 的规则分组键。
def _rule_key(value):
    if value is None:
        return 'none'
    key = 'rule:' + value
    return key if len(key) <= 64 else 'rule:' + hashlib.sha256(value.encode()).hexdigest()[:59]


# LLM: 前十九规则按窗口计数排名，剩余合成 other；每组均带全部空桶。
# 函数用途: 提供可分组的告警时间序列。
def alerts_trend(call):
    window, since, until = _period(call)
    rows, coverage = _snapshot(call, ('soc_alerts',))
    alerts = [row for row in rows['soc_alerts'] if _within(row, 'created_at', since, until)]
    bucket = call.params.get('bucket', 'hour' if until - since <= 48 * 3600 else 'day')
    grouping = call.params.get('group_by')
    groups = {}
    for row in alerts:
        key = (_rule_key(row.get('rule_id')) if grouping == 'rule' else row.get(grouping, 'none')) if grouping else 'all'
        groups.setdefault(key or 'none', []).append(row)
    if not grouping:
        groups.setdefault('all', [])
    if grouping == 'rule' and len(groups) > 19:
        keep = set(sorted(groups, key=lambda key: (-len(groups[key]), key))[:19])
        rest = [row for key, values in groups.items() if key not in keep for row in values]
        groups = {key: values for key, values in groups.items() if key in keep}
        groups['other'] = rest
    series = [{'key': key, 'points': _points(groups[key], since, until, bucket)} for key in sorted(groups)]
    if not series:
        _points([], since, until, bucket)
    return {'window': window, 'bucket': bucket, 'series': series, 'coverage': coverage}


# LLM: 规范动作和 telemetry 别名使用同一 handler，确认与解决统计不受创建窗口限制。
# 函数用途: 提供 SOC 耗时、分析师吞吐与自动化指标。
def soc_metrics(call):
    window, since, until = _period(call)
    rows, coverage = _snapshot(call, ('soc_alerts', 'soc_cases', 'soc_feedback'))
    alerts, cases = rows['soc_alerts'], rows['soc_cases']
    current = [row for row in alerts if _within(row, 'created_at', since, until)]
    triaged = Counter(row['assignee'] for row in alerts if row.get('assignee')
        and row['status'] != 'new' and _within(row, 'acknowledged_at', since, until))
    closed = Counter(row['assignee'] for row in cases if row.get('assignee') and _within(row, 'closed_at', since, until))
    analysts = [{'analyst': key, 'alerts_triaged': triaged[key], 'cases_closed': closed[key]}
        for key in sorted(triaged.keys() | closed.keys())]
    if len(analysts) > 200:
        coverage = 'partial'
    ai = [row for row in current if row.get('verdict') is not None]
    verdicts = effective_verdicts(rows['soc_feedback'], until=until)
    originals = {}
    for feedback in sorted(rows['soc_feedback'], key=lambda row: (_epoch(row['created_at']), row['override_id'])):
        if _epoch(feedback['created_at']) < until:
            originals.setdefault(feedback['alert_id'], feedback.get('original_verdict'))
    return {'window': window, 'mttd_seconds': _mean_duration(alerts, 'acknowledged_at', since, until),
        'mttr_seconds': _mean_duration(alerts, 'resolved_at', since, until),
        'case_mttr_seconds': _mean_duration(cases, 'closed_at', since, until),
        'analysts': analysts[:200], 'queue': {key: sum(row['status'] == key for row in current)
            for key in ('new', 'acknowledged', 'in_progress')},
        'automation': {'ai_verdicts': len(ai), 'human_overrides': sum(row['alert_id'] in verdicts for row in ai),
            'agreement_rate': sum(verdicts.get(row['alert_id'], row['verdict'])
                == originals.get(row['alert_id'], row['verdict']) for row in ai) / len(ai) if ai else None},
        'coverage': coverage}


# LLM: 每个 breakdown 包含全部有效告警，缺 verdict 明确归 none，不减少分母。
# 函数用途: 提供分类统计和规则、实体排名。
def alert_stats(call):
    window, since, until = _period(call)
    rows, coverage = _snapshot(call, ('soc_alerts', 'monitor_rules'))
    alerts = [row for row in rows['soc_alerts'] if _within(row, 'created_at', since, until)]
    result = {'window': window, 'total': len(alerts), 'coverage': coverage,
        'top_rules': _top_rules(alerts, rows['monitor_rules']),
        'top_entities': [{'type': key[0], 'value': key[1], 'count': count}
            for key, count in sorted(_entities(alerts).items(), key=lambda item: (-item[1], item[0]))[:10]]}
    for field in ('severity', 'status', 'source', 'verdict'):
        result['by_' + field] = dict(sorted(Counter(row.get(field) or 'none' for row in alerts).items()))
    return result


# LLM: 规则计算只消费结构化告警、反馈、SLA 和指标；证据保留命中的真实记录。
# 函数用途: 生成可解释、确定性的 SOC 洞察，不调用模型。
def insights(call):
    window, since, until = _period(call)
    rows, coverage = _snapshot(call, ('soc_alerts', 'soc_cases', 'soc_feedback',
        'monitor_rules', 'monitor_sla_policies', 'threat_indicators'))
    current = [row for row in rows['soc_alerts'] if _within(row, 'created_at', since, until)]
    previous = [row for row in rows['soc_alerts'] if _within(row, 'created_at', since - (until - since), since)]
    verdicts = effective_verdicts(rows['soc_feedback'], until=until)
    found, groups = [], {}
    for row in current:
        if row.get('rule_id'):
            groups.setdefault(row['rule_id'], []).append(row)
    for key, alerts in sorted(groups.items()):
        benign = [row for row in alerts if row['status'] in {'resolved', 'closed'}
            and verdicts.get(row['alert_id'], row.get('verdict')) in {'benign', 'false_positive'}]
        if len(alerts) >= 10 and len(benign) / len(alerts) >= 0.5:
            found.append({'kind': 'noisy_rule', 'title': '规则告警需要复核', 'detail': '窗口内至少一半告警已被裁定为低价值。',
                'severity': 'medium', 'evidence': {'rule_id': key, 'alerts': len(alerts), 'benign': len(benign),
                    'alert_ids': sorted(row['alert_id'] for row in benign)}})
    high = sum(row['severity'] in {'high', 'critical'} for row in current)
    prior = sum(row['severity'] in {'high', 'critical'} for row in previous)
    if prior and high >= prior * 1.5:
        found.append({'kind': 'rising_severity', 'title': '高严重度告警上升', 'detail': '较前一等长窗口增长至少一半。',
            'severity': 'high', 'evidence': {'current': high, 'previous': prior}})
    for entity, count in sorted(_entities(current).items(), key=lambda item: (-item[1], item[0])):
        if count >= 5:
            found.append({'kind': 'top_entity', 'title': '实体重复出现在告警中', 'detail': '同一实体关联至少五条告警。',
                'severity': 'info', 'evidence': {'type': entity[0], 'value': entity[1], 'count': count}})
    policies = _policy_map(rows['monitor_sla_policies'])
    risky = []
    for row in current:
        target = policies.get((row['space_id'], row['severity']), {}).get('resolve_minutes')
        if row['status'] in {'new', 'acknowledged', 'in_progress'} and type(target) is int and target >= 0:
            if until - _epoch(row['created_at']) >= target * 60 * 0.8:
                risky.append(row['alert_id'])
    if risky:
        found.append({'kind': 'sla_risk', 'title': '开放告警接近解决时限', 'detail': '已使用至少八成 SLA 解决时限。',
            'severity': 'high', 'evidence': {'count': len(risky), 'alert_ids': sorted(risky)}})
    indicators = {}
    for row in rows['threat_indicators']:
        if _epoch(row['created_at']) < until:
            indicators.setdefault(row.get('value'), []).append(row['indicator_id'])
    for entity in sorted(_entities(current)):
        if entity[1] in indicators:
            ids = sorted(row['alert_id'] for row in current
                if any(item.get('value') == entity[1] for item in row['entities']))
            found.append({'kind': 'known_indicator', 'title': '告警实体命中已知指标', 'detail': '实体值与当前 owner 指标账本精确相同。',
                'severity': 'high', 'evidence': {'type': entity[0], 'value': entity[1], 'alert_ids': ids,
                    'indicator_ids': sorted(indicators[entity[1]])}})
    return {'window': window, 'insights': found[:20], 'coverage': 'partial' if len(found) > 20 else coverage}


# LLM: 扩展只在冻结 usage.get 真实就绪时披露，不根据空模型账本造零。
# 函数用途: 供父级注册两个成本动作的 readiness。
def costs_ready(call):
    return call.services.ready('usage.get')


# LLM: 只读冻结调用不接受异步受理冒充数据；不构造第二个授权身份。
# 函数用途: 取得同上下文冻结动作的真实数据。
def _frozen(call, action, params):
    from ..gateway_parts.business_api_v13 import v13_call_frozen
    result = v13_call_frozen(call, action, params)
    if isinstance(result, DomainResult):
        if result.http_status != 200:
            raise HostAPIError('HOST_NOT_READY', 503)
        return dict(result.data)
    return result


# LLM: 分页穷尽后再排名，避免只排第一页；异常重复游标不能无限循环。
# 函数用途: 读取冻结 owner Agent 或空间目录及其覆盖状态。
def _directory(call, action, params):
    rows, cursors, coverage = [], set(), 'complete'
    request = dict(params, limit=100)
    for _page_index in range(1000):
        page = _frozen(call, action, request)
        rows.extend(page['items'])
        if page.get('coverage') not in {'complete', 'partial'}:
            coverage = 'partial'
        cursor = page['next_cursor']
        if cursor is None:
            if page.get('coverage') != 'complete':
                coverage = 'partial'
            return rows, coverage
        if cursor in cursors:
            raise HostAPIError('CURSOR_EXPIRED', 503)
        cursors.add(cursor)
        request['cursor'] = cursor
    raise HostAPIError('LIMIT_EXCEEDED')


# LLM: 时窗和可选空间都传给冻结 reader，不在本切片重算 token。
# 函数用途: 对准确过滤范围查询用量。
def _usage(call, since, until, extra=None):
    filters = {'since': _iso(since), 'until': _iso(until)}
    if call.params.get('space_id'):
        filters['space_id'] = call.params['space_id']
    if extra:
        filters.update(extra)
    return _frozen(call, 'usage.get', {'filter': filters})


# LLM: 覆盖未知与任一计量空值都向上传递；不同货币不可相加。
# 函数用途: 汇总冻结 UsageMeasure，保留无法证明的字段为空。
def _sum_measures(measures):
    result = {}
    for key in ('input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens', 'cost'):
        values = [row[key] for row in measures]
        result[key] = sum(values) if all(value is not None for value in values) else None
    currencies = {row['currency'] for row in measures}
    result['currency'] = next(iter(currencies)) if len(currencies) == 1 else None
    if result['currency'] is None:
        result['cost'] = None
    result['coverage'] = ('complete' if all(row['coverage'] == 'complete' for row in measures)
        and len(currencies) <= 1 else 'partial')
    return result


# LLM: 无 task 的调查没有可证明用量，不能当成零 token。
# 函数用途: 构造明确未知的计量项供总计传播。
def _unknown_measure():
    return dict.fromkeys(('input_tokens', 'cached_input_tokens', 'uncached_input_tokens',
        'output_tokens', 'cost', 'currency')) | {'coverage': 'unknown'}


# LLM: 空 token 排在有真实数值的记录后面，同值以规范 ID 排序。
# 函数用途: 为成本排名建立确定性排序键。
def _usage_rank(row, identity):
    tokens = row['actual']['input_tokens']
    return tokens is None, -(tokens or 0), row[identity]


# LLM: 模型账本没有按模型 token 拆分；只累计明确的尝试次数，坏账保留未知标志。
# 函数用途: 从当前 owner 已授权线程读取模型尝试数量。
def _model_attempts(call, since, until):
    from ..conversation.background_team_lifecycle import background_identity
    store = getattr(call.context.host, 'conversation_store', None)
    if store is None:
        return [], False
    try:
        threads, errors = store.threads.list_report(limit=0)
        counts, complete = Counter(), not errors
        for thread in threads:
            if thread.owner_id != call.owner_id:
                continue
            identity = background_identity(thread)
            if call.params.get('space_id') and (identity is None or identity.space_id != call.params['space_id']):
                continue
            facts = {'owner_id': call.owner_id, 'agent_id': thread.thread_id}
            if identity is not None:
                facts['space_id'] = identity.space_id
            call.context.check('usage.get', facts)
            events, load_errors = store.model_usage.events_report(thread.thread_id)
            complete = complete and not load_errors
            seen = set()
            for event in events:
                if event.event_id in seen:
                    complete = False
                    continue
                seen.add(event.event_id)
                if not math.isfinite(event.created_at):
                    complete = False
                    continue
                if not since <= event.created_at < until:
                    continue
                attempts = event.model_calls.get('model_attempt_counts')
                if not isinstance(attempts, dict):
                    if event.model_calls.get('models'):
                        complete = False
                    continue
                for model, count in attempts.items():
                    if not isinstance(model, str) or not model or len(model) > 200 or type(count) is not int or count < 0:
                        complete = False
                        continue
                    counts[model] += count
        if len(counts) > 100:
            complete = False
        return [{'model': model, 'attempt_count': count} for model, count in
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:100]], complete
    except (HostAPIError, BusinessError):
        raise
    except (OSError, ValueError, TypeError, RuntimeError):
        return [], False


# LLM: 总计、Agent 与空间全部查询冻结 usage.get；不按价格表或模型名推测费用。
# 函数用途: 提供成本仪表盘真实用量、排名和未知范围。
def costs_dashboard(call):
    window, since, until = _period(call)
    _resolve_scope(call)
    usage = _usage(call, since, until)
    unknown = {'tokens_by_model', *usage['unknown']}
    coverage = 'complete'
    measures = [usage['actual'], usage['estimated']]
    agent_params = {'lifecycle': 'all'}
    if call.params.get('space_id'):
        agent_params['space_id'] = call.params['space_id']
    agents, agent_coverage = _directory(call, 'background_agents.list', agent_params)
    spaces, space_coverage = _directory(call, 'spaces.list', {'state': 'all'})
    by_agent, by_space, seen = [], [], set()
    for agent in agents:
        if agent['agent_id'] in seen:
            coverage = 'partial'
            unknown.add('duplicate_agents')
            continue
        seen.add(agent['agent_id'])
        record = _usage(call, since, until, {'agent_id': agent['agent_id']})
        measures.extend((record['actual'], record['estimated']))
        unknown.update(record['unknown'])
        by_agent.append({'agent_id': agent['agent_id'], 'name': agent.get('name'),
            'actual': record['actual'], 'estimated': record['estimated']})
    seen = set()
    for space in spaces:
        if call.params.get('space_id') and space['space_id'] != call.params['space_id']:
            continue
        if space['space_id'] in seen:
            coverage = 'partial'
            unknown.add('duplicate_spaces')
            continue
        seen.add(space['space_id'])
        record = _usage(call, since, until, {'space_id': space['space_id']})
        measures.extend((record['actual'], record['estimated']))
        unknown.update(record['unknown'])
        by_space.append({'space_id': space['space_id'], 'actual': record['actual'], 'estimated': record['estimated']})
    by_model, models_complete = _model_attempts(call, since, until)
    if not models_complete:
        unknown.add('model_attempts')
        coverage = 'partial'
    if any(row['cost'] is None for row in measures):
        unknown.add('cost')
    if (any(row['coverage'] != 'complete' for row in measures) or agent_coverage != 'complete'
            or space_coverage != 'complete' or len(by_agent) > 100 or len(by_space) > 100):
        coverage = 'partial'
    return {'window': window, 'totals': {'actual': usage['actual'], 'estimated': usage['estimated']},
        'by_agent': sorted(by_agent, key=lambda row: _usage_rank(row, 'agent_id'))[:100],
        'by_space': sorted(by_space, key=lambda row: _usage_rank(row, 'space_id'))[:100],
        'by_model': by_model, 'unknown': _unknown_list(unknown), 'coverage': coverage}


# LLM: 协议最多二十个未知项，强制保留不可推算的模型 token 与费用标记。
# 函数用途: 生成有界、稳定的未知来源清单。
def _unknown_list(values):
    first = [key for key in ('tokens_by_model', 'cost') if key in values]
    return (first + sorted(set(values) - set(first)))[:20]


# LLM: 每个真实 run 查询准确 task；无 task 或重复 task 不冒充零用量、不重复求和。
# 函数用途: 聚合窗口内调查任务的实际用量与排名。
def costs_investigations(call):
    window, since, until = _period(call)
    runs, clean = _records(call, 'soc_investigations')
    runs = [row for row in runs if _within(row, 'started_at', since, until)]
    top, measures, unknown, tasks = [], [], set(), set()
    for run in sorted(runs, key=lambda row: row['run_id']):
        if not run.get('task_id'):
            unknown.add('runs_without_task')
            measures.append(_unknown_measure())
            continue
        if run['task_id'] in tasks:
            unknown.add('duplicate_tasks')
            clean = False
            continue
        tasks.add(run['task_id'])
        usage = _usage(call, since, until, {'task_id': run['task_id'], 'space_id': run['space_id']})
        actual = usage['actual']
        measures.append(actual)
        unknown.update(usage['unknown'])
        top.append({'run_id': run['run_id'], 'case_id': run.get('case_id'), 'task_id': run['task_id'], 'actual': actual})
    totals = _sum_measures(measures)
    if totals['cost'] is None:
        unknown.add('cost')
    count = len(runs)
    partial = not clean or bool(unknown - {'cost'}) or totals['coverage'] != 'complete'
    return {'window': window, 'investigations': count, 'totals': totals,
        'avg_input_tokens': totals['input_tokens'] / count if count and totals['input_tokens'] is not None else None,
        'avg_output_tokens': totals['output_tokens'] / count if count and totals['output_tokens'] is not None else None,
        'top': sorted(top, key=lambda row: _usage_rank(row, 'run_id'))[:call.params.get('limit', 50)],
        'unknown': _unknown_list(unknown), 'coverage': 'partial' if partial else 'complete'}
