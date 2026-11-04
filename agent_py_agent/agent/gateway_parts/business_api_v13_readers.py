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
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..artifacts.registry_reader import latest_records_from_lines
from ..common.log_redaction import redact_sensitive_value
from ..common.nofollow_fs import read_bytes_beneath
from ..conversation.background_team_lifecycle import background_identity
from ..conversation.business_access import BusinessError
from ..conversation.business_contracts import BusinessExecutionContext
from ..conversation.business_observability import (
    BusinessObservationService,
    ObservationPage,
    ObservationRecord,
    _observation_cached,
    _observation_task,
    _observation_thread,
    observation_request,
)
from ..conversation.decision_audit import decision_usage_summary
from ..conversation.display_archive import (
    DisplayArchiveError,
    _read_manifest,
    read_display_archive_page,
)
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
    return all(BusinessObservationService._visible(service, member, 'usage.get', scope, actor) for member in record.members)


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
        readers.update({'tool_calls': source.tool_calls, 'tool_calls.get': source.tool_detail})
    media = home / 'artifacts' / 'business' / REGISTRY_RELATIVE_PATH
    if source.runtime_artifacts or media.is_file():
        readers.update({'artifacts': source.artifacts, 'artifacts.get': source.artifact_detail})
    if store is not None:
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
    # LLM: 默认root会刷新可用来源；签名状态按物理库/home/owner保存，不依赖临时Store对象身份。
    # 这只是最多256项的进程内游标签名状态，不记录日志、权限、结果或业务账本。
    _cursor_keys = {}
    _cursor_lock = threading.Lock()

    # LLM: 不构造执行器；同namespace复用密钥，首次插入到达上限时淘汰最早条目。
    # 函数用途: 保存源引用和有界的owner日志游标密钥，确保跨HTTP分页仍能验签。
    def __init__(self, home, owner, store, repo, *, resource_provider=None):
        self.home, self.owner, self.store, self.repo = home, owner, store, repo
        self.resource_provider = resource_provider
        namespace = (str(Path(store.storage.root)) if store is not None else '', str(home), owner)
        with self._cursor_lock:
            if namespace not in self._cursor_keys:
                if len(self._cursor_keys) >= 256:
                    self._cursor_keys.pop(next(iter(self._cursor_keys)))
                self._cursor_keys[namespace] = os.urandom(32)
            self.key = self._cursor_keys[namespace]
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
    def joined(self, table, *, operation_id=None):
        if table not in {'tool_operations', 'artifact_records'}:
            raise ValueError('invalid source')
        condition, values = 't.owner_id=?', [self.owner]
        if operation_id is not None:
            if table != 'tool_operations':
                raise ValueError('invalid operation source')
            condition += ' AND x.operation_id=?'
            values.append(operation_id)
        with self.repo._runtime_connection() as conn:
            return conn.execute('SELECT x.*,ar.run_id,t.thread_id,tr.metadata_json FROM ' + table + ' x '
                'JOIN agent_runs ar ON ar.agent_run_id=x.agent_run_id '
                'JOIN agent_attempts aa ON aa.attempt_id=x.attempt_id AND aa.agent_run_id=ar.agent_run_id '
                'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                'WHERE ' + condition + ' ORDER BY x.created_at,x.rowid', values).fetchall()

    # LLM: handler开始与嵌套outcome为执行事实；点详情限制源SQL，成功状态不等于副作用已提交。
    # 函数用途: 将runtime工具账投影为工具调用视图，保留既有结算协议。
    def tool_calls(self, params):
        records = []
        selected = ({'operation_id': params['operation_id']} if 'operation_id' in params else {})
        for row in self.joined('tool_operations', **selected):
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
                executed = result['handler_executed'] if settled and result else bool(row['handler_started_at'])
                duration = result['duration_ms'] if settled and result else None
                ref = result['result_ref'] if settled and result else row['operation_id']
            error_code = outcome.get('error_code')
            value = {'operation_id': row['operation_id'], 'run_id': row['run_id'], 'attempt_id': row['attempt_id'],
                'task_id': None, 'tool_name': row['operation_type'], 'status': row['status'],
                'executed': executed, 'effect_outcome': effect, 'duration_ms': duration,
                'error': {'code': error_code, 'message': error_code, 'retryable': False, 'details': []} if error_code else None,
                'result_ref': ref, '_input': {}}
            record = self.record(value, row)
            value['task_id'] = record.task_id
            records.append(record)
        return ObservationPage(tuple(records))

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

    # LLM: 详情包装不能丢弃授权事实。
    # 函数用途: 复用原记录的身份生成详情行。
    def wrap(self, value, row):
        return ObservationRecord(value, row.owner_id, row.space_id, row.project_ref, row.task_id,
            row.thread_id, row.run_id, row.attempt_id, row.source_cursor)

    # LLM: runtime产物引用内容寻址快照；媒体账沿原nofollow及解析器，不读取live workspace。
    # 函数用途: 合并规范产物来源，保持各自版本与owner身份。
    def artifacts(self, params):
        records = []
        if self.runtime_artifacts:
            for row in self.joined('artifact_records'):
                value = {'artifact_id': row['artifact_record_id'], 'version_ref': row['content_digest'],
                    'name': Path(row['rel_path']).name, 'mime_type': 'application/octet-stream',
                    'size_bytes': row['size'], 'content_sha256': row['content_digest'],
                    'task_id': None, 'space_id': None, 'available': False}
                record = self.record(value, row)
                value.update(task_id=record.task_id, space_id=record.space_id)
                records.append(record)
        parts = ('artifacts', 'business', *REGISTRY_RELATIVE_PATH.parts)
        raw = read_bytes_beneath(self.home, parts)
        if raw is not None:
            media, errors = latest_records_from_lines(self.home.joinpath(*parts), raw.decode('utf-8').splitlines(), _record_from_payload)
            if errors:
                raise ValueError('artifact registry unreadable')
            for item in media.values():
                meta = item.metadata
                if meta.get('owner_id') != self.owner or item.source != 'business_upload':
                    continue
                value = {'artifact_id': item.artifact_id, 'version_ref': meta['version_ref'], 'name': meta['display_name'],
                    'mime_type': item.mime_type, 'size_bytes': item.size_bytes, 'content_sha256': item.sha256,
                    'task_id': item.task_id or None, 'space_id': meta['space_id'], 'available': item.status == 'ready'}
                records.append(ObservationRecord(value, self.owner, meta['space_id'], task_id=item.task_id or None))
        elif not self.runtime_artifacts:
            raise FileNotFoundError('artifact source missing')
        return ObservationPage(tuple(records))

    # LLM: 指定旧版本不回退latest，无安全内容运输口的runtime快照不暴露路径。
    # 函数用途: 返回精确产物版本和原业务媒体读取引用。
    def artifact_detail(self, params):
        records = []
        for row in self.artifacts(params).records:
            value = row.value
            if value['artifact_id'] == params['artifact_id'] and params.get('version_ref', value['version_ref']) == value['version_ref']:
                records.append(self.wrap({'artifact': value, 'version_ref': value['version_ref'],
                    'read_ref': value['artifact_id'], 'available': value['available']}, row))
        return ObservationPage(tuple(records))

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

    # LLM: 线程列表和distinct身份按请求复用，不从过滤字段捏造空间归属或缓存授权。
    # 函数用途: 返回与请求结构化过滤相符的规范线程。
    def selected_threads(self, params):
        threads, errors = _observation_cached((self.store.threads, 'list', None),
            lambda: self.store.threads.list_report(limit=0))
        if errors:
            raise ValueError('thread source unreadable')
        result = []
        filters = params.get('filter', {})
        for thread in threads:
            _observation_cached((self.store.threads, 'record', thread.thread_id), lambda: thread)
            if thread.owner_id != self.owner:
                continue
            if filters.get('agent_id') is not None and filters['agent_id'] != thread.thread_id:
                continue
            if any(filters.get(key) is not None for key in ('attempt_id', 'workflow_run_id')):
                raise ValueError('usage scope unavailable')
            result.append(thread)
        return result

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
        events, members, revisions = {}, [], []
        for thread in self.selected_threads(params):
            revision = self.store.model_usage.revision(thread.thread_id)
            if not revision:
                continue
            rows, errors = self.store.model_usage.events_report(thread.thread_id)
            if errors or revision != self.store.model_usage.revision(thread.thread_id):
                raise ValueError('usage ledger unreadable or changed')
            revisions.append([thread.thread_id, revision])
            events[thread.thread_id] = []
            seen = set()
            for event in rows:
                if event.thread_id != thread.thread_id or event.event_id in seen:
                    raise ValueError('usage event identity duplicate or mismatch')
                seen.add(event.event_id)
                if not since <= event.created_at < until:
                    continue
                facts = self.usage_event_record(thread, event)
                if any(filters.get(key) is not None and getattr(facts, key) != filters[key]
                        for key in ('space_id', 'project_ref', 'task_id', 'run_id')):
                    continue
                events[thread.thread_id].append(event)
                members.append(facts)
        if not events:
            raise ValueError('usage source missing')
        return _DecisionWindow(self.store.model_usage, events, dict(revisions)), tuple(members), _digest(revisions)

    # LLM: 探测不解析事件或列出线程；存在不等于可读，坏账在查询时保持unknown。
    # 函数用途: 用原用量目录中的文件存在性廉价检查来源。
    def usage_exists(self):
        try:
            with os.scandir(self.store.model_usage._directory) as entries:
                return any(entry.name.endswith('.jsonl') and entry.is_file(follow_symlinks=False)
                    for entry in entries)
        except (OSError, ValueError, TypeError, KeyError):
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
        # 一次线程列表为任务两端点读提供规范对象，避免重复读取同一线程文件。
        self.selected_threads({})
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
        report = decision_usage_summary(window, list(window.events), since=0)
        if report['unreadable_threads']:
            raise ValueError('usage ledger unreadable')
        unknown = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens', 'cost', 'currency'))
        actual, estimated = dict(unknown, coverage='partial'), dict(unknown, coverage='unknown')
        if report['input_reported_calls']:
            actual['input_tokens'] = report['input_tokens_reported']
        if report['estimated_unfinished_calls']:
            estimated.update(input_tokens=report['input_tokens_estimated_unfinished'], coverage='partial')
        common = {}
        for key in ('space_id', 'project_ref', 'task_id', 'thread_id', 'run_id', 'attempt_id'):
            values = {getattr(member, key) for member in members}
            common[key] = next(iter(values)) if len(values) == 1 else None
        # 空窗口不伪造发生时间或0 token；准确身份来自规范源，不能直接复制filter。
        if not members:
            common.update(self.empty_usage_scope(filters))
        value = {'actual': actual, 'estimated': estimated,
            'unknown': ['decision_usage_only', 'cost_unavailable', 'output_tokens_unavailable', 'cache_breakdown_unavailable',
                'resource_usage_owner_current'],
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
        events = [event for rows in window.events.values() for event in rows]
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
            chosen = _DecisionWindow(window.original, chosen_events, window.revisions)
            pages.append(self.usage_page(request, chosen, tuple(chosen_members), revision, resources))
        return tuple(pages), window.events


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
