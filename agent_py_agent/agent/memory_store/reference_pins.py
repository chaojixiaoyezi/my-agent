# LLM: 仅缓存单次retention阶段的引用投影；每个删除前仍核来源文件/目录身份，任何变化重新严格扫描，未知不得视为无引用。
# 模块用途: 记录pin视图读取的源代次，避免为每个目标重复解析全部候选、正式记忆和会话。
from __future__ import annotations

import hashlib
import os
import stat
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from ..common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath

# 小型 pin 来源增加内容代次；单次读取上限为 16 MiB，不沿链接或无界读取特殊文件。
_MAX_PIN_SOURCE_DIGEST_BYTES = 16 * 1024 * 1024
_SOURCES: ContextVar[dict | None] = ContextVar("retention_pin_sources", default=None)
_SOURCE_CONTENTS: ContextVar[dict | None] = ContextVar("retention_pin_source_contents", default=None)
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


# LLM: 元数据取相同六字段；类型只保留S_IFMT，不能把读取后的atime等无关变化视为新代。
# 函数用途: 统一路径与FD的来源身份比较。
def _metadata_stamp(info):
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns, info.st_ctime_ns)


# LLM: 仅限本次snapshot的临时内容缓存，代次变或冷却立即移除；不读文件也不授予pin完整性。
# 函数用途: 获取同代热源的块对照，回收失效条目的体积预算。
def _cached_source_body(path, stamp):
    cache = _SOURCE_CONTENTS.get()
    descriptor = cache["fds"].get(path) if cache is not None else None
    if descriptor is not None and descriptor[0] != stamp:
        del cache["fds"][path]
        os.close(descriptor[1])
    cached = cache["files"].get(path) if cache is not None else None
    if cached is not None and cached[0] != stamp:
        cache["bytes"] -= cached[3]
        del cache["files"][path]
        cached = None
    return cache, cached


# LLM: 只归还本snapshot已持有的只读FD，全部尝试后传播首个关闭异常；供重建与弱引用终结器调用。
# 函数用途: 在重建或apply快照销毁时释放所有保留句柄。
def _close_source_descriptors(descriptors):
    first = None
    for _, descriptor in descriptors.values():
        try:
            os.close(descriptor)
        except OSError as error:
            if first is None:
                first = error
    descriptors.clear()
    if first is not None:
        raise first


# LLM: 仅复用相同来源代次的原nofollow只读FD，最多保留128个；打开及读取失败均不降级。
# 函数用途: 省去每个动作重复打开父目录和文件，后续仍逐次核FD/路径并读取实际字节。
def _open_source_descriptor(path, stamp, cache):
    previous = cache["fds"].get(path) if cache is not None else None
    if previous is not None:
        return previous[1], True
    descriptor = open_readonly_file_beneath(path.parent, (path.name,))
    retained = cache is not None and len(cache["fds"]) < 128
    if retained:
        cache["fds"][path] = (stamp, descriptor)
    return descriptor, retained


# LLM: 每次仍沿nofollow实际读完整文件；相同块只省hash，非省读取。单文件块缓存<=16MiB，总量<=4倍，超额只流读。
#   首尾FD/路径不一致拒绝；最多读初始大小加一块，兼容粗时钟copyout的旧size，不允许持续增长造成无界读取。
# 函数用途: 流式验证热源实际字节，只为内容改变的块链重新计算摘要。
def _file_digest(path, stamp):
    cache, cached = _cached_source_body(path, stamp)
    remember = (cache is not None and (cached is not None or len(cache["files"]) < 128)
                and stamp[3] <= _MAX_PIN_SOURCE_DIGEST_BYTES
                and cache["bytes"] + (0 if cached is not None else stamp[3]) <= 4 * _MAX_PIN_SOURCE_DIGEST_BYTES)
    blocks = [] if remember else None
    descriptor, retained = _open_source_descriptor(path, stamp, cache)
    try:
        opened = os.fstat(descriptor)
        if _metadata_stamp(opened) != stamp or opened.st_nlink != 1:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = None if cached is not None else hashlib.blake2b(digest_size=16)
        remaining, index, size = stamp[3] + 64 * 1024, 0, 0
        while remaining:
            raw = os.read(descriptor, min(remaining, 64 * 1024, _MAX_PIN_SOURCE_DIGEST_BYTES))
            if not raw:
                break
            remaining -= len(raw)
            size += len(raw)
            matching = cached is not None and index < len(cached[2]) and raw == cached[2][index]
            if digest is None and not matching:
                digest = hashlib.blake2b(digest_size=16)
                for previous in cached[2][:index]:
                    digest.update(previous)
            if digest is not None:
                digest.update(raw)
            if blocks is not None:
                blocks.append(cached[2][index] if matching else raw)
                if size > _MAX_PIN_SOURCE_DIGEST_BYTES:
                    blocks = None
            index += 1
        after = os.fstat(descriptor)
        if not remaining or _metadata_stamp(after) != stamp or after.st_nlink != 1:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
    except BaseException:
        if retained:
            del cache["fds"][path]
        retained = False
        raise
    finally:
        if not retained:
            os.close(descriptor)
    if _metadata_stamp(path.stat(follow_symlinks=False)) != stamp:
        raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
    if digest is None and index != len(cached[2]):
        digest = hashlib.blake2b(digest_size=16)
        for previous in cached[2][:index]:
            digest.update(previous)
    value = cached[1] if digest is None else digest.digest()
    if (blocks is not None and digest is not None
            and cache["bytes"] + size - (cached[3] if cached is not None else 0) <= 4 * _MAX_PIN_SOURCE_DIGEST_BYTES):
        cache["bytes"] += size - (cached[3] if cached is not None else 0)
        cache["files"][path] = (stamp, value, tuple(blocks), size)
    return value


# LLM: 只列当前层完整名称与inode，不沿成员链接；首尾核目录FD和路径，权限等错误不得当空目录。
# 函数用途: 识别粗时钟片内新增、删去或替换的pin来源。
def _directory_digest(path, stamp):
    descriptor = open_directory_beneath(path, (), require_dir_fd=True)
    try:
        if _metadata_stamp(os.fstat(descriptor)) != stamp:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        with os.scandir(descriptor) as entries:
            members = sorted((os.fsencode(entry.name), entry.inode()) for entry in entries)
        digest = hashlib.blake2b(digest_size=16)
        for name, inode in members:
            digest.update(name + b"\0" + str(inode).encode("ascii") + b"\0")
        if _metadata_stamp(os.fstat(descriptor)) != stamp:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
    finally:
        os.close(descriptor)
    if _metadata_stamp(path.stat(follow_symlinks=False)) != stamp:
        raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
    return digest.digest()


# LLM: 复用规范2秒粗时钟窗口，mtime/ctime取较新者；窗口内文件和目录都做内容观察，窗口外只核元数据。
# 函数用途: 取得来源代次；避免冷源每动作全文hash，同时保留同tick及大文件保护。
def source_stamp(path):
    if not isinstance(path, Path):
        path = Path(path)
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        _cached_source_body(path, None)
        return None
    stamp = _metadata_stamp(info)
    if time.time_ns() - max(stamp[4:]) > CACHE_TRUST_AGE_SECONDS * 1_000_000_000:
        _cached_source_body(path, None)
        return stamp
    if stat.S_ISREG(info.st_mode):
        return (*stamp, _file_digest(path, stamp))
    if stat.S_ISDIR(info.st_mode):
        return (*stamp, _directory_digest(path, stamp))
    return stamp


# LLM: 热转冷只在真实字节仍等于旧摘要时通过，并将观察更新为冷代次；不能直接忽略旧热摘要。
# 函数用途: 既不漏掉冷却前同tick改写，也不把单纯冷却视为来源损坏。
def _matching_stamp(path, current, previous):
    if current == previous:
        return True
    if (current is None or previous is None or len(current) != 6 or len(previous) != 7
            or current != previous[:6]):
        return False
    digest = _file_digest(path, current) if stat.S_ISREG(current[2]) else _directory_digest(path, current)
    return digest == previous[6]


# LLM: 逐源重核并只更新经真实内容验证的热转冷观察；遇到变化立即停止，不改变pin投影。
# 函数用途: 验证全源稳定并归一化已经沉降的来源代次。
def _refresh_sources(sources):
    for path, previous in sources.items():
        current = source_stamp(path)
        if not _matching_stamp(path, current, previous):
            return False
        sources[path] = current
    return True


# LLM: 只在本次严格pin扫描上下文记录源，不同代使快照立即失效；热转冷仍须比对实际内容。
# 函数用途: 将文件或发现目录加入当前引用视图的失效监测。
def watch_pin_source(path):
    sources = _SOURCES.get()
    if sources is not None:
        path = Path(path)
        stamp = source_stamp(path)
        if path in sources and not _matching_stamp(path, stamp, sources[path]):
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        sources[path] = stamp


# LLM: 对象仅在一次apply内使用；旧视图必须逐源重核，变化/错误完整重建且保留原complete门，不跨owner缓存。
# 类用途: 在同阶段复用引用解析结果，保留执行前新引用与坏源的关闭式防线。
class ReferencePinSnapshot:
    # LLM: 构造不读写文件；builder为原严格扫描，临时块对照只属于该snapshot且有总字节上限。
    # 函数用途: 绑定本次owner和严格扫描器，准备有界热源块缓存。
    def __init__(self, home, roots, now, builder):
        self.home, self.roots, self.now, self.builder = home, roots, now, builder
        self.sources, self.value = {}, None
        self.contents = {"files": {}, "fds": {}, "bytes": 0}
        weakref.finalize(self, _close_source_descriptors, self.contents["fds"])

    # LLM: 块对照上下文只在该snapshot调用栈生效，退出包括异常均恢复，不成为全局内容缓存。
    # 函数用途: 在本次引用复核中启用有界热源字节对照。
    def current(self, errors, *, target=None):
        token = _SOURCE_CONTENTS.set(self.contents)
        try:
            return self._current(errors, target=target)
        finally:
            _SOURCE_CONTENTS.reset(token)

    # LLM: 代次变或时钟回拨重建，热转冷最后比对真实内容；坏源/读中变化不得复用旧视图。
    # 函数用途: 逐源重核并返回完整pins，冷源不重复解析或hash。
    def _current(self, errors, *, target=None):
        try:
            if self.value is not None and _refresh_sources(self.sources):
                return self.value
        except OSError:
            pass
        self.value = None
        _close_source_descriptors(self.contents["fds"])
        self.contents["files"].clear()
        self.contents["bytes"] = 0
        sources = {}
        token = _SOURCES.set(sources)
        try:
            watch_pin_source(self.home.owner_home_dir)
            watch_pin_source(self.home.owner_retention_json)
            result = self.builder(self.home, self.roots, errors, now=self.now)
            if target is not None and not result[1] and result[0].complete_for(target, errors):
                result = (result[0], True)
            try:
                stable = _refresh_sources(sources)
            except OSError:
                stable = False
        finally:
            _SOURCES.reset(token)
        self.sources = sources
        self.value = result if stable and result[1] else None
        if self.value is None:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        return self.value
