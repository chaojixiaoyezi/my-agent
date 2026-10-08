# LLM: 只保存读取侧定位元数据；LRU淘汰/指纹失效只能变慢，不写权威文件，不存正文或推进持久游标。
# 模块用途: 为消息和审计扫描共用有界LRU、文件身份和保守的已见ID过滤器。
from __future__ import annotations

import hashlib
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any


# LLM: dev/ino识别替换，size识别截断，纳秒mtime/ctime挡住同尺寸改写和权限变化；不是执行或授权事实。
# 类用途: 保存一次只读stat的完整缓存校验指纹。
@dataclass(frozen=True)
class FileStamp:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int

    # LLM: 不读取文件正文；同路径inode变化不能共用旧偏移。
    # 函数用途: 判定两个指纹是否仍属于同一文件。
    def same_file(self, other: FileStamp) -> bool:
        return self.dev == other.dev and self.ino == other.ino


# LLM: 元数据读取失败沿调用方原错误合同上报，不以缺省指纹缓存损坏文件。
# 函数用途: 从stat或fstat结果生成只读指纹。
def file_stamp(info: os.stat_result) -> FileStamp:
    return FileStamp(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: key保留完整绝对canonical路径，不把同ID或同inode的不同owner合并。
# 函数用途: 为读取缓存构造按绝对路径隔离的键。
def cursor_key(path: Path, target: str) -> tuple[str, str]:
    return os.path.abspath(path), target


# LLM: 锁只守进程内LRU元数据，绝不包IO或代替原写锁；值应为不可变快照，淘汰不改读取结果。
# 类用途: 给各读取领域提供短临界区的有界LRU。
class CursorCache:
    # LLM: 上界由调用领域的有注释常量给定，不增加用户配置或持久化状态。
    # 函数用途: 初始化空的进程内缓存。
    def __init__(self, limit: int):
        self.limit = limit
        self._items: OrderedDict[object, Any] = OrderedDict()
        self._lock = Lock()

    # LLM: 读取仅改变LRU次序；缓存缺失不是坏账，调用方须完整扫描。
    # 函数用途: 查找并提升一个定位快照。
    def get(self, key: object) -> Any:
        with self._lock:
            value = self._items.get(key)
            if value is not None:
                self._items.move_to_end(key)
            return value

    # LLM: 锁内替换快照并淘汰最旧项，不保存正文、异常或跨owner的读取对象。
    # 函数用途: 保存一个有界定位提示。
    def put(self, key: object, value: Any) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)


# LLM: Bloom只决定能否安全记住新ID的首位置；可能已见时不缓存，误报只能导致下次完整扫描。
# 函数用途: 标记结构化ID并返回它此前是否可能出现过，避免重复ID使缓存跳过原首条匹配。
def note_seen(seen: bytearray, target: str) -> bool:
    digest = hashlib.blake2b(target.encode("utf-8", "surrogatepass"), digest_size=12).digest()
    indexes = [int.from_bytes(digest[i:i + 4], "little") % (len(seen) * 8) for i in (0, 4, 8)]
    existed = all(seen[index // 8] & (1 << (index % 8)) for index in indexes)
    for index in indexes:
        seen[index // 8] |= 1 << (index % 8)
    return existed
