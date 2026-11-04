"""有限业务目录的永久逐键回执和单一提交日志。

LLM: 所有写口只由 BusinessSpaceService 在原 quota→目录锁内调用；回执唯一键
由 owner 私有路径与同锁内不可改写检查共同约束。读取不恢复、不创建任何文件。
"""
from __future__ import annotations

import hashlib
import json
import re

from ..common.nofollow_fs import (
    list_names_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)
from ..user_space.owner_quota import OwnerQuotaChange
from .business_access import BusinessError


# LLM: 保留目录原有严格 JSON 编码及有限数值约束。
# 函数用途: 编码小快照、单条回执与检查点。
def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


# LLM: 仅在quota→领域锁内对固定journal或已校验旧源/日志的准确叶子调用；不删正式行。
# 函数用途: 回收发布前或物化时崩溃留下的候选，沿原配额协议结算释放字节。
def discard_pending_candidates(root, payloads, admission):
    parents = {}
    for parts, _raw in payloads:
        parents.setdefault(parts[:-1], set()).add(parts[-1])
    candidates = []
    for parent, leaves in parents.items():
        for name in list_names_beneath(root, parent):
            match = re.fullmatch(r'\.(.+)\.tmp-[0-9a-f]{32}', name)
            if match is not None and match.group(1) in leaves:
                candidates.append((*parent, name))
    if candidates:
        admission.check([OwnerQuotaChange(root.joinpath(*parts), final_size=None) for parts in candidates])
        for parts in candidates:
            unlink_file_beneath(root, parts)


# LLM: 行内再封 owner/key，唯一键以原 actor+idempotency 摘要定位；不允许覆盖不同的历史证明。
# 类用途: 为原业务目录提供逐键回执及可幂等恢复的物化协议。
class ReceiptRows:
    # LLM: 构造只保存原服务引用，实体 shape 校验仍由原服务承担。
    # 函数用途: 绑定准确 owner 和原 catalog 路径。
    def __init__(self, service):
        self.service, self.root, self.owner = service, service.owner_root, service.owner_id
        self.journal = ('data', 'business', '.catalog-transaction.json')

    # LLM: 仅接受原规范摘要，输入原文不进入文件路径。
    # 函数用途: 返回一条回执的私有叶子路径。
    def parts(self, key):
        if type(key) is not str or re.fullmatch(r'[0-9a-f]{64}', key) is None:
            raise BusinessError('STORE_UNAVAILABLE')
        return ('data', 'business', 'receipts', key + '.json')

    # LLM: 只看固定目录的直接子项，不枚举永久回执行。
    # 函数用途: 缺失catalog时辨别已有库，避免成功回执与空目录并存。
    def has_history(self):
        return bool({'receipts', '.receipt-migration.json'} & set(
            list_names_beneath(self.root, ('data', 'business'))))

    # LLM: 永久回执严格保留原 fingerprint/result；坏值不能当作没有回执重新执行。
    # 函数用途: 校验单条原目录回执。
    def validate_receipt(self, receipt):
        if (type(receipt) is not dict or set(receipt) != {'fingerprint', 'result'}
                or type(receipt['result']) is not dict or type(receipt['fingerprint']) is not str
                or re.fullmatch(r'[0-9a-f]{64}', receipt['fingerprint']) is None):
            raise BusinessError('STORE_UNAVAILABLE')

    # LLM: no-follow 读取时验证封套，跨 owner 或 key 不匹配不能返回原结果。
    # 函数用途: 按唯一键读取永久回执。
    def get(self, key):
        raw = read_bytes_beneath(self.root, self.parts(key))
        if raw is None:
            return None
        try:
            row = json.loads(raw)
            if (type(row) is not dict or set(row) != {'schema', 'owner_id', 'key', 'receipt'}
                    or row['schema'] != 'business-receipt-row.v1' or row['owner_id'] != self.owner
                    or row['key'] != key):
                raise ValueError('receipt identity')
            self.validate_receipt(row['receipt'])
            return row['receipt']
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 持有原目录锁才能调用；同键相同可滚前，同键不同一律拒绝，禁止洗掉旧证明。
    # 函数用途: 生成有唯一键约束的回执写入计划。
    def payload(self, key, receipt):
        self.validate_receipt(receipt)
        existing = self.get(key)
        if existing is not None and existing != receipt:
            raise BusinessError('STORE_UNAVAILABLE')
        return self.parts(key), encode({'schema': 'business-receipt-row.v1',
            'owner_id': self.owner, 'key': key, 'receipt': receipt})

    # LLM: 日志的实体、修订和回执同为已提交事实；读取日志不执行任何恢复写入。
    # 函数用途: 读取并验证尚未完全物化的唯一提交日志。
    def pending(self):
        raw = read_bytes_beneath(self.root, self.journal)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
            if value == {}:
                return None
            if (type(value) is not dict or set(value) != {'schema', 'owner_id', 'state', 'key', 'receipt'}
                    or value['schema'] != 'business-catalog-transaction.v1' or value['owner_id'] != self.owner):
                raise ValueError('catalog journal')
            self.parts(value['key'])
            self.validate_receipt(value['receipt'])
            self.service._validate_catalog(value['state'])
            if value['state'].get('receipt_rows') is not True or value['state']['receipts']:
                raise ValueError('catalog journal state')
            return value
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 仅旧源迁移或已发布日志恢复回收准确候选；正常新提交不枚举永久回执目录。
    # 函数用途: 在原quota锁内写入有界批次，恢复时先释放同目标的孤儿副本。
    def write(self, payloads, admission, *, recover_candidates=False):
        if recover_candidates:
            discard_pending_candidates(self.root, payloads, admission)
        admission.check([OwnerQuotaChange(self.root.joinpath(*path), final_size=len(raw))
                         for path, raw in payloads])
        for path, raw in payloads:
            write_bytes_atomic_beneath(self.root, path, raw)

    # LLM: 旧源验证后才回收对应候选；checkpoint及目标前缀复核后才切换目录快照。
    # 函数用途: 分批迁移并续传永久回执，不把恢复清理混入正常事务。
    def migrate(self, state, admission):
        if state.get('receipt_rows') is True and not state['receipts']:
            return state
        entries = sorted(state['receipts'].items())
        checkpoint = ('data', 'business', '.receipt-migration.json')
        digest = hashlib.sha256(encode(state)).hexdigest()
        offset = 0
        raw = read_bytes_beneath(self.root, checkpoint)
        if raw is not None:
            try:
                saved = json.loads(raw)
                if (type(saved) is not dict or saved.get('owner_id') != self.owner
                        or saved.get('source_digest') != digest or type(saved.get('offset')) is not int
                        or not 0 <= saved['offset'] <= len(entries)):
                    raise ValueError('migration checkpoint')
                offset = saved['offset']
            except (ValueError, TypeError, KeyError) as error:
                raise BusinessError('STORE_UNAVAILABLE') from error
        for start in range(0, len(entries), 256):
            end = min(start + 256, len(entries))
            payloads = []
            for position in range(start, end):
                key, receipt = entries[position]
                if position < offset:
                    existing = self.get(key)
                    if existing == receipt:
                        continue
                    if existing is not None:
                        raise BusinessError('STORE_UNAVAILABLE')
                payloads.append(self.payload(key, receipt))
            if not payloads:
                continue
            payloads.append((checkpoint, encode({'owner_id': self.owner,
                'source_digest': digest, 'offset': max(offset, end)})))
            self.write(payloads, admission, recover_candidates=True)
        snapshot = {**state, 'receipts': {}, 'receipt_rows': True}
        if entries:
            self.write([(self.service.state_parts, encode(snapshot))], admission, recover_candidates=True)
        return snapshot

    # LLM: 日志中的回执与小快照共同物化，任何中断只允许幂等滚前而不重做 mutate。
    # 函数用途: 为原目录事务生成恢复或提交后的写入计划。
    def materialized(self, pending):
        return [self.payload(pending['key'], pending['receipt']),
                (self.service.state_parts, encode(pending['state']))]

    # LLM: 固定journal候选不是提交；只有已校验pending的恢复才扫描其目标目录。
    # 函数用途: 恢复发布前及物化中断，保留紧quota重放且不扫描无关历史行。
    def recover(self, admission):
        pending = self.pending()
        discard_pending_candidates(self.root, [(self.journal, b'')], admission)
        if pending is not None:
            payloads = self.materialized(pending)
            self.write(payloads, admission, recover_candidates=True)
            write_bytes_atomic_beneath(self.root, self.journal, encode({}))
