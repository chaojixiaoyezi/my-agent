"""模型池只是原准入/调用账的投影；配置与选模仍由既有 writer 和线程 CAS 维护。"""
from __future__ import annotations

import math
from pathlib import Path

from ..common.json_io import locked_json_path
from ..conversation.session_tasks import SESSION_TASK_TERMINAL_STATUSES
from ..gateway_parts.business_api_v13 import v13_require_access, v13_require_admin
from ..llm_scale import hot_path
from ..plugin_host.errors import HostAPIError
from ..settings.config_io import load_simple_yaml
from ..settings.model_profiles import ModelProfileError, selected_model_config
from ..settings.model_scope import thread_model_execution_status
from ..settings.parameter_changes import ChangeOrigin, WritePaths, reset_parameter, set_parameter
from ..settings.parameter_registry import parameter_registry
from ..settings.thread_model_selection import default_model_profile_id, thread_model_profile_id
from ..settings.user_config_capability import user_config_path


def _owner_settings_path(call):
    """只接受此 owner 内实际加载的用户配置，不让普通 owner 写共享部署配置。"""
    selected = user_config_path(call.context.host.config)
    if selected is None:
        selected = call.owner_home / 'config' / 'agent_config.yaml'
    path = Path(selected).resolve()
    if not path.is_relative_to(call.owner_home.resolve()):
        return None
    return path


def _settings_path(call):
    spec = parameter_registry().get('model_retry_backoff_seconds')
    path = _owner_settings_path(call)
    if spec is None or not spec.writer or path is None or not path.is_file():
        raise HostAPIError('HOST_NOT_READY', 503)
    return path


def _validate_backoff(value):
    if (type(value) is not list or not 1 <= len(value) <= 8 or any(
            type(item) not in (int, float) or not math.isfinite(item) or not 0 <= item <= 300 for item in value)):
        raise HostAPIError('INVALID_SCHEMA', 400)
    return [float(item) for item in value]


def _policy(call):
    config = call.context.host.config
    path = _owner_settings_path(call)
    saved = load_simple_yaml(path) if path is not None and path.is_file() else {}
    if 'model_retry_backoff_seconds' in saved:
        values, source = saved['model_retry_backoff_seconds'], 'owner'
    else:
        values = getattr(config, 'model_retry_backoff_seconds', [2.0, 5.0, 15.0])
        configured = (getattr(config, 'config_sources', {}) or {}).get('model_retry_backoff_seconds', {})
        source = 'deployment' if configured and configured.get('source') != 'default' else 'default'
    return {'backoff_seconds': _validate_backoff(list(values)), 'source': source, 'cas': 'none'}


def get_retry_policy(call):
    v13_require_access(call)
    return {'policy': _policy(call)}


def set_retry_policy(call):
    v13_require_access(call)
    values = _validate_backoff(call.params['backoff_seconds'])
    path = _settings_path(call)
    result = set_parameter('model_retry_backoff_seconds', values,
        paths=WritePaths(user_path=path), origin=ChangeOrigin('model'))
    if not result.get('ok'):
        raise HostAPIError('HOST_NOT_READY', 503)
    return {'policy': {'backoff_seconds': values, 'source': 'owner', 'cas': 'none'}}


def _rate_limiter(call):
    # 未接线时不另建桶，更不能把新建空桶当作原限流状态。
    return getattr(call.context.host, 'tenant_rate_limiter', None)


def reset_pool(call):
    v13_require_admin(call)
    scope = call.params['scope']
    if scope == 'owner_rate_bucket':
        limiter = _rate_limiter(call)
        if limiter is None:
            return {'reset': [], 'unchanged': [scope]}
        limiter.reset(call.owner_id)
    else:
        path = _settings_path(call)
        if 'model_retry_backoff_seconds' not in load_simple_yaml(path):
            return {'reset': [], 'unchanged': [scope]}
        result = reset_parameter('model_retry_backoff_seconds', paths=WritePaths(user_path=path),
            origin=ChangeOrigin('model'))
        if not result.get('ok'):
            raise HostAPIError('HOST_NOT_READY', 503)
    return {'reset': [scope], 'unchanged': []}


def pool_status(call):
    """只读原 owner 的有界模型明细；窗口之外的历史明确标记 partial。"""
    from ..agent_core.model.call_runtime import model_call_ledger

    v13_require_access(call)
    limiter = hot_path._limiter()
    admission = {'global_in_flight': limiter.in_flight() if limiter else 0,
        'global_max_in_flight': limiter._max if limiter else 0}
    rate = _rate_limiter(call)
    if rate is not None:
        admission['owner_rate_tokens_available'] = rate.available(call.owner_id)
    models = {}
    for record in model_call_ledger(call.context.host).records():
        name = str(record.model or '')
        if not name:
            continue
        profile = str(record.metadata.get('profile_id', ''))
        key = (name, profile)
        row = models.setdefault(key, {'model': name, 'profile_id': profile, 'in_flight': 0,
            'recent_calls': 0, 'recent_failures': 0})
        row['recent_calls'] += 1
        if record.status in {'failed', 'timed_out', 'cancelled'}:
            row['recent_failures'] += 1
        if record.status not in {'completed', 'failed', 'timed_out', 'cancelled'}:
            row['in_flight'] += 1
    return {'admission': admission, 'models': [models[key] for key in sorted(models)][:100],
        'retry_policy': _policy(call), 'coverage': 'partial'}


def _task_thread(call):
    """准确点查原会话任务和目标线程；跨 owner 与缺失返回同一错误。"""
    v13_require_access(call)
    store = getattr(call.context.host, 'conversation_store', None)
    if store is None:
        raise HostAPIError('HOST_NOT_READY', 503)
    task = store.session_tasks.load(call.params['task_id'])
    if task is None:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    thread = store.threads.load(task.target_thread_id)
    if thread is None or thread.owner_id != call.owner_id:
        raise HostAPIError('RESOURCE_NOT_FOUND', 404)
    facts = {'owner_id': call.owner_id, 'task_id': task.task_id, 'thread_id': thread.thread_id}
    if thread.business_context:
        facts['space_id'] = thread.business_context.space_id
    v13_require_access(call, facts)
    return task, thread


def task_resolution(call):
    task, thread = _task_thread(call)
    agent = call.context.host
    default = default_model_profile_id(agent)
    profile = thread.model_profile_id or default
    result = {'task_id': task.task_id, 'thread_id': thread.thread_id, 'model_profile_id': profile,
        'source': thread.model_selection_source if thread.model_selection_source != 'unknown' else 'default',
        'owner_default_profile_id': default, 'model_selection_revision': thread.model_selection_revision}
    parent = agent.conversation_store.threads.load(task.sender_thread_id)
    if parent is not None and parent.owner_id == call.owner_id and parent.thread_id != thread.thread_id:
        result.update(parent_thread_id=parent.thread_id, parent_model_profile_id=parent.model_profile_id)
    status = thread_model_execution_status(agent, thread.thread_id)
    bound = status.get('bound') or {}
    try:
        config = selected_model_config(agent, profile_id=profile)
    except ModelProfileError:
        return result
    result['effective_model'] = bound.get('model_name', config.model_name)
    result['context_window_tokens'] = max(0, int(config.model_context_window_tokens))
    return result


def set_task_override(call):
    """沿原任务锁挡住终态竞争，再进入原选模 CAS；不建任务级模型覆盖表。"""
    agent = call.context.host
    task, _ = _task_thread(call)
    with locked_json_path(agent.conversation_store.session_tasks._path(task.task_id)):
        task, thread = _task_thread(call)
        if task.status in SESSION_TASK_TERMINAL_STATUSES:
            raise HostAPIError('STATE_CONFLICT', 409)
        expected = call.params['expected_model_selection_revision']
        if thread.model_selection_revision != expected:
            raise HostAPIError('REVISION_CONFLICT', 409)
        try:
            selected = thread_model_profile_id(agent, thread.thread_id,
                select=call.params['model_profile_id'], expected_revision=expected)
        except ModelProfileError as error:
            latest = agent.conversation_store.threads.load(thread.thread_id)
            if latest is not None and latest.model_selection_revision != expected:
                raise HostAPIError('REVISION_CONFLICT', 409) from error
            raise HostAPIError('RESOURCE_NOT_FOUND', 404) from error
        return {'thread_id': thread.thread_id, 'model_profile_id': selected,
            'model_selection_revision': expected + 1, 'source': 'explicit'}


def idempotent_handler(handler):
    """只复用原 Store 回执；先耐久登记再执行外部写，失去结果时禁止盲目重做。"""
    def invoke(call):
        import hashlib
        import json

        from .v13_store import v13_store_for

        v13_require_access(call)
        if call.action == 'llm_pool.reset':
            v13_require_admin(call)
        if call.action == 'llm_pool.task_override.set':
            _task_thread(call)
        store = v13_store_for(call, 'owner')
        digest = hashlib.sha256(json.dumps(call.params, sort_keys=True, allow_nan=False).encode()).hexdigest()
        key_digest = hashlib.sha256(call.idempotency_key.encode()).hexdigest()
        reservation_key = 'reserve-' + key_digest
        completion_key = 'complete-' + key_digest
        claimed = False

        def reserve(records):
            nonlocal claimed
            claimed = True
            return {'reserved': True}

        # 该回执只表示本键已领取，不复制配置或模型选择；崩溃后不得据它猜测外部效果。
        store.transaction('side_questions', reserve, idempotency_key=reservation_key, params_digest=digest)

        def mutate(records):
            if not claimed:
                raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown')
            try:
                return {'result': handler(call)}
            except HostAPIError as error:
                # 明确拒绝同样保存准确回执，不能把已知的 CAS/配置错误降成未知。
                return {'error': {'code': error.code, 'status': error.status, 'outcome': error.outcome}}

        try:
            receipt = store.transaction('side_questions', mutate, idempotency_key=completion_key,
                params_digest=digest)
        except (OSError, RuntimeError) as error:
            raise HostAPIError('OUTCOME_UNKNOWN', 503, 'unknown') from error
        if 'error' in receipt:
            error = receipt['error']
            raise HostAPIError(error['code'], error['status'], error['outcome'])
        return receipt['result']

    return invoke
