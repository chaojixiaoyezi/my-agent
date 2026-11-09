"""业务观察只读入口；原任务、执行、消息和事件仍是唯一事实源。

额外reader由宿主绑定原账本，不接受请求提供的路径、owner或可执行回调。
本模块不取claim、不确认观察、不启动runner，也不建立第二份事件/任务状态。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..common.log_redaction import redact_sensitive_value
from ..plugin_host.business_action_specs import business_defs
from ..runtime_db.repository import AGENT_RUN_TERMINAL_STATUSES, RuntimeRepository
from .background_team_lifecycle import background_identity
from .business_access import (
    BusinessError,
    require_access,
    require_actor,
    validate_params,
    validate_result,
)
from .business_contracts import BusinessExecutionContext

OBSERVATION_ACTIONS = frozenset({
    'tasks.list', 'tasks.get', 'agent_tree.get', 'subagents.list', 'subagents.get', 'messages.list',
    'executions.list', 'executions.get', 'tool_calls.list', 'tool_calls.get', 'logs.read',
    'artifacts.list', 'artifacts.get', 'changes.get', 'approvals.list', 'approvals.get',
    'permissions.get', 'usage.get', 'events.list',
})

_observation_request_data = ContextVar('business_observation_request_data', default=None)


# LLM: 只复用当前调用栈的数据，不保存actor或授权裁决；异常退出也必须丢弃。
# 函数用途: 为嵌套冻结调用和直接观察查询建立请求局部读缓存。
@contextmanager
def observation_request():
    if _observation_request_data.get() is not None:
        yield
        return
    token = _observation_request_data.set({})
    try:
        yield
    finally:
        _observation_request_data.reset(token)


# LLM: None和读失败也在本请求复用；无作用域时不缓存，下一请求必须重新读事实。
# 函数用途: 在当前请求内复用一次规范读取。
def _observation_cached(key, read):
    cache = _observation_request_data.get()
    if cache is None:
        return read()
    if key not in cache:
        if len(cache) >= 1024:
            cache.pop(next(iter(cache)))
        try:
            cache[key] = True, read()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
            cache[key] = False, error
    succeeded, value = cache[key]
    if not succeeded:
        raise value
    return value


# LLM: 以原Store组件和准确ID为键，不将同名其它owner数据复用。
# 函数用途: 对当前请求的distinct线程只执行一次require。
def _observation_thread(store, thread_id):
    return _observation_cached((store.threads, 'record', thread_id), lambda: store.threads.require(thread_id))


# LLM: 缺失任务同样只读一次，但不能跨请求保存缺失或身份事实。
# 函数用途: 对当前请求的distinct任务只执行一次load。
def _observation_task(store, task_id):
    return _observation_cached((store.session_tasks, 'record', task_id), lambda: store.session_tasks.load(task_id))


def _copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value else None


def _view(kind, row):
    return redact_sensitive_value({key: _copy(row[key]) for key in business_defs()[kind]['properties'] if key in row})


def _coverage(values):
    if 'expired' in values:
        return 'expired'
    if all(value == 'complete' for value in values):
        return 'complete'
    return 'unknown' if all(value == 'unknown' for value in values) else 'partial'


@dataclass(frozen=True)
class ObservationRecord:
    value: dict
    owner_id: str
    space_id: str | None = None
    project_ref: str | None = None
    task_id: str | None = None
    thread_id: str | None = None
    run_id: str | None = None
    attempt_id: str | None = None
    source_cursor: int = 0


@dataclass(frozen=True)
class ObservationPage:
    records: tuple[ObservationRecord, ...] = ()
    coverage: str = 'complete'
    revision: str = ''
    source_paged: bool = False
    next_position: dict | None = None


@dataclass(frozen=True)
class ObservationSourcePage:
    """宿主验证后的源分页参数；不属于公开请求Schema。"""
    position: dict | None = None
    limit: int = 50

    def __post_init__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 100 or (self.position is not None and type(self.position) is not dict):
            raise ValueError('invalid observation source page')


def bounded_observation_reader(read):
    """宿主显式登记支持源分页的reader，旧自定义reader参数合同保持不变。"""
    read.observation_source_paging_version = 1
    return read


def runtime_source_revision(path):
    """主库及WAL版本约束同一次读窗，不创建来源或扫描历史。"""
    values = []
    for suffix in ('', '-wal'):
        try:
            info = Path(str(path) + suffix).stat()
        except FileNotFoundError:
            values.append(None)
        else:
            if suffix == '-wal' and info.st_size == 0:
                values.append(None)
                continue
            values.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return _digest(values)


def source_after(request, revision):
    if request is None or request.position is None:
        return 0
    position = request.position
    if (type(position) is not dict or set(position) != {'revision', 'after'} or position.get('revision') != revision
            or type(position.get('after')) is not int or position['after'] < 0):
        raise BusinessError('CURSOR_EXPIRED')
    return position['after']


class BusinessObservationService:
    # LLM: 观察只借原mode=ro连接及其有界busy等待，不复用C短写的existing_only零等待模式；不建库或迁移。
    # 函数用途: 绑定同owner原事实源，让并发事件写入的短锁不会中断查询与SSE复核。
    def __init__(self, owner_root, *, owner_id, store=None, runtime=None, readers=None, cursor_key=None, scope_provider=None):
        self.owner_id = owner_id
        self.owner_root, self.store = Path(owner_root).absolute(), store
        self.runtime = RuntimeRepository(runtime.db_path, read_only=True) if runtime is not None else None
        self.readers = dict(readers or {})
        self.scope_provider = scope_provider
        self._cursor_key = cursor_key if cursor_key is not None else os.urandom(32)
        if type(self._cursor_key) is not bytes or len(self._cursor_key) < 32:
            raise ValueError('cursor key requires 32 host bytes')

    def authorize_call(self, action, params, *, actor):
        require_actor(actor, self)
        filters = params.get('filter', {})
        require_access(actor, action, service=self, **filters,
            **{key: value for key, value in params.items() if key not in {'filter', 'cursor', 'limit'}})

    def scope_digest(self, action, params, actor):
        return _digest([self.owner_id, actor.subject_id, action,
            self.scope_provider(actor) if self.scope_provider is not None else None,
            {key: value for key, value in params.items() if key not in {'cursor', 'limit'}}])

    def cursor(self, scope, position):
        raw = json.dumps({'scope': scope, 'position': position, 'expires': time.time() + 3600}, separators=(',', ':')).encode()
        token = base64.urlsafe_b64encode(raw + hmac.digest(self._cursor_key, raw, 'sha256')).decode().rstrip('=')
        if len(token) > 2048:
            raise BusinessError('CURSOR_EXPIRED')
        return token

    def cursor_position(self, cursor, scope):
        if cursor is None:
            return None
        try:
            if type(cursor) is not str or not 1 <= len(cursor) <= 2048:
                raise ValueError('cursor shape')
            value = base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True)
            raw, signature = value[:-32], value[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, raw, 'sha256')):
                raise ValueError('cursor signature')
            state = json.loads(raw)
            if state['scope'] != scope or state['expires'] <= time.time():
                raise ValueError('cursor scope or expiry')
            return state['position']
        except (ValueError, TypeError, KeyError, UnicodeError):
            raise BusinessError('CURSOR_EXPIRED') from None

    def _visible(self, record, action, params, actor):
        if not isinstance(record, ObservationRecord) or record.owner_id != self.owner_id:
            return False
        facts = {key: getattr(record, key) for key in ('space_id', 'project_ref', 'task_id', 'thread_id', 'run_id', 'attempt_id')}
        filters = params.get('filter', {})
        for key in ('space_id', 'project_ref', 'task_id', 'run_id', 'attempt_id'):
            selected = filters.get(key, params.get(key))
            if selected is not None and facts[key] != selected:
                return False
        if filters.get('agent_id') is not None and record.thread_id != filters['agent_id']:
            return False
        if filters.get('workflow_run_id') is not None and record.value.get('workflow_run_id') != filters['workflow_run_id']:
            return False
        for key in ('since', 'until'):
            if filters.get(key):
                stamp = record.value.get('occurred_at') or record.value.get('started_at')
                if stamp is None or (key == 'since' and stamp < filters[key]) or (key == 'until' and stamp > filters[key]):
                    return False
        if filters.get('include_completed') is False and record.value.get('status') in {'done', 'failed', 'cancelled', 'succeeded'}:
            return False
        return actor.authorize(action, {'owner_id': self.owner_id, **facts}) is True

    def _source(self, name, params, *, source_page=None):
        try:
            if name in self.readers:
                reader = self.readers[name]
                page = (reader(_copy(params), source_page=source_page) if source_page is not None
                        and getattr(reader, 'observation_source_paging_version', None) == 1 else reader(_copy(params)))
            elif name == 'tasks':
                page = self._tasks()
            elif name == 'executions':
                page = self._executions(params, source_page=source_page)
            else:
                return ObservationPage(coverage='unknown')
            if (not isinstance(page, ObservationPage) or page.coverage not in {'complete', 'partial', 'expired', 'unknown'}
                    or any(not isinstance(row, ObservationRecord) for row in page.records)
                    or type(page.source_paged) is not bool or type(page.revision) is not str
                    or (page.next_position is not None and type(page.next_position) is not dict)):
                raise BusinessError('OBSERVATION_SOURCE_INVALID')
            return page
        except BusinessError as error:
            if error.code == 'CURSOR_EXPIRED':
                raise
            return ObservationPage(coverage='unknown')
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            # 缺来源、坏账和短暂读不到不冒充空的完整查询；公开响应不携底层路径。
            return ObservationPage(coverage='unknown')

    def _paged_source(self, name, action, params, actor):
        native = name == 'executions' and name not in self.readers
        reader = self.readers.get(name)
        if not native and getattr(reader, 'observation_source_paging_version', None) != 1:
            return self._source(name, params)
        scope = self.scope_digest(action, params, actor)
        position = self.cursor_position(params.get('cursor'), scope)
        request = ObservationSourcePage(position, params.get('limit', 50))
        return self._source(name, params, source_page=request)

    def _paginate(self, records, page, action, params, actor):
        scope = self.scope_digest(action, params, actor)
        if page.source_paged:
            chosen = records[:params.get('limit', 50)]
            cursor = self.cursor(scope, page.next_position) if page.next_position is not None else None
            return chosen, cursor
        position = self.cursor_position(params.get('cursor'), scope)
        revision = page.revision or _digest([row.value for row in records])
        offset = 0
        if position is not None:
            if position.get('revision') != revision or type(position.get('offset')) is not int or position['offset'] < 0:
                raise BusinessError('CURSOR_EXPIRED')
            offset = position['offset']
        limit = params.get('limit', 50)
        chosen = records[offset:offset + limit]
        next_cursor = self.cursor(scope, {'revision': revision, 'offset': offset + limit}) if offset + limit < len(records) else None
        return chosen, next_cursor

    # LLM: 直接观察调用也有独立作用域；已在业务调用内时共享数据但仍逐次活授权。
    # 函数用途: 验证并分派一次原观察查询。
    @observation_request()
    def query(self, action: str, params: dict, *, actor: object) -> dict:
        if action not in OBSERVATION_ACTIONS:
            raise BusinessError('UNSUPPORTED_ACTION')
        params = validate_params(action, params)
        self.authorize_call(action, params, actor=actor)
        if action == 'events.list':
            result = self._events(params, actor)
        elif action in self.readers:
            result = self._detail_port(action, params, actor)
        elif action in {'subagents.list', 'subagents.get'}:
            result = self._subagents(action, params, actor)
        elif action == 'tasks.get':
            result = self._task_detail(params, actor)
        elif action == 'messages.list':
            result = self._messages(params, actor)
        elif action == 'agent_tree.get':
            result = self._tree(params, actor)
        elif action == 'usage.get':
            result = self._usage(params, actor)
        else:
            routes = {'tasks.list': ('tasks', 'TaskView'), 'executions.list': ('executions', 'ExecutionView'),
                'tool_calls.list': ('tool_calls', 'ToolCallView'), 'artifacts.list': ('artifacts', 'ArtifactView'),
                'approvals.list': ('approvals', 'ApprovalView')}
            if action in routes:
                source, kind = routes[action]
                page = self._paged_source(source, action, params, actor)
                records = [row for row in page.records if self._visible(row, action, params, actor)]
                chosen, cursor = self._paginate(records, page, action, params, actor)
                result = {'items': [_view(kind, row.value) for row in chosen], 'next_cursor': cursor,
                    'coverage': 'partial' if cursor and page.coverage == 'complete' else page.coverage}
            elif action == 'executions.get':
                page = self._paged_source('executions', action, params, actor)
                rows = [row for row in page.records if self._visible(row, action, params, actor)
                        and (params.get('attempt_id') is not None or row.value.get('_is_current') is True)]
                if len(rows) != 1:
                    raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
                execution = _view('ExecutionView', rows[0].value)
                result = {'execution': execution, 'actual_outcome': execution['effect_outcome'],
                          'retry_refs': execution['retry_refs'], 'allowed_actions': []}
            else:
                raise BusinessError('HOST_NOT_READY')
        self.authorize_call(action, params, actor=actor)
        return validate_result(action, result)

    def _detail_port(self, action, params, actor):
        """宿主原读口保留自己的源游标/retention证据；闭合schema拒绝混入内部字段。"""
        page = self._source(action, params)
        rows = [row for row in page.records if self._visible(row, action, params, actor)]
        if len(rows) != 1:
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
        result = redact_sensitive_value(_copy(rows[0].value))
        if page.coverage != 'complete':
            if 'coverage' not in result:
                raise BusinessError('HOST_NOT_READY')
            result['coverage'] = _coverage((page.coverage, result['coverage']))
        return result

    # LLM: 使用本请求列表已读的canonical任务，不重复读取或缓存访问裁决。
    # 函数用途: 返回已授权任务详情和原关联。
    def _task_detail(self, params, actor):
        page = self._source('tasks', params)
        rows = [row for row in page.records if self._visible(row, 'tasks.get', params, actor)]
        if len(rows) != 1:
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
        task = _observation_task(self.store, params['task_id'])
        if task is None:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        workflow = task.metadata.get('workflow_ref')
        # 普通规范任务没有Workflow关联；保留null，不捏造引用或修改任务元数据。
        return {'task': _view('TaskView', rows[0].value), 'assignments': [{
            'assignment_ref': task.task_id, 'task_id': task.task_id, 'sender_thread_id': task.sender_thread_id,
            'target_thread_id': task.target_thread_id, 'status': task.status, 'result_refs': list(task.result_refs)}],
            'workflow_ref': workflow, 'result_refs': list(task.result_refs), 'allowed_actions': []}

    # LLM: 同一线程/任务在本页只读一次，消息行仍逐条活授权且沿原归档边界。
    # 函数用途: 返回有界规范消息页及真实历史边界。
    def _messages(self, params, actor):
        scope = self.scope_digest('messages.list', params, actor)
        position = self.cursor_position(params.get('cursor'), scope)
        target = params['target']
        if self.store is None or target['kind'] == 'subagent':
            # 子代理原文须经W4准确任务路径/attempt读口；不能拿根线程历史冒充。
            raise BusinessError('HOST_NOT_READY')
        thread = _observation_thread(self.store, target['id'])
        if thread.owner_id != self.owner_id:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        identity = background_identity(thread)
        if target['kind'] == 'controller' and thread.metadata.get('business_controller') is not True:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        if target['kind'] == 'background_main' and identity is None:
            raise BusinessError('OBSERVATION_NOT_FOUND')
        path = self.store.storage.message_path(thread.thread_id)
        info = path.stat() if path.exists() else None
        generation = _digest([info.st_dev, info.st_ino]) if info is not None else 'unavailable'
        if position is not None and position.get('generation') != generation:
            raise BusinessError('CURSOR_EXPIRED')
        facts = ObservationRecord({}, self.owner_id, identity.space_id if identity else None,
            identity.project_ref if identity else None, thread_id=thread.thread_id)
        require_access(actor, 'messages.list', service=self, owner_id=facts.owner_id,
            space_id=facts.space_id, project_ref=facts.project_ref, thread_id=facts.thread_id)
        before, offset, bound_limit = None, 0, params.get('limit', 50)
        if position is not None:
            before, offset, bound_limit = position.get('before'), position.get('offset'), position.get('bound_limit')
            if (type(before) is not int or before < 0 or type(offset) is not int or offset < 0
                    or type(bound_limit) is not int or not 1 <= bound_limit <= 100):
                raise BusinessError('CURSOR_EXPIRED')
        page = self.store.messages.history_page_report(thread.thread_id, before=before, limit=bound_limit)
        if page.errors:
            raise BusinessError('CURSOR_EXPIRED' if position else 'HOST_NOT_READY')
        if offset > len(page.rows):
            raise BusinessError('CURSOR_EXPIRED')
        selected = page.rows[offset:offset + params.get('limit', 50)]
        items = []
        for row in selected:
            if row.role not in {'user', 'assistant', 'tool', 'host'}:
                continue
            task_id, context = row.metadata.get('task_id'), None
            task = _observation_task(self.store, task_id) if task_id else None
            if task and task.metadata.get('business_context'):
                context = BusinessExecutionContext.from_dict(task.metadata['business_context'])
                if context.owner_id != self.owner_id or context.task_id != task.task_id:
                    continue
            message_facts = ObservationRecord({}, self.owner_id, context.space_id if context else facts.space_id,
                context.project_ref if context else facts.project_ref, task.task_id if task else None, thread.thread_id)
            if not self._visible(message_facts, 'messages.list', params, actor):
                continue
            if len(row.content) > 262144:
                raise BusinessError('RESULT_LIMIT_EXCEEDED')
            items.append({'message_id': row.message_id, 'thread_id': thread.thread_id,
                'task_id': message_facts.task_id, 'space_id': message_facts.space_id, 'role': row.role,
                'text': redact_sensitive_value(row.content), 'occurred_at': _timestamp(row.created_at),
                'source': {'kind': 'canonical_message', 'source_ref': row.message_id, 'revision': None}, 'artifact_refs': []})
        offset += len(selected)
        next_position = ({'before': page.after, 'offset': offset, 'bound_limit': bound_limit} if offset < len(page.rows)
            else {'before': page.before, 'offset': 0, 'bound_limit': bound_limit} if page.before else None)
        if next_position is not None:
            next_position['generation'] = generation
        coverage = 'partial' if next_position else 'complete'
        return {'items': items, 'next_cursor': self.cursor(scope, next_position) if next_position else None,
            'coverage': coverage, 'history_boundary': {
                'first_message_id': items[0]['message_id'] if items else None,
                'last_message_id': items[-1]['message_id'] if items else None,
                'coverage': coverage, 'reason_code': 'CANONICAL_PAGE'}}

    def _subagents(self, action, params, actor):
        page = self._paged_source('executions', action, params, actor)
        records = []
        for record in page.records:
            value = record.value
            if not value.get('_is_current') or not value.get('_parent_run_id') or not self._visible(record, action, params, actor):
                continue
            if params.get('parent_run_id') is not None and value['_parent_run_id'] != params['parent_run_id']:
                continue
            if params.get('include_completed') is False and value['status'] in {'done', 'failed', 'cancelled'}:
                continue
            records.append(record)
        if action == 'subagents.get' and len(records) != 1:
            raise BusinessError('OBSERVATION_NOT_FOUND' if page.coverage == 'complete' else 'HOST_NOT_READY')
        chosen, cursor = self._paginate(records, page, action, params, actor)
        items = [{'run_id': row.run_id, 'parent_run_id': row.value['_parent_run_id'], 'thread_id': row.thread_id,
            'task_id': row.task_id, 'space_id': row.space_id, 'status': row.value['status'],
            'model_profile_id': None, 'result_refs': []} for row in chosen]
        if action == 'subagents.list':
            return {'items': items, 'next_cursor': cursor, 'coverage': page.coverage}
        if not items[0]['thread_id']:
            raise BusinessError('HOST_NOT_READY')
        return {'subagent': items[0], 'thread_ref': items[0]['thread_id'],
            'children_ref': items[0]['run_id'], 'allowed_actions': []}

    # LLM: 列表已经读取的任务用于本请求点读；线程身份按distinct ID读取，不缓存授权。
    # 函数用途: 投影规范任务并保留坏上下文的partial标志。
    def _tasks(self):
        if self.store is None:
            return ObservationPage(coverage='unknown')
        tasks, errors = _observation_cached((self.store.session_tasks, 'list', None),
            lambda: self.store.session_tasks.list_report(limit=0))
        rows = []
        for task in tasks:
            _observation_cached((self.store.session_tasks, 'record', task.task_id), lambda: task)
            target = _observation_thread(self.store, task.target_thread_id)
            sender = _observation_thread(self.store, task.sender_thread_id)
            if target.owner_id != self.owner_id or sender.owner_id != self.owner_id:
                continue
            raw = task.metadata.get('business_context')
            context = BusinessExecutionContext.from_dict(raw) if raw else None
            if context and (context.owner_id != self.owner_id or context.task_id != task.task_id):
                errors.append({'code': 'CONTEXT_MISMATCH'})
                continue
            value = {'task_id': task.task_id, 'thread_id': task.target_thread_id,
                'space_id': context.space_id if context else None, 'project_ref': context.project_ref if context else None,
                'status': task.status, 'current_run_id': None, 'result_refs': list(task.result_refs),
                'revision': int(task.updated_at * 1000000)}
            rows.append(ObservationRecord(value, self.owner_id, value['space_id'], value['project_ref'], task.task_id, task.target_thread_id))
        return ObservationPage(tuple(rows), 'partial' if errors else 'complete')

    def _executions(self, params=None, *, source_page=None):
        if self.runtime is None:
            return ObservationPage(coverage='unknown')
        params = params or {}
        request = source_page or ObservationSourcePage(limit=100)
        revision = runtime_source_revision(self.runtime.db_path)
        after = source_after(request, revision)
        limit = min(100, request.limit)
        selected_run = params.get('run_id')
        selected_attempt = params.get('attempt_id')
        more, next_position = False, None
        with self.runtime._runtime_connection() as conn:
            if selected_run is not None:
                where = 'ar.run_id=? AND t.owner_id=?'
                values = [selected_run, self.owner_id]
                if selected_attempt is not None:
                    where += ' AND aa.attempt_id=?'
                    values.append(selected_attempt)
                else:
                    where += ' AND aa.attempt_id=ar.current_attempt_id'
                source = 'agent_attempts aa'
            else:
                # 先按canonical rowid固定窗口，再join/owner过滤；稀疏权限不能驱动SQL扫描全部历史。
                positions = conn.execute('SELECT rowid FROM agent_attempts WHERE rowid>? ORDER BY rowid LIMIT ?',
                                         (after, limit + 1)).fetchall()
                more = len(positions) > limit
                positions = positions[:limit]
                if more:
                    next_position = {'revision': revision, 'after': positions[-1][0]}
                if not positions:
                    return ObservationPage(revision=revision, source_paged=True)
                where = 'aa.rowid IN (' + ','.join('?' for _ in positions) + ') AND t.owner_id=?'
                values = [row[0] for row in positions] + [self.owner_id]
                source = 'agent_attempts aa'
            raw = conn.execute('SELECT ar.run_id,ar.current_attempt_id,ar.parent_agent_run_id,pa.run_id AS parent_run_id,'
                't.thread_id,tr.metadata_json,aa.attempt_id,aa.status,aa.started_at,aa.ended_at,aa.metadata_json AS attempt_metadata '
                'FROM ' + source + ' JOIN agent_runs ar ON ar.agent_run_id=aa.agent_run_id '
                'JOIN task_runs tr ON tr.task_run_id=ar.task_run_id JOIN tasks t ON t.task_id=tr.task_id '
                'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id WHERE ' + where +
                ' ORDER BY aa.rowid LIMIT ?', [*values, 2 if selected_run is not None else limit]).fetchall()
        if runtime_source_revision(self.runtime.db_path) != revision:
            raise BusinessError('CURSOR_EXPIRED')
        rows, incomplete = [], False
        for row in raw:
            metadata = json.loads(row['metadata_json'])
            context = BusinessExecutionContext.from_dict(metadata['business_context']) if metadata.get('business_context') else None
            if context and (context.owner_id != self.owner_id or metadata.get('session_task_ref') != context.task_id):
                incomplete = True
                continue
            facts = json.loads(row['attempt_metadata'])
            outcome = facts.get('effect_outcome', 'running' if row['status'] == 'running' else 'unknown')
            if outcome not in {'none', 'not_started', 'committed', 'running', 'unknown'}:
                outcome = 'unknown'
            value = {'run_id': row['run_id'], 'attempt_id': row['attempt_id'], 'task_id': context.task_id if context else None,
                'thread_id': row['thread_id'], 'space_id': context.space_id if context else None, 'status': row['status'],
                'effect_outcome': outcome, 'started_at': _timestamp(row['started_at']), 'ended_at': _timestamp(row['ended_at']),
                'error': None, 'retry_refs': [], '_is_current': row['current_attempt_id'] == row['attempt_id'],
                '_parent_run_id': row['parent_run_id'] or None}
            rows.append(ObservationRecord(value, self.owner_id, value['space_id'], context.project_ref if context else None,
                value['task_id'], value['thread_id'], value['run_id'], value['attempt_id']))
        return ObservationPage(tuple(rows), 'partial' if incomplete or more else 'complete', revision, True, next_position)

    # LLM: 线程原对象与其它本请求观察共用读取，节点/边仍逐条授权，不跨请求保存。
    # 函数用途: 从规范执行和线程生成准确范围的观察树。
    def _tree(self, params, actor):
        execution_page = self._source('executions', params)
        executions = [row for row in execution_page.records if row.value.get('_is_current')
                      and self._visible(row, 'agent_tree.get', params, actor)]
        records, edges, controllers = {}, [], {}
        thread_coverage = 'unknown'
        if self.store is not None:
            threads, errors = _observation_cached((self.store.threads, 'list', None),
                lambda: self.store.threads.list_report(limit=0))
            thread_coverage = 'partial' if errors else 'complete'
            for thread in threads:
                _observation_cached((self.store.threads, 'record', thread.thread_id), lambda: thread)
                if thread.owner_id != self.owner_id:
                    continue
                identity = background_identity(thread)
                if thread.metadata.get('business_controller') is True:
                    controllers[thread.thread_id] = thread
                if identity is None or identity.space_id != params['space_id']:
                    continue
                value = {'kind': 'background_main', 'id': thread.thread_id, 'thread_id': thread.thread_id,
                    'parent_run_id': None, 'managed_by_thread_id': identity.managed_by_thread_id,
                    'space_id': identity.space_id, 'task_id': None, 'run_id': None,
                    'model_profile_id': thread.model_profile_id or None, 'status': identity.identity_state,
                    'has_children': False}
                record = ObservationRecord(value, self.owner_id, identity.space_id, identity.project_ref, thread_id=thread.thread_id)
                if self._visible(record, 'agent_tree.get', params, actor):
                    records[thread.thread_id] = record
        for row in list(records.values()):
            identifier = row.value['managed_by_thread_id']
            if identifier in controllers:
                thread = controllers[identifier]
                value = {'kind': 'controller', 'id': identifier, 'thread_id': identifier, 'parent_run_id': None,
                    'managed_by_thread_id': None, 'space_id': None, 'task_id': None, 'run_id': None,
                    'model_profile_id': thread.model_profile_id or None, 'status': thread.status, 'has_children': False}
                control = ObservationRecord(value, self.owner_id, params['space_id'], thread_id=identifier)
                if self._visible(control, 'agent_tree.get', params, actor):
                    records[identifier] = control
                    edges.append({'from_id': identifier, 'to_id': row.value['id'], 'relation': 'manages'})
        for row in executions:
            value = {'kind': 'subagent' if row.value.get('_parent_run_id') else 'background_main',
                'id': row.run_id, 'thread_id': row.thread_id, 'parent_run_id': row.value.get('_parent_run_id'),
                'managed_by_thread_id': None, 'space_id': row.space_id, 'task_id': row.task_id,
                'run_id': row.run_id, 'model_profile_id': None, 'status': row.value['status'], 'has_children': False}
            records[row.run_id] = ObservationRecord(value, row.owner_id, row.space_id, row.project_ref,
                row.task_id, row.thread_id, row.run_id, row.attempt_id)
        for row in executions:
            parent = row.value.get('_parent_run_id')
            source, relation = (parent, 'child_of') if parent else (row.thread_id, 'executes')
            if source in records:
                edges.append({'from_id': source, 'to_id': row.run_id, 'relation': relation})
        if params.get('include_completed') is False:
            records = {key: value for key, value in records.items()
                       if value.value['status'] not in {'done', 'failed', 'cancelled', 'succeeded'}}
        edges = [edge for edge in edges if edge['from_id'] in records and edge['to_id'] in records]
        children = {identifier: [] for identifier in records}
        for edge in edges:
            children[edge['from_id']].append(edge['to_id'])
        for identifier, record in records.items():
            record.value['has_children'] = bool(children[identifier])
        root = params.get('root_agent_id')
        if root is not None and root not in records:
            raise BusinessError('OBSERVATION_NOT_FOUND' if execution_page.coverage == thread_coverage == 'complete' else 'HOST_NOT_READY')
        roots = [root] if root else sorted(set(records) - {edge['to_id'] for edge in edges})
        frontier, selected, seen, has_more = [(item, 0) for item in roots], [], set(), False
        depth = params.get('max_depth', 20)
        while frontier:
            identifier, level = frontier.pop(0)
            if identifier in seen:
                continue
            seen.add(identifier)
            selected.append(records[identifier])
            if level < depth:
                frontier.extend((child, level + 1) for child in sorted(children[identifier]))
            elif children[identifier]:
                has_more = True
        page = ObservationPage(tuple(selected), _coverage((thread_coverage, execution_page.coverage)))
        chosen, cursor = self._paginate(selected, page, 'agent_tree.get', params, actor)
        ids = {record.value['id'] for record in chosen}
        return {'nodes': [_view('NodeView', row.value) for row in chosen],
            'edges': [edge for edge in edges if edge['to_id'] in ids], 'next_cursor': cursor,
            'coverage': _coverage((page.coverage, 'partial' if has_more or cursor else 'complete')),
            'has_more_descendants': has_more or cursor is not None or execution_page.next_position is not None}

    # LLM: 内部非catalog端口不放宽usage授权；每组及其所有成员仍先核与末核，缓存不含裁决。
    # 函数用途: 对单窗口的多个准确子范围读取用量，可将已整窗授权的事件供模型次数聚合复用。
    @observation_request()
    def usage_groups(self, params, selectors, *, actor, include_events=False):
        params = validate_params('usage.get', params)
        requests = []
        for selector in selectors:
            if (not isinstance(selector, dict) or set(selector) - {'task_id', 'agent_id', 'space_id'}
                    or any(key in params.get('filter', {}) and value != params['filter'][key]
                        for key, value in selector.items())):
                raise BusinessError('INVALID_SCHEMA')
            request = validate_params('usage.get', {'filter': {**params.get('filter', {}), **selector}})
            self.authorize_call('usage.get', request, actor=actor)
            requests.append(request)
        if include_events and (not selectors or selectors[0] != {}):
            raise BusinessError('INVALID_SCHEMA')
        if not requests:
            return [], None
        reader = self.readers.get('usage.batch')
        if not callable(reader):
            # 聚合DTO没有分组事实时不能倒推子范围，也不恢复N次全窗口读取。
            raise BusinessError('HOST_NOT_READY')
        try:
            pages, events = reader(_copy(params), _copy(requests))
            if (not isinstance(pages, (tuple, list)) or len(pages) != len(requests)
                    or not isinstance(events, dict)):
                raise ValueError('usage groups source shape')
            values = []
            for request, page in zip(requests, pages):
                if (not isinstance(page, ObservationPage)
                        or page.coverage not in {'complete', 'partial'} or len(page.records) != 1
                        or not self._visible(page.records[0], 'usage.get', request, actor)):
                    raise BusinessError('HOST_NOT_READY')
                values.append(validate_result('usage.get', redact_sensitive_value(_copy(page.records[0].value))))
        except BusinessError:
            raise
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            raise BusinessError('HOST_NOT_READY') from None
        for request, page in zip(requests, pages):
            self.authorize_call('usage.get', request, actor=actor)
            if not self._visible(page.records[0], 'usage.get', request, actor):
                raise BusinessError('HOST_NOT_READY')
        return values, events if include_events else None

    def _usage(self, params, actor):
        measured = self._source('usage', params)
        rows = [row for row in measured.records if self._visible(row, 'usage.get', params, actor)]
        if rows:
            if len(rows) != 1:
                raise BusinessError('OBSERVATION_SOURCE_INVALID')
            return redact_sensitive_value(_copy(rows[0].value))
        resources = self._source('resource_usage', params)
        rows = [row for row in resources.records if self._visible(row, 'usage.get', params, actor)]
        if len(rows) != 1:
            # 冻结ResourceUsage计数字段不接受null；没有真实观察时明确拒绝，不能编造0。
            raise BusinessError('HOST_NOT_READY', details={'coverage': 'unknown', 'source': 'resource_usage'})
        unknown = dict.fromkeys(('input_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'output_tokens', 'cost', 'currency'))
        unknown['coverage'] = 'unknown'
        return {'actual': dict(unknown), 'estimated': dict(unknown), 'unknown': ['usage_source_unavailable'],
            'resource_usage': _view('ResourceUsage', rows[0].value)}

    def _event_generation(self):
        if self.runtime is None:
            return 'unavailable'
        try:
            info = self.runtime.db_path.stat()
        except OSError:
            return 'unavailable'
        return _digest([info.st_dev, info.st_ino])

    def initial_event_cursor(self, filters, *, actor):
        params = {'filter': filters}
        self.authorize_call('events.list', params, actor=actor)
        generation, after = self._event_generation(), 0
        if self.runtime is not None and generation != 'unavailable':
            with self.runtime._runtime_connection() as conn:
                first, last = self._event_bounds(conn)
            after = first - 1 if first is not None else last
        return self.cursor(self.scope_digest('events.list', params, actor),
            {'generation': generation, 'after': after})

    def _event_bounds(self, conn):
        """保留期清空后仍读取规范自增水位，区分新快照与清理前的零位点游标。"""
        # min 和 max 分开写，才能各自走索引端点，O(log N)。
        return conn.execute('SELECT (SELECT min(seq) FROM runtime_events),'
            'COALESCE((SELECT max(seq) FROM runtime_events),'
            '(SELECT seq FROM sqlite_sequence WHERE name=\'runtime_events\'),0)').fetchone()

    # LLM: 事件复核复用本帧distinct身份读取，不复用任何授权裁决。
    # 函数用途: 用本次TaskRun规范关联投影无attempt生命周期层级。
    def _event_without_attempt(self, source):
        """缺省 attempt 是合法生命周期层级，只用本次 TaskRun 的规范关联构造公开事实。"""
        try:
            metadata = json.loads(source['metadata_json'])
            context = BusinessExecutionContext.from_dict(metadata['business_context']) if metadata.get('business_context') else None
            if context and (context.owner_id != source['owner_id'] or metadata.get('session_task_ref') != context.task_id):
                return None
            task_id = context.task_id if context else source['canonical_task_id']
            if context and self.store is not None:
                task = _observation_task(self.store, task_id)
                if (task is None or task.target_thread_id != source['thread_id']
                        or task.task_run_id and task.task_run_id != source['canonical_task_run_id']
                        or task.metadata.get('business_context') != metadata['business_context']
                        or any(_observation_thread(self.store, tid).owner_id != self.owner_id
                            for tid in (task.sender_thread_id, task.target_thread_id))):
                    return None
            space_id, project_ref = (context.space_id, context.project_ref) if context else (None, None)
            if source['run_id'] is not None:
                value = {'kind': 'subagent' if source['parent_run_id'] else 'background_main',
                    'id': source['run_id'], 'thread_id': source['thread_id'],
                    'parent_run_id': source['parent_run_id'], 'managed_by_thread_id': None,
                    'space_id': space_id, 'task_id': task_id, 'run_id': source['run_id'],
                    'model_profile_id': None, 'status': source['agent_status'],
                    'has_children': bool(source['has_children']), '_parent_run_id': source['parent_run_id']}
            else:
                status, updated_at = source['task_run_status'], source['task_run_updated_at']
                if source['event_type'] == 'task_run.closed':
                    # TaskRun 可合法重开；关闭事件只投影原生产者的终态字段，其余原始payload不公开。
                    closed = json.loads(source['payload_json'])
                    if not isinstance(closed, dict) or closed.get('status') not in AGENT_RUN_TERMINAL_STATUSES:
                        return None
                    status, updated_at = closed['status'], source['created_at']
                value = {'task_id': task_id, 'thread_id': source['thread_id'], 'space_id': space_id,
                    'project_ref': project_ref, 'status': status, 'current_run_id': None,
                    'result_refs': [], 'revision': int(updated_at * 1000000)}
            return ObservationRecord(value, source['owner_id'], space_id, project_ref, task_id,
                source['thread_id'], source['run_id'], None)
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _events(self, params, actor):
        scope = self.scope_digest('events.list', params, actor)
        generation = self._event_generation()
        position = self.cursor_position(params.get('cursor'), scope)
        after = 0
        if position is not None:
            if (position.get('generation') != generation or type(position.get('after')) is not int or position['after'] < 0):
                raise BusinessError('CURSOR_EXPIRED')
            after = position['after']
        events, coverage = [], 'unknown'
        if self.runtime is not None and generation != 'unavailable':
            with self.runtime._runtime_connection() as conn:
                first, last = self._event_bounds(conn)
                retained_after = first - 1 if first is not None else last
                if position is not None and not retained_after <= after <= last:
                    raise BusinessError('CURSOR_EXPIRED')
                if position is None:
                    after = retained_after
                raw = conn.execute('SELECT e.*,ar.run_id,ar.agent_run_id AS canonical_agent_run_id,'
                    'aa.agent_run_id AS attempt_agent_run_id,ar.task_run_id AS agent_task_run_id,ar.status AS agent_status,'
                    'pa.run_id AS parent_run_id,tr.task_run_id AS canonical_task_run_id,tr.task_id AS canonical_task_id,'
                    'tr.metadata_json,tr.status AS task_run_status,tr.updated_at AS task_run_updated_at,t.owner_id,t.thread_id,'
                    'agent_task.owner_id AS agent_owner_id,attempt_task.owner_id AS attempt_owner_id,'
                    'EXISTS(SELECT 1 FROM agent_runs child WHERE child.parent_agent_run_id=ar.agent_run_id '
                    'AND child.task_run_id=tr.task_run_id) AS has_children '
                    'FROM runtime_events e LEFT JOIN agent_attempts aa ON aa.attempt_id=e.attempt_id '
                    'LEFT JOIN agent_runs ar ON ar.agent_run_id=COALESCE(NULLIF(e.agent_run_id,\'\'),aa.agent_run_id) '
                    'LEFT JOIN task_runs agent_tr ON agent_tr.task_run_id=ar.task_run_id '
                    'LEFT JOIN tasks agent_task ON agent_task.task_id=agent_tr.task_id '
                    'LEFT JOIN agent_runs attempt_ar ON attempt_ar.agent_run_id=aa.agent_run_id '
                    'LEFT JOIN task_runs attempt_tr ON attempt_tr.task_run_id=attempt_ar.task_run_id '
                    'LEFT JOIN tasks attempt_task ON attempt_task.task_id=attempt_tr.task_id '
                    'LEFT JOIN agent_runs pa ON pa.agent_run_id=ar.parent_agent_run_id AND pa.task_run_id=ar.task_run_id '
                    'LEFT JOIN task_runs tr ON tr.task_run_id=COALESCE(NULLIF(e.task_run_id,\'\'),ar.task_run_id) '
                    'LEFT JOIN tasks t ON t.task_id=tr.task_id WHERE e.seq>? ORDER BY e.seq LIMIT ?',
                    (after, params.get('limit', 50))).fetchall()
            execution_page = self._source('executions', params)
            execution = {(row.run_id, row.attempt_id): row for row in execution_page.records}
            coverage = execution_page.coverage
            for source in raw:
                # 明确属于其它owner的行不披露，也不能用其坏引用阻塞本owner；跨owner矛盾仍保留为缺口。
                # 非空引用必须已解析才能证明外部归属，缺省的optional引用则不等待后继记录。
                if (source['owner_id'] not in (None, self.owner_id)
                        and (not source['agent_run_id'] or source['agent_owner_id'] not in (None, self.owner_id))
                        and (not source['attempt_id'] or source['attempt_owner_id'] not in (None, self.owner_id))):
                    after = source['seq']
                    continue
                if (source['canonical_task_run_id'] is None or source['owner_id'] is None
                        or source['attempt_id'] and source['attempt_agent_run_id'] != source['canonical_agent_run_id']
                        or (source['agent_run_id'] or source['attempt_id'])
                        and source['agent_task_run_id'] != source['canonical_task_run_id']):
                    coverage = 'partial'
                    break
                if source['owner_id'] != self.owner_id:
                    after = source['seq']
                    continue
                row = (execution.get((source['run_id'], source['attempt_id'])) if source['attempt_id']
                    else self._event_without_attempt(source))
                if row is None:
                    # 真正缺失的非空引用保留重连机会；空attempt不会等待并不存在的后继attempt。
                    coverage = 'partial'
                    break
                after = source['seq']
                event_facts = ObservationRecord({**row.value, 'occurred_at': _timestamp(source['created_at'])},
                    row.owner_id, row.space_id, row.project_ref, row.task_id, row.thread_id, row.run_id, row.attempt_id)
                if not self._visible(event_facts, 'events.list', params, actor):
                    continue
                cursor = self.cursor(scope, {'generation': generation, 'after': after})
                node = ({'kind': 'subagent' if row.value.get('_parent_run_id') else 'background_main', 'id': row.run_id}
                    if row.run_id is not None else None)
                kind = 'ExecutionView' if row.attempt_id is not None else 'NodeView' if node else 'TaskView'
                events.append({'stream_item': 'event', 'event_id': source['event_id'], 'cursor': cursor,
                    'event_type': source['event_type'], 'occurred_at': _timestamp(source['created_at']) or '', 'node': node,
                    'space_id': row.space_id, 'project_ref': row.project_ref, 'task_id': row.task_id,
                    'run_id': row.run_id, 'attempt_id': row.attempt_id, 'operation_id': None,
                    'payload': _view(kind, row.value), 'coverage': execution_page.coverage})
        return {'events': events, 'next_cursor': self.cursor(scope, {'generation': generation, 'after': after}),
            'coverage': coverage, 'reset_required': False}

    # LLM: 重连基线的嵌套查询共用当前快照作用域，返回后丢弃身份读取，不跨帧缓存。
    # 函数用途: 生成沿原授权和游标边界的重连基线。
    @observation_request()
    def snapshot(self, filters, *, actor):
        """重连基线与补拉沿同一读门；分页未覆盖部分明确保留partial与原游标。"""
        tasks = self.query('tasks.list', {'filter': filters, 'limit': 100}, actor=actor)
        executions = self.query('executions.list', {'filter': filters, 'limit': 100}, actor=actor)
        if filters.get('space_id'):
            tree = self.query('agent_tree.get', {'space_id': filters['space_id'], 'limit': 100}, actor=actor)
            # 其它filter只能收窄snapshot，不能用tree忽略task/run限制扩大可见数据。
            nodes = [node for node in tree['nodes'] if not filters.get('run_id') or node['run_id'] == filters['run_id']]
            nodes = [node for node in nodes if not filters.get('task_id') or node['task_id'] == filters['task_id']]
        else:
            nodes, coverages = [], []
            for sid in sorted({row['space_id'] for row in tasks['items'] + executions['items'] if row.get('space_id')}):
                tree = self.query('agent_tree.get', {'space_id': sid, 'limit': 100}, actor=actor)
                coverages.append(tree['coverage'])
                nodes.extend(tree['nodes'])
            tree = {'coverage': _coverage(coverages) if coverages else 'unknown', 'next_cursor': None}
            nodes = list({(node['kind'], node['id']): node for node in nodes}.values())
        # snapshot中的树节点同样与全部ReadFilter取交集，不能只有事件页收窄。
        source = self._source('executions', {'filter': filters})
        by_run = {row.run_id: row for row in source.records if row.value.get('_is_current')}
        narrowed = []
        for node in nodes:
            if node['run_id']:
                facts = by_run.get(node['run_id'])
            else:
                identity = None
                if self.store is not None:
                    thread = _observation_thread(self.store, node['thread_id'])
                    identity = background_identity(thread)
                facts = ObservationRecord(node, self.owner_id, node['space_id'] or filters.get('space_id'),
                    identity.project_ref if identity else None, node['task_id'], node['thread_id'])
            if facts is not None and self._visible(facts, 'events.list', {'filter': filters}, actor):
                narrowed.append(node)
        nodes = narrowed
        coverage = _coverage((tasks['coverage'], executions['coverage'], tree['coverage'], 'partial' if len(nodes) > 100 else 'complete'))
        return {'nodes': nodes[:100], 'tasks': tasks['items'], 'executions': executions['items'],
            'next_cursor': tasks['next_cursor'] or executions['next_cursor'] or tree['next_cursor'], 'coverage': coverage}
