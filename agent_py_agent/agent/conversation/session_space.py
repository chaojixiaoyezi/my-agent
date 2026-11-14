"""当前发送者的会话空间选择；只保存线程元数据，不修改已受理任务。"""
from __future__ import annotations

from dataclasses import replace

from ..runtime_errors import DataCorruptionError
from .route_mode import _setting_key

_KEY = 'session_space.v1'


def read_session_space(home, thread, *, actor_id=''):
    """读取准确线程快照；缺席为未选择，损坏状态不降级为空。"""
    if thread is None:
        return {'space_id': None, 'revision': 0, 'source': 'default'}
    key = _setting_key(home, thread, actor_id)
    state = thread.metadata.get(_KEY)
    if state is None:
        return {'space_id': None, 'revision': 0, 'source': 'default'}
    if (not isinstance(state, dict) or state.get('owner_id') != str(home.owner_id)
            or not isinstance(state.get('actors'), dict)):
        raise DataCorruptionError('会话空间选择记录无法读取')
    row = state['actors'].get(key)
    if row is None:
        return {'space_id': None, 'revision': 0, 'source': 'default'}
    if (not isinstance(row, dict) or 'space_id' not in row
            or row['space_id'] is not None and (not isinstance(row['space_id'], str) or not row['space_id'])
            or type(row.get('revision')) is not int or row['revision'] < 1):
        raise DataCorruptionError('会话空间选择记录格式无效')
    return {'space_id': row['space_id'], 'revision': row['revision'],
            'source': 'default' if row['space_id'] is None else 'thread'}


def set_session_space(home, threads, thread, *, actor_id, space_id, expected_revision,
                      idempotency_key, authorize):
    """锁内重核权限与选择版本；并发写保留其它元数据，仅末次操作支持无副作用重放。"""
    _setting_key(home, thread, actor_id)
    if (space_id is not None and (not isinstance(space_id, str) or not space_id) or not isinstance(idempotency_key, str)
            or not idempotency_key or type(expected_revision) is not int or expected_revision < 0):
        raise ValueError('会话空间选择要求非空空间标识或显式null、幂等键与非负原版本')
    from ..plugin_host.errors import HostAPIError

    def update(current):
        key = _setting_key(home, current, actor_id)
        authorize()
        previous = read_session_space(home, current, actor_id=actor_id)
        state = current.metadata.get(_KEY) or {'owner_id': str(home.owner_id), 'actors': {}}
        row = state['actors'].get(key)
        if row is not None and row.get('operation_key') == idempotency_key:
            if row['space_id'] != space_id:
                raise HostAPIError('IDEMPOTENCY_CONFLICT', 409)
            return current
        if previous['revision'] != expected_revision:
            raise HostAPIError('REVISION_CONFLICT', 409)
        row = {'space_id': space_id, 'revision': previous['revision'] + 1,
               'operation_key': idempotency_key}
        return replace(current, metadata={**current.metadata, _KEY: {
            **state, 'actors': {**state['actors'], key: row}}}, revision=current.revision + 1)

    saved = threads.update_atomic(thread.thread_id, update)
    return read_session_space(home, saved, actor_id=actor_id)
