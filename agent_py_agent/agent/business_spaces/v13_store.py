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
from ..conversation.business_receipt_rows import discard_pending_candidates
from ..user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .v13_history_rows import HistoryRows, TouchedRows

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

    # LLM: 缺失元数据必须与已有历史区别；读者采样journal后核缺失，写者在锁内立即核验。
    # 函数用途: 读取校验集合状态，拒绝把损坏库重置成空库。
    def _read_file(self, collection, *, allow_missing=False):
        raw = read_bytes_beneath(self.owner_home, ('data', 'business_v13', collection + '.json'))
        if raw is None:
            if not allow_missing and self._rows(collection).has_history():
                raise BusinessError('STORE_UNAVAILABLE')
            return {'schema_version': 'business-v13.v1', 'owner_id': self.owner_id,
                    'collection': collection, 'revision': 0, 'records': {}, 'receipts': {},
                    **({'_missing_snapshot': True} if allow_missing else {})}
        try:
            state = json.loads(raw)
            self._validate_state(state, collection)
            return state
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 持久边界同时校验旧快照与新行日志；业务 schema 仍由拥有集合的分片维护。
    # 函数用途: 检查状态、增量删除标记与回执结构。
    def _validate_state(self, state, collection):
        if (type(state) is not dict or state.get('schema_version') != 'business-v13.v1'
                or state.get('owner_id') != self.owner_id or state.get('collection') != collection
                or type(state.get('revision')) is not int or state['revision'] < 0
                or type(state.get('records')) is not dict or type(state.get('receipts')) is not dict
                or any(type(k) is not str or not k or type(v) is not dict for k, v in state['records'].items())):
            raise ValueError('state')
        if ('history_rows' in state and type(state['history_rows']) is not bool
                or 'deleted_records' in state and (type(state['deleted_records']) is not list
                    or any(type(key) is not str or not key for key in state['deleted_records']))):
            raise ValueError('row state')
        for key, receipt in state['receipts'].items():
            if (type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None or type(receipt) is not dict
                    or set(receipt) != {'fingerprint', 'result'} or type(receipt['result']) is not dict
                    or type(receipt['fingerprint']) is not str
                    or re.fullmatch(r'[0-9a-f]{64}', receipt['fingerprint']) is None):
                raise ValueError('receipt')

    # LLM: 永久运行及审计按键物化；其它自然有界实体仍保留小快照。
    # 函数用途: 判断哪些集合使用惰性目标记录。
    def _incremental(self, collection):
        return collection in {'script_runs', 'async_jobs', 'audit_log'}

    # LLM: 调用方已经核验集合 scope，行封套再次绑定准确 owner。
    # 函数用途: 获取没有持久连接的逐键历史后端。
    def _rows(self, collection):
        return HistoryRows(self.owner_home, self.owner_id, collection)

    # LLM: 新格式元数据不携带永久回执；记录增量只存在于同一 transaction journal。
    # 函数用途: 从已提交的日志状态生成有界物化快照。
    def _snapshot(self, state):
        return {key: value for key, value in state.items() if key != 'deleted_records'} | {
            'history_rows': True, 'receipts': {},
            'records': {} if self._incremental(state['collection']) else state['records']}

    # LLM: 只在 quota→目录锁内迁移；内部审计路径不走公开 scope 检查，每批先准入再 checkpoint。
    # 函数用途: 可中断续传地迁移旧回执和永久记录，保留原指纹、修订及全部证明。
    def _prepare(self, collection, admission):
        state = self._read_file(collection)
        if state.get('history_rows') is True and not state['receipts']:
            return state
        entries = [('receipts', key, value) for key, value in sorted(state['receipts'].items())]
        if self._incremental(collection):
            entries.extend(('records', key, value) for key, value in sorted(state['records'].items()))
        if entries:
            rows = self._rows(collection)
            checkpoint = ('data', 'business_v13', '.migrate-' + collection + '.json')
            digest = hashlib.sha256(_encode(state)).hexdigest()
            raw = read_bytes_beneath(self.owner_home, checkpoint)
            offset = 0
            if raw is not None:
                try:
                    saved = json.loads(raw)
                    if (type(saved) is not dict or saved.get('owner_id') != self.owner_id
                            or saved.get('source_digest') != digest or type(saved.get('offset')) is not int
                            or not 0 <= saved['offset'] <= len(entries)):
                        raise ValueError('checkpoint')
                    offset = saved['offset']
                except (ValueError, TypeError, KeyError) as error:
                    raise BusinessError('STORE_UNAVAILABLE') from error
            for start in range(0, len(entries), 256):
                end = min(start + 256, len(entries))
                payloads = []
                for position in range(start, end):
                    kind, key, value = entries[position]
                    if position < offset:
                        existing = rows.get(kind, key)
                        if existing == value:
                            continue
                        if existing is not None:
                            raise BusinessError('STORE_UNAVAILABLE')
                    payloads.append(rows.payload(kind, key, value))
                if not payloads:
                    continue
                payloads.append((checkpoint, _encode({'owner_id': self.owner_id,
                    'source_digest': digest, 'offset': max(offset, end)})))
                self._write_payloads(payloads, admission)
        # 新空集合无需先写元数据；有旧内容才做最后切换，避免改变首次 journal 提交语义。
        snapshot = self._snapshot(state)
        if entries:
            path = ('data', 'business_v13', collection + '.json')
            self._write_payloads([(path, _encode(snapshot))], admission)
        return snapshot

    # LLM: 精确回收已校验旧源/日志的目标候选后再检查配额；不把候选当作已提交事实。
    # 函数用途: 按同一个准入上下文恢复并写入有界物化批次。
    def _write_payloads(self, payloads, admission):
        discard_pending_candidates(self.owner_home, payloads, admission)
        admission.check([OwnerQuotaChange(self.owner_home.joinpath(*path), final_size=len(raw))
                         for path, raw in payloads])
        for path, raw in payloads:
            write_bytes_atomic_beneath(self.owner_home, path, raw)

    # LLM: 兼容旧全快照 journal；新 journal 只物化本笔回执、记录 delta 和审计行。
    # 函数用途: 生成一次提交的全部可幂等滚前写入，不增加第二个提交点。
    def _materialized(self, pending):
        payloads = []
        for collection, state in (*sorted(pending['states'].items()), ('audit_log', pending['audit'])):
            path = ('data', 'business_v13', collection + '.json')
            if state.get('history_rows') is not True:
                payloads.append((path, _encode(state)))
                continue
            rows = self._rows(collection)
            payloads.extend(rows.payload('receipts', key, value) for key, value in state['receipts'].items())
            if self._incremental(collection):
                payloads.extend(rows.payload('records', key, value) for key, value in state['records'].items())
                payloads.extend(rows.payload('records', key, None) for key in state.get('deleted_records', []))
            payloads.append((path, _encode(self._snapshot(state))))
        return payloads

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

    # LLM: 前后读取同一个提交点；历史映射只加载实际访问的目标，日志覆盖仍是唯一已提交视图。
    # 函数用途: 取得包含未完成物化日志的逻辑集合状态，不恢复写盘。
    def _read(self, collection):
        self._collection(collection)
        for attempt in range(3):
            before = self._pending()
            state = self._read_file(collection, allow_missing=True)
            after = self._pending()
            if before == after:
                if after:
                    state = after['audit'] if collection == 'audit_log' else after['states'].get(collection, state)
                if state.pop('_missing_snapshot', False) and self._rows(collection).has_history():
                    raise BusinessError('STORE_UNAVAILABLE')
                if state.get('history_rows') is True and self._incremental(collection):
                    overlay = dict(state['records'])
                    overlay.update(dict.fromkeys(state.get('deleted_records', [])))
                    state = {**state, 'records': TouchedRows(self._rows(collection), overlay=overlay)}
                return state
        raise BusinessError('HOST_NOT_READY')

    # LLM: 回调由可信组合根绑定，每次提交前重查实时权限。
    # 函数用途: 执行可选的写入末核，拒绝失效授权。
    def _authorize(self):
        if self.authorize is not None and self.authorize() is not True:
            raise BusinessError('CAPABILITY_DENIED')

    # LLM: 固定journal叶子的未发布候选可在同锁内回收；只有校验通过的正式日志允许滚前。
    # 函数用途: 回收发布前孤儿并恢复上一笔提交，不重做mutate或豁免quota。
    def _recover(self, admission):
        pending = self._pending()
        journal = ('data', 'business_v13', '.transaction.json')
        discard_pending_candidates(self.owner_home, [(journal, b'')], admission)
        if not pending:
            return
        payloads = self._materialized(pending)
        self._write_payloads(payloads, admission)
        write_bytes_atomic_beneath(self.owner_home, journal, _encode({}))

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

    # LLM: 同一日志提交修订、逐键回执、目标 delta 及本笔审计；重放先点查回执，不加载历史运行。
    # 函数用途: 在原 quota 和目录锁内原子变更多集合，保留 CAS 与物化中断的逻辑一致性。
    def transaction_many(self, collections, mutate, *, idempotency_key, params_digest,
                         expected_revision=None, receipt_view=None):
        if self._readonly:
            raise BusinessError('CAPABILITY_DENIED')
        if (type(collections) not in (tuple, list) or not collections
                or any(type(name) is not str for name in collections)
                or len(set(collections)) != len(collections)):
            raise BusinessError('INVALID_SCHEMA')
        names = sorted(collections)
        for name in names:
            self._collection(name)
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
                states = {name: self._prepare(name, admission) for name in names}
                receipts = [self._rows(name).get('receipts', key) for name in names]
                if any(receipt is not None for receipt in receipts):
                    if any(receipt is None or receipt['fingerprint'] != fingerprint for receipt in receipts):
                        raise BusinessError('IDEMPOTENCY_CONFLICT')
                    if any(receipt['result'] != receipts[0]['result'] for receipt in receipts):
                        raise BusinessError('STORE_UNAVAILABLE')
                    self._authorize()
                    return _copy(receipts[0]['result'])
                for name, state in states.items():
                    if revisions[name] is not None and state['revision'] != revisions[name]:
                        raise BusinessError('REVISION_CONFLICT')
                before = {name: _copy(state['records']) for name, state in states.items()
                          if not self._incremental(name)}
                records = {name: TouchedRows(self._rows(name)) if self._incremental(name) else state['records']
                           for name, state in states.items()}
                original_records = dict(records)
                result = mutate(records)
                if set(records) != set(names):
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                if type(result) is not dict:
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                result = _copy(result)
                receipt_result = result if receipt_view is None else receipt_view(_copy(result))
                if type(receipt_result) is not dict:
                    raise BusinessError('DOMAIN_RESULT_INVALID')
                audit = self._prepare('audit_log', admission)
                at = _timestamp(self.clock())
                audit['revision'] += 1
                for name, state in states.items():
                    if self._incremental(name):
                        if records[name] is original_records[name]:
                            delta = records[name].delta()
                        else:
                            if (type(records[name]) is not dict or any(
                                    type(item) is not str or not item or type(value) is not dict
                                    for item, value in records[name].items())):
                                raise BusinessError('DOMAIN_RESULT_INVALID')
                            # 显式整体替换才枚举，前像必须取变更前的 before 而非已经改过的 cache。
                            list(original_records[name])
                            previous = original_records[name].before
                            delta = {item: records[name].get(item) for item in previous.keys() | records[name].keys()
                                     if previous.get(item) != records[name].get(item)}
                        state['records'] = {item: value for item, value in delta.items() if value is not None}
                        state['deleted_records'] = [item for item, value in delta.items() if value is None]
                        changed = sorted(delta)
                    else:
                        state['records'] = records[name]
                        changed = sorted(k for k in before[name].keys() | state['records'].keys()
                                         if before[name].get(k) != state['records'].get(k))
                    state['revision'] += 1
                    state['receipts'][key] = {'fingerprint': fingerprint, 'result': _copy(receipt_result)}
                    try:
                        self._validate_state(state, name)
                    except (ValueError, TypeError, KeyError) as error:
                        raise BusinessError('DOMAIN_RESULT_INVALID') from error
                    # 审计单行受100个ID上限约束；所有分段仍在同一journal与quota提交内，不拆产品事务。
                    for offset in range(0, max(1, len(changed)), 100):
                        entry_id = 'audit-' + uuid.uuid4().hex
                        audit['records'][entry_id] = {'entry_id': entry_id, 'owner_id': self.owner_id,
                            'at': at, 'subject_id': self.subject_id, 'action': self.action or name,
                            'collection': name, 'record_ids': changed[offset:offset + 100], 'outcome': 'committed',
                            'revision': state['revision'], 'created_at': at, 'updated_at': at}
                pending = {'states': states, 'audit': audit}
                journal_parts = ('data', 'business_v13', '.transaction.json')
                materialized = self._materialized(pending)
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

    # LLM: 惰性行在读取后再次核集合修订，正文与用于游标的 revision 必须一起返回。
    # 函数用途: 读取一个一致的记录值或完整列表及其同笔修订。
    def _record_view(self, collection, record_id, *, all_rows=False):
        for _attempt in range(3):
            state = self._read(collection)
            records = state['records']
            result = ([_copy(records[key]) for key in sorted(records)] if all_rows
                      else _copy(records.get(record_id)))
            if not isinstance(records, TouchedRows) or state['revision'] == self._read(collection)['revision']:
                return result, state['revision']
        raise BusinessError('HOST_NOT_READY')

    # LLM: 排序按记录键，显式全列表才枚举历史；返回副本，不创建目录或回执。
    # 函数用途: 读取集合全部记录并复核惰性视图的修订。
    def list(self, collection):
        return self._record_view(collection, None, all_rows=True)[0]

    # LLM: 点查只加载准确目标行，不读取旧运行；记录与元数据必须属于同一逻辑修订。
    # 函数用途: 读取指定记录，缺失返回 None。
    def get(self, collection, record_id):
        return self._record_view(collection, record_id)[0]

    # LLM: 修订由集合状态提供，不另建索引。
    # 函数用途: 返回集合级 CAS 版本。
    def revision(self, collection):
        return self._read(collection)['revision']

    # LLM: 精确筛选与一致读取后核验的修订共同绑定游标，不能先取旧修订再拼接新行。
    # 函数用途: 分页读取同一逻辑提交的集合内容。
    def page(self, collection, *, cursor=None, limit=50, filters=None):
        filters = {} if filters is None else filters
        if type(filters) is not dict:
            raise BusinessError('INVALID_SCHEMA')
        records, revision = self._record_view(collection, None, all_rows=True)
        rows = [row for row in records
                if all(row.get(k) == v for k, v in filters.items())]
        return _page_rows(rows, [str(self.owner_home), self.owner_id, self.subject_id, collection,
                                revision, filters, rows], cursor=cursor, limit=limit)


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
