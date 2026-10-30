# LLM: 仅缓存单次retention阶段的引用投影；每个删除前仍核来源文件/目录身份，任何变化重新严格扫描，未知不得视为无引用。
# 模块用途: 记录pin视图读取的源代次，避免为每个目标重复解析全部候选、正式记忆和会话。
from __future__ import annotations

import hashlib
import stat
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..common.nofollow_fs import read_bytes_beneath

# 小型 pin 来源增加内容代次；单次读取上限为 16 MiB，不沿链接或无界读取特殊文件。
_MAX_PIN_SOURCE_DIGEST_BYTES = 16 * 1024 * 1024
_SOURCES: ContextVar[dict | None] = ContextVar("retention_pin_sources", default=None)
_TARGET_READ: ContextVar[bool] = ContextVar("retention_pin_target_read", default=False)


# LLM: 仅目标分域复核允许以已打开FD的新代次起读，首末仍严格稳定且禁止inode替换、缩短和读中变化。
# 函数用途: 标记一次有界的目标引用复核，退出恢复原读取合同。
@contextmanager
def target_pin_read():
    token = _TARGET_READ.set(True)
    try:
        yield
    finally:
        _TARGET_READ.reset(token)


# LLM: 只提供同栈读取模式，不构成来源权威或跳过完整性检查。
# 函数用途: 查询是否处于目标分域的FD快照复核中。
def target_pin_read_enabled():
    return _TARGET_READ.get()


# LLM: nofollow元数据只用于否定缓存有效性，不授予删除或认定正文无引用；非ENOENT错误原样传播。
# 函数用途: 取得源路径代次；小型普通文件增加内容摘要，识别同tick等长原地改写。
def source_stamp(path):
    path = Path(path)
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    stamp = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stat.S_ISREG(info.st_mode) and info.st_size <= _MAX_PIN_SOURCE_DIGEST_BYTES:
        raw = read_bytes_beneath(path.parent, (path.name,), max_bytes=_MAX_PIN_SOURCE_DIGEST_BYTES)
        if raw is None:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        return (*stamp, hashlib.blake2b(raw, digest_size=16).digest())
    return stamp


# LLM: 只在本次严格pin扫描上下文记录源，重复观察不同代会使快照立即失效，不改持久源或授权。
# 函数用途: 将文件或发现目录加入当前引用视图的失效监测。
def watch_pin_source(path):
    sources = _SOURCES.get()
    if sources is not None:
        path = Path(path)
        stamp = source_stamp(path)
        if path in sources and sources[path] != stamp:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        sources[path] = stamp


# LLM: 对象仅在一次apply内使用；旧视图必须逐源重核，变化/错误完整重建且保留原complete门，不跨owner缓存。
# 类用途: 在同阶段复用引用解析结果，保留执行前新引用与坏源的关闭式防线。
class ReferencePinSnapshot:
    # LLM: 构造不读取/写入文件；builder必须是原严格pin扫描，不接受模型提供的视图。
    # 函数用途: 绑定本次owner、允许外部根、运行时钟及原引用扫描器。
    def __init__(self, home, roots, now, builder):
        self.home, self.roots, self.now, self.builder = home, roots, now, builder
        self.sources, self.value = {}, None

    # LLM: 任一源代次变即重建；target仅允许原pins的严格分域复核，坏源不得以旧缓存放行。
    # 函数用途: 返回经本次源代次复核的完整引用集合。
    def current(self, errors, *, target=None):
        try:
            if self.value is not None and all(source_stamp(path) == stamp for path, stamp in self.sources.items()):
                return self.value
        except OSError:
            pass
        sources = {}
        token = _SOURCES.set(sources)
        try:
            watch_pin_source(self.home.owner_home_dir)
            watch_pin_source(self.home.owner_retention_json)
            result = self.builder(self.home, self.roots, errors, now=self.now)
            if target is not None and not result[1] and result[0].complete_for(target, errors):
                result = (result[0], True)
            stable = all(source_stamp(path) == stamp for path, stamp in sources.items())
        finally:
            _SOURCES.reset(token)
        self.sources = sources
        self.value = result if stable and result[1] else None
        if self.value is None:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        return self.value
