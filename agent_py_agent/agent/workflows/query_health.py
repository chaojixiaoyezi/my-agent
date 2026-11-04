"""不可信源报告的纯值归一与查询健康回执；无IO或权限副作用。"""
from __future__ import annotations

from copy import deepcopy

from ..assets import safe_asset_id
from ..assets.descriptor import access_target_requires_network


# LLM: 来源ID只有结构校验，不解释正文或授予路径权限。
# 函数用途: 丢弃不能作为来源身份的值。
def _source_id(value):
    if not isinstance(value, str):
        return ''
    try:
        return safe_asset_id(value)
    except ValueError:
        return ''


# LLM: 未知状态或错误类型不获得健康身份；显式HTTP失败优先于脚本自报ok。
# 函数用途: 保守归一一行来源观察。
def _observation(row):
    if not isinstance(row, dict) or not (identity := _source_id(row.get('source_id'))):
        return None
    status, hits = row.get('status'), row.get('hit_count')
    known = ('ok', 'zero_hits', 'auth_expired', 'endpoint_unreachable', 'not_found', 'offline', 'transient_error')
    if not isinstance(status, str) or status not in known:
        status = 'transient_error'
    if type(hits) is not int or hits < 0 or status == 'zero_hits' and hits != 0:
        hits, status = 0, 'transient_error'
    http_status, error = row.get('http_status'), row.get('error_code', '')
    if http_status is not None and (type(http_status) is not int or not 100 <= http_status <= 599):
        http_status, status = None, 'transient_error'
    if not isinstance(error, str):
        error, status = '', 'transient_error'
    if status in ('ok', 'zero_hits') and (error or http_status is not None and http_status >= 400):
        status = 'transient_error'
    return {'source_id': identity, 'status': status, 'hit_count': hits,
            'http_status': http_status, 'error_code': error}


# LLM: 输出数据不授予权限，坏类型不能抛到执行链。
# 函数用途: 归一有界的源报告。
def normalize_source_report(source_report, *, max_sources=64, max_discovered=256):
    if (not isinstance(source_report, dict) or source_report.get('stage') != 'probe+fetch'
            or not isinstance(source_report.get('sources'), list)
            or not isinstance(source_report.get('discovered_sources'), list)):
        return None
    rows = {}
    incomplete = (len(source_report['sources']) > max_sources
                  or len(source_report['discovered_sources']) > max_discovered)
    for value in source_report['sources'][:max_sources]:
        row = _observation(value)
        if row is None:
            incomplete = True
        if row is not None:
            previous = rows.get(row['source_id'])
            if previous is not None and previous != row:
                row = {**row, 'status': 'transient_error', 'hit_count': 0}
            rows[row['source_id']] = row
    discovered = {_source_id(value) for value in source_report['discovered_sources'][:max_discovered]}
    return {'sources': list(rows.values()), 'discovered_sources': sorted(discovered - {''}),
            'stage': 'probe+fetch', 'incomplete': incomplete or '' in discovered}


# LLM: 清单来自宿主但消费者容忍旧形状；不从错误类型推导core或凭据身份。
# 函数用途: 取得清单的安全来源索引。
def _registered(manifest, *, max_sources=256):
    values = manifest.get('sources', []) if isinstance(manifest, dict) else []
    values = values[:max_sources] if isinstance(values, list) else []
    return {identity: row for row in values if isinstance(row, dict)
            and (identity := _source_id(row.get('source_id')))}


# LLM: 只有已报告成功且HTTP未失败的源可贡献命中与健康计数。
# 函数用途: 判断归一来源观察是否健康。
def _healthy(row):
    return row['status'] in ('ok', 'zero_hits')


# LLM: 指引只根据宿主结构化裁决生成；一行文字不再被解析成机器权限或阻断依据。
# 函数用途: 解释阻断、未知覆盖与清单漂移。
def _guidance(blocked, coverage, unregistered, missing, degraded):
    parts = []
    if blocked:
        parts.append('数据获取失败或覆盖不足，不得据此给出“无异常”结论；请说明失败并询问是否重试')
    elif coverage == 'undeclared':
        parts.append('未声明来源报告，查询覆盖范围未验证')
    elif degraded:
        parts.append('仅交付健康来源的数据，必须说明部分来源查询失败，不能把失败算作零命中')
    if unregistered:
        parts.append('保留新来源的数据，并询问用户是否登记新来源')
    if missing:
        parts.append('说明已登记来源缺失，并询问用户是否移除退休来源；未经确认不得移除')
    return '；'.join(parts)


# LLM: 仅依赖结构化源报告、清单、重试账和结果错误码。
# 函数用途: 构造健康回执及分析阻断依据。
def build_query_health(source_report, manifest=None, retries=(), error_code='', *, declared_sources=()):
    report = normalize_source_report(source_report)
    coverage = 'declared' if manifest is not None else 'undeclared'
    registered = _registered(manifest)
    rows = {row['source_id']: row for row in report['sources']} if report else {}
    discovered = set(report['discovered_sources']) if report else set()
    unregistered = sorted(discovered - registered.keys()) if manifest is not None else []
    missing = sorted(registered.keys() - discovered) if report else sorted(registered)
    declared = {_source_id(value.get('source_id') if isinstance(value, dict) else value)
                for value in declared_sources if isinstance(value, (str, dict))}
    expected = (registered.keys() | declared) - {''}
    healthy = [row for row in rows.values() if _healthy(row)]
    degraded = {identity: {'source_id': identity, 'error_code': 'SOURCE_MISSING'}
                for identity in expected - rows.keys()}
    alerts = []
    for identity, row in rows.items():
        if not _healthy(row):
            degraded[identity] = {'source_id': identity, 'error_code': row['error_code'] or
                ('SOURCE_MISSING' if row['status'] in ('not_found', 'offline') else
                 str(row['http_status']) if row['http_status'] is not None else row['status'])}
        if row['status'] in ('auth_expired', 'endpoint_unreachable', 'not_found') or row['http_status'] in (401, 403, 404):
            ref = registered.get(identity, {}).get('credential_ref', '')
            alerts.append({'source_id': identity, 'credential_ref': ref if isinstance(ref, str) else '',
                           'source_health_status': 'AUTH_OR_ENDPOINT_EXPIRED'})
    hits = any(row['hit_count'] > 0 for row in healthy)
    incomplete = bool(report and report['incomplete'])
    failed = bool(degraded or error_code or incomplete)
    classification = ('PARTIAL_SOURCE_FAILURE' if hits and failed else 'SUCCESS_WITH_HITS' if hits else
                      'TRANSIENT_FAILURE_EMPTY' if failed or not healthy else 'SUCCESS_ZERO_HITS')
    core_failed = any(row.get('core') is True and identity in degraded for identity, row in registered.items())
    blocked = (classification == 'TRANSIENT_FAILURE_EMPTY' or core_failed) and bool(report or manifest is not None or error_code)
    reason = 'SOURCE_REPORT_MISSING' if report is None and manifest is not None else 'QUERY_FAILED_NO_DATA' if blocked else None
    return {'schema_version': 'query_health.v1', 'classification': classification,
        'analysis_blocked': bool(blocked), 'block_reason': reason, 'coverage': coverage,
        'healthy_sources_queried': len(healthy), 'degraded_sources': [degraded[key] for key in sorted(degraded)],
        'auth_alerts': alerts, 'unregistered_sources_detected': unregistered, 'registered_sources_missing': missing,
        'retries': deepcopy(list(retries)) if isinstance(retries, (list, tuple)) else [],
        'guidance': _guidance(blocked, coverage, unregistered, missing, degraded or incomplete) +
            ('；来源报告截断或含无效记录，覆盖范围未验证' if incomplete else '')}


# LLM: 显式写声明胜过方法名；网络只认标准只读方法，其他目标须显式read标志。
# 函数用途: 判断一个已声明访问目标是否只读。
def _readonly_target(target):
    if not isinstance(target, dict):
        return False
    extra = target.get('extra', {})
    extra = extra if isinstance(extra, dict) else {}
    if extra.get('read_only') is False or extra.get('access') in ('write', 'read_write'):
        return False
    method = target.get('method', '')
    if access_target_requires_network(target):
        return isinstance(method, str) and method.upper() in ('GET', 'HEAD', 'OPTIONS')
    return extra.get('read_only') is True or extra.get('access') == 'read'


# LLM: 只读取已声明访问方式及显式幂等标记。
# 函数用途: 判断脚本是否可以自动重试。
def retry_is_safe(record):
    metadata = record.get('metadata', {})
    metadata = metadata if isinstance(metadata, dict) else {}
    policy = metadata.get('retry_policy', {})
    if isinstance(policy, dict) and policy.get('idempotent') is True:
        return True
    targets = record.get('access_targets', [])
    if not isinstance(targets, (list, tuple)) or not targets or not all(_readonly_target(row) for row in targets):
        return False
    contract = record.get('access_contract')
    if contract is not None and not isinstance(contract, dict):
        return False
    sources = contract.get('data_sources', []) if contract is not None else record.get('data_sources', [])
    return isinstance(sources, (list, tuple)) and all(isinstance(row, dict)
        and isinstance(row.get('query_scope', {}), dict)
        and row.get('access', row.get('query_scope', {}).get('access', 'read')) == 'read'
        and row.get('query_scope', {}).get('access', 'read') == 'read' for row in sources)


# LLM: 不读取异常说明、stdout或stderr来判定瞬态失败。
# 函数用途: 从源报告及运行错误码取得瞬态原因。
def transient_failure_code(source_report, error_code=''):
    codes = ('ECONNRESET', 'ETIMEDOUT', 'SCRIPT_TIMEOUT', 'timeout', '502', '503', '504')
    if error_code in codes:
        return error_code
    report = normalize_source_report(source_report)
    for row in report['sources'] if report else ():
        if row['error_code'] in codes:
            return row['error_code']
        if row['http_status'] in (502, 503, 504):
            return str(row['http_status'])
        if row['status'] == 'transient_error':
            return 'transient_error'
    return ''
