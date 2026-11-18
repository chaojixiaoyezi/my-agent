"""管理员从规范用户目录选择出站策略目标。显示名只用于查找，写入只认原目录中的普通用户 ID。"""
from __future__ import annotations

import json
import os
import time

from ..common.nofollow_fs import open_directory_beneath, read_tail_snapshot_beneath
from ..owner_wake_discovery import discover_owner_home_page
from ..plugin_host.errors import HostAPIError
from .approval_mode import is_permission_admin
from .local_user_store import LocalUserStore
from .owner_admin_controls import owner_home_paths_by_id
from .owner_egress import OwnerEgressPolicy, OwnerEgressPolicyError, OwnerEgressPolicyStore
from .owner_egress_gateway import current_gateway_owner_egress
from .owner_resolver import resolve_owner_home

EGRESS_READ = ('egress_list_users', 'egress_get', 'egress_get_default')
EGRESS_ACTIONS = (*EGRESS_READ, 'egress_set_users', 'egress_set_default')
_ENDPOINTS = {'type': 'array', 'description': '准确 IP 和端口的组合。启用白名单时，空数组拒绝全部业务目的地。',
              'items': {'type': 'object', 'additionalProperties': False, 'required': ['ip', 'port'], 'properties': {
                  'ip': {'type': 'string', 'description': '单个 IPv4 或 IPv6 地址。'},
                  'port': {'type': 'integer', 'minimum': 1, 'maximum': 65535, 'description': '准确目的端口。'}}}}
_MODE = {'type': 'string', 'enum': ['inherit', 'unrestricted', 'allowlist'],
         'description': '跟随全局、单独不限制或仅放行该用户清单。全局默认可选后两项。'}
EGRESS_FIELDS = {
    'mode': _MODE, 'endpoints': _ENDPOINTS,
    'query': {'type': 'string', 'description': '按已登记显示名或规范用户编号筛选，每页有 next_cursor 时继续。'},
    'cursor': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 3, 'maxItems': 3,
               'description': '原样传上一页的 next_cursor，首次查询省略。'},
    'updates': {'type': 'array', 'minItems': 1, 'maxItems': 2000, 'description': '一个或多个普通用户各自的规则，省略 endpoints 保留原清单。',
                'items': {'type': 'object', 'required': ['owner_id', 'mode'], 'additionalProperties': False,
                          'properties': {'owner_id': {'type': 'string', 'description': 'egress_list_users 返回的规范 ID。'},
                                         'mode': _MODE, 'endpoints': _ENDPOINTS}}},
}


def _invalid(message, hint='请调用 egress_list_users 并使用返回的规范 owner_id。'):
    return OwnerEgressPolicyError('EGRESS_ADMIN_INVALID_ARGUMENT', message, next_action=hint)


def _target(home, value):
    target = owner_home_paths_by_id(home, value)
    if target is None or target.owner_kind != 'user' or target.owner_id != value:
        raise _invalid('owner_id 必须是列表中已有普通用户的完整编号，管理员和群不能作为此操作的目标。')
    try:
        fd = open_directory_beneath(home.root, ('owners', *value.split('/')), require_dir_fd=True)
        os.close(fd)
    except (OSError, ValueError):
        raise _invalid('owner_id 对应的用户目录缺失、已变化或无法安全确认。') from None
    return target.owner_id


def _feishu_names(home, subjects):
    # 名称是展示材料。一次只读一个有界尾窗；未覆盖的旧名称显式标记，规范 open_id 仍可选择。
    if not subjects:
        return {}, 'complete'
    path = home.provider_identity_dir / 'feishu.jsonl'
    try:
        snapshot = read_tail_snapshot_beneath(home.root, path.relative_to(home.root).parts, max_bytes=512 * 1024)
    except FileNotFoundError:
        return {}, 'unrecorded'
    except (OSError, ValueError):
        return {}, 'unavailable'
    partial = len(snapshot.content) < snapshot.stat.st_size
    lines = snapshot.content.splitlines()[int(partial):]
    names = {}
    for line in lines:
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            partial = True
            continue
        if not isinstance(record, dict) or record.get('provider') != 'feishu':
            continue
        subject = record.get('provider_subject_id')
        if not isinstance(subject, str) or subject not in subjects:
            continue
        names.pop(subject, None)
        if (record.get('owner_kind') == 'user' and record.get('owner_id') == subject
                and isinstance(record.get('display_name'), str) and record['display_name']):
            names[subject] = record['display_name']
    return names, 'partial' if partial else 'complete'


def _page(home, params):
    cursor, query, limit = params.get('cursor'), params.get('query', ''), params.get('limit', 50)
    if (type(limit) is not int or not 1 <= limit <= 200 or not isinstance(query, str) or len(query) > 256
            or cursor is not None and (not isinstance(cursor, list) or len(cursor) != 3
            or any(not isinstance(v, str) or len(v) > 256 for v in cursor))):
        raise _invalid('分页参数无效。', 'limit 填 1～200，query 填文本，cursor 复制上一页的三段 next_cursor。')
    page = discover_owner_home_page(home.owners_dir, limit=limit, after_cursor=tuple(cursor) if cursor else None)
    targets = [r.identity for r in page.targets if r.identity.owner_kind == 'user']
    names, name_coverage = _feishu_names(home, {i.owner_id for i in targets if i.provider == 'feishu'})
    rows, local = [], LocalUserStore(home.root)
    for identity in targets:
        key = resolve_owner_home(home.root, identity).owner_id
        name = names.get(identity.owner_id, '') if identity.provider == 'feishu' else ''
        if identity.provider == 'local':
            try:
                account = local.get(identity.owner_id)
                name = account.display_name if account else ''
            except (HostAPIError, OSError, ValueError):
                name = ''
        if query.casefold() not in (key + ' ' + name).casefold():
            continue
        row = {'owner_id': key, 'provider': identity.provider, 'display_name': name}
        if identity.provider == 'feishu':
            row['feishu_open_id'] = identity.owner_id
        rows.append(row)
    return {'users': rows, 'next_cursor': page.next_cursor, 'feishu_display_name_coverage': name_coverage}


def _policy(params):
    mode = params.get('mode')
    if mode not in ('inherit', 'unrestricted', 'allowlist'):
        raise _invalid('mode 必须为 inherit、unrestricted 或 allowlist。', '从 egress_list_users 取得目标后，给每个 updates 项明确指定 mode。')
    return OwnerEgressPolicy.from_dict({'mode': mode, 'endpoints': params['endpoints']}) if 'endpoints' in params else mode


def manage_owner_egress(home, params):
    if not is_permission_admin(home):
        raise OwnerEgressPolicyError('EGRESS_ADMIN_REQUIRED', '只有本机管理员可读取和设置普通用户出站策略。',
                                    status_code=403, next_action='使用本机管理员主会话。')
    action = params.get('action')
    fields = {'egress_list_users': {'limit', 'cursor', 'query'}, 'egress_get': {'owner_id'}, 'egress_get_default': set(),
              'egress_set_default': {'mode', 'endpoints'}, 'egress_set_users': {'updates'}}
    if not isinstance(action, str) or action not in fields or set(params) - fields[action] - {'action'}:
        raise _invalid('此出站策略动作包含未知字段。', '列表用 egress_list_users；批量设置用 egress_set_users 并传 updates。')
    store = OwnerEgressPolicyStore(home.root)
    if action == 'egress_list_users':
        return _page(home, params)
    if action == 'egress_get_default':
        return {'default': store.get_default().to_dict()}
    if action == 'egress_set_default':
        change = _policy(params)
        runtime = current_gateway_owner_egress(home.root)
        policy = runtime.set_default(store, change) if runtime is not None else store.set_default(change)
        return {'default': policy.to_dict()}
    if action == 'egress_get':
        key = _target(home, params.get('owner_id'))
        runtime = current_gateway_owner_egress(home.root)
        if runtime is not None:
            return {'owner_id': key, **runtime.observe(store, key)}
        policy, effective = store.inspect_owner(key)
        return {'owner_id': key, 'policy': policy.to_dict(), 'effective': effective.to_dict()}
    rows = params.get('updates')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2000:
        raise _invalid('updates 需要 1～2000 条用户设置。', '示例：{"action":"egress_set_users","updates":[{"owner_id":"local/alice","mode":"inherit"}]}')
    updates = {}
    for item in rows:
        if not isinstance(item, dict) or set(item) - {'owner_id', 'mode', 'endpoints'}:
            raise _invalid('每个 updates 项仅接受 owner_id、mode 和可选 endpoints。')
        key = _target(home, item.get('owner_id'))
        if key in updates:
            raise _invalid('同一批次包含重复 owner_id。', '合并同一用户的设置后重新提交。')
        updates[key] = _policy(item)
    runtime = current_gateway_owner_egress(home.root)
    if runtime is None:
        return {'users': [{'owner_id': key, 'policy': value.to_dict()} for key, value in store.apply(updates).items()]}
    results = []
    deadline = time.monotonic() + 30
    for key, change in updates.items():
        if time.monotonic() >= deadline:
            results.append({
                'owner_id': key, 'status': 'not_started', 'error_code': 'EGRESS_USER_BATCH_DEADLINE',
                'message': '批次达到时间预算，此用户尚未进入发布。',
                'next_action': '读取批次结果，只提交尚未开始的用户。',
            })
            continue
        try:
            value = runtime.apply(store, key, change)
        except OwnerEgressPolicyError as exc:
            results.append({'owner_id': key, 'status': exc.side_effect, 'error_code': exc.code,
                            'message': str(exc), 'next_action': exc.next_action, **exc.details})
        else:
            results.append({'owner_id': key, 'status': 'applied', 'policy': value.to_dict()})
    if any(row['status'] != 'applied' for row in results):
        partial = any(row['status'] == 'applied' or row.get('policy_committed') for row in results)
        uncertain = any(row['status'] == 'unconfirmed' for row in results)
        raise OwnerEgressPolicyError(
            'EGRESS_BATCH_PARTIAL_COMMIT' if partial else 'EGRESS_BATCH_REJECTED',
            '部分或全部用户策略未完成，逐用户结果已保留。',
            status_code=503, side_effect='unconfirmed' if partial or uncertain else 'not_started',
            next_action='按 users 中各用户的状态核对规则，仅重新提交尚未写入且已修复的用户。',
            details={'users': results},
        )
    return {'users': results}
