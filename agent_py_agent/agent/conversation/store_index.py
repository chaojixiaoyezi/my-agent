# LLM: 会话读取侧投影独立于 Store；保持物理文件指纹、错误、排序和双预算，不能以缓存决定业务状态。
# 模块用途: 共用观察、唤醒、进度策略的有界扫描缓存，目录不可读保留错误并回读原文件。
from __future__ import annotations

import json
import logging
import os
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

_STORE_LOGGER = logging.getLogger("agent.conversation.store")

_SCAN_INDEX_SCHEMA = "conversation.scan_index.v1"
_SCAN_INDEX_WARNING_LIMIT = 32
_SCAN_INDEX_MAX_ENTRIES = 8192
_SCAN_INDEX_MAX_RECORDS = 30000
_SCAN_INDEX_MAX_RECORDS_PER_FILE = 10000
_SCAN_INDEX_REGISTRY_LOCK = threading.Lock()


# LLM: 指纹必须覆盖"文件身份+内容+元数据变更"三类事实:原子替换(write_json_file_atomic)
# 会换 inode,JSONL 追加会改 size/mtime,原地改写会改 ctime;任一项变化即视为索引条目过期。
# 函数用途: 取一个台账文件当前指纹;文件不可 stat 时返回 None 表示索引不可用。
def _file_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: 身份字段(file stem 与记录内主键)必须一致,否则该投影条目视为不可信并强制回到权威
# 读取;这既能拦住被污染的投影,也不会改变"文件名与主键不一致"的既有语义(仍按主键返回)。
# 函数用途: 校验一条投影负载的主键是否与文件名 stem 相符。
def _scan_index_identity_matches(payload: object, identity_key: str, expected: str) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get(identity_key) or "") == expected


# LLM: 条目是权威文件的派生快照，不拥有业务状态；指纹和读取错误必须一起保留。
# 类用途: 记录一次磁盘读取的负载、身份指纹及索引记账信息。
@dataclass(frozen=True)
class _ScanIndexEntry:
    """一条派生投影:权威文件的指纹 + 该次权威读取的结果(负载或结构化错误)。"""

    fingerprint: tuple[int, int, int, int]
    ok: bool
    payload: Any
    error: dict[str, Any] | None = None
    records: int = 0
    # 绝对路径用于"该条目绑定哪个目录"的判定:清理只在同目录范围内进行(wake 目录下的
    # 条目永远不该因为 progress_policies 目录少了个同名文件而被清掉)。
    path: str = ""


# LLM: 这里用 os.scandir 而不是 Path.glob,唯一目的是把"目录自身读不到"与"目录为空"分开:
# glob 在权限不足时静默返回空列表,调用方会把一次瞬时故障当成"文件都被删了",进而清掉
# 全部有效投影。scandir 会抛 OSError,于是这里能明确返回 None(不可列出)而不是空集合。
# 成员口径必须与 Path.glob 逐字一致:glob 对最后一段**只按名字匹配、不看条目类型**,断链
# 符号链接、FIFO、名字匹配的目录都会被列出。绝不能加 is_file()/is_dir() 过滤——那会让
# 调用方"少看一批文件":断链的 wake-*.json 本来会产生一条 load_error,而 runtime/guidance
# 消费方以"有 load_error 就不消费"作硬门,静默丢文件等于悄悄绕过该硬门。
# 函数用途: 列出目录下匹配单段 fnmatch 模式的名字(成员集合与 Path.glob 相同),目录不存在
# 或不是目录视为空集合,目录不可列出返回 None。
def _scan_dir_match_names(directory: Path, pattern: str) -> frozenset[str] | None:
    try:
        with os.scandir(directory) as items:
            return frozenset(
                item.name for item in items if fnmatchcase(item.name, pattern)
            )
    except FileNotFoundError:
        # 目录还没建 = 权威空集合,与旧 glob 的行为一致(不算不可读,不误清)。
        return frozenset()
    except NotADirectoryError:
        return frozenset()
    except OSError:
        return None


# LLM: 目录索引只缓存已验证的读取结果；失效回到原文件，目录不可读不得冒充空目录。
# 类用途: 管理单个台账目录的有界 LRU 投影和诊断，不写权威文件。
class ScanIndex:
    # LLM: 该对象只保存"上次权威读取的负载",不保存任何裁决状态;调用方必须先给出刚刚
    # stat 到的指纹才能取用条目,因此并发写只会造成未命中,不会读到过期内容。
    # 函数用途: 初始化空的有界缓存、计数和锁；目录数据在首次读取时才进入索引。
    def __init__(self, name: str, *, max_entries: int = _SCAN_INDEX_MAX_ENTRIES) -> None:
        self.name = name
        self.schema = _SCAN_INDEX_SCHEMA
        self.max_entries = max(0, int(max_entries))
        self.warnings: list[dict[str, Any]] = []
        self.hits = 0
        self.misses = 0
        self.stale = 0
        self.evictions = 0
        # 因"目录里已不存在该文件名"被清掉的条目数,与预算淘汰分开计数,便于诊断轮换频率。
        self.prunes = 0
        # 目录本身列不出来(权限/IO 异常)的次数。该状态下一次都不清,只整体回退全量扫描。
        self.dir_unreadable = 0
        self._entries: OrderedDict[str, _ScanIndexEntry] = OrderedDict()
        self._records = 0
        self._order_names: frozenset[str] | None = None
        self._order_paths: list[Path] | None = None
        self._lock = threading.RLock()

    # LLM: 目录扫描的 canonical 顺序完全由"文件名集合"决定(同一父目录、目录内名字唯一),
    # 因此按 frozenset(名字) 记忆排序结果:集合没变就直接复用,集合变了才重新排序。排序是
    # 集合的纯函数,所以这不是新的顺序规则,只是不再每轮为几千个 Path 做 pathlib 比较;
    # 文件集合仍每轮重新枚举(见 ScanIndexes.usable 的同一份 listing)。
    # 目录列不出来时退回旧的 glob 口径继续枚举:枚举失败绝不能让调用方少看一批文件,清不清理
    # 是 ScanIndexes.usable 的事,与"本次能看到哪些路径"无关。
    # 函数用途: 返回目录下匹配 pattern 的路径,按旧实现的 canonical 顺序排列;返回值只读。
    def ordered_paths(self, directory: Path, pattern: str) -> list[Path]:
        names = _scan_dir_match_names(directory, pattern)
        if names is None:
            # 目录读不到 ≠ 目录是空的,因此这里也绝不更新排序记忆,否则"暂时读不到"会被记成
            # "目录为空"并在下一次冒充权威集合。glob 的返回值仍然给出真实路径。
            try:
                return sorted(directory.glob(pattern))
            except OSError:
                return []
        with self._lock:
            if names == self._order_names and self._order_paths is not None:
                return self._order_paths
        paths = sorted(directory / name for name in names)
        with self._lock:
            self._order_names = names
            self._order_paths = paths
        return paths

    # LLM: 清理只在"目录这一次真的被完整列出"之后发生,而且每个候选名字都要再用一次
    # stat 复核:stat 成功说明文件还在(只是本轮没进匹配集合),一条都不清。因此权限抖动、
    # 并发 rename、glob 瞬时失败都不会误删有效投影,最坏只是慢一轮。
    # live_names 为 None 表示目录本轮不可列出:保持全部投影不动并计入 dir_unreadable,
    # 绝不能把"暂时读不到"当成"已删除"而清掉索引。
    # 函数用途: 丢弃"绑定到该目录且文件已确认消失"的条目,返回清理条数;目录不可列出时返回 -1。
    def prune_missing(self, directory: Path, live_names: frozenset[str] | None) -> int:
        if live_names is None:
            with self._lock:
                self.dir_unreadable += 1
            return -1
        prefix = f"{directory}{os.sep}"
        with self._lock:
            candidates = [
                name
                for name, entry in self._entries.items()
                if name not in live_names and entry.path.startswith(prefix)
            ]
        dropped = 0
        for name in candidates:
            with self._lock:
                entry = self._entries.get(name)
            if entry is None or _file_fingerprint(Path(entry.path)) is not None:
                continue
            with self._lock:
                if name in self._entries:
                    self._drop_entry(name)
                    self.prunes += 1
                    dropped += 1
        return dropped

    # LLM: 告警按 (reason,key) 去重并有上限,避免轮询路径刷日志;这是"索引不可用已回退
    # 全量扫描"的结构化事实,不是错误状态；调用方将诊断放入 details，不据此改变唤醒/恢复裁决。
    # 函数用途: 记录并输出一条结构化索引告警(去重、有界)，不修改权威文件。
    def warn(
        self, reason: str, *, key: str = "", details: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        record = {
            "schema": "conversation.scan_index.warning.v1",
            "index": self.name,
            "reason": str(reason),
            "key": str(key),
            **(details or {}),
        }
        with self._lock:
            duplicate = any(
                item.get("reason") == record["reason"] and item.get("key") == record["key"]
                for item in self.warnings
            )
            if duplicate:
                return record
            if len(self.warnings) < _SCAN_INDEX_WARNING_LIMIT:
                self.warnings.append(record)
        _STORE_LOGGER.warning(
            "conversation.scan_index.unusable %s",
            json.dumps(record, ensure_ascii=False, sort_keys=True),
        )
        return record

    # LLM: 命中条件=指纹逐字段相等(+ 主键身份相符);指纹不等条目立即丢弃并计为 stale,
    # 由调用方读取权威文件后重新写入,绝不"部分信任"。
    # 函数用途: 用指纹查可用投影；身份不符按 details 记录诊断，返回 None 后仍由调用方读权威文件。
    def lookup(
        self, path: Path, fingerprint: tuple[int, int, int, int], *, identity_key: str = ""
    ) -> _ScanIndexEntry | None:
        name = path.name
        with self._lock:
            entry = self._entries.get(name)
            if entry is None:
                self.misses += 1
                return None
            if entry.fingerprint != fingerprint:
                self._drop_entry(name)
                self.stale += 1
                return None
            if entry.ok and identity_key and not _scan_index_identity_matches(
                entry.payload, identity_key, path.stem
            ):
                self._drop_entry(name)
                self.warn("entry_identity_mismatch", key=name, details={"path": str(path)})
                return None
            self._entries.move_to_end(name)
            self.hits += 1
            return entry

    # LLM: 写入只发生在"刚完成一次权威读取且读前读后指纹一致"之后,因此投影与磁盘字节
    # 同源;同一文件重复写入是幂等的(覆盖同名条目,不产生第二份账),并且写完后立即按
    # **条目数 + 记录数**两条预算做 LRU 淘汰——只按记录数淘汰时,dict 负载(记录恒为 0)
    # 的目录会让 max_entries 完全失效,长期轮换文件名就能无限占内存。
    # 函数用途: 缓存一次权威读取结果；超预算时用明确的路径、条数和上限记录告警，不改原文件。
    def store(
        self,
        path: Path,
        fingerprint: tuple[int, int, int, int],
        *,
        ok: bool,
        payload: Any,
        error: dict[str, Any] | None = None,
    ) -> None:
        name = path.name
        records = len(payload) if ok and isinstance(payload, list) else 0
        if records > _SCAN_INDEX_MAX_RECORDS_PER_FILE:
            self.warn(
                "entry_record_budget_exceeded",
                key=name,
                details={"path": str(path), "records": records, "limit": _SCAN_INDEX_MAX_RECORDS_PER_FILE},
            )
            return
        with self._lock:
            self._drop_entry(name)
            self._entries[name] = _ScanIndexEntry(
                fingerprint=fingerprint,
                ok=ok,
                payload=payload,
                error=error,
                records=records,
                path=str(path),
            )
            self._records += records
            while self._entries and (
                len(self._entries) > self.max_entries
                or (self._records > _SCAN_INDEX_MAX_RECORDS and len(self._entries) > 1)
            ):
                self._evict_oldest()
            if self._records > _SCAN_INDEX_MAX_RECORDS:
                # 单条负载本身超全局记录预算:不缓存它(与旧口径一致),避免为它清空整个索引。
                self._drop_entry(name)
                self.warn(
                    "record_budget_exceeded",
                    key=name,
                    details={"path": str(path), "records": records, "limit": _SCAN_INDEX_MAX_RECORDS},
                )

    # LLM: 淘汰与显式丢弃共用同一条记账路径,否则 _records 会因漏减而失真,让记录维度
    # 预算跟着失效。任何移除条目的地方都必须走这里。
    # 函数用途: 移除一条投影并同步记录数记账。
    def _drop_entry(self, name: str) -> None:
        entry = self._entries.pop(name, None)
        if entry is None:
            return
        self._records -= entry.records

    # LLM: 与所有删除入口共用记录计数，调用方已持有索引锁，不改变磁盘文件。
    # 函数用途: 按 LRU 淘汰最旧的一条投影并计入 evictions。
    def _evict_oldest(self) -> None:
        oldest = next(iter(self._entries))
        self._drop_entry(oldest)
        self.evictions += 1

    # LLM: 清空等价于冷启动,只影响耗时;保留告警与计数便于诊断。
    # 函数用途: 丢弃全部投影条目(供失效处理与测试)。
    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._records = 0

    # LLM: 统计是锁内的一致副本，只用于诊断，不能据此裁决唤醒或恢复。
    # 函数用途: 返回该索引的条目/记录占用、淘汰/清理/降级计数与命中统计,供诊断和测试断言。
    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "index": self.name,
                "schema": self.schema,
                "entries": len(self._entries),
                "records": self._records,
                "max_entries": self.max_entries,
                "max_records": _SCAN_INDEX_MAX_RECORDS,
                "hits": self.hits,
                "misses": self.misses,
                "stale": self.stale,
                "evictions": self.evictions,
                "prunes": self.prunes,
                "dir_unreadable": self.dir_unreadable,
                "warnings": list(self.warnings),
            }


# LLM: 每个存储上下文持有一个惰性注册表，锁内创建唯一索引；不新增持久状态或跨 owner 共享。
# 类用途: 按账本名称组织扫描缓存，并集中执行有效性核对和权威文件读取。
class ScanIndexes:
    # LLM: 索引在当前存储上下文的 ScanIndexes 实例中惰性注册，不写盘、不扫描初始化目录；
    # 进程重启或索引被丢弃只会让下一次调用退回全量权威扫描,绝不改变任何返回语义。
    # 函数用途: 取得某个台账目录(唤醒/观察/策略)的进程内增量索引。
    def get(self, name: str) -> ScanIndex:
        registry = self.__dict__.get("_scan_index_registry")
        if registry is None:
            with _SCAN_INDEX_REGISTRY_LOCK:
                registry = self.__dict__.get("_scan_index_registry")
                if registry is None:
                    registry = {}
                    self.__dict__["_scan_index_registry"] = registry
        index = registry.get(name)
        if index is None:
            with _SCAN_INDEX_REGISTRY_LOCK:
                index = registry.get(name)
                if index is None:
                    index = ScanIndex(name)
                    registry[name] = index
        return index

    # LLM: 该判定只回答"这次能不能用投影加速",不回答任何业务问题;不可用时本次调用整体
    # 退回全量权威扫描并留结构化告警,因此索引缺失/损坏永远不会让人少读一条记录。
    # 判定同时承担"跟盘对账":上界若只按当前目录文件数判断,被删除/轮换掉的旧文件名条目
    # 会一直驻留(实测 max_entries=4 时驻留 41 个已删名字)。因此这里重新列一次目录,并在
    # **列目录成功**时清掉已确认消失文件名的投影;目录列不出来时一条都不清,只降级全量扫描。
    # pattern 必须与调用方 ordered_paths 用的那个逐字相同:上界口径与"本次实际要扫的文件
    # 集合"必须同源,否则给目录换一个更宽的模式就会凭空改变 dir_too_large 的触发点。
    # 函数用途: 判断能否使用增量索引，显式传递诊断明细；清理已确认删除的投影，不修改权威文件。
    def usable(
        self, index: ScanIndex, *, key: str, directory: Path, pattern: str
    ) -> bool:
        if index.schema != _SCAN_INDEX_SCHEMA:
            index.warn(
                "index_rejected",
                key=key,
                details={"expected": _SCAN_INDEX_SCHEMA, "found": str(index.schema)},
            )
            return False
        live_names = _scan_dir_match_names(directory, pattern)
        if index.prune_missing(directory, live_names) < 0:
            # 目录暂时读不到(权限/IO 抖动):保持全部投影不动,本次退回全量扫描,
            # 结果依旧正确,绝不用"读不到"冒充"已删除"。
            index.warn("dir_unreadable", key=key, details={"path": str(directory)})
            return False
        entries = len(live_names or ())
        if entries > index.max_entries:
            index.warn(
                "dir_too_large",
                key=key,
                details={"entries": int(entries), "limit": int(index.max_entries)},
            )
            return False
        return True

    # LLM: 复用投影的前提是"文件指纹与写入投影时逐字段相同";读权威文件前后再各取一次
    # 指纹,期间发生并发写就不入索引,避免把半新内容固化。索引只省 I/O,过滤/排序/limit
    # 语义完全仍由调用方按权威负载判定。
    # 函数用途: 读一个台账文件,命中有效投影就复用,否则读权威文件并刷新投影。
    def read(
        self,
        index: ScanIndex,
        path: Path,
        reader: Callable[[Path], tuple[bool, Any, dict[str, Any] | None]],
        *,
        identity_key: str = "",
        use_index: bool = True,
    ) -> tuple[bool, Any, dict[str, Any] | None]:
        current = _file_fingerprint(path)
        if use_index and current is not None:
            entry = index.lookup(path, current, identity_key=identity_key)
            if entry is not None:
                # 结构化错误按"每次一份副本"交还:旧实现每次读取都新建错误字典,副本能保证
                # 消费方改自己拿到的 error 对象时不会污染索引里保存的那一份。
                return (
                    entry.ok,
                    entry.payload,
                    dict(entry.error) if entry.error is not None else None,
                )
        ok, payload, error = reader(path)
        if use_index:
            after = _file_fingerprint(path)
            if ok and after is not None and after == current:
                if not identity_key or _scan_index_identity_matches(
                    payload, identity_key, path.stem
                ):
                    index.store(
                        path,
                        after,
                        ok=ok,
                        payload=payload,
                        # 索引只持有自己私有的错误副本,不与被调用方共享同一个 dict。
                        error=dict(error) if error is not None else None,
                    )
        return ok, payload, error
