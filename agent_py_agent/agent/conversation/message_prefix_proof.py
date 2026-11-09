"""稳定热消息源的首ID否定提示；只保有界位图，不保正文、FD或消费游标。"""
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock, get_ident

from ..io.cursor_cache import note_seen

# 每个热源的位图256KiB，不保存消息ID或正文。
_HOT_PREFIX_FILTER_BYTES = 256 * 1024
# 全进程最多16源，位图驻留总量不超过4MiB；借用中的条目不可淘汰。
_HOT_PREFIX_MAX_SOURCES_COUNT = 16
# 小文件沿原定位缓存即可，不为其建立大位图。
_HOT_PREFIX_MIN_SOURCE_BYTES = 1024 * 1024


@dataclass
class _Prefix:
    identity: object
    bits: bytearray
    offset: int = 0
    lock: object = field(default_factory=Lock)


class PrefixSession:
    """只借一个源的非阻塞独占提示；异常或源复核失败不得发表覆盖范围。"""
    def __init__(self, entry, start):
        self._entry = entry
        self._next = start
        self._blocked = False
        self._pid = os.getpid()
        self._thread = get_ident()

    def observe(self, start, end, target):
        entry = self._entry
        if entry is None or self._pid != os.getpid() or self._thread != get_ident() or self._blocked:
            return False
        if start != self._next or end <= start or not isinstance(target, str):
            self._blocked = True
            return False
        self._next = end
        # 过长ID不提升游标；它不可能与其它<=256字符的ID相等，故不必驻留。
        return bool(target and len(target) <= 256 and not note_seen(entry.bits, target))

    def close(self, *, verified=False):
        if self._pid != os.getpid():
            self._entry = None
            return
        if self._thread != get_ident():
            return
        entry, self._entry = self._entry, None
        if entry is None:
            return
        try:
            if not verified:
                entry.bits.clear()
                entry.offset = 0
            elif not self._blocked:
                entry.offset = self._next
        finally:
            entry.lock.release()


class HotPrefixCache:
    """源忙、证据断裂或容量全被借用时直接放弃，不等待、不扩大预算。"""
    def __init__(self, *, filter_bytes=_HOT_PREFIX_FILTER_BYTES,
                 max_sources=_HOT_PREFIX_MAX_SOURCES_COUNT, min_source_bytes=_HOT_PREFIX_MIN_SOURCE_BYTES):
        if (type(filter_bytes) is not int or filter_bytes <= 0 or type(max_sources) is not int
                or max_sources <= 0 or type(min_source_bytes) is not int or min_source_bytes < 0):
            raise ValueError('invalid_hot_prefix_capacity')
        self.filter_bytes, self.max_sources, self.min_source_bytes = filter_bytes, max_sources, min_source_bytes
        self._pid = os.getpid()
        self._entries = OrderedDict()
        self._lock = Lock()

    def begin(self, path, stamp, changed_ns, start, *, trusted):
        if self._pid != os.getpid():
            # fork后的单线程新生代不获取父进程遗留锁，也不继承父提示。
            self._entries, self._lock, self._pid = OrderedDict(), Lock(), os.getpid()
        if (not trusted or type(changed_ns) is not int or type(start) is not int or start < 0
                or stamp.size < self.min_source_bytes):
            return None
        key = os.path.abspath(path)
        identity = (stamp, changed_ns)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                if start:
                    return None
                if len(self._entries) >= self.max_sources:
                    for old_key, old in self._entries.items():
                        if old.lock.acquire(blocking=False):
                            # 清空原对象，已关闭session即使仍被引用也不钉住旧大位图。
                            old.bits.clear()
                            old.lock.release()
                            del self._entries[old_key]
                            break
                    else:
                        return None
                entry = _Prefix(identity, bytearray())
                self._entries[key] = entry
            if not entry.lock.acquire(blocking=False):
                return None
            if entry.identity != identity or start == 0 or len(entry.bits) != self.filter_bytes:
                entry.bits.clear()
                entry.offset = 0
                entry.identity = identity
                if start:
                    entry.lock.release()
                    return None
                entry.bits = bytearray(self.filter_bytes)
            if entry.offset != start:
                entry.lock.release()
                return None
            self._entries.move_to_end(key)
            return PrefixSession(entry, start)

    def usage(self):
        with self._lock:
            return len(self._entries), sum(len(entry.bits) for entry in self._entries.values())


HOT_PREFIXES = HotPrefixCache()
