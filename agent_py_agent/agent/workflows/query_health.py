"""不可信源报告的纯值归一与查询健康回执；无IO或权限副作用。"""
from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from ..assets import safe_asset_id
from ..assets.descriptor import access_target_requires_network
from ..common.log_redaction import redact_sensitive_value
from .models import redact_bearer_values


def _duration(value):
    """仅真实有限非负数可作耗时样本；bool/缺失不能补为零。"""
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def _attempts(value):
    return value if type(value) is int and value >= 1 else None


def _field_counts(known, unknown):
    return {'known': known, 'unknown': unknown,
            'coverage': 'complete' if not unknown else 'partial' if known else 'unknown'}


def _merge_coverage(values: tuple[str, ...]) -> str:
    return 'unknown' if 'unknown' in values else 'partial' if 'partial' in values else 'complete'


def _health_runs(run_store, workflow_id, limit):
    """只消费原读取端口；旧 list_runs 的返回条数不证明其实际 I/O 有界。"""
    try:
        sampler = getattr(run_store, 'sample_runs_readonly', None)
        if callable(sampler):
            sample = sampler(workflow_id=workflow_id, limit=limit)
            runs, metadata = sample['runs'], sample
        else:
            runs = run_store.list_runs(workflow_id=workflow_id, limit=limit)
            metadata = getattr(run_store, 'health_read_coverage', None)
        if not isinstance(runs, (list, tuple)):
            raise TypeError('runs 必须是原记录窗口')
        has_metadata = isinstance(metadata, dict)
        metadata = metadata if has_metadata else {}
        coverage = metadata.get('coverage', 'unknown')
        if coverage not in ('complete', 'partial', 'unknown'):
            coverage = 'unknown'
        if has_metadata and type(metadata.get('truncated')) is not bool:
            coverage = 'unknown'
        truncated = metadata.get('truncated') is True or len(runs) > limit
        if not has_metadata and len(runs) >= limit:
            truncated, coverage = True, 'partial'
        if truncated and coverage == 'complete':
            coverage = 'partial'
        source = {'coverage': coverage, 'truncated': truncated}
        if not has_metadata:
            source['io_bounded'] = False
        for key in ('scan_limit', 'scan_count', 'read_limit', 'read_count'):
            value = metadata.get(key)
            if type(value) is int and value >= 0:
                source[key] = value
        return runs[:limit], source
    except PermissionError:
        raise
    except Exception as exc:
        # 不输出可能含凭据的异常正文，也不把读失败伪装成完整零次。
        return [], {'coverage': 'unknown', 'truncated': False, 'io_bounded': False,
                    'error_type': type(exc).__name__, 'error_code': 'WORKFLOW_HEALTH_READ_FAILED',
                    'effect_outcome': 'unknown', 'next_action': '检查原读取端口与覆盖元数据后重试'}


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


# LLM: 提取单个运行各步骤的健康快照与规范引用；严格脱敏敏感凭据与内部错误。
# 函数用途: 将 StepExecutionRecord 转换为只读健康统计项。
def summarize_run_step_health(run, *, redact=True):
    step_summaries = []
    for step in getattr(run, 'steps', ()):
        error_msg = getattr(step, 'error_message', '')
        if redact and error_msg:
            error_msg = str(redact_sensitive_value(error_msg))
        escalation = getattr(step, 'escalation_result', {}) or {}
        tool_receipt = escalation.get('tool_receipt', {}) or {}
        agent_task = escalation.get('agent_task', {}) or {}
        subwf_receipt = escalation.get('subworkflow_receipt', {}) or {}

        # effect/子运行及其它领域operation引用不签发工具操作身份；缺原回执就保持未知。
        operation_id = tool_receipt.get('operation_id') or ''
        subworkflow_run_id = (
            getattr(step, 'subworkflow_run_id', '')
            or subwf_receipt.get('child_run_id', '')
            or escalation.get('subworkflow_run_id', '')
        )
        task_id = (
            agent_task.get('task_id')
            or escalation.get('task_id', '')
        )
        effect_id = (
            getattr(step, 'effect_id', '')
            or subworkflow_run_id
            or operation_id
            or escalation.get('effect_id', '')
        )

        attempts = _attempts(getattr(step, 'attempt_count', None))
        max_attempts = _attempts(getattr(step, 'max_attempts', None))
        status = getattr(step, 'status', None)
        status = status if isinstance(status, str) and status else None
        error_code = getattr(step, 'error_code', None)
        error_code = error_code if isinstance(error_code, str) else None
        if redact and error_code:
            error_code = redact_bearer_values(error_code)
        step_summaries.append({
            'step_id': step.step_id,
            'name': getattr(step, 'name', ''),
            'mode': getattr(step, 'mode', ''),
            'status': status,
            'duration_ms': _duration(getattr(step, 'duration_ms', None)),
            'attempt_count': attempts,
            'max_attempts': max_attempts,
            'retried': attempts > 1 if attempts is not None else None,
            'error_code': error_code,
            'error_message': error_msg,
            'needs_repair': getattr(step, 'needs_repair', False),
            'effect_id': effect_id,
            'operation_id': operation_id,
            'subworkflow_run_id': subworkflow_run_id,
            'task_id': task_id,
        })

    result = {
        'workflow_run_id': getattr(run, 'workflow_run_id', ''),
        'workflow_id': getattr(run, 'workflow_id', ''),
        'status': getattr(run, 'status', ''),
        'duration_ms': _duration(getattr(run, 'duration_ms', None)),
        'total_steps': getattr(run, 'total_steps', len(step_summaries)),
        'completed_steps': getattr(run, 'completed_steps', 0),
        'steps': step_summaries,
    }
    return redact_bearer_values(result) if redact else result


# LLM: 构造有界工作流及各步骤的健康视图，严禁全量扫描历史文件 (WF-11)。
# 函数用途: 按 bounded_limit 读取有限运行并聚合耗时、故障分类、重试与规范引用。
def build_workflow_health_view(run_store, *, workflow_id=None, limit=50, redact=True):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit 必须是 1..100 的严格整数')
    bounded_limit = limit
    runs, source_coverage = _health_runs(run_store, workflow_id, bounded_limit)
    coverage = source_coverage['coverage']
    truncated = source_coverage['truncated']

    run_counts = {
        'succeeded': 0,
        'failed': 0,
        'running': 0,
        'paused_for_resume': 0,
        'waiting_for_agent': 0,
        'cancelled': 0,
    }
    workflow_counts: dict[str, dict[str, int]] = {}
    step_aggregates: dict[str, dict[str, Any]] = {}
    overall_reason_classes: dict[str, int] = {}
    total_attempts_all = 0
    retried_steps_all = 0
    fields = ('duration_ms', 'attempt_count', 'max_attempts', 'status', 'error_code')
    field_counts = {key: [0, 0] for key in fields}
    known_attempts_all = unknown_attempts_all = 0
    all_effect_ids: set[str] = set()
    all_operation_ids: set[str] = set()
    all_subworkflow_run_ids: set[str] = set()
    all_task_ids: set[str] = set()

    for run in runs:
        st = getattr(run, 'status', 'unknown')
        if st in run_counts:
            run_counts[st] += 1
        else:
            run_counts[st] = run_counts.get(st, 0) + 1

        wid = getattr(run, 'workflow_id', '') or 'unknown'
        if wid not in workflow_counts:
            workflow_counts[wid] = {
                'succeeded': 0,
                'failed': 0,
                'running': 0,
                'paused_for_resume': 0,
                'waiting_for_agent': 0,
                'cancelled': 0,
                'total': 0,
            }
        workflow_counts[wid]['total'] += 1
        if st in workflow_counts[wid]:
            workflow_counts[wid][st] += 1

        summary = summarize_run_step_health(run, redact=redact)
        for s in summary['steps']:
            sid = s['step_id']
            if sid not in step_aggregates:
                step_aggregates[sid] = {
                    'step_id': sid,
                    'name': s['name'],
                    'mode': s['mode'],
                    'execution_count': 0,
                    'status_counts': {},
                    'durations': [],
                    'failure_count': 0,
                    'reason_classes': {},
                    'total_attempts': 0,
                    'retried_count': 0,
                    'known_attempts': 0,
                    'unknown_attempts': 0,
                    'field_counts': {key: [0, 0] for key in fields},
                    'effect_ids': set(),
                    'operation_ids': set(),
                    'subworkflow_run_ids': set(),
                    'task_ids': set(),
                }
            agg = step_aggregates[sid]
            agg['execution_count'] += 1
            step_st = s['status'] or 'unknown'
            agg['status_counts'][step_st] = agg['status_counts'].get(step_st, 0) + 1
            if s['duration_ms'] is not None:
                agg['durations'].append(s['duration_ms'])
            for key in fields:
                known = s[key] is not None
                if key == 'error_code' and s['status'] == 'failed' and not s[key]:
                    known = False
                index = 0 if known else 1
                agg['field_counts'][key][index] += 1
                field_counts[key][index] += 1

            if s['status'] == 'failed' or s['error_code']:
                agg['failure_count'] += 1
                err_code = s['error_code'] or 'STEP_FAILED'
                agg['reason_classes'][err_code] = agg['reason_classes'].get(err_code, 0) + 1
                overall_reason_classes[err_code] = overall_reason_classes.get(err_code, 0) + 1

            attempts = s['attempt_count']
            if attempts is None:
                agg['unknown_attempts'] += 1
                unknown_attempts_all += 1
            else:
                agg['total_attempts'] += attempts
                total_attempts_all += attempts
                agg['known_attempts'] += 1
                known_attempts_all += 1
            if s['retried']:
                agg['retried_count'] += 1
                retried_steps_all += 1

            if s['effect_id']:
                agg['effect_ids'].add(s['effect_id'])
                all_effect_ids.add(s['effect_id'])
            if s['operation_id']:
                agg['operation_ids'].add(s['operation_id'])
                all_operation_ids.add(s['operation_id'])
            if s['subworkflow_run_id']:
                agg['subworkflow_run_ids'].add(s['subworkflow_run_id'])
                all_subworkflow_run_ids.add(s['subworkflow_run_id'])
            if s['task_id']:
                agg['task_ids'].add(s['task_id'])
                all_task_ids.add(s['task_id'])

    step_summaries_compiled = {}
    for sid, agg in step_aggregates.items():
        durations = agg['durations']
        cnt = len(durations)
        if cnt > 0:
            sorted_d = sorted(durations)
            min_ms = round(sorted_d[0], 2)
            max_ms = round(sorted_d[-1], 2)
            avg_ms = round(sum(value / cnt for value in sorted_d), 2)
            p95_idx = max(0, int(math.ceil(0.95 * cnt)) - 1)
            p95_ms = round(sorted_d[p95_idx], 2)
        else:
            min_ms = avg_ms = max_ms = p95_ms = None

        step_summaries_compiled[sid] = {
            'step_id': sid,
            'name': agg['name'],
            'mode': agg['mode'],
            'execution_count': agg['execution_count'],
            'status_counts': agg['status_counts'],
            'duration_ms': {
                'count': cnt,
                'min_ms': min_ms,
                'avg_ms': avg_ms,
                'max_ms': max_ms,
                'p95_ms': p95_ms,
            },
            'failure_count': agg['failure_count'],
            'reason_classes': agg['reason_classes'],
            'field_coverage': {key: _field_counts(*counts) for key, counts in agg['field_counts'].items()},
            'retry_stats': {
                'total_attempts': agg['total_attempts'] if agg['known_attempts'] else None,
                'retried_steps': agg['retried_count'],
                'known_attempt_steps': agg['known_attempts'],
                'unknown_attempt_steps': agg['unknown_attempts'],
                'coverage': _field_counts(agg['known_attempts'], agg['unknown_attempts'])['coverage'],
            },
            'canonical_references': {
                'effect_ids': sorted(agg['effect_ids']),
                'operation_ids': sorted(agg['operation_ids']),
                'subworkflow_run_ids': sorted(agg['subworkflow_run_ids']),
                'task_ids': sorted(agg['task_ids']),
            },
        }

    field_coverage = {key: _field_counts(*counts) for key, counts in field_counts.items()}
    field_status = 'partial' if any(counts[1] for counts in field_counts.values()) else 'complete'
    result = {
        'schema_version': 'workflow_health.v1',
        'workflow_id': workflow_id,
        'coverage': _merge_coverage((coverage, field_status)),
        'source_coverage': source_coverage,
        'field_coverage': field_coverage,
        'truncated': truncated,
        'sampled_runs': len(runs),
        'bounded_limit': bounded_limit,
        'run_counts': run_counts,
        'workflow_counts': workflow_counts,
        'step_health': step_summaries_compiled,
        'reason_classes': overall_reason_classes,
        'retry_stats': {
            'total_attempts': total_attempts_all if known_attempts_all or not unknown_attempts_all else None,
            'retried_steps': retried_steps_all,
            'known_attempt_steps': known_attempts_all,
            'unknown_attempt_steps': unknown_attempts_all,
            'coverage': _field_counts(known_attempts_all, unknown_attempts_all)['coverage'],
        },
        'canonical_references': {
            'effect_ids': sorted(all_effect_ids),
            'operation_ids': sorted(all_operation_ids),
            'subworkflow_run_ids': sorted(all_subworkflow_run_ids),
            'task_ids': sorted(all_task_ids),
        },
    }
    return redact_bearer_values(result) if redact else result


# LLM: 便捷工作流状态健康报告；复用 build_workflow_health_view 的有界读取。
# 函数用途: 向外提供与既有契约对齐的健康报告。
def workflow_state_report(run_store, workflow_id=None, *, limit=50, redact=True):
    return build_workflow_health_view(run_store, workflow_id=workflow_id, limit=limit, redact=redact)
