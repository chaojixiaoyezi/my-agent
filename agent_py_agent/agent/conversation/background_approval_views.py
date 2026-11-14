"""显式后台会话的短期审批接收兴趣；不签发工具许可或改变任务状态。"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

from ..common.json_io import (
    locked_json_path,
    write_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import read_bytes_beneath
from ..common.opaque_id import validate_opaque_id

_SCHEMA = 'background_approval_views.v1'
# 同一个规范后台线程最多同时保留 8 个独立界面席位，防并行窗口兴趣无限增长。
_MAX_VIEW_COUNT = 8
# 显式后台会话接收兴趣有效 30 秒；失联、过期或未来时戳不作为审批接收者。
_LEASE_SECONDS = 30


def _path(agent, thread_id):
    thread_id = validate_opaque_id(thread_id, kind='thread_id')
    return (Path(agent.conversation_store.storage.root) / 'subagent_tool_approvals' / '.views'
            / hashlib.sha256(thread_id.encode()).hexdigest()[:2] / (thread_id + '.json'))


def _read(agent, thread_id):
    path = _path(agent, thread_id)
    try:
        raw = read_bytes_beneath(path.parent, (path.name,), max_bytes=64 * 1024, require_dir_fd=True)
    except FileNotFoundError:
        return {}
    if raw is None:
        return {}
    row = json.loads(raw)
    if not isinstance(row, dict):
        raise ValueError('BACKGROUND_APPROVAL_INTEREST_INVALID')
    if (row.get('schema_version') != _SCHEMA or row.get('thread_id') != thread_id
            or row.get('owner_id') != agent.home_paths.owner_id or not isinstance(row.get('views'), dict)
            or len(row['views']) > _MAX_VIEW_COUNT):
        raise ValueError('BACKGROUND_APPROVAL_INTEREST_INVALID')
    return row['views']


def _fresh(row):
    seen = row.get('seen_at') if isinstance(row, dict) else None
    return type(seen) in (int, float) and 0 <= time.time() - seen <= _LEASE_SECONDS


def _source_current(agent, row):
    if not _fresh(row):
        return False
    store = agent.conversation_store
    source, error = store.threads.resolve_report(channel='chat',
        channel_conversation_id=row.get('conversation_id', ''), channel_user_id=row.get('actor_id', ''))
    return bool(error is None and source is not None and source.thread_id == row.get('source_thread_id')
                and source.owner_id == agent.home_paths.owner_id
                and any(binding.channel == 'chat' and binding.channel_conversation_id == row.get('conversation_id')
                        and binding.channel_user_id == row.get('actor_id') for binding in source.channel_bindings))


def renew(agent, thread_id, binding):
    """只有受信入口传入已核验的来源绑定；每线程最多8个独立界面席位。"""
    view_id = validate_opaque_id(binding['view_id'], kind='approval_view_id')
    path = _path(agent, thread_id)
    with locked_json_path(path):
        views = {key: value for key, value in _read(agent, thread_id).items() if _fresh(value)}
        old = views.get(view_id)
        if old is not None and any(old.get(key) != value for key, value in binding.items()):
            raise ValueError('BACKGROUND_APPROVAL_VIEW_CONFLICT')
        if old is not None and 0 <= time.time() - old['seen_at'] < _LEASE_SECONDS / 2:
            return
        if old is None and len(views) >= _MAX_VIEW_COUNT:
            raise ValueError('BACKGROUND_APPROVAL_VIEW_LIMIT')
        views[view_id] = {**binding, 'seen_at': time.time()}
        write_json_file_atomic_unlocked(path, {'schema_version': _SCHEMA, 'owner_id': agent.home_paths.owner_id,
                                               'thread_id': thread_id, 'views': views})


def close(agent, thread_id, binding):
    """撤销自己的精确席位，不使并行另一界面的兴趣失效。"""
    path = _path(agent, thread_id)
    with locked_json_path(path):
        views = _read(agent, thread_id)
        old = views.get(binding['view_id'])
        if old is not None and any(old.get(key) != value for key, value in binding.items()):
            raise ValueError('BACKGROUND_APPROVAL_VIEW_CONFLICT')
        views.pop(binding['view_id'], None)
        views = {key: value for key, value in views.items() if _fresh(value)}
        if views:
            write_json_file_atomic_unlocked(path, {'schema_version': _SCHEMA, 'owner_id': agent.home_paths.owner_id,
                                                   'thread_id': thread_id, 'views': views})
        else:
            path.unlink(missing_ok=True)  # 只删已锁内核收的空兴趣；原锁文件不动。



def receiving(agent, thread_id):
    """发布/等待只看原线程的有界兴趣；当前来源绑定失效不能当接收者。"""
    try:
        rows = _read(agent, thread_id)
        if rows and not any(_fresh(row) for row in rows.values()):
            # 只懒删本线程的过期瞬态文件；锁内复读，不能把刚续租/新加入的席位覆盖掉。
            path = _path(agent, thread_id)
            with locked_json_path(path):
                rows = _read(agent, thread_id)
                if not any(_fresh(row) for row in rows.values()):
                    path.unlink(missing_ok=True)
                    return False
        return any(_source_current(agent, row) for row in rows.values())
    except (OSError, ValueError, TypeError, AttributeError):
        return False


@contextmanager
def decision_scope(agent, thread_id, binding):
    """兴趣锁→原permission锁；关闭返回后旧界面不能再决定，兴趣本身从不批准。"""
    with locked_json_path(_path(agent, thread_id)):
        row = _read(agent, thread_id).get(binding['view_id'])
        if (row is None or any(row.get(key) != value for key, value in binding.items())
                or not _source_current(agent, row)):
            raise ValueError('BACKGROUND_APPROVAL_VIEW_INACTIVE')
        yield
