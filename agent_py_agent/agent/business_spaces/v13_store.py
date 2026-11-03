"""V13 owner 集合的耐久事务、同笔回执与只读分页。

写入沿 quota→目录锁→no-follow 链；单一提交日志把集合和审计绑定为同笔提交，
替换中断后读者使用日志中的已提交视图，下一位写者只做幂等滚前。
本模块不创建 Agent、不授予角色，记录级权限仍由拥有集合的分片核验。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import read_bytes_beneath, write_bytes_atomic_beneath
from ..conversation.business_access import BusinessError
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy

# 只保护分页，不是授权或持久状态；进程重启后旧游标明确过期。
_CURSOR_KEY = os.urandom(32)


# LLM: 严格 JSON 同时拒绝不可序列化结果和非有限数字。
# 函数用途: 生成稳定的私有 JSON 字节，用于指纹和原子写。
def _encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


# LLM: 不把可变的调用方字典暴露为持久状态或回执引用。
# 函数用途: 复制纯 JSON 值。
def _copy(value):
    return json.loads(_encode(value))


# LLM: 根仅由可信组合根传入；路径推导不等于授权。
# 函数用途: 从规范 owner 路径推导编号，非规范测试根使用目录名。
def _owner_id(owner_home):
    path = Path(owner_home)
    if path.parent.name == 'local' and path.parent.parent.name == 'owners':
        return 'local/' + path.name
    if (path.parent.name in ('users', 'groups') and path.parents[2].name == 'providers'
            and path.parents[3].name == 'owners'):
        return '/'.join(path.parts[-4:])
    return path.name


# LLM: 时间来自注入时钟，统一输出可排序的 UTC 时间。
# 函数用途: 把 epoch 秒转为审计时间。
def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


# LLM: 绑定范围、修订、筛选和可见行；游标内容不授予访问权限。
# 函数用途: 在进程内跨 Store 实例安全分页，拒绝篡改或过期游标。
def _page_rows(rows, binding_context, *, cursor=None, limit=50):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise BusinessError('INVALID_SCHEMA')
    binding = hashlib.sha256(_encode(binding_context)).hexdigest()
    position = 0
    if cursor is not None:
        try:
            if type(cursor) is not str or len(cursor) > 4096:
                raise ValueError('cursor')
            token = json.loads(base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True))
            if (type(token) is not dict or set(token) != {'binding', 'position', 'signature'}
                    or token['binding'] != binding or type(token['position']) is not int
                    or not 0 <= token['position'] <= len(rows) or type(token['signature']) is not str):
                raise ValueError('cursor')
            signature = hmac.digest(_CURSOR_KEY, _encode([binding, token['position']]), 'sha256').hex()
            if not hmac.compare_digest(signature, token['signature']):
                raise ValueError('cursor')
            position = token['position']
        except (ValueError, UnicodeError, TypeError, KeyError) as error:
            raise BusinessError('CURSOR_EXPIRED') from error
    end = min(position + limit, len(rows))
    next_cursor = None
    if end < len(rows):
        signature = hmac.digest(_CURSOR_KEY, _encode([binding, end]), 'sha256').hex()
        next_cursor = base64.urlsafe_b64encode(_encode(
            {'binding': binding, 'position': end, 'signature': signature})).decode('ascii')
    return {'items': _copy(rows[position:end]), 'next_cursor': next_cursor,
            'coverage': 'partial' if next_cursor else 'complete'}


class V13Store:
    """宿主显式绑定 owner；transaction 的 CAS 针对整个集合修订。"""

    # LLM: 构造仅绑定位置和实时末核回调，不读盘或写盘。
    # 函数用途: 创建 owner 存储句柄。
    def __init__(self, owner_home, *, owner_id=None, subject_id='host', action='',
                 clock=time.time, authorize=None):
        self.owner_home = Path(os.path.abspath(owner_home))
        self.owner_id = _owner_id(self.owner_home) if owner_id is None else owner_id
        if (type(self.owner_id) is not str or not self.owner_id or type(subject_id) is not str
                or not subject_id or not callable(clock) or (authorize is not None and not callable(authorize))):
            raise BusinessError('ACTOR_REQUIRED')
        self.subject_id, self.action = subject_id, action
        self.clock, self.authorize = clock, authorize
        self._readonly = False
        self._scope = 'owner'

    # LLM: 跨 owner 只读句柄没有准入、锁或回执写入路径。
    # 函数用途: 创建拒绝所有写操作的存储句柄。
    @classmethod
    def read_only(cls, owner_home):
        store = cls(owner_home)
        store._readonly = True
        return store

    # LLM: 主体与集合 scope 在操作时核，不只在工厂构造时核。
    # 函数用途: 验证集合名和注册 scope，阻止路径穿越或选错 home。
    def _collection(self, collection):
        if type(collection) is not str or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', collection) is None:
            raise BusinessError('INVALID_SCHEMA')
        from ..gateway_parts.business_api_v13 import COLLECTIONS
        scope = COLLECTIONS.get(collection, 'owner')
        if scope != self._scope:
            raise BusinessError('BUSINESS_ACCESS_DENIED')
        return ('data', 'business_v13', collection + '.json')

    # LLM: 读取不建目录，坏 JSON 或跨 owner 文件不能变成空成功。
    # 函数用途: 读取并校验集合状态。
    def _read_file(self, collection):
        raw = read_bytes_beneath(self.owner_home, ('data', 'business_v13', collection + '.json'))
        if raw is None:
            return {'schema_version': 'business-v13.v1', 'owner_id': self.owner_id,
                    'collection': collection, 'revision': 0, 'records': {}, 'receipts': {}}
        try:
            state = json.loads(raw)
            self._validate_state(state, collection)
            return state
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 持久边界只接受宿主形状，记录业务 schema 由拥有集合的分片维护。
    # 函数用途: 检查状态与回执结构。
    def _validate_state(self, state, collection):
        if (type(state) is not dict or state.get('schema_version') != 'business-v13.v1'
                or state.get('owner_id') != self.owner_id or state.get('collection') != collection
                or type(state.get('revision')) is not int or state['revision'] < 0
                or type(state.get('records')) is not dict or type(state.get('receipts')) is not dict
                or any(type(k) is not str or not k or type(v) is not dict for k, v in state['records'].items())):
            raise ValueError('state')
        for key, receipt in state['receipts'].items():
            if (type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None or type(receipt) is not dict
                    or set(receipt) != {'fingerprint', 'result'} or type(receipt['result']) is not dict
                    or type(receipt['fingerprint']) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', receipt['fingerprint']) is None):
                raise ValueError('receipt')

    # LLM: 日志是唯一跨文件提交点，读取只投影，不执行恢复写入。
    # 函数用途: 读取未完成物化的已提交集合与审计快照。
    def _pending(self):
        raw = read_bytes_beneath(self.owner_home, ('data', 'business_v13', '.transaction.json'))
        if raw is None:
            return {}
        try:
            pending = json.loads(raw)
            if pending == {}:
                return {}
            if type(pending) is not dict:
                raise ValueError('journal')
            # 旧单集合提交点沿同一日志读取，不产生第二份权威。
            if set(pending) == {'collection', 'state', 'audit'}:
                pending = {'states': {pending['collection']: pending['state']}, 'audit': pending['audit']}
            if (set(pending) != {'states', 'audit'} or type(pending['states']) is not dict
                    or not pending['states']):
                raise ValueError('journal')
            for collection, state in pending['states'].items():
                if (type(collection) is not str or collection == 'audit_log'
                        or re.fullmatch(r'[a-z][a-z0-9_]{0,63}', collection) is None):
                    raise ValueError('journal')
                self._validate_state(state, collection)
            self._validate_state(pending['audit'], 'audit_log')
            return pending
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 前后读取同一个提交点，若并发提交则重采而不返回跨版本拼接。
    # 函数用途: 取得包含已提交日志覆盖的逻辑集合状态。
    def _read(self, collection):
        self._collection(collection)
        for attempt in range(3):
            before = self._pending()
            state = self._read_file(collection)
            after = self._pending()
            if before == after:
                if after and collection in after['states']:
                    return _copy(after['states'][collection])
                if after and collection == 'audit_log':
                    return _copy(after['audit'])
                return state
        raise BusinessError('HOST_NOT_READY')

    # LLM: 回调由可信组合根绑定，每次提交前重查实时权限。
    # 函数用途: 执行可选的写入末核，拒绝失效授权。
    def _authorize(self):
        if self.authorize is not None and self.authorize() is not True:
            raise BusinessError('CAPABILITY_DENIED')

    # LLM: 仅持 quota 与目录锁的写者恢复已提交日志，不重做 mutate 或返回未脱敏结果。
    # 函数用途: 幂等物化上一笔已提交事务。
    def _recover(self, admission):
        pending = self._pending()
        if not pending:
            return
        entries = (*sorted(pending['states'].items()), ('audit_log', pending['audit']))
        changes = [OwnerQuotaChange(self.owner_home / 'data' / 'business_v13' / (name + '.json'),
                                   final_size=len(_encode(state))) for name, state in entries]
        admission.check(changes)
        for name, state in entries:
            write_bytes_atomic_beneath(self.owner_home, ('data', 'business_v13', name + '.json'), _encode(state))
        write_bytes_atomic_beneath(self.owner_home, ('data', 'business_v13', '.transaction.json'), _encode({}))

    # LLM: 幂等键按主体和动作隔离，参数摘要与 CAS 都参与指纹；第一次响应和回执分别复制。
    # 函数用途: 在单笔锁内变更集合、保存回执并追加审计，返回 mutate 的字典结果。
    def transaction(self, collection, mutate, *, idempotency_key, params_digest,
                    expected_revision=None, receipt_view=None):
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise BusinessError('INVALID_SCHEMA')
        if not callable(mutate):
            raise BusinessError('INVALID_SCHEMA')

        # LLM: 单集合入口只适配参数，提交逻辑与多集合完全共用。
        # 函数用途: 把集合记录传给原 mutate。
        def single(records):
            return mutate(records[collection])

        return self.transaction_many((collection,), single, idempotency_key=idempotency_key,
            params_digest=params_digest, expected_revision=expected_revision, receipt_view=receipt_view)

    # LLM: 所有集合、回执和审计沿同一个日志提交点；物化中断不会暴露半笔逻辑视图。
    # 函数用途: 在同一 quota 和目录锁内原子变更多集合；CAS 可为共同整数或逐集合修订映射。
    def transaction_many(self, collections, mutate, *, idempotency_key, params_digest,
                         expected_revision=None, receipt_view=None):
        if self._readonly:
            raise BusinessError('CAPABILITY_DENIED')
        if (type(collections) not in (tuple, list) or not collections
                or any(type(name) is not str for name in collections)
                or len(set(collections)) != len(collections)):
            raise BusinessError('INVALID_SCHEMA')
        names = sorted(collections)
        parts = {name: self._collection(name) for name in names}
        if 'audit_log' in names:
            raise BusinessError('CAPABILITY_DENIED')
        revisions = dict.fromkeys(names, expected_revision)
        if type(expected_revision) is dict:
            if set(expected_revision) != set(names):
                raise BusinessError('INVALID_SCHEMA')
            revisions = dict(expected_revision)
        if (type(idempotency_key) is not str or not idempotency_key or len(idempotency_key) > 128
                or type(params_digest) is not str or not params_digest or not callable(mutate)
                or any(value is not None and (type(value) is not int or value < 0) for value in revisions.values())
                or (receipt_view is not None and not callable(receipt_view))):
            raise BusinessError('INVALID_SCHEMA')
        key = hashlib.sha256(_encode([self.subject_id, self.action, idempotency_key])).hexdigest()
        # 保持原单集合回执指纹稳定；多集合名称按排序后的集合绑定。
        identity = names[0] if len(names) == 1 else names
        fingerprint = hashlib.sha256(_encode([identity, self.action, params_digest, expected_revision])).hexdigest()
        self._authorize()
        gate = owner_quota_enforcer_from_policy(self.owner_home)
        with gate.admission() as admission:
            with locked_private_directory(self.owner_home, relative_parts=('data', 'business_v13'), lock_name='.store.lock'):
                self._authorize()
                self._recover(admission)
                states = {name: self._read_file(name) for name in names}
                receipts = [state['receipts'].get(key) for state in states.values()]
                if any(receipt is not None for receipt in receipts):
                    if any(receipt is None or receipt['fingerprint'] != fingerprint for receipt in receipts):
                        raise BusinessError('IDEMPOTENCY_CONFLICT')
                    if any(receipt['result'] != receipts[0]['result'] for receipt in receipts):
                        raise BusinessError('STORE_UNAVAILABLE')
                    return _copy(receipts[0]['result'])
                for name, state in states.items():
                    if revisions[name] is not None and state['revision'] != revisions[name]:
                        raise BusinessError('REVISION_CONFLICT')
                before = {name: _copy(state['records']) for name, state in states.items()}
                records = {name: state['records'] for name, state in states.items()}
                result = mutate(records)
                if set(records) != set(names):
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                if type(result) is not dict:
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                result = _copy(result)
                receipt_result = result if receipt_view is None else receipt_view(_copy(result))
                if type(receipt_result) is not dict:
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                audit = self._read_file('audit_log')
                at = _timestamp(self.clock())
                audit['revision'] += 1
                for name, state in states.items():
                    state['records'] = records[name]
                    state['revision'] += 1
                    state['receipts'][key] = {'fingerprint': fingerprint, 'result': _copy(receipt_result)}
                    try:
                        self._validate_state(state, name)
                    except (ValueError, TypeError, KeyError) as error:
                        raise BusinessError('DOMAIN_RESULT_INVALID') from error
                    changed = sorted(k for k in before[name].keys() | state['records'].keys()
                                     if before[name].get(k) != state['records'].get(k))
                    # 审计单行受100个ID上限约束；所有分段仍在同一journal与quota提交内，不拆产品事务。
                    for offset in range(0, max(1, len(changed)), 100):
                        entry_id = 'audit-' + uuid.uuid4().hex
                        audit['records'][entry_id] = {'entry_id': entry_id, 'owner_id': self.owner_id,
                            'at': at, 'subject_id': self.subject_id, 'action': self.action or name,
                            'collection': name, 'record_ids': changed[offset:offset + 100], 'outcome': 'committed',
                            'revision': state['revision'], 'created_at': at, 'updated_at': at}
                pending = {'states': states, 'audit': audit}
                journal_parts = ('data', 'business_v13', '.transaction.json')
                materialized = [(parts[name], _encode(state)) for name, state in states.items()]
                materialized.append((('data', 'business_v13', 'audit_log.json'), _encode(audit)))
                payloads = [*materialized, (journal_parts, _encode(pending))]
                admission.check([OwnerQuotaChange(self.owner_home.joinpath(*path), final_size=len(raw))
                                 for path, raw in payloads])
                self._authorize()
                # 日志替换即提交；此后的异常不能谎称未开始，同键可安全重放。
                write_bytes_atomic_beneath(self.owner_home, journal_parts, _encode(pending))
                for path, raw in materialized:
                    write_bytes_atomic_beneath(self.owner_home, path, raw)
                write_bytes_atomic_beneath(self.owner_home, journal_parts, _encode({}))
                return result

    # LLM: 排序按记录键，返回副本，不为读取创建目录或回执。
    # 函数用途: 读取集合全部记录。
    def list(self, collection):
        records = self._read(collection)['records']
        return [_copy(records[key]) for key in sorted(records)]

    # LLM: 不把缺记录伪装成跨 owner 成功。
    # 函数用途: 读取指定记录，缺失返回 None。
    def get(self, collection, record_id):
        row = self._read(collection)['records'].get(record_id)
        return None if row is None else _copy(row)

    # LLM: 修订由集合状态提供，不另建索引。
    # 函数用途: 返回集合级 CAS 版本。
    def revision(self, collection):
        return self._read(collection)['revision']

    # LLM: 精确字段筛选参与游标绑定；变更筛选、主体、home 或修订使游标失效。
    # 函数用途: 分页读取集合。
    def page(self, collection, *, cursor=None, limit=50, filters=None):
        filters = {} if filters is None else filters
        if type(filters) is not dict:
            raise BusinessError('INVALID_SCHEMA')
        state = self._read(collection)
        rows = [_copy(row) for key, row in sorted(state['records'].items())
                if all(row.get(k) == v for k, v in filters.items())]
        return _page_rows(rows, [str(self.owner_home), self.owner_id, self.subject_id, collection,
                                state['revision'], filters, rows], cursor=cursor, limit=limit)


# LLM: scope 只选注册集合的存放 home，不替分片完成记录级授权。
# 函数用途: 为实时 V13 调用绑定 owner 或 main Store 和提交末核。
def v13_store_for(call, scope='owner'):
    from ..gateway_parts.business_api_v13 import v13_require_access

    v13_require_access(call)
    if scope not in ('owner', 'main'):
        raise BusinessError('INVALID_SCHEMA')
    home = call.owner_home
    owner_id = call.owner_id
    if scope == 'main':
        from ..user_space.owner_resolver import resolve_owner_home
        paths = call.context.host.home_paths
        home = resolve_owner_home(paths.root).home_dir
        owner_id = 'local/main'

    # LLM: 核原调用 owner，而不是用 main 存储的 owner 冒充当前用户。
    # 函数用途: 在事务提交前重新检查原宿主上下文。
    def authorize():
        return v13_require_access(call)

    store = V13Store(home, owner_id=owner_id, subject_id=call.subject_id,
                     action=call.action, clock=call.clock, authorize=authorize)
    store._scope = scope
    return store
