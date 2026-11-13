"""默认业务观察只读适配器，原运行库、展示归档与媒体登记仍为唯一事实源。

LLM: 不创建来源或执行Agent。缺源不注册，读失败交原_source标为unknown；
每条记录携原owner/space/thread/run/attempt，由原观察服务逐条授权并末核。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import read_bytes_beneath, read_text_beneath
from ..contracts.error_taxonomy import error_contract
from ..conversation.background_team_lifecycle import background_identity
from ..conversation.business_access import BusinessError
from ..conversation.business_contracts import BusinessExecutionContext
from ..conversation.business_observability import (
    BusinessObservationService,
    ObservationPage,
    ObservationRecord,
    ObservationSourceError,
    ObservationSourcePage,
    _observation_cached,
    _observation_task,
    _observation_thread,
    bounded_observation_reader,
    observation_request,
    public_result_refs,
    runtime_source_revision,
    source_after,
)
from ..conversation.decision_audit import decision_usage_summary
from ..conversation.display_archive import (
    DisplayArchiveError,
    _read_manifest,
    read_display_archive_page,
)
from ..conversation.usage_sources import UsageSourcePlan
from ..runtime_db.repository import RuntimeRepository


# LLM: 指纹仅作只读分页版本，不建立第二事实库。
# 函数用途: 为源快照生成稳定摘要。
def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# LLM: 时间直接取自规范账本，不推测缺失值。
# 函数用途: 将秒数转换为UTC时间。
def _stamp(value):
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None
    except (OverflowError, OSError) as error:
        raise ValueError('source timestamp out of range') from error


@dataclass(frozen=True)
class _ScopedUsageRecord(ObservationRecord):
    """一次准确筛选的用量聚合行；成员只保存本次查询已读的canonical授权事实。"""
    members: tuple = ()
    filter_digest: str = ''


# LLM: 聚合的时间范围不能伪装成单次发生时间；成员逐条使用原_visible及原actor，显式projection不变。
# 函数用途: 供原_usage选择行时验证默认reader的范围证明和所有成员授权。
def usage_record_visible(service, record, params, actor):
    if not isinstance(record, _ScopedUsageRecord):
        return BusinessObservationService._visible(service, record, 'usage.get', params, actor)
    if record.filter_digest != _digest(params.get('filter', {})):
        return False
    scope = {'filter': {key: value for key, value in params.get('filter', {}).items() if key not in {'since', 'until'}}}
    if not BusinessObservationService._visible(service, record, 'usage.get', scope, actor):
        return False
    # 时间边界由源按原created_at数值过滤，不能再用ISO文本排序覆盖它；其它成员事实逐条末核。
    members = iter(record.members)
    try:
        return all(BusinessObservationService._visible(service, member, 'usage.get', scope, actor) for member in members)
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return False
    finally:
        close = getattr(members, 'close', None)
        if close is not None:
            close()


class DefaultBusinessObservationService(BusinessObservationService):
    """只为默认reader提供聚合范围证明；原query、schema与权限末核保持不变。"""

    # LLM: 仅识别本模块生成的范围证明；普通行和显式宿主投影仍走原过滤授权。
    # 函数用途: 在原观察流程内逐成员核验聚合，不给闭合DTO添加虚构发生时间。
    def _visible(self, record, action, params, actor):
        if action == 'usage.get' and isinstance(record, _ScopedUsageRecord):
            return usage_record_visible(self, record, params, actor)
        return super()._visible(record, action, params, actor)


class _DecisionWindow:
    """原model_usage的内存只读筛选视图，不增加落盘账、不替换原计数算法。"""

    # LLM: revision在原events_report前后核验后固定到本次窗口，分组不重新读磁盘。
    # 函数用途: 包装本次已验证窗口及原事件对象给decision_usage_summary。
    def __init__(self, original, events, revisions):
        self.model_usage, self.original, self.events = self, original, events
        self.revisions = revisions

    # LLM: 版本仍来自原事实源，但不在本地分组时重复stat历史文件。
    # 函数用途: 返回本窗口已核验的原账本版本。
    def revision(self, thread_id):
        return self.revisions[thread_id]

    # LLM: 异常已在投影前失败关闭，不用过滤掩盖坏账。
    # 函数用途: 返回当前窗口原事件对象。
    def events_report(self, thread_id):
        return self.events[thread_id], []


class _ModelUsageWindow:
    """在原decision遍历中同时聚合完整调用账，流式来源只消费一次。"""
    def __init__(self, original):
        self.model_usage, self.original = self, original
        self.total = {}
        self.has_decision_purpose = self.has_other_purpose = False

    def revision(self, thread_id):
        return self.original.revision(thread_id)

    def events_report(self, thread_id):
        events, errors = self.original.events_report(thread_id)
        return self._events(events), errors

    def _events(self, events):
        from ..conversation.store_usage import _sum_model_call_summaries

        iterator = iter(events)
        try:
            for event in iterator:
                # 原落盘账已把累计快照转换为delta；不得再叠加purpose分区到根总量。
                self.total = _sum_model_call_summaries([self.total, event.model_calls],
                    include_purposes=False, count_unknown=False)
                purposes = event.model_calls.get('purpose_breakdown', {})
                if isinstance(purposes, dict):
                    for key, value in purposes.items():
                        if key != 'schema' and isinstance(value, dict) and value.get('physical_model_attempt_count'):
                            if key == 'decision':
                                self.has_decision_purpose = True
                            else:
                                self.has_other_purpose = True
                yield event
        finally:
            close = getattr(iterator, 'close', None)
            if close is not None:
                close()


def _model_usage_measures(total, report):
    """沿原provider/estimated分区及逐字段reported计数，缺字段不补零。"""
    fields = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'uncached_input_tokens',
                           'output_tokens', 'cost', 'currency'))
    actual, estimated = dict(fields, coverage='partial'), dict(fields, coverage='unknown')
    usage = total.get('usage_breakdown', {})
    provider, local = usage.get('provider', {}), usage.get('estimated', {})
    input_calls = provider.get('input_tokens_reported_call_count', 0)
    cache_calls = provider.get('cache_read_input_tokens_reported_call_count', 0)
    if input_calls:
        actual['input_tokens'] = provider['input_tokens']
    elif report['input_reported_calls']:
        # 旧账只有decision分区时保留原已知输入，不将它另加到根摘要。
        actual['input_tokens'] = report['input_tokens_reported']
    if provider.get('output_tokens_reported_call_count'):
        actual['output_tokens'] = provider['output_tokens']
    if cache_calls:
        actual['cached_input_tokens'] = provider['cache_read_input_tokens']
    if input_calls and input_calls == cache_calls == provider.get('call_count'):
        uncached = provider['input_tokens'] - provider['cache_read_input_tokens']
        if uncached >= 0:
            actual['uncached_input_tokens'] = uncached
    if local.get('call_count') or local.get('unfinished_call_count'):
        estimated.update(input_tokens=local.get('input_tokens', 0) + local.get('unfinished_input_tokens', 0),
                         coverage='partial')
        if local.get('call_count'):
            estimated['output_tokens'] = local.get('output_tokens', 0)
    elif report['estimated_unfinished_calls']:
        estimated.update(input_tokens=report['input_tokens_estimated_unfinished'], coverage='partial')
    return actual, estimated


class _UsageEventSources:
    def __init__(self, window):
        self.window = window

    def __iter__(self):
        if self.window.source_plan is None:
            yield from self.window.explicit_revisions
            return
        with closing(self.window.source_plan.rows()) as rows:
            for identifier, revision in rows:
                self.window.source_revisions[identifier] = revision
                self.window.source_revisions.move_to_end(identifier)
                if len(self.window.source_revisions) > 128:
                    self.window.source_revisions.popitem(last=False)
                yield identifier

    def __getitem__(self, identifier):
        return _StreamUsageEvents(self.window, identifier)

    def values(self):
        for identifier in self:
            yield self[identifier]

    def items(self):
        for identifier in self:
            yield identifier, self[identifier]


class _StreamUsageEvents:
    def __init__(self, window, thread_id):
        self.window, self.thread_id = window, thread_id

    def __iter__(self):
        with closing(self.window.pairs(self.thread_id)) as pairs:
            for event, facts in pairs:
                self.window.capture(facts)
                yield event


class _StreamUsageMembers:
    def __init__(self, window):
        self.window = window

    def __bool__(self):
        return self.window.member_count > 0

    def __iter__(self):
        with closing(iter(self.window.events)) as identifiers:
            for identifier in identifiers:
                with closing(self.window.pairs(identifier)) as pairs:
                    for _, facts in pairs:
                        yield facts


class _StreamDecisionWindow:
    """时间索引流式窗口；只保固定线程集合、计数与范围证明，不保所有事件正文。"""
    def __init__(self, source, threads, revisions, params, since, until):
        self.source = source
        self.source_plan = threads if isinstance(threads, UsageSourcePlan) else None
        self.explicit_revisions = revisions
        self.source_revisions = OrderedDict()
        self.params, self.since, self.until = params, since, until
        self.model_usage, self.original = self, source.store.model_usage
        self.events = _UsageEventSources(self)
        self.members = _StreamUsageMembers(self)
        self.member_count, self.common, self.mixed = 0, {}, set()

    def revision(self, thread_id):
        return self.explicit_revisions[thread_id] if self.source_plan is None else self.source_revisions[thread_id]

    def events_report(self, thread_id):
        return self.events[thread_id], []

    def capture(self, facts):
        self.member_count += 1
        for key in ('space_id', 'project_ref', 'task_id', 'thread_id', 'run_id', 'attempt_id'):
            value = getattr(facts, key)
            if key not in self.common:
                self.common[key] = value
            elif key not in self.mixed and self.common[key] != value:
                self.common[key] = None
                self.mixed.add(key)

    def pairs(self, thread_id):
        expected, store = self.revision(thread_id), self.source.store.model_usage
        thread = _observation_thread(self.source.store, thread_id)
        if thread.owner_id != self.source.owner:
            raise ValueError('usage source owner mismatch')
        if store.revision(thread_id) != expected:
            raise ValueError('usage source generation changed')
        rows, errors = store.events_window_report(thread_id, since=self.since, until=self.until)
        if errors:
            raise ValueError('usage window source unreadable')
        try:
            for event in rows:
                if event.thread_id != thread_id or not self.since <= event.created_at < self.until:
                    raise ValueError('usage event indexed range mismatch')
                facts = self.source.usage_event_record(thread, event)
                filters = self.params.get('filter', {})
                if any(filters.get(key) is not None and getattr(facts, key) != filters[key]
                       for key in ('space_id', 'project_ref', 'task_id', 'run_id')):
                    continue
                yield event, facts
            if store.revision(thread_id) != expected:
                raise ValueError('usage source generation changed')
        except RuntimeError as error:
            raise ValueError('usage window source unreadable') from error
        finally:
            close = getattr(rows, 'close', None)
            if close is not None:
                close()


def _observed_tool_error(control_code, *, result=None, workflow=None, fallback_message=''):
    """公开已结算结构化原因和宿主恢复说明，旧的工作流回执沿同一失败步骤核验。"""
    from ..workflows.review_receipts import pending_review_error

    receipt = result if isinstance(result, dict) else {}
    reason = pending_review_error(workflow)
    if not reason and control_code == 'TOOL_EXECUTION_FAILED':
        reported = receipt.get('reported_error_code')
        if isinstance(reported, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', reported):
            reason = reported
    code = reason or control_code
    if not isinstance(code, str) or not code:
        return None
    contract = error_contract(code)
    retryable = receipt.get('retryable') is True and receipt.get('effect_outcome') in {'none', 'not_started'}
    if contract.code == code and code != 'UNKNOWN_ERROR':
        return {'code': code, 'message': contract.recovery_hint, 'retryable': retryable and contract.retryable,
                'details': [{'key': 'recommended_action', 'value': contract.recommended_action}]}
    return {'code': code, 'message': fallback_message or code, 'retryable': retryable,
            'details': []}


# LLM: 跨业务空间、工作流、后台助手及受管操作账本核验操作回执；仅本owner可见。
# 函数用途: 跨多个持久事实源统一检索操作回执。
def business_operation_receipt(home: Path, owner: str, store: Any, repo: Any, operation_id: str) -> dict[str, Any] | None:
    if (
        not isinstance(operation_id, str)
        or not operation_id
        or '/' in operation_id
        or '\\' in operation_id
        or operation_id in {'.', '..'}
    ):
        return None

    if repo is not None:
        try:
            with repo._runtime_connection() as conn:
                row = conn.execute(
                    "SELECT x.*, ar.run_id, t.thread_id, tr.metadata_json FROM tool_operations x "
                    "JOIN agent_runs ar ON ar.agent_run_id=x.agent_run_id "
                    "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id "
                    "JOIN tasks t ON t.task_id=tr.task_id "
                    "WHERE x.operation_id=? AND t.owner_id=?",
                    (operation_id, owner),
                ).fetchone()
                if row is not None:
                    status = row['status']
                    state = 'completed' if status == 'SUCCEEDED' else (
                        'failed' if status in ('FAILED', 'CANCELLED') else (
                            'pending' if status in ('CLAIMED', 'EXECUTING') else 'unknown'
                        )
                    )
                    outcome = {}
                    if row['outcome_json']:
                        try:
                            outcome = json.loads(row['outcome_json'])
                        except Exception:
                            outcome = {}
                    target_refs = [operation_id]
                    result_obj = outcome.get('result')
                    workflow = (result_obj.get('result_envelope') if isinstance(result_obj, dict)
                                and row['operation_type'] == 'workflows' else None)
                    error = (_observed_tool_error(outcome.get('error_code') or status,
                                                  result=result_obj, workflow=workflow) if state == 'failed' else None)
                    if isinstance(result_obj, dict) and isinstance(result_obj.get('result_ref'), str) and result_obj['result_ref']:
                        target_refs = [result_obj['result_ref']]
                    return {
                        'operation_id': operation_id,
                        'state': state,
                        'target_refs': target_refs,
                        'error': error,
                    }
        except Exception:
            pass

    try:
        raw_receipt = read_text_beneath(home, ('data', 'business', 'receipts', f"{operation_id}.json"))
        if raw_receipt is not None:
            data = json.loads(raw_receipt)
            if data.get('owner_id') == owner:
                result = data.get('receipt', {}).get('result', {})
                space_ref = result.get('space_id') or operation_id
                return {
                    'operation_id': operation_id,
                    'state': 'completed',
                    'target_refs': [str(space_ref)],
                    'error': None,
                }
    except Exception:
        pass

    try:
        raw_catalog = read_text_beneath(home, ('data', 'business', 'catalog.json'))
        if raw_catalog is not None:
            catalog = json.loads(raw_catalog)
            for space in catalog.get('spaces', []):
                if space.get('space_id') == operation_id or space.get('operation_id') == operation_id:
                    return {
                        'operation_id': operation_id,
                        'state': 'completed',
                        'target_refs': [str(space['space_id'])],
                        'error': None,
                    }
    except Exception:
        pass

    if store is not None:
        try:
            direct = store.threads.load(operation_id)
            if direct is not None and direct.owner_id == owner:
                return {
                    'operation_id': operation_id,
                    'state': 'completed',
                    'target_refs': [direct.thread_id],
                    'error': None,
                }
            threads, _ = store.threads.list_report(limit=200)
            for th in threads:
                if th.owner_id != owner:
                    continue
                receipts = th.metadata.get('background_receipts', {})
                retirement = th.metadata.get('background_retirement', {})
                if (
                    operation_id in receipts
                    or retirement.get('operation_id') == operation_id
                    or th.thread_id == operation_id
                ):
                    return {
                        'operation_id': operation_id,
                        'state': 'completed',
                        'target_refs': [th.thread_id],
                        'error': None,
                    }
        except Exception:
            pass

    try:
        raw_run = read_text_beneath(home, ('data', 'workflow_runs', operation_id, 'run.json'))
        if raw_run is not None:
            run_data = json.loads(raw_run)
            if run_data.get('owner_id') == owner:
                st = run_data.get('status', 'unknown')
                state = 'completed' if st in ('succeeded', 'skipped') else (
                    'failed' if st in ('failed', 'cancelled') else (
                        'pending' if st in ('queued', 'running', 'waiting_for_agent') else 'unknown'
                    )
                )
                err = (_observed_tool_error('WORKFLOW_FAILED', workflow=run_data,
                       fallback_message=str(run_data.get('failed_step_id') or 'WORKFLOW_FAILED')) if state == 'failed' else None)
                target_refs = [operation_id]
                bctx = run_data.get('business_context') or {}
                if isinstance(bctx, dict) and bctx.get('space_id'):
                    target_refs = [str(bctx['space_id'])]
                return {
                    'operation_id': operation_id,
                    'state': state,
                    'target_refs': target_refs,
                    'error': err,
                }
    except Exception:
        pass

    try:
        raw_fallback = read_text_beneath(home, ('data', 'workflows', 'runs', f"{operation_id}.json"))
        if raw_fallback is not None:
            run_data = json.loads(raw_fallback)
            if run_data.get('owner_id') == owner:
                st = run_data.get('status', 'unknown')
                state = 'completed' if st in ('succeeded', 'skipped') else (
                    'failed' if st in ('failed', 'cancelled') else (
                        'pending' if st in ('queued', 'running', 'waiting_for_agent') else 'unknown'
                    )
                )
                err = (_observed_tool_error('WORKFLOW_FAILED', workflow=run_data,
                       fallback_message=str(run_data.get('failed_step_id') or 'WORKFLOW_FAILED')) if state == 'failed' else None)
                target_refs = [operation_id]
                bctx = run_data.get('business_context') or {}
                if isinstance(bctx, dict) and bctx.get('space_id'):
                    target_refs = [str(bctx['space_id'])]
                return {
                    'operation_id': operation_id,
                    'state': state,
                    'target_refs': target_refs,
                    'error': err,
                }
    except Exception:
        pass

    return None


# LLM: 注册只核schema与来源存在性，不能读取历史正文或计算资源快照；坏源在实际读取时保持unknown。
# 函数用途: 廉价装配同owner默认reader，不接受请求选择owner或文件路径。
def default_observation_readers(owner_home, owner_id, store, runtime, *, resource_provider=None) -> dict:
    home = Path(owner_home).resolve()
    if store is not None and not Path(store.storage.root).resolve().is_relative_to(home):
        return {}
    repo = None
    if runtime is not None and Path(runtime.db_path).resolve().is_relative_to(home):
        repo = RuntimeRepository(runtime.db_path, read_only=True)
    source = _OwnerReaders(home, owner_id, store, repo, resource_provider=resource_provider)
    readers = {}
    resource_available = source.resource_exists()
    if source.opens_table('tool_operations'):
        readers.update({
            'tool_calls': source.tool_calls,
            'tool_calls.get': source.tool_detail,
            'logs.read': source.logs,
            'changes.get': source.changes,
            'permissions.get': source.permissions,
            'operations.get': source.operations,
        })
    media = home / 'artifacts' / 'business' / REGISTRY_RELATIVE_PATH
    if source.runtime_artifacts or media.is_file():
        readers['artifacts'] = source.artifacts
    if source.runtime_artifacts or media.is_file() or store is not None:
        readers['artifacts.get'] = source.artifact_detail
    if store is not None:
        readers['changes.get'] = source.changes
        archive = Path(store.storage.root) / 'display_archives'
        if archive.is_dir() and not archive.is_symlink():
            try:
                with os.scandir(archive):
                    pass
            except OSError:
                pass
            else:
                readers['logs.read'] = source.logs
        if resource_available and source.usage_exists():
            readers['usage'] = source.usage
            readers['usage.batch'] = source.usage_batch
    if resource_available:
        readers['resource_usage'] = source.resources
    return readers


class _OwnerReaders:
    """绑定一个owner的只读来源；游标签名不保存业务事实。"""
    # 同一进程用固定秘密派生各命名空间密钥；分页寿命不受其它 owner 的访问量影响。
    # 不保存 owner 列表，内存恒定；重启仍沿原合同使进程内游标失效。
    _cursor_secret = os.urandom(32)

    # 函数用途: 绑定来源并派生隔离的日志游标签名，不创建执行器或持久账本。
    def __init__(self, home, owner, store, repo, *, resource_provider=None):
        self.home, self.owner, self.store, self.repo = home, owner, store, repo
        self.resource_provider = resource_provider
        namespace = (str(Path(store.storage.root)) if store is not None else '', str(home), owner)
        self.key = hmac.digest(self._cursor_secret,
            json.dumps(namespace, ensure_ascii=False, separators=(',', ':')).encode(), 'sha256')
        self.runtime_artifacts = self.opens_table('artifact_records')

    # LLM: 来源不存在或无法打开时保持未就绪。
    # 函数用途: 检查规范表及owner关联链可读取。
    def opens_table(self, table):
        if self.repo is None or table not in {'tool_operations', 'artifact_records'}:
            return False
        try:
            with self.repo._runtime_connection() as conn:
                conn.execute('SELECT 1 FROM ' + table + ' x JOIN agent_runs ar ON ar.agent_run_id=x.agent_run_id '
                    'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id LIMIT 0')
            return True
        except (OSError, sqlite3.Error, ValueError):
            return False

    # LLM: business_context必须与同owner原SessionTask身份一致。
    # 函数用途: 为原运行行恢复逐记录授权事实。
    def record(self, value, row):
        metadata = json.loads(row['metadata_json'])
        raw = metadata.get('business_context')
        context = BusinessExecutionContext.from_dict(raw) if raw else None
        if context and (context.owner_id != self.owner or context.task_id != metadata.get('session_task_ref')):
            raise ValueError('observation context mismatch')
        return ObservationRecord(value, self.owner, context.space_id if context else None,
            context.project_ref if context else None, context.task_id if context else None,
            row['thread_id'], row['run_id'], row['attempt_id'])

    # LLM: 同owner且准确attempt属于同run；点详情必须在SQL中筛选，历史尝试不偷换成current。
    # 函数用途: 读取工具或产物账的严格授权join，可按operation精确点读。
    def joined(self, table, *, operation_id=None, rowids=None):
        if table not in {'tool_operations', 'artifact_records'}:
            raise ValueError('invalid source')
        condition, values = 't.owner_id=?', [self.owner]
        if operation_id is not None:
            if table != 'tool_operations':
                raise ValueError('invalid operation source')
            condition += ' AND x.operation_id=?'
            values.append(operation_id)
        if rowids is not None:
            if not rowids:
                return []
            condition += ' AND x.rowid IN (' + ','.join('?' for _ in rowids) + ')'
            values.extend(rowids)
        bound = 2 if operation_id is not None else len(rowids) if rowids is not None else None
        with self.repo._runtime_connection() as conn:
            return conn.execute('SELECT x.*,ar.run_id,t.thread_id,tr.metadata_json FROM ' + table + ' x '
                'JOIN agent_runs ar ON ar.agent_run_id=x.agent_run_id '
                'JOIN agent_attempts aa ON aa.attempt_id=x.attempt_id AND aa.agent_run_id=ar.agent_run_id '
                'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                'WHERE ' + condition + ' ORDER BY x.rowid' + (' LIMIT ?' if bound is not None else ''),
                [*values, bound] if bound is not None else values).fetchall()

    # LLM: handler开始与嵌套outcome为执行事实；点详情限制源SQL，成功状态不等于副作用已提交。
    # 函数用途: 将runtime工具账投影为工具调用视图，保留既有结算协议。
    @bounded_observation_reader
    def tool_calls(self, params, *, source_page=None):
        records = []
        request = source_page or ObservationSourcePage(limit=100)
        revision = runtime_source_revision(self.repo.db_path)
        filters = params.get('filter') or {}
        scoped_recent = bool(filters.get('run_id') or filters.get('attempt_id'))
        if scoped_recent and request.position is not None:
            position = request.position
            if (type(position) is not dict or set(position) != {'revision', 'before'}
                    or position['revision'] != revision or type(position['before']) is not int or position['before'] < 1):
                raise BusinessError('CURSOR_EXPIRED')
            after = position['before']
        else:
            after = source_after(request, revision)
        next_position = None
        if 'operation_id' in params:
            selected = self.joined('tool_operations', operation_id=params['operation_id'])
        else:
            limit = min(100, request.limit)
            scope, values = '', []
            if filters.get('run_id'):
                scope = ('agent_run_id IN (SELECT ar.agent_run_id FROM agent_runs ar '
                         'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                         'JOIN tasks t ON t.task_id=tr.task_id WHERE ar.run_id=? AND t.owner_id=?) AND ')
                values.extend((filters['run_id'], self.owner))
            if filters.get('attempt_id'):
                scope += ('attempt_id IN (SELECT aa.attempt_id FROM agent_attempts aa '
                          'JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                          'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                          'JOIN tasks t ON t.task_id=tr.task_id WHERE aa.attempt_id=? AND t.owner_id=?) AND ')
                values.extend((filters['attempt_id'], self.owner))
            with self.repo._runtime_connection() as conn:
                if scope:
                    # 选中执行时从这个run或attempt最新工具向前翻页，旧ASC游标到升级后显式过期。
                    positions = conn.execute('SELECT rowid FROM tool_operations WHERE ' + scope +
                        'rowid<? ORDER BY rowid DESC LIMIT ?', (*values, after or (1 << 63) - 1, limit + 1)).fetchall()
                else:
                    positions = conn.execute('SELECT rowid FROM tool_operations WHERE rowid>? ORDER BY rowid LIMIT ?',
                                             (after, limit + 1)).fetchall()
            if len(positions) > limit:
                next_position = {'revision': revision, 'before' if scope else 'after': positions[limit - 1][0]}
            selected = self.joined('tool_operations', rowids=[row[0] for row in positions[:limit]])
            if scope:
                selected.reverse()
        for row in selected:
            outcome = json.loads(row['outcome_json'])
            if not isinstance(outcome, dict):
                raise ValueError('invalid tool outcome')
            result = outcome.get('result', {})
            settled = bool(row['settled_at'])
            # 原收口事务给handler从未启动的领取行写的未启动取消回执无result_ref/duration_ms；
            # 只对该准确形状免这两项，并固定投影为未执行，其它行仍走下方原严格校验。
            not_started = (row['status'] == 'CANCELLED' and isinstance(result, dict)
                and result.get('schema_version') == 'tool_execution_result.v1'
                and result.get('tool') == row['operation_type']
                and result.get('effect_outcome') == 'not_started' and result.get('handler_executed') is False
                and type(result.get('ok')) is bool)
            if not_started:
                pass
            elif result:
                if (not isinstance(result, dict) or result.get('schema_version') != 'tool_execution_result.v1'
                        or result.get('tool') != row['operation_type']
                        or not isinstance(result.get('result_ref'), str) or not result['result_ref'].strip()
                        or type(result.get('handler_executed')) is not bool
                        or type(result.get('ok')) is not bool
                        or type(result.get('duration_ms')) not in (int, float)
                        or not math.isfinite(result['duration_ms']) or result['duration_ms'] < 0
                        or result.get('effect_outcome') not in ('', 'failed', 'none', 'not_started', 'committed', 'running', 'unknown')):
                    raise ValueError('invalid native tool result')
            elif settled and row['status'] in ('SUCCEEDED', 'FAILED'):
                raise ValueError('settled tool result missing')
            if not_started:
                # 回执只证明从未执行：不读它没有的结果引用/耗时，也不从ok或行状态推断已执行。
                executed, effect, duration, ref = False, 'not_started', None, row['operation_id']
            else:
                # 原生空值/failed没有副作用提交证明；冻结词表不增加状态，也不从SUCCEEDED推断committed。
                effect = result.get('effect_outcome') if settled and result else (
                    'running' if row['status'] == 'EXECUTING' else 'unknown')
                if effect not in ('none', 'not_started', 'committed', 'running', 'unknown'):
                    effect = 'unknown'
                # UNKNOWN未结算不等于没有回执：只保留已严格校验的执行旁证，不提升副作用状态。
                has_receipt = bool(result) and (settled or row['status'] == 'UNKNOWN')
                executed = result['handler_executed'] if has_receipt else bool(row['handler_started_at'])
                duration = result['duration_ms'] if has_receipt else None
                ref = result['result_ref'] if has_receipt else row['operation_id']
            error_code = outcome.get('error_code')
            workflow = (result.get('result_envelope') if isinstance(result, dict)
                        and row['operation_type'] == 'workflows' else None)
            stored_input = result.get('redacted_input') if isinstance(result, dict) else None
            if stored_input is None and isinstance(result, dict) and isinstance(result.get('result_envelope'), dict):
                stored_input = result['result_envelope'].get('redacted_input')
            redacted_input = stored_input if isinstance(stored_input, dict) else {}
            value = {'operation_id': row['operation_id'], 'run_id': row['run_id'], 'attempt_id': row['attempt_id'],
                'task_id': None, 'tool_name': row['operation_type'], 'status': row['status'],
                'executed': executed, 'effect_outcome': effect, 'duration_ms': duration,
                'error': _observed_tool_error(error_code, result=result, workflow=workflow) if error_code else None,
                'result_ref': ref, '_input': redacted_input}
            record = self.record(value, row)
            value['task_id'] = record.task_id
            records.append(record)
        if runtime_source_revision(self.repo.db_path) != revision:
            raise BusinessError('CURSOR_EXPIRED')
        return ObservationPage(tuple(records), 'partial' if next_position is not None else 'complete',
                               revision, True, next_position)

    # LLM: 详情沿同一行授权，不把不存在或跨owner操作伪装成功。
    # 函数用途: 从原工具行生成冻结详情。
    def tool_detail(self, params):
        records = []
        for row in self.tool_calls(params).records:
            if row.value['operation_id'] == params['operation_id']:
                value = {key: item for key, item in row.value.items() if not key.startswith('_')}
                records.append(self.wrap({'tool_call': value, 'redacted_input': row.value['_input'],
                    'result_ref': value['result_ref'], 'effect_outcome': value['effect_outcome']}, row))
        return ObservationPage(tuple(records))

    # LLM: 变更详情按task读取代码与媒体变更；非本owner任务拒绝披露。
    # 函数用途: 返回任务的代码或产物变更事实及证据引用。
    def changes(self, params):
        from ..conversation.session_tasks import SESSION_TASK_RESULT_ARTIFACTS_KEY
        from ..plugin_host.business_action_specs import business_action_spec, validate_object

        task_id = params['task_id']
        candidate_ref = params.get('candidate_ref')
        task_row = None
        space_id, project_ref, thread_id = None, None, None
        meta = {}
        complete = True
        if self.repo is not None and self.repo.db_path.is_file():
            with self.repo._runtime_connection() as conn:
                task_row = conn.execute(
                    "SELECT t.*, tr.metadata_json FROM tasks t "
                    "LEFT JOIN task_runs tr ON tr.task_id=t.task_id "
                    "WHERE t.task_id=? AND t.owner_id=? ORDER BY tr.rowid DESC LIMIT 1",
                    (task_id, self.owner),
                ).fetchone()
                if task_row is not None:
                    thread_id = task_row['thread_id'] or None
                    if task_row['metadata_json']:
                        meta = json.loads(task_row['metadata_json'])
        task_obj = None
        if self.store is not None:
            task_obj = _observation_task(self.store, task_id)
            if task_obj is not None:
                target_thread = _observation_thread(self.store, task_obj.target_thread_id)
                if target_thread.owner_id != self.owner:
                    return ObservationPage()
                thread_id = task_obj.target_thread_id
                meta.update(task_obj.metadata)
        if task_row is None and task_obj is None:
            return ObservationPage()

        ctx_raw = meta.get('business_context')
        context = BusinessExecutionContext.from_dict(ctx_raw) if ctx_raw else None
        if context:
            expected_task = task_obj.task_id if task_obj is not None else meta.get('session_task_ref')
            if context.owner_id != self.owner or context.task_id != expected_task:
                raise ValueError('change source business context mismatch')
            space_id = context.space_id
            project_ref = context.project_ref

        schema = business_action_spec('changes.get').result_schema
        raw_changes = meta.get('changes', [])
        changes_list = []
        if isinstance(raw_changes, list):
            complete = complete and len(raw_changes) <= 100
            for ch in raw_changes[:100]:
                try:
                    validate_object({'changes': [ch], 'evidence_refs': [], 'coverage': 'complete'}, schema)
                except (ValueError, BusinessError):
                    complete = False
                    continue
                changes_list.append(ch)
        else:
            complete = False

        evidence_refs = []
        raw_refs = meta.get('evidence_refs') or meta.get('result_refs') or []
        if isinstance(raw_refs, list):
            complete = complete and len(raw_refs) <= 100
            for ref in raw_refs[:100]:
                try:
                    validate_object({'changes': [], 'evidence_refs': [ref], 'coverage': 'complete'}, schema)
                except (ValueError, BusinessError):
                    complete = False
                    continue
                if Path(ref).is_absolute():
                    complete = False
                    continue
                evidence_refs.append(ref)
        else:
            complete = False
        if task_obj is not None:
            # 公开结果复用任务详情的登记映射，绝不截断本机路径冒充产物ID。
            refs, coverage = public_result_refs(task_obj, store=self.store, owner_root=self.home)
            evidence_refs.extend(refs)
            complete = complete and (coverage is None or coverage['state'] == 'complete')
            pins = task_obj.metadata.get(SESSION_TASK_RESULT_ARTIFACTS_KEY, [])
            digests = {}
            ref_set = set(refs)
            for item in pins if isinstance(pins, list) else ():
                if isinstance(item, dict) and item.get('artifact_id') in ref_set:
                    digest = item.get('sha256')
                    if isinstance(digest, str) and re.fullmatch(r'[0-9a-f]{64}', digest):
                        digests.setdefault(item['artifact_id'], set()).add(digest)
            for ref, values in digests.items():
                if len(values) == 1:
                    changes_list.append({'target_ref': ref, 'field': 'content_digest',
                                         'before': None, 'after': next(iter(values))})
                else:
                    complete = False
        if self.runtime_artifacts and task_row is not None:
            with self.repo._runtime_connection() as conn:
                art_rows = conn.execute(
                    "SELECT ar.artifact_record_id, ar.content_digest FROM artifact_records ar "
                    "JOIN agent_runs agr ON agr.agent_run_id=ar.agent_run_id "
                    "JOIN agent_attempts aa ON aa.attempt_id=ar.attempt_id AND aa.agent_run_id=agr.agent_run_id "
                    "JOIN task_runs tr ON tr.task_run_id=agr.task_run_id "
                    "JOIN tasks t ON t.task_id=tr.task_id "
                    "WHERE t.task_id=? AND t.owner_id=? ORDER BY ar.rowid LIMIT 101",
                    (task_id, self.owner),
                ).fetchall()
            complete = complete and len(art_rows) <= 100
            targets = {change['target_ref'] for change in changes_list}
            for row in art_rows[:100]:
                ref = row['artifact_record_id']
                evidence_refs.append(ref)
                if ref not in targets:
                    changes_list.append({'target_ref': ref, 'field': 'content_digest',
                                         'before': None, 'after': row['content_digest']})
                    targets.add(ref)
        evidence_refs = list(dict.fromkeys(evidence_refs))
        if candidate_ref and candidate_ref not in evidence_refs and not any(
                change['target_ref'] == candidate_ref for change in changes_list):
            return ObservationPage(coverage='complete' if complete else 'partial')
        complete = complete and len(changes_list) <= 100 and len(evidence_refs) <= 100

        value = {
            'changes': changes_list[:100],
            'evidence_refs': evidence_refs[:100],
            'coverage': 'complete' if complete else 'partial',
        }
        return ObservationPage((ObservationRecord(value, self.owner, space_id, project_ref, task_id, thread_id),))

    # LLM: 有效权限摘要按目标节点计算生效范围及来源；只读展示，不提升或赋予权限。
    # 函数用途: 返回目标节点的有效资源范围及来源。
    def permissions(self, params):
        target = params['target']
        task_id = params.get('task_id')
        kind = target['kind']
        target_id = target['id']
        space_ids = []
        thread_ids = []
        task_ids = [task_id] if task_id else []
        project_ref = None
        revision = 0

        found = False
        if kind == 'controller':
            found = True
            thread_ids.append(target_id)
        elif kind == 'background_main':
            if self.store is not None:
                try:
                    thread = _observation_thread(self.store, target_id)
                    if thread is not None and thread.owner_id == self.owner:
                        found = True
                        thread_ids.append(thread.thread_id)
                        identity = background_identity(thread)
                        if identity:
                            if identity.space_id:
                                space_ids.append(identity.space_id)
                            project_ref = identity.project_ref
                            revision = identity.identity_revision
                except Exception:
                    pass
        elif kind == 'subagent':
            if self.repo is not None:
                with self.repo._runtime_connection() as conn:
                    row = conn.execute(
                        "SELECT ar.run_id, t.thread_id, t.owner_id, tr.metadata_json FROM agent_runs ar "
                        "JOIN task_runs tr ON tr.task_run_id=ar.task_run_id "
                        "JOIN tasks t ON t.task_id=tr.task_id WHERE ar.run_id=? AND t.owner_id=?",
                        (target_id, self.owner),
                    ).fetchone()
                    if row is not None:
                        found = True
                        if row['thread_id']:
                            thread_ids.append(row['thread_id'])
                        try:
                            meta = json.loads(row['metadata_json'])
                            ctx = BusinessExecutionContext.from_dict(meta.get('business_context'))
                            if ctx:
                                if ctx.space_id:
                                    space_ids.append(ctx.space_id)
                                project_ref = ctx.project_ref
                                if ctx.task_id and ctx.task_id not in task_ids:
                                    task_ids.append(ctx.task_id)
                        except Exception:
                            pass
        if not found and self.store is not None:
            try:
                thread = _observation_thread(self.store, target_id)
                if thread is not None and thread.owner_id == self.owner:
                    found = True
                    thread_ids.append(thread.thread_id)
            except Exception:
                pass
        if not found:
            return ObservationPage()

        effective_scope = {
            'owner_id': self.owner,
            'space_ids': space_ids,
            'project_ref': project_ref,
            'task_ids': task_ids,
            'thread_ids': thread_ids,
            'permission_revision': revision,
        }
        sources = [{
            'kind': kind,
            'source_ref': target_id,
            'revision': revision if revision > 0 else None,
        }]
        value = {
            'effective_scope': effective_scope,
            'unavailable': [],
            'sources': sources,
        }
        rec = ObservationRecord(value, self.owner, space_ids[0] if space_ids else None, project_ref,
            task_ids[0] if task_ids else None, thread_ids[0] if thread_ids else None)
        return ObservationPage((rec,))

    # LLM: 稳定操作回执按多来源检索；跨owner操作拒绝披露。
    # 函数用途: 返回操作的耐久执行回执与终态。
    def operations(self, params):
        operation_id = params['operation_id']
        receipt = business_operation_receipt(self.home, self.owner, self.store, self.repo, operation_id)
        if receipt is None:
            return ObservationPage()
        rec = ObservationRecord(receipt, self.owner)
        return ObservationPage((rec,))

    # LLM: 详情包装不能丢弃授权事实。
    # 函数用途: 复用原记录的身份生成详情行。
    def wrap(self, value, row):
        return ObservationRecord(value, row.owner_id, row.space_id, row.project_ref, row.task_id,
            row.thread_id, row.run_id, row.attempt_id, row.source_cursor)

    # LLM: runtime产物引用内容寻址快照；媒体账沿原nofollow及解析器，不读取live workspace。
    # 函数用途: 合并规范产物来源，保持各自版本与owner身份。
    @bounded_observation_reader
    def artifacts(self, params, *, source_page=None):
        from ..artifacts import registry_locator
        from ..conversation.registered_artifact_observation import (
            discover_registered,
            registered_artifact,
        )

        request = source_page or ObservationSourcePage(limit=params.get('limit', 50))
        position = request.position or {'phase': 'registered', 'after': ''}
        phase, after = position.get('phase'), position.get('after')
        if phase not in {'discovery', 'registered', 'runtime', 'media'} or not isinstance(after, (str, int)):
            raise BusinessError('CURSOR_EXPIRED')
        records, next_position = [], None
        revision = registry_locator.revision(self.home)
        if position.get('revision') is not None and position['revision'] != revision:
            raise BusinessError('CURSOR_EXPIRED')
        if phase in {'discovery', 'registered'}:
            if phase == 'discovery' or request.position is None:
                discovery = discover_registered(self)
                if discovery['coverage'] == 'partial':
                    return ObservationPage((), 'partial', source_paged=True,
                        next_position={'phase': 'discovery', 'after': discovery.get('after', 0)})
                revision = registry_locator.revision(self.home)
                after = ''
            rows, next_after = registry_locator.page(self.home, after=str(after),
                task_id=params.get('filter', {}).get('task_id'), limit=request.limit)
            incomplete = False
            for artifact_id, _, _, _ in rows:
                try:
                    found = registered_artifact(self, artifact_id, discover=False, with_content=False)
                except ObservationSourceError:
                    incomplete = True
                    continue
                if found is not None:
                    records.append(found[0])
            if rows or next_after:
                next_position = ({'phase': 'registered', 'after': next_after} if next_after
                    else {'phase': 'runtime', 'after': 0})
                return ObservationPage(tuple(records), 'unknown' if incomplete else 'partial', source_paged=True,
                    next_position={**next_position, 'revision': revision})
            phase, after = 'runtime', 0
        if phase == 'runtime':
            if self.runtime_artifacts:
                with self.repo._runtime_connection() as db:
                    ids = [row[0] for row in db.execute('SELECT rowid FROM artifact_records WHERE rowid>? ORDER BY rowid LIMIT ?',
                        (int(after), request.limit + 1)).fetchall()]
                for row in self.joined('artifact_records', rowids=ids[:request.limit]):
                    value = {'artifact_id': row['artifact_record_id'], 'version_ref': row['content_digest'],
                        'name': Path(row['rel_path']).name, 'mime_type': 'application/octet-stream',
                        'size_bytes': row['size'], 'content_sha256': row['content_digest'],
                        'task_id': None, 'space_id': None, 'available': False}
                    record = self.record(value, row)
                    value.update(task_id=record.task_id, space_id=record.space_id)
                    records.append(record)
                if records or len(ids) > request.limit:
                    next_position = ({'phase': 'runtime', 'after': ids[request.limit - 1]} if len(ids) > request.limit
                        else {'phase': 'media', 'after': ''})
                    return ObservationPage(tuple(records), 'partial', source_paged=True, next_position={**next_position, 'revision': revision})
            phase, after = 'media', ''
        from ..conversation.task_artifact_observation import _read_source

        parts = ('artifacts', 'business', *REGISTRY_RELATIVE_PATH.parts)
        raw = _read_source(self.home, parts, 2 * 1024 * 1024, 'ARTIFACT_REGISTRY_UNREADABLE')
        if raw is not None:
            if len(raw) > 2 * 1024 * 1024:
                raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_MEDIA_REGISTRY_READ_BUDGET',
                    'side_effect_state': 'none', 'next_action': '原媒体登记簿超过有界读取预算，不能把截断内容当完整列表。'})
            media, errors = latest_records_from_lines(self.home.joinpath(*parts), raw.decode('utf-8').splitlines(), _record_from_payload)
            if errors:
                raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_MEDIA_REGISTRY_UNREADABLE',
                    'side_effect_state': 'none', 'next_action': '原媒体登记簿无法解析，修复来源后重试。'})
            selected = sorted((item for item in media.values() if item.artifact_id > str(after)
                and item.metadata.get('owner_id') == self.owner and item.source == 'business_upload'), key=lambda item: item.artifact_id)
            for item in selected[:request.limit]:
                meta = item.metadata
                value = {'artifact_id': item.artifact_id, 'version_ref': meta['version_ref'], 'name': meta['display_name'],
                    'mime_type': item.mime_type, 'size_bytes': item.size_bytes, 'content_sha256': item.sha256,
                    'task_id': item.task_id or None, 'space_id': meta['space_id'], 'available': item.status == 'ready'}
                records.append(ObservationRecord(value, self.owner, meta['space_id'], task_id=item.task_id or None))
            if len(selected) > request.limit:
                next_position = {'phase': 'media', 'after': selected[request.limit - 1].artifact_id}
        # 原run发现是渐进的；没有覆盖证明不能把旧registry的漏项宣称为完整空集。
        return ObservationPage(tuple(records), 'unknown', source_paged=True,
            next_position={**next_position, 'revision': revision} if next_position else None)

    # LLM: 指定旧版本不回退latest，无安全内容运输口的runtime快照不暴露路径。
    # 函数用途: 返回精确产物版本和原业务媒体读取引用。
    def artifact_detail(self, params):
        from ..conversation.registered_artifact_observation import (
            discover_registered,
            registered_artifact,
        )
        from ..conversation.task_artifact_observation import _error, _read_source, task_artifact

        registered = registered_artifact(self, params['artifact_id'], params.get('version_ref'), discover=False)
        if registered is not None:
            facts, _, _ = registered
            artifact = facts.value
            return ObservationPage((self.wrap({'artifact': artifact, 'version_ref': artifact['version_ref'],
                'read_ref': artifact['artifact_id'], 'available': artifact['available']}, facts),))
        try:
            found, artifact, task, context = task_artifact(self.store, self.home, self.owner,
                params['artifact_id'], params.get('version_ref'))
        except BusinessError as error:
            if error.details.get('reason') not in {'ARTIFACT_TASK_INDEX_INCOMPLETE', 'ARTIFACT_TASK_INDEX_UNAVAILABLE'}:
                raise
            found = False
        if found:
            if artifact is None:
                return ObservationPage()
            value = {'artifact': artifact, 'version_ref': artifact['version_ref'],
                     'read_ref': artifact['artifact_id'], 'available': artifact['available']}
            return ObservationPage((ObservationRecord(value, self.owner,
                context.space_id if context else None, context.project_ref if context else None,
                task.task_id, task.target_thread_id),))
        # 点详情不调用全量artifacts列表：旧runtime来源也只按准确ID取一页。
        records = []
        selected = []
        if self.runtime_artifacts:
            with self.repo._runtime_connection() as conn:
                ids = [row[0] for row in conn.execute(
                    'SELECT rowid FROM artifact_records WHERE artifact_record_id=? LIMIT 2',
                    (params['artifact_id'],)).fetchall()]
            for row in self.joined('artifact_records', rowids=ids):
                value = {'artifact_id': row['artifact_record_id'], 'version_ref': row['content_digest'],
                    'name': Path(row['rel_path']).name, 'mime_type': 'application/octet-stream',
                    'size_bytes': row['size'], 'content_sha256': row['content_digest'],
                    'task_id': None, 'space_id': None, 'available': False}
                record = self.record(value, row)
                value.update(task_id=record.task_id, space_id=record.space_id)
                selected.append(record)
        if selected:
            # 精确runtime登记已命中；无关媒体目录缺失不能抹掉原版本元数据。
            return ObservationPage(tuple(self.wrap({'artifact': row.value, 'version_ref': row.value['version_ref'],
                'read_ref': row.value['artifact_id'], 'available': row.value['available']}, row)
                for row in selected if params.get('version_ref', row.value['version_ref']) == row.value['version_ref']))
        parts = ('artifacts', 'business', *REGISTRY_RELATIVE_PATH.parts)
        raw = _read_source(self.home, parts, 2 * 1024 * 1024, 'ARTIFACT_REGISTRY_UNREADABLE')
        if raw is not None:
            from ..conversation.business_observability import ObservationSourceError
            if len(raw) > 2 * 1024 * 1024:
                raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_REGISTRY_READ_BUDGET',
                    'next_action': '原媒体登记簿超过点查询只读预算，不能把截断记录当最新版本。'})
            try:
                media, errors = latest_records_from_lines(self.home.joinpath(*parts), raw.decode('utf-8').splitlines(), _record_from_payload)
            except (OSError, ValueError, TypeError) as error:
                raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_REGISTRY_UNREADABLE',
                    'next_action': '原媒体登记簿无法解析，修复后重试。'}) from error
            if errors:
                raise ObservationSourceError('HOST_NOT_READY', details={'reason': 'ARTIFACT_REGISTRY_UNREADABLE',
                    'next_action': '原媒体登记簿有坏记录，修复后重试。'})
            item = media.get(params['artifact_id'])
            if item is not None and item.metadata.get('owner_id') == self.owner and item.source == 'business_upload':
                meta = item.metadata
                value = {'artifact_id': item.artifact_id, 'version_ref': meta['version_ref'], 'name': meta['display_name'],
                    'mime_type': item.mime_type, 'size_bytes': item.size_bytes, 'content_sha256': item.sha256,
                    'task_id': item.task_id or None, 'space_id': meta['space_id'], 'available': item.status == 'ready'}
                selected.append(ObservationRecord(value, self.owner, meta['space_id'], task_id=item.task_id or None))
        for row in selected:
            value = row.value
            if params.get('version_ref', value['version_ref']) == value['version_ref']:
                records.append(self.wrap({'artifact': value, 'version_ref': value['version_ref'],
                    'read_ref': value['artifact_id'], 'available': value['available']}, row))
        if records or selected:
            return ObservationPage(tuple(records))
        discovery = discover_registered(self)
        registered = registered_artifact(self, params['artifact_id'], params.get('version_ref'), discover=False)
        if registered is not None:
            facts, _, _ = registered
            artifact = facts.value
            return ObservationPage((self.wrap({'artifact': artifact, 'version_ref': artifact['version_ref'],
                'read_ref': artifact['artifact_id'], 'available': artifact['available']}, facts),))
        if discovery['coverage'] == 'complete':
            return ObservationPage()
        raise _error('HOST_NOT_READY', 'ARTIFACT_REGISTRY_DISCOVERY_INCOMPLETE',
            '未在当前已核验登记定位中找到该ID；旧run定位正沿有界页自动发现，不能据此判定不存在。')

    # LLM: 游标绑定owner、原归档、页、字节位置及期限，不改变原分页身份。
    # 函数用途: 生成日志分页位置的不透明签名。
    def log_cursor(self, source, page, offset):
        raw = json.dumps([self.owner, source, page, offset, time.time() + 3600], separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw + hmac.digest(self.key, raw, 'sha256')).decode().rstrip('=')

    # LLM: 签名错误或换来源游标不重置为第一页。
    # 函数用途: 核对日志分页位置。
    def log_position(self, source, cursor):
        if cursor is None:
            return 0, 0
        try:
            data = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            raw, signature = data[:-32], data[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self.key, raw, 'sha256')):
                raise ValueError('signature')
            owner, ref, page, offset, expires = json.loads(raw)
            if (owner != self.owner or ref != source or expires <= time.time()
                    or type(page) is not int or type(offset) is not int or min(page, offset) < 0):
                raise ValueError('scope')
            return page, offset
        except (ValueError, TypeError, UnicodeError):
            raise BusinessError('CURSOR_EXPIRED') from None

    # LLM: manifest先取原thread身份，再用原reader读不可变页，不扫描日志、不接受宿主路径。
    # 函数用途: 按原archive_id返回有界且脱敏的日志页。
    def logs(self, params):
        source = params['source_ref']
        if source.startswith('tool-operation://'):
            return self.operation_logs(params)
        if not re.fullmatch('[0-9a-f]{32}', source):
            return ObservationPage()
        root = Path(self.store.storage.root) / 'display_archives'
        if root.is_symlink():
            raise ValueError('archive root changed')
        reference = _read_manifest(root / source)
        if reference['archive_id'] != source:
            raise ValueError('archive identity')
        thread = self.store.threads.require(reference['thread_id'])
        if thread.owner_id != self.owner:
            return ObservationPage()
        identity = background_identity(thread)
        facts = ObservationRecord({}, self.owner, identity.space_id if identity else None,
            identity.project_ref if identity else None, thread_id=thread.thread_id)
        page, offset = self.log_position(source, params.get('cursor'))
        ref = {key: reference[key] for key in ('schema', 'archive_id', 'thread_id', 'page_count', 'row_count', 'char_count')}
        try:
            report = read_display_archive_page(self.store, ref, page)
        except DisplayArchiveError as error:
            if error.code != 'DISPLAY_ARCHIVE_EXPIRED':
                raise
            return ObservationPage((self.wrap({'chunks': [], 'next_cursor': None, 'coverage': 'expired',
                'truncated': False, 'gap': {'reason_code': 'RETENTION', 'from_cursor': params.get('cursor'),
                    'to_cursor': None, 'reset_required': True}}, facts),), coverage='expired')
        text = redact_sensitive_value('\n'.join(row['text'] for row in report['rows']))
        raw = text.encode('utf-8')
        if offset > len(raw):
            raise BusinessError('CURSOR_EXPIRED')
        end = min(len(raw), offset + params.get('max_bytes', 262144))
        selected = raw[offset:end].decode('utf-8', errors='ignore')
        end = offset + len(selected.encode('utf-8'))
        if end == offset and offset < len(raw):
            raise ValueError('max_bytes splits a character')
        next_page, next_offset = (page, end) if end < len(raw) else (page + 1, 0)
        cursor = self.log_cursor(source, next_page, next_offset) if end < len(raw) or report['has_next'] else None
        start_ref, end_ref = self.log_cursor(source, page, offset), self.log_cursor(source, page, end)
        value = {'chunks': [{'stream': 'host', 'text': selected, 'start_cursor': start_ref,
            'end_cursor': end_ref, 'byte_count': len(selected.encode('utf-8'))}] if selected else [],
            'next_cursor': cursor, 'coverage': 'partial' if cursor else 'complete', 'truncated': False, 'gap': None}
        return ObservationPage((self.wrap(value, facts),))

    def operation_logs(self, params):
        """由原工具回执读取独立输出流，沿相同操作归属和权限返回分页。"""
        source = params['source_ref']
        operation_id = source.rsplit('/', 1)[-1]
        records = self.tool_calls({'operation_id': operation_id}).records
        if not records or records[0].value['result_ref'] != source:
            return ObservationPage()
        facts = records[0]
        rows = self.joined('tool_operations', operation_id=operation_id)
        if not rows:
            return ObservationPage()
        result = json.loads(rows[0]['outcome_json']).get('result', {})
        if result.get('result_ref') != source:
            raise BusinessError('CURSOR_EXPIRED')
        envelope = result.get('result_envelope') or {}
        display = envelope.get('display') or {}
        if not isinstance(display, dict) or display.get('kind') != 'command':
            return ObservationPage((self.wrap({'chunks': [], 'next_cursor': None,
                'coverage': 'unknown', 'truncated': False,
                'gap': {'reason_code': 'STRUCTURED_COMMAND_LOG_UNAVAILABLE',
                    'from_cursor': params.get('cursor'), 'to_cursor': None,
                    'reset_required': False}}, facts),))
        header = {'operation_id': operation_id, 'return_code': display.get('return_code'),
            'redacted_input': facts.value['_input']}
        streams = [('host', json.dumps(header, ensure_ascii=False, sort_keys=True))]
        complete = display.get('capture_complete') is True
        truncated = False
        for name in ('stdout', 'stderr'):
            text = display.get(name)
            if isinstance(text, str):
                streams.append((name, text))
            else:
                complete = False
            truncated = truncated or display.get(name + '_truncated') is True
        streams = [(name, redact_sensitive_value(text).encode('utf-8')) for name, text in streams]
        digest = hashlib.sha256(b''.join(name.encode() + b'\0' + text for name, text in streams)).hexdigest()
        cursor_source = source + '#' + digest
        page, offset = self.log_position(cursor_source, params.get('cursor'))
        if page >= len(streams) or offset > len(streams[page][1]):
            raise BusinessError('CURSOR_EXPIRED')
        chunks, remaining = [], params.get('max_bytes', 262144)
        while page < len(streams) and remaining:
            name, raw = streams[page]
            selected = raw[offset:offset + remaining].decode('utf-8', errors='ignore')
            length = len(selected.encode('utf-8'))
            if not length and offset < len(raw):
                if chunks:
                    break
                raise BusinessError('INVALID_SCHEMA', details={
                    'reason': 'LOG_BYTE_LIMIT_SPLITS_UTF8', 'minimum_max_bytes': 4,
                    'next_action': '将 max_bytes 提高至至少 4 后重试。'})
            if length:
                chunks.append({'stream': name, 'text': selected, 'byte_count': length,
                    'start_cursor': self.log_cursor(cursor_source, page, offset),
                    'end_cursor': self.log_cursor(cursor_source, page, offset + length)})
            offset += length
            remaining -= length
            if offset == len(raw):
                page, offset = page + 1, 0
        cursor = self.log_cursor(cursor_source, page, offset) if page < len(streams) else None
        gap = None if complete and not truncated else {
            'reason_code': 'COMMAND_CAPTURE_TRUNCATED' if truncated else 'COMMAND_CAPTURE_INCOMPLETE',
            'from_cursor': None, 'to_cursor': None, 'reset_required': False}
        value = {'chunks': chunks, 'next_cursor': cursor,
            'coverage': 'complete' if cursor is None and gap is None else 'partial',
            'truncated': truncated, 'gap': gap}
        return ObservationPage((self.wrap(value, facts),))

    # LLM: 线程列表和distinct身份按请求复用，不从过滤字段捏造空间归属或缓存授权。
    # 函数用途: 返回与请求结构化过滤相符的规范线程。
    def selected_threads(self, params, *, since=0, until=float('inf')):
        filters = params.get('filter', {})
        if any(filters.get(key) is not None for key in ('attempt_id', 'workflow_run_id')):
            raise ValueError('usage scope unavailable')
        identifier = filters.get('agent_id')
        if identifier is None and filters.get('task_id'):
            task = _observation_task(self.store, filters['task_id'])
            if task is None:
                raise ValueError('usage task unavailable')
            sender = _observation_thread(self.store, task.sender_thread_id)
            if sender.owner_id != self.owner:
                raise ValueError('usage task owner mismatch')
            identifier = task.target_thread_id
        if identifier is None and filters.get('run_id'):
            with self.repo._runtime_connection() as conn:
                rows = conn.execute('SELECT t.thread_id FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                                    'JOIN tasks t ON t.task_id=tr.task_id WHERE ar.run_id=? AND t.owner_id=? LIMIT 2',
                                    (filters['run_id'], self.owner)).fetchall()
            if len(rows) != 1:
                raise ValueError('usage run source unavailable')
            identifier = rows[0][0]
        if identifier is None:
            return self.store.model_usage.window_source_plan(since=since, until=until)
        thread = _observation_thread(self.store, identifier)
        if thread.owner_id != self.owner:
            raise ValueError('usage thread owner mismatch')
        return (thread,)

    # LLM: 用量事件task_id回到canonical任务和两端owner；每个ID本请求只读一次，不缓存授权。
    # 函数用途: 为原用量事件恢复准确授权事实，不把同线程不同task混算。
    def usage_event_record(self, thread, event):
        identity = background_identity(thread)
        context, task = None, None
        if event.task_id:
            task = _observation_task(self.store, event.task_id)
            if task is not None:
                sender = _observation_thread(self.store, task.sender_thread_id)
                target = _observation_thread(self.store, task.target_thread_id)
                if (sender.owner_id != self.owner or target.owner_id != self.owner
                        or task.target_thread_id != thread.thread_id):
                    raise ValueError('usage task identity mismatch')
                raw = task.metadata.get('business_context')
                context = BusinessExecutionContext.from_dict(raw) if raw else None
                if context and (context.owner_id != self.owner or context.task_id != task.task_id):
                    raise ValueError('usage business identity mismatch')
        return ObservationRecord({'occurred_at': _stamp(event.created_at)}, self.owner,
            context.space_id if context else identity.space_id if identity else None,
            context.project_ref if context else identity.project_ref if identity else None,
            task.task_id if task else None, thread.thread_id, event.run_id or None)

    # LLM: 先完成时间/身份范围与异常检查，再交原decision算法；原事件与成员事实一一对应。
    # 函数用途: 筛选原增量账本，并收集可逐条复验的成员事实。
    def usage_window(self, params):
        filters = params.get('filter', {})
        since = datetime.fromisoformat(filters['since'].replace('Z', '+00:00')).timestamp() if filters.get('since') else 0
        until = datetime.fromisoformat(filters['until'].replace('Z', '+00:00')).timestamp() if filters.get('until') else float('inf')
        if until < since:
            raise ValueError('usage window reversed')
        threads = self.selected_threads(params, since=since, until=until)
        revisions = {}
        if not isinstance(threads, UsageSourcePlan):
            for thread in threads:
                revision = self.store.model_usage.revision(thread.thread_id)
                if revision:
                    revisions[thread.thread_id] = revision
            if not revisions:
                raise ValueError('usage source missing')
        window = _StreamDecisionWindow(self, threads, revisions, params, since, until)
        revision = threads.generation if isinstance(threads, UsageSourcePlan) else _digest(sorted(revisions.items()))
        return window, window.members, revision

    # LLM: 探测不解析事件或列出线程；存在不等于可读，坏账在查询时保持unknown。
    # 函数用途: 用原用量目录中的文件存在性廉价检查来源。
    def usage_exists(self):
        try:
            source_index = self.store.model_usage._source_index()
            if source_index.path.is_file() and not source_index.path.is_symlink():
                return True
            with os.scandir(self.store.model_usage._directory) as entries:
                for _ in range(128):
                    entry = next(entries, None)
                    if entry is None:
                        return False
                    if ((entry.name.endswith('.jsonl') and entry.is_file(follow_symlinks=False))
                            or (entry.name == 'archive' and entry.is_dir(follow_symlinks=False))):
                        return True
                # 大目录尚未核完也只装只读端口；真实查询仍须完整来源索引，绝不伪造空账或0费用。
                return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return False

    # LLM: 注册只核公开活动源的小型身份元数据，不扫描任务、线程或运行历史；坏源不能冒充已接线。
    # 函数用途: 廉价验证同物理owner活动源，完整资源快照仍延迟到需要它的请求。
    def resource_exists(self):
        if self.store is None or self.repo is None:
            return False
        return self.model_activity() is not None

    # LLM: 失败的None也在当前请求复用；缓存只存读数据，不存任何授权裁决。
    # 函数用途: 惰性读取本请求唯一的owner资源快照。
    def current_resources(self):
        return _observation_cached((self, 'resources'), self.resource_snapshot)

    # LLM: 只校验原公开活动源的owner/home/PID/整数合同，不读取历史或退回不可信全局值。
    # 函数用途: 取得轻量物理模型活动事实，供注册身份核验和真实资源请求共用。
    def model_activity(self):
        from collections.abc import Mapping

        from ..observability import concurrency_metrics

        if self.store is None or self.repo is None:
            return None
        provider = self.resource_provider
        if provider is None:
            provider = concurrency_metrics.owner_model_activity_provider(self.home, self.owner)
        if provider is not None:
            try:
                snapshot = provider() if callable(provider) else None
                if not isinstance(snapshot, Mapping):
                    return None
                source_home = Path(snapshot.get('owner_home'))
                value = snapshot.get('active_model_calls')
                if (snapshot.get('owner_id') != self.owner or not source_home.is_absolute()
                        or source_home.resolve() != self.home
                        or type(snapshot.get('process_id')) is not int
                        or snapshot['process_id'] != os.getpid()
                        or type(value) is not int or value < 0):
                    return None
            except Exception:
                return None
        else:
            # 旧宿主/测试的已采样owner标签保留；无标签的全局总量永远不是本owner事实。
            metrics = concurrency_metrics._METRICS
            if metrics is None:
                return None
            meter = metrics.llm_inflight
            with meter._lock:
                value = meter._vals.get((('owner_id', self.owner),))
            if type(value) is float:
                if not value.is_integer() or value < 0:
                    return None
            elif type(value) is not int or value < 0:
                return None
        return int(value)

    # LLM: 公共活动事实在实际读取时重新核验；任务和distinct线程本请求只读一次。
    # 函数用途: 获取真实owner模型并发及规范任务/子运行计数，不把未知冒充空闲0。
    def resource_snapshot(self):
        value = self.model_activity()
        if value is None:
            return None
        # 当前资源只读取规范任务及其准确端点；历史用量来源索引不提供资源身份或授权。
        tasks, errors = _observation_cached((self.store.session_tasks, 'list', None),
            lambda: self.store.session_tasks.list_report(limit=0))
        if errors:
            return None
        queued = 0
        for task in tasks:
            _observation_cached((self.store.session_tasks, 'record', task.task_id), lambda: task)
            target = _observation_thread(self.store, task.target_thread_id)
            sender = _observation_thread(self.store, task.sender_thread_id)
            if target.owner_id == self.owner and sender.owner_id == self.owner and task.status == 'queued':
                queued += 1
        try:
            with self.repo._runtime_connection() as conn:
                active = conn.execute('SELECT count(*) FROM agent_runs ar JOIN task_runs tr ON tr.task_run_id=ar.task_run_id '
                    'JOIN tasks t ON t.task_id=tr.task_id WHERE t.owner_id=? AND ar.parent_agent_run_id IS NOT NULL '
                    "AND ar.parent_agent_run_id!='' AND ar.status IN ('running','queued','created')", (self.owner,)).fetchone()[0]
        except (OSError, sqlite3.Error, ValueError):
            return None
        return {'active_model_calls': int(value), 'queued_tasks': queued, 'active_subagents': active,
            'rss_bytes': None, 'source': 'owner_concurrency_metrics+canonical_runtime'}

    # LLM: 缺真实资源计量则失败关闭，禁止默认0；全owner计数不能冒充过滤子集。
    # 函数用途: 提供原_usage降级路径需要的同请求资源行。
    @observation_request()
    def resources(self, params):
        value = self.current_resources()
        if value is None or params.get('filter'):
            raise ValueError('owner resource meter unavailable')
        return ObservationPage((ObservationRecord(value, self.owner),))

    # LLM: decision_audit只有已报输入和未结束估算输入，其余字段null，决策子集不宣称完整。
    # 函数用途: 惰性读取原决策用量与同请求资源快照并明确未知范围。
    @observation_request()
    def usage(self, params):
        resources = self.current_resources()
        if resources is None:
            raise ValueError('resource source missing')
        window, members, revision = self.usage_window(params)
        return self.usage_page(params, window, members, revision, resources)

    # LLM: 空组只能由同owner的规范任务、Agent或catalog证明范围；不以filter自证、不缓存授权。
    # 函数用途: 为合法无事件范围取准确身份，缺源、跨owner及未知范围仍失败关闭。
    def empty_usage_scope(self, filters):
        if filters.get('task_id'):
            task = _observation_task(self.store, filters['task_id'])
            if task is None:
                raise ValueError('usage task unavailable')
            target = _observation_thread(self.store, task.target_thread_id)
            sender = _observation_thread(self.store, task.sender_thread_id)
            if target.owner_id != self.owner or sender.owner_id != self.owner:
                raise ValueError('usage task owner mismatch')
            raw = task.metadata.get('business_context')
            context = BusinessExecutionContext.from_dict(raw) if raw else None
            if context and (context.owner_id != self.owner or context.task_id != task.task_id):
                raise ValueError('usage task context mismatch')
            return {'task_id': task.task_id, 'thread_id': target.thread_id,
                'space_id': context.space_id if context else None, 'project_ref': context.project_ref if context else None}
        if filters.get('agent_id'):
            thread = _observation_thread(self.store, filters['agent_id'])
            identity = background_identity(thread)
            if thread.owner_id != self.owner or identity is None:
                raise ValueError('usage agent identity unavailable')
            return {'thread_id': thread.thread_id, 'space_id': identity.space_id, 'project_ref': identity.project_ref}
        if filters.get('space_id'):
            from ..conversation.business_spaces import BusinessSpaceService

            state = _observation_cached((self, 'catalog'),
                lambda: BusinessSpaceService(self.home, owner_id=self.owner, enabled=True)._read())
            space = state['spaces'].get(filters['space_id'])
            if space is None or space['owner_id'] != self.owner:
                raise ValueError('usage space unavailable')
            return {'space_id': space['space_id']}
        if any(filters.get(key) is not None for key in ('project_ref', 'run_id', 'attempt_id')):
            raise ValueError('empty usage scope not witnessed')
        return {}

    # LLM: 与公共usage共用算法；空组取规范身份后仍走逐次与返回前授权，不重读用量账本。
    # 函数用途: 将已筛选窗口投影为带准确范围证明的原Usage结果，空计量保持null。
    def usage_page(self, params, window, members, revision, resources):
        filters = params.get('filter', {})
        # 时间窗已按原created_at筛完。since=0避免原mtime快路径误跳过带未来事件的合法账本。
        counted = _ModelUsageWindow(window)
        report = decision_usage_summary(counted, window.events, since=0)
        if report['unreadable_threads']:
            raise ValueError('usage ledger unreadable')
        actual, estimated = _model_usage_measures(counted.total, report)
        common = dict(window.common) if isinstance(window, _StreamDecisionWindow) else {}
        if not isinstance(window, _StreamDecisionWindow):
            for key in ('space_id', 'project_ref', 'task_id', 'thread_id', 'run_id', 'attempt_id'):
                values = {getattr(member, key) for member in members}
                common[key] = next(iter(values)) if len(values) == 1 else None
        # 空窗口不伪造发生时间或0 token；准确身份来自规范源，不能直接复制filter。
        if not members:
            common.update(self.empty_usage_scope(filters))
        unknown = ['cost_unavailable', 'resource_usage_owner_current']
        if not counted.has_other_purpose:
            unknown.append('decision_usage_only' if counted.has_decision_purpose else 'call_purpose_unavailable')
        if actual['output_tokens'] is None:
            unknown.append('output_tokens_unavailable')
        if actual['cached_input_tokens'] is None or actual['uncached_input_tokens'] is None:
            unknown.append('cache_breakdown_unavailable')
        value = {'actual': actual, 'estimated': estimated, 'unknown': unknown,
                 'resource_usage': resources}
        record = _ScopedUsageRecord(value, self.owner, **common, members=members, filter_digest=_digest(filters))
        return ObservationPage((record,), coverage='partial', revision=revision)

    # LLM: 此端口只由同进程观察服务调用，不注册catalog；仅返回本次原事件和成员证明。
    # 函数用途: 一次读取窗口，按task、Agent、space索引在内存投影多个准确范围。
    @observation_request()
    def usage_batch(self, params, requests):
        resources = self.current_resources()
        if resources is None:
            raise ValueError('resource source missing')
        window, members, revision = self.usage_window(params)
        event_map, source_revisions, gathered_members = {}, {}, []
        # batch本就保留所选窗口；一次canonical消费同时收事件和授权事实，避免members再读来源。
        with closing(iter(window.events)) as identifiers:
            for identifier in identifiers:
                source_revisions[identifier] = window.revision(identifier)
                with closing(window.pairs(identifier)) as pairs:
                    selected_events = []
                    for event, facts in pairs:
                        selected_events.append(event)
                        gathered_members.append(facts)
                        window.capture(facts)
                    event_map[identifier] = selected_events
        if any(window.original.revision(identifier) != expected for identifier, expected in source_revisions.items()):
            raise ValueError('usage source generation changed')
        events = [event for rows in event_map.values() for event in rows]
        members = tuple(gathered_members)
        if len(events) != len(members):
            raise ValueError('usage member alignment mismatch')
        indexes = {key: {} for key in ('task_id', 'agent_id', 'space_id')}
        for index, member in enumerate(members):
            for key, groups in indexes.items():
                value = member.thread_id if key == 'agent_id' else getattr(member, key)
                groups.setdefault(value, set()).add(index)
        pages = []
        base = params.get('filter', {})
        for request in requests:
            selected = None
            for key, groups in indexes.items():
                value = request.get('filter', {}).get(key)
                if value is not None and value != base.get(key):
                    positions = groups.get(value, set())
                    selected = positions if selected is None else selected.intersection(positions)
            positions = range(len(members)) if selected is None else sorted(selected)
            chosen_events, chosen_members = {}, []
            for index in positions:
                event, member = events[index], members[index]
                chosen_events.setdefault(event.thread_id, []).append(event)
                chosen_members.append(member)
            chosen = _DecisionWindow(window.original, chosen_events, source_revisions)
            pages.append(self.usage_page(request, chosen, tuple(chosen_members), revision, resources))
        return tuple(pages), event_map


# LLM: 四参数默认工厂没有真实前台执行句柄；组合根持有真实Agent时才显式装配审批。
# 函数用途: 绑定原生审批读口，不签票、不决定、不创建替代Agent。
def native_approval_readers(agent, paths, owner_id, store) -> dict:
    if agent is None or store is None or paths is None or not paths.processing.is_dir():
        return {}
    source = _NativeApprovals(agent, paths, owner_id, store)
    try:
        with os.scandir(paths.processing):
            pass
    except OSError:
        return {}
    return {'approvals': source.list, 'approvals.get': source.detail}


class _NativeApprovals:
    """原生待决审批只读投影，决定仍只走专用人工通道。"""

    # LLM: 保存真实宿主，不构造运行时替身或新的审批账。
    # 函数用途: 绑定规范Gateway与owner Store。
    def __init__(self, agent, paths, owner, store):
        self.agent, self.paths, self.owner, self.store = agent, paths, owner, store

    # LLM: 列表重用原生pending资格/版本，逐次重读，不缓存审批授权。
    # 函数用途: 合并前台及主/子任务原生待决审批。
    def list(self, params):
        from ..contracts.tool_approval import approval_deadline
        from ..conversation.agent_tool_approval import list_pending_agent_tool_approvals
        from ..plugin_host.interactions import approval_interaction, approval_revision
        from .admin_control_service import pending_tool_approvals
        from .control_service import GatewayControlScope

        threads, errors = self.store.threads.list_report(limit=0)
        if errors:
            raise ValueError('approval threads unavailable')
        rows, seen = [], set()
        for thread in threads:
            if thread.owner_id != self.owner:
                continue
            identity = background_identity(thread)
            for binding in thread.channel_bindings:
                scope = GatewayControlScope(binding.channel_user_id, binding.channel, binding.channel_conversation_id)
                for pending in pending_tool_approvals(self.agent, self.paths, scope):
                    request = pending.request
                    if request.permission_id in seen:
                        continue
                    seen.add(request.permission_id)
                    deadline = approval_deadline(request)
                    if deadline <= time.time():
                        continue
                    value = {'approval_id': request.permission_id, 'thread_id': thread.thread_id,
                        'task_id': None, 'run_id': request.binding['run_id'], 'revision': approval_revision(request.to_dict()),
                        'state': 'pending', 'summary': redact_sensitive_value(request.description)[:4096],
                        'expires_at': _stamp(deadline), 'allowed_decisions': []}
                    rows.append(ObservationRecord(value, self.owner, identity.space_id if identity else None,
                        identity.project_ref if identity else None, thread_id=thread.thread_id, run_id=value['run_id']))
            # 跨owner原始base Agent不能读取别人的主/子执行资格，未接真实owner宿主时不冒充完整。
            if (getattr(getattr(self.agent, 'home_paths', None), 'owner_id', None) != self.owner
                    or getattr(self.agent, 'conversation_store', None) is not self.store):
                continue
            for root in thread.active_task_ids:
                for pending in list_pending_agent_tool_approvals(self.agent, root_task_id=root):
                    interaction = approval_interaction(self.agent, pending, root)
                    if interaction['interaction_id'] in seen:
                        continue
                    target = self.store.threads.require(interaction['thread_id'])
                    if target.owner_id != self.owner:
                        continue
                    seen.add(interaction['interaction_id'])
                    target_identity = background_identity(target)
                    value = {'approval_id': interaction['interaction_id'], 'thread_id': target.thread_id,
                        'task_id': None, 'run_id': interaction['run_id'], 'revision': interaction['revision'],
                        'state': 'pending', 'summary': redact_sensitive_value(interaction['request']['description'])[:4096],
                        'expires_at': _stamp(interaction['expires_at']), 'allowed_decisions': []}
                    rows.append(ObservationRecord(value, self.owner, target_identity.space_id if target_identity else None,
                        target_identity.project_ref if target_identity else None, thread_id=target.thread_id, run_id=value['run_id']))
        full = (getattr(getattr(self.agent, 'home_paths', None), 'owner_id', None) == self.owner
            and getattr(self.agent, 'conversation_store', None) is self.store)
        return ObservationPage(tuple(rows), coverage='complete' if full else 'partial')

    # LLM: 详情必须仍为本次pending，不回放旧行，也不披露批准动作。
    # 函数用途: 返回精确审批版本与期限。
    def detail(self, params):
        page = self.list(params)
        rows = []
        for row in page.records:
            if row.value['approval_id'] == params['approval_id']:
                rows.append(ObservationRecord({'approval': row.value, 'expires_at': row.value['expires_at'], 'allowed_actions': []},
                    row.owner_id, row.space_id, row.project_ref, row.task_id, row.thread_id, row.run_id))
        # 不完整来源的详情不冒充完整列表；成功准确匹配的pending身份已由原读口验证。
        return ObservationPage(tuple(rows), coverage='complete' if rows else page.coverage)
