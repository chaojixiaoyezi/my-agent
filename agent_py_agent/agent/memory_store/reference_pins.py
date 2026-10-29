# LLM: 仅缓存单次retention阶段的引用投影；每个删除前仍核来源文件/目录身份，任何变化重新严格扫描，未知不得视为无引用。
# 模块用途: 记录pin视图读取的源代次，避免为每个目标重复解析全部候选、正式记忆和会话。
from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

_SOURCES: ContextVar[dict | None] = ContextVar("retention_pin_sources", default=None)


# LLM: nofollow元数据只用于否定缓存有效性，不授予删除或认定正文无引用；非ENOENT错误原样传播。
# 函数用途: 取得源路径的类型、身份与变化时间，缺失也作为需要监测的事实。
def source_stamp(path):
    try:
        info = Path(path).stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


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

    # LLM: 无缓存或任一源身份漂移才重建；重建期间又漂移直接拒绝本次删除，不返回旧视图。
    # 函数用途: 返回经本次源代次复核的完整引用集合。
    def current(self, errors):
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
            stable = all(source_stamp(path) == stamp for path, stamp in sources.items())
        finally:
            _SOURCES.reset(token)
        self.sources = sources
        self.value = result if stable and result[1] else None
        if self.value is None:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        return self.value
