"""单进程有界协作租约；文档正文只沿原 owner Store 的集合 CAS 提交。"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass

from ..conversation.business_access import BusinessError
from .v13_store import v13_store_for

try:
    limits = importlib.import_module('.gap158_limits', __package__)
except ModuleNotFoundError as error:
    # 仅允许 W0 本模块尚未合入；它内部缺依赖或导入错误不能被默认值掩盖。
    if error.name != __package__ + '.gap158_limits':
        raise
    limits = None


def _bounds():
    if limits is None:
        ttl_seconds, max_members, max_bytes = 30, 50, 262144
        return ttl_seconds, max_members, max_bytes
    return (limits.PRESENCE_TTL_SECONDS, limits.PRESENCE_MAX_MEMBERS_PER_DOC,
            limits.DOC_STATE_MAX_BYTES)


def _access(call):
    # 延迟导入避免注册器加载本分片时反向读取尚未装配完成的动作表。
    from ..gateway_parts.business_api_v13 import v13_require_access
    return v13_require_access(call)


def _now(call):
    value = call.clock()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise BusinessError('HOST_NOT_READY')
    return value


def _params(call, required, optional=()):
    params = call.params
    if (type(params) is not dict or not set(required) <= params.keys()
            or params.keys() - set(required) - set(optional)):
        raise BusinessError('INVALID_SCHEMA')
    for name in ('doc_id', 'member_id'):
        if name in params and (type(params[name]) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', params[name]) is None):
            raise BusinessError('INVALID_SCHEMA')
    return params


def _write_key(call):
    if (type(call.idempotency_key) is not str
            or re.fullmatch(r'[A-Za-z0-9_-]{16,128}', call.idempotency_key) is None):
        raise BusinessError('INVALID_SCHEMA')


def _digest(action, params):
    try:
        return hashlib.sha256(json.dumps([action, params], ensure_ascii=False,
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise BusinessError('INVALID_SCHEMA') from error


def _state_copy(value):
    """严格 JSON 和 UTF-8 规范字节一起核验，不能把 tuple、非字符串键或 NaN 悄悄转换。"""
    max_bytes = _bounds()[2]
    active, visited = set(), 0

    def check(node):
        nonlocal visited
        visited += 1
        if visited > max_bytes:
            raise BusinessError('LIMIT_EXCEEDED', details={'limit': 'DOC_STATE_MAX_BYTES'})
        if node is None or type(node) in (bool, int):
            return
        if type(node) is float:
            if not math.isfinite(node):
                raise ValueError('finite JSON')
            return
        if type(node) is str:
            if len(node) > max_bytes:
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': 'DOC_STATE_MAX_BYTES'})
            return
        if type(node) not in (dict, list) or id(node) in active:
            raise ValueError('JSON tree')
        if len(node) > max_bytes:
            raise BusinessError('LIMIT_EXCEEDED', details={'limit': 'DOC_STATE_MAX_BYTES'})
        active.add(id(node))
        try:
            if type(node) is dict:
                for key, child in node.items():
                    if type(key) is not str:
                        raise ValueError('JSON key')
                    check(key)
                    check(child)
            else:
                for child in node:
                    check(child)
        finally:
            active.remove(id(node))

    try:
        check(value)
        chunks, byte_count = [], 0
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':'), allow_nan=False)
        for chunk in encoder.iterencode(value):
            byte_count += len(chunk.encode('utf-8'))
            if byte_count > max_bytes:
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': 'DOC_STATE_MAX_BYTES'})
            chunks.append(chunk)
        # 返回独立纯 JSON 值，调用方之后改动原字典不能改变本笔待提交正文。
        return json.loads(''.join(chunks))
    except BusinessError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError) as error:
        raise BusinessError('INVALID_SCHEMA') from error


@dataclass
class _Member:
    member_id: str
    subject_id: str
    role: str
    joined_at: float
    last_seen_at: float
    expires_at: float

    def view(self):
        # subject 是租约的内部绑定，不扩充 PresenceMember DTO。
        return {name: getattr(self, name) for name in
                ('member_id', 'role', 'joined_at', 'last_seen_at', 'expires_at')}


class _PresenceRegistry:
    """只枚举有界活跃集，不扫 owner 磁盘；进程重启后客户端必须重新 join。"""

    def __init__(self, *, ttl_seconds=None, max_members=None):
        ttl, members, _max_bytes = _bounds()
        self.ttl_seconds = ttl if ttl_seconds is None else ttl_seconds
        self.max_members = members if max_members is None else max_members
        # 额外维度共用成员上限的预算，避免无限创建单成员文档/owner 绕过内存界限。
        self.max_owners = self.max_docs_per_owner = self.max_members
        self.max_total_members = self.max_members * self.max_members
        self.lock = threading.RLock()
        self.owners = {}
        self.receipts = OrderedDict()
        self.total_members = 0

    def prune(self, now):
        for owner_id, docs in list(self.owners.items()):
            for doc_id, members in list(docs.items()):
                for member_id, member in list(members.items()):
                    if member.expires_at <= now:
                        del members[member_id]
                        self.total_members -= 1
                if not members:
                    del docs[doc_id]
            if not docs:
                del self.owners[owner_id]
        for key, (_digest_value, _result, expires_at) in list(self.receipts.items()):
            if expires_at <= now:
                del self.receipts[key]

    def members(self, owner_id, doc_id):
        return self.owners.get(owner_id, {}).get(doc_id, {})

    @staticmethod
    def driver(members):
        return next((member.member_id for member in members.values()
                     if member.role == 'driver'), '')

    def admit(self, owner_id, doc_id, members):
        checks = (
            (len(members) >= self.max_members, 'PRESENCE_MAX_MEMBERS_PER_DOC'),
            (self.total_members >= self.max_total_members, 'presence_total_members'),
            (owner_id not in self.owners and len(self.owners) >= self.max_owners,
             'presence_active_owners'),
            (doc_id not in self.owners.get(owner_id, {})
             and len(self.owners.get(owner_id, {})) >= self.max_docs_per_owner,
             'presence_docs_per_owner'),
        )
        for exhausted, limit in checks:
            if exhausted:
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': limit})

    def control(self, call, mutate):
        _write_key(call)
        fingerprint = _digest(call.action, call.params)
        with self.lock:
            now = _now(call)
            self.prune(now)
            _access(call)
            key = (call.owner_id, call.subject_id, call.action, call.idempotency_key)
            previous = self.receipts.get(key)
            if previous is not None:
                if previous[0] != fingerprint:
                    raise BusinessError('IDEMPOTENCY_CONFLICT')
                return json.loads(previous[1])
            # 未过期回执不能被容量淘汰，否则旧 leave 可以再次命中新 join 的租约。
            # 在变更租约前拒绝新控制，饱和错误不能留下半次副作用。
            if len(self.receipts) >= self.max_total_members:
                if call.action == 'collab_presence.join':
                    members = self.members(call.owner_id, call.params['doc_id'])
                    if call.params['member_id'] not in members:
                        self.admit(call.owner_id, call.params['doc_id'], members)
                raise BusinessError('HOST_NOT_READY')
            result = mutate(now)
            # 在线控制不落盘；回执也限 TTL/容量，不保留永久历史，不把回执当 driver 授权。
            self.receipts[key] = (fingerprint, json.dumps(result), now + self.ttl_seconds)
            return result


_presence = _PresenceRegistry()


def join(call):
    _access(call)
    params = _params(call, ('doc_id', 'member_id', 'role'),
                     ('takeover', 'expected_driver_member_id'))
    if (type(params['role']) is not str or params['role'] not in ('driver', 'navigator', 'observer')
            or type(params.get('takeover', False)) is not bool
            or ('expected_driver_member_id' in params
                and (type(params['expected_driver_member_id']) is not str
                     or len(params['expected_driver_member_id']) > 128))
            or (params.get('takeover', False) and params['role'] != 'driver')):
        raise BusinessError('INVALID_SCHEMA')
    doc_id, member_id = params['doc_id'], params['member_id']

    def mutate(now):
        members = _presence.members(call.owner_id, doc_id)
        previous = members.get(member_id)
        if previous is not None and previous.subject_id != call.subject_id:
            raise BusinessError('STATE_CONFLICT')
        driver = _presence.driver(members)
        if params['role'] == 'driver':
            if params.get('takeover', False):
                # 即使旧 driver 已过期，带着旧 ID 的接管也必须冲突，不能隐式降级成普通加入。
                if params.get('expected_driver_member_id') != driver:
                    raise BusinessError('REVISION_CONFLICT')
            elif driver and driver != member_id:
                raise BusinessError('STATE_CONFLICT')
        if previous is None:
            _presence.admit(call.owner_id, doc_id, members)
        # 容量/授权/CAS 全部核完才创建桶或降级旧 driver，失败不能留下半次接管。
        if params['role'] == 'driver' and driver and driver != member_id:
            members[driver].role = 'navigator'
        at = max(now, previous.last_seen_at) if previous is not None else now
        member = _Member(member_id, call.subject_id, params['role'],
                         previous.joined_at if previous is not None else now,
                         at, at + _presence.ttl_seconds)
        if previous is None:
            _presence.total_members += 1
        _presence.owners.setdefault(call.owner_id, {}).setdefault(doc_id, {})[member_id] = member
        return {'member': member.view(), 'driver_member_id': _presence.driver(
            _presence.members(call.owner_id, doc_id))}

    return _presence.control(call, mutate)


def heartbeat(call):
    _access(call)
    params = _params(call, ('doc_id', 'member_id'))

    def mutate(now):
        member = _presence.members(call.owner_id, params['doc_id']).get(params['member_id'])
        if member is None or member.subject_id != call.subject_id:
            raise BusinessError('RESOURCE_NOT_FOUND')
        member.last_seen_at = max(now, member.last_seen_at)
        member.expires_at = member.last_seen_at + _presence.ttl_seconds
        return {'member': member.view()}

    return _presence.control(call, mutate)


def leave(call):
    _access(call)
    params = _params(call, ('doc_id', 'member_id'))

    def mutate(now):
        members = _presence.members(call.owner_id, params['doc_id'])
        member = members.get(params['member_id'])
        if member is None or member.subject_id != call.subject_id:
            return {'left': False}
        del members[params['member_id']]
        _presence.total_members -= 1
        _presence.prune(now)
        return {'left': True}

    return _presence.control(call, mutate)


def list_members(call):
    _access(call)
    params = _params(call, ('doc_id',))
    with _presence.lock:
        _presence.prune(_now(call))
        _access(call)
        members = _presence.members(call.owner_id, params['doc_id'])
        return {'members': [members[key].view() for key in sorted(members)],
                'driver_member_id': _presence.driver(members)}


def doc_state_get(call):
    _access(call)
    doc_id = _params(call, ('doc_id',))['doc_id']
    store = v13_store_for(call, 'owner')
    # 记录与集合修订同次读取；分开 get/revision 会把旧正文和新 CAS 号拼在一起。
    row, revision = store._record_view('collab_doc_state', doc_id)
    result = {'doc_id': doc_id, 'revision': revision}
    if row is not None and row.get('owner_id') == call.owner_id:
        result.update({name: row[name] for name in ('state', 'updated_by', 'updated_at')})
    _access(call)
    return result


def _require_driver(call, doc_id, member_id):
    _access(call)
    member = _presence.members(call.owner_id, doc_id).get(member_id)
    if (member is None or member.subject_id != call.subject_id or member.role != 'driver'
            or member.expires_at <= _now(call)):
        raise BusinessError('VERIFICATION_FAILED')
    return True


def doc_state_propose(call):
    _access(call)
    params = _params(call, ('doc_id', 'member_id', 'state'))
    _write_key(call)
    if type(call.expected_revision) is not int or call.expected_revision < 0:
        raise BusinessError('INVALID_SCHEMA')
    state = _state_copy(params['state'])
    doc_id, member_id = params['doc_id'], params['member_id']
    fingerprint = _digest(call.action, {'doc_id': doc_id, 'member_id': member_id, 'state': state})
    with _presence.lock:
        _presence.prune(_now(call))
        _require_driver(call, doc_id, member_id)
        store = v13_store_for(call, 'owner')
        # 全部路径固定 presence → quota → Store；锁跨越原提交点，接管/leave 不能穿过授权窗口。
        # TTL 和实时 access 仍在原 Store 最后一次 authorize 复核，排队期间到期也不得提交。
        store.authorize = lambda: _require_driver(call, doc_id, member_id)

        def mutate(rows):
            _require_driver(call, doc_id, member_id)
            # 原 Store 是整集合快照；持久文档数也共用有界预算，不能随使用年限无限扩大。
            if doc_id not in rows and len(rows) >= _presence.max_docs_per_owner:
                raise BusinessError('LIMIT_EXCEEDED', details={'limit': 'doc_state_docs_per_owner'})
            rows[doc_id] = {'doc_id': doc_id, 'owner_id': call.owner_id, 'state': state,
                            'updated_by': member_id, 'updated_at': _now(call)}
            return {'doc_id': doc_id, 'revision': call.expected_revision + 1}

        return store.transaction('collab_doc_state', mutate, idempotency_key=call.idempotency_key,
                                 params_digest=fingerprint, expected_revision=call.expected_revision)
