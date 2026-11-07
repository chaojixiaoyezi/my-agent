"""旁路提问仅写自己的 owner 集合，线程、消息与主回合准入均不参与写入。"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict, deque
from contextlib import ExitStack
from contextvars import Context
from dataclasses import dataclass, field
from types import SimpleNamespace

from ..concurrency.daemon_executor import DurableDaemonThreadPoolExecutor
from ..conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from ..gateway_parts.business_api_v13 import v13_require_access
from ..plugin_host.errors import HostAPIError
from ..settings.model_profiles import selected_model_config
from ..settings.model_scope import (
    model_dependencies_scope,
    prepare_model_dependencies,
    selected_model_scope,
)
from .v13_store import _page_rows, v13_store_for

try:
    from importlib import import_module

    _limits = import_module(__package__ + '.gap158_limits')
except ModuleNotFoundError as error:
    if error.name != __package__ + '.gap158_limits':
        raise
    # 并行切片尚未合入基础设施时按合同默认值启动，不生成第二份常量目录。
    _limits = SimpleNamespace(SIDE_QUESTIONS_MAX_PER_CONVERSATION=50,
        SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER=2, SIDE_QUESTION_WORKERS=4,
        SIDE_QUESTION_EVENT_RING=256, SIDE_QUESTION_CONTEXT_MAX_MESSAGES=40,
        SIDE_QUESTION_CONTEXT_MAX_CHARS=120000)

_executor = DurableDaemonThreadPoolExecutor(max_workers=_limits.SIDE_QUESTION_WORKERS, thread_name_prefix='side-question')
_lock = threading.RLock()
_jobs: dict[tuple, dict] = {}
_events: OrderedDict[tuple, dict] = OrderedDict()
_terminal = frozenset({'answered', 'failed', 'cancelled'})
_fields = ('side_question_id', 'request_id', 'conversation_id', 'question', 'state', 'answer',
    'error_code', 'model_profile_id', 'created_at', 'finished_at')


@dataclass
class _Job:
    call: object
    row_id: str
    token: threading.Event = field(default_factory=threading.Event)
    future: object = None


def _owner_key(call):
    return (str(call.owner_home.resolve()), call.owner_id)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _view(row):
    return {key: row[key] for key in _fields if key in row}


def _conversation(call, conversation_id):
    """认证 owner 的规范线程是唯一来源；不存在与跨 owner 不可区分。"""
    v13_require_access(call)
    agent = call.context.host
    store = getattr(agent, 'conversation_store', None)
    if store is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    thread = store.threads.load(conversation_id)
    if thread is None or thread.owner_id != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    if thread.owner_home and thread.owner_home != str(call.owner_home):
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    v13_require_access(call, {'owner_id': thread.owner_id, 'conversation_id': thread.thread_id,
        **({'space_id': thread.business_context.space_id} if thread.business_context else {})})
    return thread


def _row(call, request_id):
    row = v13_store_for(call, 'owner').get('side_questions', request_id)
    if row is None or row.get('owner_id') != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    _conversation(call, row['conversation_id'])
    v13_require_access(call, row)
    return row


def _event_state(call, request_id):
    """插入和发事件共用同一个 LRU 边界，queued 也不能漏掉裁剪。"""
    key = (*_owner_key(call), request_id)
    state = _events.setdefault(key, {'seq': 0, 'items': deque(maxlen=_limits.SIDE_QUESTION_EVENT_RING)})
    _events.move_to_end(key)
    bound = _limits.SIDE_QUESTIONS_MAX_PER_CONVERSATION * _limits.SIDE_QUESTION_WORKERS
    while len(_events) > bound:
        _events.popitem(last=False)
    return state


def _emit(call, request_id, kind, *, text=None, error_code=None):
    """环只保存有界观察，不参与持久状态裁决；淘汰后明确 overflow。"""
    state = _event_state(call, request_id)
    state['seq'] += 1
    extra = {}
    if text is not None:
        extra['text'] = text
    if error_code is not None:
        extra['error_code'] = error_code
    state['items'].append({'seq': state['seq'], 'kind': kind, 'at': call.clock(), **extra})


def _transition(job, state, *, answer=None, error_code=None, actor_call=None):
    """Store 事务核终态单调；控制使用当前操作者，不借创建者身份。调用者持本模块锁。"""
    call = actor_call if actor_call is not None else job.call
    store = v13_store_for(call, 'owner')
    extra = {}
    if answer is not None:
        extra['answer'] = answer
    if error_code is not None:
        extra['error_code'] = error_code

    changed = False

    def mutate(rows):
        nonlocal changed
        row = rows.get(job.row_id)
        if row is None or row.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        if row['state'] in _terminal:
            return {'side_question': _view(row)}
        _conversation(call, row['conversation_id'])
        row.update(state=state, **extra)
        changed = True
        if state in _terminal:
            row['finished_at'] = call.clock()
        return {'side_question': _view(row)}

    # 外部取消绑定原信封键和目标；内部 worker 迁移仍使用自己的状态键。
    key = call.idempotency_key if actor_call is not None else job.row_id + '-' + state
    digest = call.params if actor_call is not None else [job.row_id, state, extra]
    result = store.transaction('side_questions', mutate, idempotency_key=key, params_digest=_digest(digest))
    row = result['side_question']
    if changed:
        _emit(call, row['request_id'], 'started' if state == 'running' else state,
            text=answer, error_code=error_code)
    return row


def _context_tail(agent, thread_id):
    """仅从原只读尾窗取上下文，再按字符预算裁剪，不读取全量归档。"""
    entries, errors = agent.conversation_store.messages.recent_report(
        thread_id, limit=_limits.SIDE_QUESTION_CONTEXT_MAX_MESSAGES)
    if errors:
        raise HostAPIError('HOST_NOT_READY', 503)
    remaining, selected = _limits.SIDE_QUESTION_CONTEXT_MAX_CHARS, []
    for entry in reversed(entries):
        if remaining <= 0:
            break
        if entry.role not in {'user', 'assistant'}:
            continue
        text = entry.content[-remaining:]
        selected.append({'role': entry.role, 'content': text})
        remaining -= len(text)
    return list(reversed(selected))


def _run(job):
    """整个 worker 位于新 Context；即使取消，直到物理生成退出才释放 owner 并发预算。"""
    call, agent = job.call, job.call.context.host
    try:
        with _lock:
            if job.token.is_set():
                return
            row = _transition(job, 'running')
            if row['state'] != 'running':
                return
        thread_id = row['conversation_id']
        _conversation(call, thread_id)
        with ExitStack() as stack:
            profile = row.get('model_profile_id')
            if profile:
                # 显式覆盖不依赖失效的旧模型，也不写回 canonical 选择。
                dependencies = prepare_model_dependencies(agent, selected_model_config(agent, profile_id=profile))
                if dependencies is not None:
                    stack.enter_context(model_dependencies_scope(agent, dependencies))
            else:
                # 初始化旧空选模会改 thread 修订，旁路必须明确使用只读绑定。
                stack.enter_context(selected_model_scope(agent, thread_id=thread_id, active=False, initialize_thread=False))
            messages = _context_tail(agent, thread_id)
            if job.token.is_set():
                return
            response = generate_auxiliary_model_response(AuxiliaryModelCallRequest(agent=agent,
                prompt=row['question'], messages=messages, purpose='side_question', thread_id=thread_id,
                request_id=row['request_id'], system_instruction='只回答旁路问题。历史仅供参考，不执行工具，不改变主会话。'))
            with _lock:
                if not job.token.is_set():
                    _transition(job, 'answered', answer=str(getattr(response, 'text', '') or '')[:20000])
    except Exception:
        with _lock:
            if not job.token.is_set():
                _transition(job, 'failed', **{'error_code': 'HOST_NOT_READY'})
    finally:
        with _lock:
            owner = _owner_key(call)
            running = _jobs.get(owner, {})
            running.pop(job.row_id, None)
            if not running:
                _jobs.pop(owner, None)


def create_side_question(call):
    """先耐久受理再排入独立 daemon 池；同键重放不重复执行模型。"""
    _conversation(call, call.params['conversation_id'])
    profile = call.params.get('model_profile_id', '')
    if profile:
        try:
            selected_model_config(call.context.host, profile_id=profile)
        except ValueError as error:
            raise HostAPIError('INVALID_SCHEMA', 400) from error
    store = v13_store_for(call, 'owner')
    row_id = 'side-' + _digest([call.owner_id, call.subject_id, call.idempotency_key])[:48]
    owner = _owner_key(call)
    pruned = []

    def mutate(rows):
        if len(_jobs.get(owner, {})) >= _limits.SIDE_QUESTIONS_MAX_CONCURRENT_PER_OWNER:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        existing = [row for row in rows.values() if row.get('conversation_id') == call.params['conversation_id']]
        terminal = sorted((row for row in existing if row['state'] in _terminal),
            key=lambda row: (row['created_at'], row['side_question_id']))
        count = len(existing)
        while count >= _limits.SIDE_QUESTIONS_MAX_PER_CONVERSATION and terminal:
            old = terminal.pop(0)
            rows.pop(old['side_question_id'])
            pruned.append(old['request_id'])
            count -= 1
        if count >= _limits.SIDE_QUESTIONS_MAX_PER_CONVERSATION:
            raise HostAPIError('LIMIT_EXCEEDED', 400)
        row = {'side_question_id': row_id, 'request_id': row_id, 'owner_id': call.owner_id,
            'conversation_id': call.params['conversation_id'], 'question': call.params['question'],
            'model_profile_id': profile, 'state': 'queued', 'created_at': call.clock()}
        rows[row_id] = row
        return {'side_question': _view(row)}

    with _lock:
        # 客户端键与内部 worker 的迁移键使用不同的域，合法键不能撞到内部回执。
        create_key = 'create-' + _digest(call.idempotency_key)
        result = store.transaction('side_questions', mutate, idempotency_key=create_key,
            params_digest=_digest(call.params))
        for request_id in pruned:
            _events.pop((*owner, request_id), None)
        # 回执保持第一次 queued 形状；是否再提交看当前规范状态，而非旧回执。
        current = store.get('side_questions', row_id)
        if current is not None and current['state'] == 'queued' and row_id not in _jobs.get(owner, {}):
            job = _Job(call, row_id)
            _jobs.setdefault(owner, {})[row_id] = job
            _event_state(call, row_id)
            try:
                job.future = _executor.submit(Context().run, _run, job)
            except Exception:
                _jobs[owner].pop(row_id, None)
                _transition(job, 'failed', **{'error_code': 'HOST_NOT_READY'})
                raise HostAPIError('HOST_NOT_READY', 503) from None
        return result


def list_side_questions(call):
    """同一集合快照同时提供行与修订，避免分页游标拿错 CAS 版本。"""
    thread_id = call.params['conversation_id']
    _conversation(call, thread_id)
    store = v13_store_for(call, 'owner')
    with _lock:
        rows, revision = store._record_view('side_questions', None, all_rows=True)
        visible = [_view(row) for row in rows if row.get('owner_id') == call.owner_id
            and row.get('conversation_id') == thread_id]
        visible.sort(key=lambda row: (row['created_at'], row['side_question_id']))
        page = _page_rows(visible, [str(call.owner_home), call.owner_id, call.subject_id,
            'side_questions', thread_id, revision], cursor=call.params.get('cursor'), limit=call.params.get('limit', 50))
        return {**page, 'next_cursor': page['next_cursor'] or '', 'revision': revision}


def delete_side_question(call):
    store = v13_store_for(call, 'owner')
    row_id = call.params['side_question_id']

    def mutate(rows):
        current = rows.get(row_id)
        if current is None or current.get('owner_id') != call.owner_id:
            raise HostAPIError('RESOURCE_NOT_FOUND', 404)
        _conversation(call, current['conversation_id'])
        if current['state'] not in _terminal:
            raise HostAPIError('STATE_CONFLICT', 409)
        v13_require_access(call, current)
        rows.pop(row_id)
        # 原回执保留资源范围；删除后的重放仍须重新检查当前会话授权。
        return {'deleted': True, 'revision': call.expected_revision + 1,
            '_conversation_id': current['conversation_id']}

    with _lock:
        result = store.transaction('side_questions', mutate, expected_revision=call.expected_revision,
            idempotency_key=call.idempotency_key, params_digest=_digest(call.params))
        _conversation(call, result['_conversation_id'])
        _events.pop((*_owner_key(call), row_id), None)
        return {key: result[key] for key in ('deleted', 'revision')}


def side_question_events(call):
    with _lock:
        row = _row(call, call.params['request_id'])
        ring = _events.get((*_owner_key(call), row['request_id']))
        after = call.params.get('after_seq', 0)
        limit = call.params.get('limit', 50)
        if ring is None:
            return {'request_id': row['request_id'], 'state': row['state'], 'events': [],
                'next_seq': after, 'overflow': True}
        items = [dict(item) for item in ring['items'] if item['seq'] > after][:limit]
        overflow = bool(ring['items'] and after < ring['items'][0]['seq'] - 1)
        return {'request_id': row['request_id'], 'state': row['state'], 'events': items,
            'next_seq': items[-1]['seq'] if items else after, 'overflow': overflow}


def cancel_side_question(call):
    with _lock:
        row = _row(call, call.params['request_id'])
        owner = _owner_key(call)
        job = _jobs.get(owner, {}).get(row['side_question_id'])
        if job is None:
            job = _Job(call, row['side_question_id'])
        # 终态也经过原键回执，避免相同键换目标；仅已提交取消能设置 token。
        row = _transition(job, 'cancelled', actor_call=call)
        if row['state'] == 'cancelled':
            job.token.set()
            if job.future is not None and job.future.cancel():
                _jobs.get(owner, {}).pop(job.row_id, None)
                if not _jobs.get(owner):
                    _jobs.pop(owner, None)
        return {'request_id': row['request_id'], 'state': row['state']}


def side_questions_ready(call):
    # 就绪开关是宿主部署事实，不能随调用方当前模型绑定被覆盖。
    from ..settings.model_scope import deployment_model_config

    host = call.services.host
    return bool(getattr(deployment_model_config(host), 'side_questions_enabled', False)) and getattr(host, 'conversation_store', None) is not None
