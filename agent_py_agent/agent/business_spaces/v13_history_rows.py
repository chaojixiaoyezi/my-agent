"""V13 私有逐键历史行；只有原 Store 的 quota→目录锁写者可以物化。

LLM: 本模块不产生提交点。事务日志仍归 V13Store，行是可幂等滚前的物化结果；
读者只读取 no-follow 路径，不创建目录、恢复事务或隐式接受坏数据。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import MutableMapping

from ..common.nofollow_fs import list_names_beneath, read_bytes_beneath, write_bytes_atomic_beneath
from ..conversation.business_access import BusinessError


# LLM: 与原 Store 的严格 JSON 编码一致，拒绝非有限数字及任意 Python 对象。
# 函数用途: 序列化行和迁移检查点。
def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


# LLM: 拷贝只在单个被访问的值上执行，不复制整个历史集合。
# 函数用途: 隔离原地变更前的值和返回值。
def copy_value(value):
    return json.loads(encode(value))


# LLM: owner、集合、行种类和原始 key 同时封存在行内；文件名只用摘要，不接受路径片段。
# 类用途: 通过 no-follow 私有文件实现逐键历史行与不可改写的回执唯一键。
class HistoryRows:
    # LLM: 不执行 I/O；可信 Store 先验证 collection，并持有原 quota/目录锁再写。
    # 函数用途: 绑定一个 owner 集合的行位置。
    def __init__(self, home, owner, collection):
        self.home, self.owner, self.collection = home, owner, collection
        self.base = ('data', 'business_v13', 'history', collection)

    # LLM: hash 只用于文件定位，身份仍须从行封套核对。
    # 函数用途: 为记录、回执或审计的原始键生成单个叶子名称。
    def parts(self, kind, key):
        if kind not in {'records', 'receipts'} or type(key) is not str or not key:
            raise BusinessError('STORE_UNAVAILABLE')
        return (*self.base, kind, hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json')

    # LLM: 只看集合历史根的固定子目录与迁移标记，不枚举永久行。
    # 函数用途: 缺失快照时辨别已有库，避免回执仍在却重置修订。
    def has_history(self):
        return bool(list_names_beneath(self.home, self.base)) or read_bytes_beneath(
            self.home, ('data', 'business_v13', '.migrate-' + self.collection + '.json')) is not None

    # LLM: 解码边界不容忍 owner/key 换代、损坏封套或非 JSON 字典记录；None 是删除墓碑。
    # 函数用途: 核对逐键行的身份和值。
    def decode(self, raw, kind, key=None):
        try:
            row = json.loads(raw)
            if (type(row) is not dict or set(row) != {'schema', 'owner', 'collection', 'kind', 'key', 'value'}
                    or row['schema'] != 'business-v13-row.v1' or row['owner'] != self.owner
                    or row['collection'] != self.collection or row['kind'] != kind
                    or type(row['key']) is not str or not row['key']
                    or key is not None and row['key'] != key
                    or row['value'] is not None and type(row['value']) is not dict):
                raise ValueError('row identity')
            if kind == 'receipts':
                value = row['value']
                if (type(value) is not dict or set(value) != {'fingerprint', 'result'}
                        or type(value['result']) is not dict or type(value['fingerprint']) is not str
                        or re.fullmatch(r'[0-9a-f]{64}', value['fingerprint']) is None):
                    raise ValueError('receipt')
            return row
        except (ValueError, TypeError, KeyError) as error:
            raise BusinessError('STORE_UNAVAILABLE') from error

    # LLM: 缺失返回 None；坏行和跨 owner 行不能变成不存在。
    # 函数用途: 按准确原始键读取一条历史记录或回执。
    def get(self, kind, key):
        raw = read_bytes_beneath(self.home, self.parts(kind, key))
        return None if raw is None else self.decode(raw, kind, key)['value']

    # LLM: 返回完整写入计划给原 quota；回执唯一约束在同一目录锁中检查，绝不覆盖不同的证明。
    # 函数用途: 编码单个行的幂等物化内容。
    def payload(self, kind, key, value):
        if kind == 'receipts':
            previous = self.get(kind, key)
            if previous is not None and previous != value:
                raise BusinessError('STORE_UNAVAILABLE')
        raw = encode({'schema': 'business-v13-row.v1', 'owner': self.owner,
                      'collection': self.collection, 'kind': kind, 'key': key, 'value': value})
        self.decode(raw, kind, key)
        return self.parts(kind, key), raw

    # LLM: 仅显式 list/整体替换才枚举；只排除原 no-follow 协议精确临时名，其余异常项仍拒绝。
    # 函数用途: 读取正式记录，避免中断遗留或并发候选文件阻断恢复后的列表。
    def items(self):
        result = {}
        for name in list_names_beneath(self.home, (*self.base, 'records')):
            if re.fullmatch(r'\.[0-9a-f]{64}\.json\.tmp-[0-9a-f]{32}', name) is not None:
                continue
            if re.fullmatch(r'[0-9a-f]{64}\.json', name) is None:
                raise BusinessError('STORE_UNAVAILABLE')
            raw = read_bytes_beneath(self.home, (*self.base, 'records', name))
            if raw is None:
                raise BusinessError('HOST_NOT_READY')
            row = self.decode(raw, 'records')
            if self.parts('records', row['key'])[-1] != name:
                raise BusinessError('STORE_UNAVAILABLE')
            if row['value'] is not None:
                result[row['key']] = row['value']
        return result


# LLM: 只保存本事务访问过的键及其 before 值；values/get 返回的字典原地编辑仍会参与 delta。
# 类用途: 为原 mutate(dict) 协议提供惰性可变映射，不改变调用者签名。
class TouchedRows(MutableMapping):
    # LLM: overlay 是同一 journal 已提交的增量；读阶段不运行物化。
    # 函数用途: 绑定行后端及可选日志覆盖值。
    def __init__(self, rows, *, overlay=None):
        self.rows = rows
        self.cache = {} if overlay is None else copy_value(overlay)
        self.before = {key: copy_value(value) for key, value in self.cache.items()}
        self.enumerated = False

    # LLM: 每个键至多从后端加载一次，缺失也缓存；初值与可变字典不共享。
    # 函数用途: 读取并记住一个目标的变更前状态。
    def _load(self, key):
        if type(key) is not str or not key:
            raise BusinessError('DOMAIN_RESULT_INVALID')
        if key not in self.cache:
            value = self.rows.get('records', key)
            self.cache[key] = value
            self.before[key] = copy_value(value)

    # LLM: MutableMapping 的 get 也走这里；None 墓碑对调用方就是缺失键。
    # 函数用途: 返回可原地编辑的单个记录字典。
    def __getitem__(self, key):
        self._load(key)
        if self.cache[key] is None:
            raise KeyError(key)
        return self.cache[key]

    # LLM: 记录只能是 JSON 字典；提交前仍由原 Store 再次严格序列化。
    # 函数用途: 捕获插入或替换的目标记录。
    def __setitem__(self, key, value):
        if type(value) is not dict:
            raise BusinessError('DOMAIN_RESULT_INVALID')
        self._load(key)
        self.cache[key] = value

    # LLM: 删除保留墓碑直到 journal 物化，以免中断后重新出现旧值。
    # 函数用途: 捕获已有记录的删除操作。
    def __delitem__(self, key):
        self[key]
        self.cache[key] = None

    # LLM: 只有调用者明确遍历映射时读取所有记录；不把这一步混入单条更新。
    # 函数用途: 实现原 dict 的遍历协议并保留逐键 before。
    def __iter__(self):
        if not self.enumerated:
            for key, value in self.rows.items().items():
                if key not in self.cache:
                    self.cache[key], self.before[key] = value, copy_value(value)
            self.enumerated = True
        return iter([key for key, value in self.cache.items() if value is not None])

    # LLM: len 属于显式整体访问；不用于热路径的隐式真假检查。
    # 函数用途: 返回现存记录数量。
    def __len__(self):
        return sum(1 for _key in self)

    # LLM: 比较范围严格限于 cache，不扫描全部原始文件。
    # 函数用途: 返回本事务插入、删除或原地编辑的键和值。
    def delta(self):
        return {key: copy_value(value) for key, value in self.cache.items() if self.before[key] != value}
