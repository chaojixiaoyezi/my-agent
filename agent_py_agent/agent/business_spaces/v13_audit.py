"""共享请求、决策和 V13 写审计的只读合并投影。

每条记录仅输出冻结 AdminAuditRecord 的六个字段；来源失败明确报告 partial。
host 范围必须重验原生管理员上下文，actor 参数只作筛选而不能签发权限。
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ..conversation.business_access import BusinessError
from ..gateway_parts.paths import gateway_paths_from_root
from ..gateway_parts.request_audit_records import (
    OutcomeQuery,
    decision_observation_records,
    request_outcome_records,
)
from .v13_store import V13Store, _encode, _page_rows, _timestamp


# LLM: 查询只接受有限时间值，不把异常输入当作无过滤。
# 函数用途: 解析可空的 ISO UTC 时间。
def _time_bound(value):
    if value is None:
        return None
    if type(value) is not str:
        raise BusinessError('INVALID_SCHEMA')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('timezone')
        return parsed.timestamp()
    except (ValueError, OverflowError) as error:
        raise BusinessError('INVALID_SCHEMA') from error


# LLM: 真实 HTTP handler 走原 AuthMiddleware；V13Call 则复用分发器的原生管理员末核。
# 函数用途: 拒绝任意角色字符串或布尔值伪造的跨 owner 审计权限。
def _require_host_admin(admin_context):
    from ..auth.middleware import AuthMiddleware

    middleware = getattr(admin_context, '_auth_middleware', None)
    if isinstance(middleware, AuthMiddleware):
        peer = getattr(admin_context, 'client_address', ())
        peer_ip = peer[0] if isinstance(peer, (tuple, list)) and peer else None
        if middleware.require_admin(admin_context.headers, peer_ip)[0]:
            return
        raise BusinessError('CAPABILITY_DENIED')
    if admin_context is not None:
        from ..gateway_parts.business_api_v13 import V13Call, v13_require_admin
        if isinstance(admin_context, V13Call):
            admin_context.context.check(admin_context.action, {'owner_id': admin_context.owner_id})
            v13_require_admin(admin_context)
            return
    raise BusinessError('CAPABILITY_DENIED')


# LLM: 必须能从显式 owner 根定位同一规范 home，不能读进程 HOME 猜另一个用户。
# 函数用途: 为无宿主的只读调用取得纯路径上下文。
def _base_home(owner_home):
    from ..user_space.home_layout import home_paths

    path = Path(os.path.abspath(owner_home))
    for parent in path.parents:
        if parent.name == 'owners':
            return home_paths(parent.parent)
    raise BusinessError('HOST_NOT_READY')


# LLM: 宿主仅提供实际来源位置，不改变 owner 过滤条件。
# 函数用途: 解析同一 Gateway 队列和范围内的 owner 只读 home。
def _sources(owner_home, owner_id, scope, host, unavailable):
    from ..user_space.owner_admin_controls import list_owner_home_paths, owner_home_paths_by_id
    from ..user_space.runtime_paths import resolve_runtime_paths_for_agent

    base = getattr(host, 'home_paths', None) if host is not None else None
    base = _base_home(owner_home) if base is None else base
    config = getattr(host, 'config', SimpleNamespace())
    if scope == 'host':
        homes, truncated = list_owner_home_paths(base, limit=500)
        if truncated:
            unavailable.append({'source': 'owners', 'reason_code': 'SOURCE_TRUNCATED'})
    else:
        own = owner_home_paths_by_id(base, owner_id)
        if own is None or Path(own.owner_home_dir).absolute() != Path(owner_home).absolute():
            raise BusinessError('RESOURCE_NOT_FOUND')
        homes = [(owner_id, own)]
    # 原队列属于基础 Gateway；host.config 的绝对覆盖也由原路径解析器处理。
    resolved = resolve_runtime_paths_for_agent(config, base.owner_home_dir, base)
    paths = gateway_paths_from_root(resolved.paths['gateway_workspace'])
    return homes, config, paths


# LLM: 冷用户只打开 ConversationStore，不构造执行 Agent、不建线程目录。
# 函数用途: 取得请求观察所需的可信 thread→owner 映射。
def _thread_owners(homes, config, host, unavailable):
    from ..gateway_parts.owner_conversation_store import owner_conversation_store_from_config

    result = {}
    for owner_id, home in homes:
        try:
            own = getattr(getattr(host, 'home_paths', None), 'owner_id', None) == owner_id
            store = getattr(host, 'conversation_store', None) if own else None
            if store is None:
                store = owner_conversation_store_from_config(
                    config if own else SimpleNamespace(), home, initialize=False)
            threads, errors = store.threads.list_report(limit=1001)
            if errors or len(threads) > 1000:
                unavailable.append({'source': 'threads:' + owner_id, 'reason_code': 'SOURCE_PARTIAL'})
            for thread in threads[:1000]:
                result[thread.thread_id] = owner_id
        except Exception:
            unavailable.append({'source': 'threads:' + owner_id, 'reason_code': 'HOST_NOT_READY'})
    return result


# LLM: 来源报告截断、坏账和不可用时不伪造完整覆盖。
# 函数用途: 读取一个原审计来源并保留其扫描限制。
def _native_rows(source, paths, thread_owners, allowed, since, unavailable):
    try:
        if source == 'decision':
            report = decision_observation_records(paths, thread_owners=thread_owners, since=since, limit=3000)
        else:
            report = request_outcome_records(paths, OutcomeQuery(allowed, thread_owners, '', since, 3000))
        if not report.get('available'):
            raise OSError('source')
        if report.get('truncated') or report.get('unreadable'):
            unavailable.append({'source': source, 'reason_code': 'SOURCE_PARTIAL'})
        return report['entries']
    except Exception:
        unavailable.append({'source': source, 'reason_code': 'HOST_NOT_READY'})
        return []


# LLM: 白名单投影从不携带 prompt、秘密或任意 metadata；记录编号由来源位置身份派生。
# 函数用途: 将原审计记录转换为冻结共享 DTO。
def _project(source, row):
    if source == 'v13':
        return {'record_id': row['entry_id'], 'at': row['at'], 'actor': row['subject_id'],
                'action': row['action'], 'outcome': row['outcome'], 'source': source}
    identity = [source, row.get('owner_id'), row.get('request_id'), row.get('kind'),
                row.get('point'), row.get('recorded_at')]
    return {'record_id': source + '-' + hashlib.sha256(_encode(identity)).hexdigest(),
            'at': _timestamp(row['recorded_at']),
            'actor': str(row.get('subject_id') or row.get('actor') or row.get('owner_id') or 'unknown'),
            'action': str(row.get('action') or row.get('kind') or source),
            'outcome': str(row.get('status') or 'observed'), 'source': source}


# LLM: 两个消费者共享同一排序、范围和游标口径；actor 是筛选，admin_context 才是权限证明。
# 函数用途: 合并分页请求、决策和 V13 审计，来源失败显式返回 unavailable。
def audit_records(owner_home, owner_id, *, scope='owner', since=None, until=None,
                  actor=None, cursor=None, limit=50, admin_context=None, host=None):
    if (scope not in ('owner', 'host') or type(owner_id) is not str or not owner_id
            or type(limit) is not int or not 1 <= limit <= 100
            or (actor is not None and (type(actor) is not str or not actor or len(actor) > 128))):
        raise BusinessError('INVALID_SCHEMA')
    lower, upper = _time_bound(since), _time_bound(until)
    if lower is not None and upper is not None and lower > upper:
        raise BusinessError('INVALID_SCHEMA')
    if scope == 'host':
        _require_host_admin(admin_context)
    if host is None and admin_context is not None:
        host = getattr(getattr(admin_context, 'context', None), 'host', None)
        if host is None:
            server = getattr(admin_context, 'server', None)
            host = getattr(server, 'agent', None)
    unavailable, merged = [], []
    homes, config, paths = _sources(owner_home, owner_id, scope, host, unavailable)
    thread_owners = _thread_owners(homes, config, host, unavailable)
    allowed = None if scope == 'host' else frozenset({owner_id})
    for source in ('decision', 'request'):
        rows = _native_rows(source, paths, thread_owners, allowed, lower if lower is not None else 0, unavailable)
        for row in rows:
            if scope == 'owner' and row.get('owner_id') != owner_id:
                continue
            try:
                merged.append(_project(source, row))
            except (ValueError, TypeError, KeyError, OverflowError):
                unavailable.append({'source': source, 'reason_code': 'SOURCE_RECORD_INVALID'})
    for source_owner, home in homes:
        try:
            store = V13Store.read_only(home.owner_home_dir)
            for row in store.list('audit_log'):
                if row.get('owner_id') == source_owner:
                    merged.append(_project('v13', row))
                else:
                    unavailable.append({'source': 'v13:' + source_owner, 'reason_code': 'SOURCE_RECORD_INVALID'})
        except Exception:
            unavailable.append({'source': 'v13:' + source_owner, 'reason_code': 'HOST_NOT_READY'})
    selected = []
    for row in merged:
        try:
            at = _time_bound(row['at'])
            if at is None:
                raise ValueError('time')
            if (lower is None or at >= lower) and (upper is None or at <= upper) and (actor is None or actor == row['actor']):
                row['at'] = _timestamp(at)
                selected.append(row)
        except (BusinessError, ValueError, TypeError, KeyError):
            unavailable.append({'source': row['source'], 'reason_code': 'SOURCE_RECORD_INVALID'})
    selected.sort(key=lambda row: (row['at'], row['source'], row['record_id']), reverse=True)
    unavailable = [dict(item) for item in {tuple(sorted(item.items())) for item in unavailable}]
    unavailable.sort(key=lambda item: (item['source'], item['reason_code']))
    page = _page_rows(selected, [str(Path(owner_home).absolute()), owner_id, scope, since, until,
                                 actor, selected, unavailable], cursor=cursor, limit=limit)
    return {'records': page['items'], 'next_cursor': page['next_cursor'],
            'coverage': 'partial' if unavailable else page['coverage'], 'unavailable': unavailable}
