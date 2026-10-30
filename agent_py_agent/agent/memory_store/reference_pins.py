# LLM: 仅缓存单次retention阶段的引用投影；每个删除前仍核来源文件/目录身份，任何变化重新严格扫描，未知不得视为无引用。
# 模块用途: 记录pin视图读取的源代次，避免为每个目标重复解析全部候选、正式记忆和会话。
from __future__ import annotations

import hashlib
import os
import stat
import time
import weakref
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from ..common.nofollow_fs import open_directory_beneath, open_readonly_file_beneath

# 热源单次系统调用分块读取上限为 64 KiB，保持流式读取与块级摘要复用粒度。
_PIN_SOURCE_CHUNK_BYTES = 64 * 1024
# 小型 pin 来源单次内容代次验证上限为 256 KiB，不沿链接或单次无界读取超大文件。
_MAX_PIN_SOURCE_DIGEST_BYTES = 4 * _PIN_SOURCE_CHUNK_BYTES
# 可续切片状态表最大保留路径数，防止离线调用缓存无界增长。
_MAX_RESUMABLE_DIGEST_ENTRIES = 128
_SOURCES: ContextVar[dict | None] = ContextVar("retention_pin_sources", default=None)
_SOURCE_CONTENTS: ContextVar[dict | None] = ContextVar("retention_pin_source_contents", default=None)
_TARGET_READ: ContextVar[bool] = ContextVar("retention_pin_target_read", default=False)
_RESUMABLE_DIGESTS: OrderedDict[Path, dict] = OrderedDict()


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


# LLM: 元数据失配或删除时同步清理可续切片状态，防止旧切片摘要跨代次残留。
# 函数用途: 获取同代热源的块对照，回收失效条目的体积预算。
def _cached_source_body(path, stamp):
    existing = _RESUMABLE_DIGESTS.get(path)
    if existing is not None and existing["stamp"] != stamp:
        _RESUMABLE_DIGESTS.pop(path, None)
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


# LLM: 小文件整读与大文件切片共用同一字节账，每current每源最多4倍单次上限（1MiB）；reserve为后续必读预留，超额不读不扣。
# 函数用途: 在实际读取前预扣来源摘要字节预算。
def _charge_digest_bytes(cache, path, cost, *, reserve=0):
    spent = cache.setdefault("digest_bytes", {})
    if spent.get(path, 0) + cost + reserve > 4 * _MAX_PIN_SOURCE_DIGEST_BYTES:
        return False
    spent[path] = spent.get(path, 0) + cost
    return True


# LLM: 与快照热窗口同口径，mtime/ctime较新者滑出2秒粗时钟窗口才算冷；冷源之后的任何写入都会推进元数据。
# 函数用途: 判断来源代次是否已脱离同tick等长改写的不可见窗口。
def _is_settled(stamp):
    return time.time_ns() - max(stamp[4:6]) > CACHE_TRUST_AGE_SECONDS * 1_000_000_000


# LLM: 每片最多256KiB，读前按实际片长扣共享字节预算，超额返回None不读；切片或尾部变化清空旧周期与解析基线，禁止拼接不同内容代次。
# 函数用途: 对超出单次预算的热文件按有界切片递增验证，任何已验切片或尾部变化立即推进代次。
def _resumable_file_digest(path, stamp, cache, *, advance=True):
    budget = max(1, int(_MAX_PIN_SOURCE_DIGEST_BYTES))
    chunk_limit = min(_PIN_SOURCE_CHUNK_BYTES, budget)
    tail_budget = min(chunk_limit, max(1, budget // 4))
    slice_budget = max(1, budget - tail_budget)
    body_limit = max(0, stamp[3] - tail_budget)
    total_slices = max(1, (body_limit + slice_budget - 1) // slice_budget)
    state = _RESUMABLE_DIGESTS.get(path)
    if (state is None or state["stamp"] != stamp or state["budget"] != budget
            or state["total_slices"] != total_slices):
        state = {
            "stamp": stamp,
            "budget": budget,
            "total_slices": total_slices,
            "tail_digest": None,
            "slice_digests": [None] * total_slices,
            "next_slice": 0,
            "cycle_verified": set(),
            "generation": 0,
            "phase": "pre",
            "baseline": None,
            "post_scope": None,
        }
        _RESUMABLE_DIGESTS[path] = state
        while len(_RESUMABLE_DIGESTS) > _MAX_RESUMABLE_DIGEST_ENTRIES:
            _RESUMABLE_DIGESTS.popitem(last=False)
    else:
        _RESUMABLE_DIGESTS.move_to_end(path)
    slice_index = (state["next_slice"] if advance else max(0, state["next_slice"] - 1)) % total_slices
    slice_start = slice_index * slice_budget
    slice_len = max(0, min(slice_budget, body_limit - slice_start))
    tail_len = max(0, stamp[3] - body_limit)
    if cache is not None and not _charge_digest_bytes(cache, path, slice_len + tail_len):
        return None
    descriptor, retained = _open_source_descriptor(path, stamp, cache)
    try:
        opened = os.fstat(descriptor)
        if _metadata_stamp(opened) != stamp or opened.st_nlink != 1:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        slice_hash = hashlib.blake2b(digest_size=16)
        os.lseek(descriptor, slice_start, os.SEEK_SET)
        remaining = slice_len
        while remaining:
            raw = os.read(descriptor, min(remaining, chunk_limit))
            if not raw:
                raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
            remaining -= len(raw)
            slice_hash.update(raw)
        tail_hash = hashlib.blake2b(digest_size=16)
        os.lseek(descriptor, body_limit, os.SEEK_SET)
        remaining = tail_len
        while remaining:
            raw = os.read(descriptor, min(remaining, chunk_limit))
            if not raw:
                raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
            remaining -= len(raw)
            tail_hash.update(raw)
        after = os.fstat(descriptor)
        if _metadata_stamp(after) != stamp or after.st_nlink != 1:
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
    slice_digest = slice_hash.digest()
    tail_digest = tail_hash.digest()
    changed = ((state["tail_digest"] is not None and state["tail_digest"] != tail_digest)
               or (state["slice_digests"][slice_index] is not None
                   and state["slice_digests"][slice_index] != slice_digest))
    if changed:
        state["generation"] += 1
        state["slice_digests"] = [None] * total_slices
        state["cycle_verified"] = set()
        state["phase"] = "pre"
        state["baseline"] = None
        state["post_scope"] = None
    state["tail_digest"] = tail_digest
    state["slice_digests"][slice_index] = slice_digest
    state["cycle_verified"].add(slice_index)
    if advance:
        state["next_slice"] = (slice_index + 1) % total_slices
    if None not in state["slice_digests"] and len(state["cycle_verified"]) == total_slices:
        return ("complete", state["generation"], tuple(state["slice_digests"]), state["tail_digest"])
    return (
        "partial",
        state["generation"],
        slice_index,
        len(state["cycle_verified"]),
        total_slices,
        slice_digest,
        state["tail_digest"],
    )


# LLM: 每次仍沿nofollow实际读完整小文件；相同块只省hash，非省读取。单文件块缓存<=256KiB，总量<=4倍，超额走有界可续切片。
#   首尾FD/路径不一致拒绝；最多读初始大小加一块，兼容粗时钟copyout的旧size，不允许持续增长造成无界读取。
# 函数用途: 流式验证热源实际字节，只为内容改变的块链重新计算摘要。
def _file_digest(path, stamp):
    cache, cached = _cached_source_body(path, stamp)
    if stamp[3] > _MAX_PIN_SOURCE_DIGEST_BYTES:
        result = _resumable_file_digest(path, stamp, cache)
        return result if result is not None else ("partial", _RESUMABLE_DIGESTS[path]["generation"])
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
        remaining, index, size = stamp[3] + _PIN_SOURCE_CHUNK_BYTES, 0, 0
        while remaining:
            raw = os.read(descriptor, min(remaining, _PIN_SOURCE_CHUNK_BYTES, _MAX_PIN_SOURCE_DIGEST_BYTES))
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


# LLM: 标记大热源在当前快照预算内尚未完成全部切片核验，触发失败关闭但保留已核切片进度供下轮续推。
# 类用途: 区分有界预算耗尽的待续验状态与真实内容篡改。
class _PartialPinVerification(OSError):
    pass


# LLM: 判断来源代次是否已完成全量内容校验；partial切片态绝不能通过完整性门或热转冷归一化。
# 函数用途: 识别未完成可续切片验证的热源代次。
def _is_complete_stamp(stamp):
    return stamp is None or len(stamp) == 6 or not (isinstance(stamp[6], tuple) and stamp[6][0] == "partial")


# LLM: 同current内未被解析标记的重复监视复用小文件摘要；解析后复读须给发布前最终复核留足一读，否则沿用旧摘要交最终复核，首读或stamp变化超额失败关闭。
# 函数用途: 让<=256KiB热源的读前、读后与最终复核共用每源1MiB预算，不因重复watch或重试反复整读。
def _budgeted_small_digest(path, stamp, cache):
    if cache is None:
        return _file_digest(path, stamp)
    verified = cache.setdefault("verified", {})
    recheck = cache.setdefault("recheck", set())
    known = verified.get(path)
    if known is not None and known[0] == stamp:
        if path not in recheck or not _charge_digest_bytes(cache, path, stamp[3], reserve=stamp[3]):
            return known[1]
    elif not _charge_digest_bytes(cache, path, stamp[3]):
        raise _PartialPinVerification("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
    recheck.discard(path)
    digest = _file_digest(path, stamp)
    verified[path] = (stamp, digest)
    return digest


# LLM: 解析器读完来源后调用；只标记本current缓存，不读文件、不改来源观察集合。
# 函数用途: 要求这些来源的下一次监视尽量实际复读，保留读后改写的逐源诊断。
def recheck_pin_sources(paths):
    cache = _SOURCE_CONTENTS.get()
    if cache is not None:
        cache.setdefault("recheck", set()).update(Path(path) for path in paths)


# LLM: 热源同tick等长改写stat不可见，post只认同一current内读到的片，跨调用旧片一律重核，单轮读不完全片就保持partial；
#   转冷后任何写入都推进stat，冷期核过的片才可跨current累积。全部读取共用每源1MiB字节预算。
# 函数用途: 推进解析前后两阶段验证，绝不以早先调用核过的片或尾片代替发布前读到的全片。
def _verify_complete_file_digest(path, stamp):
    if stamp[3] <= _MAX_PIN_SOURCE_DIGEST_BYTES:
        return _budgeted_small_digest(path, stamp, _SOURCE_CONTENTS.get())
    cache, _ = _cached_source_body(path, stamp)
    pass_token = cache.get("pass_token") if cache is not None else None
    scope = "settled" if _is_settled(stamp) else (pass_token if pass_token is not None else object())
    state = _RESUMABLE_DIGESTS.get(path)
    if state is not None and state["phase"] == "post":
        if state.get("post_scope") != scope:
            state["cycle_verified"] = set()
            state["post_scope"] = scope
        elif len(state["cycle_verified"]) == state["total_slices"]:
            return ("complete", state["generation"], *state["baseline"])
    result = ("partial", state["generation"] if state else 0)
    for _ in range(4):
        was_post = state is not None and state["phase"] == "post"
        step = _resumable_file_digest(path, stamp, cache)
        if step is None:
            break
        result = step
        state = _RESUMABLE_DIGESTS[path]
        if was_post and state["phase"] != "post":
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        if result[0] == "complete":
            if state["phase"] == "pre":
                state["baseline"] = (tuple(state["slice_digests"]), state["tail_digest"])
                state["phase"] = "post"
                state["cycle_verified"] = set()
                state["next_slice"] = 0
                state["post_scope"] = scope
            return ("complete", state["generation"], *state["baseline"])
    if state is not None and state["phase"] == "post":
        # pending持有原解析基线，不能仅凭可被LRU重置的generation接受另一snapshot重建的状态。
        return ("partial", state["generation"], state["baseline"])
    return result


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


# LLM: 仅清理热块与FD缓存；同元数据代次冷却时保留可续切片表，供热转冷完整核验后释放。
# 函数用途: 在来源滑出2秒热窗口时回收块缓存而不丢失待核切片进度。
def _evict_hot_body_cache(path, stamp):
    cache = _SOURCE_CONTENTS.get()
    descriptor = cache["fds"].get(path) if cache is not None else None
    if descriptor is not None:
        del cache["fds"][path]
        os.close(descriptor[1])
    cached = cache["files"].get(path) if cache is not None else None
    if cached is not None:
        cache["bytes"] -= cached[3]
        del cache["files"][path]
    existing = _RESUMABLE_DIGESTS.get(path)
    if existing is not None and existing["stamp"] != stamp:
        _RESUMABLE_DIGESTS.pop(path, None)


# LLM: 复用规范2秒粗时钟窗口，mtime/ctime取较新者；窗口内文件和目录都做内容观察，窗口外只核元数据。
# 函数用途: 取得来源代次；单次文件读取严格受256KiB上限约束。
def source_stamp(path):
    if not isinstance(path, Path):
        path = Path(path)
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        _cached_source_body(path, None)
        return None
    stamp = _metadata_stamp(info)
    if _is_settled(stamp):
        _evict_hot_body_cache(path, stamp)
        return stamp
    if stat.S_ISREG(info.st_mode):
        return (*stamp, _file_digest(path, stamp))
    if stat.S_ISDIR(info.st_mode):
        return (*stamp, _directory_digest(path, stamp))
    return stamp


# LLM: 快照监测入口对大热源执行有界多片核验（最多1MiB），未完成全片核验时返回partial代次以阻断放行。
# 函数用途: 为引用快照收集或复核来源代次。
def _watched_source_stamp(path):
    if not isinstance(path, Path):
        path = Path(path)
    try:
        info = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        _cached_source_body(path, None)
        return None
    stamp = _metadata_stamp(info)
    if _is_settled(stamp):
        _evict_hot_body_cache(path, stamp)
        return stamp
    if stat.S_ISREG(info.st_mode):
        return (*stamp, _verify_complete_file_digest(path, stamp))
    if stat.S_ISDIR(info.st_mode):
        return (*stamp, _directory_digest(path, stamp))
    return stamp


# LLM: 热转冷只在全部切片真实字节仍等于旧摘要时通过；未完成切片抛_PartialPinVerification等待下轮续验。
# 函数用途: 既不漏掉冷却前预算外同tick改写，也不把单纯冷却视为来源损坏。
def _matching_stamp(path, current, previous):
    if not _is_complete_stamp(current) or not _is_complete_stamp(previous):
        return False
    if current == previous:
        return True
    if (current is None or previous is None or len(current) != 6 or len(previous) != 7
            or current != previous[:6]):
        return False
    digest = _verify_complete_file_digest(path, current) if stat.S_ISREG(current[2]) else _directory_digest(path, current)
    if isinstance(digest, tuple) and digest[0] == "partial":
        raise _PartialPinVerification("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
    matched = digest == previous[6]
    if matched:
        _RESUMABLE_DIGESTS.pop(path, None)
    return matched


# LLM: partial只可续同元数据、同内容代次的post阶段；变化抛错，热转冷须全量比对后才移除热摘要。
#   发布前复核丢弃本current小文件摘要，所有解析之后至少实际复读一次，构建期复用不能替代。
# 函数用途: 逐源推进读后验证，未完成时更新待续观察而不发布pins。
def _refresh_sources(sources):
    cache = _SOURCE_CONTENTS.get()
    if cache is not None:
        cache["verified"] = {}
        cache["recheck"] = set()
    complete = True
    for path, previous in list(sources.items()):
        current = _watched_source_stamp(path)
        settled = current is not None and len(current) == 6 and previous is not None and len(previous) == 7
        if settled and stat.S_ISREG(current[2]) and isinstance(previous[6], tuple):
            current = (*current, _verify_complete_file_digest(path, current))
        if not _is_complete_stamp(previous) or not _is_complete_stamp(current):
            if not _same_post_generation(path, current, previous):
                raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
            complete = complete and _is_complete_stamp(current)
        elif not _matching_stamp(path, current, previous):
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        if settled and _is_complete_stamp(current):
            current = current[:6]
            _RESUMABLE_DIGESTS.pop(path, None)
        sources[path] = current
    return complete


# LLM: partial也携带原解析基线；同stat/generation的LRU替换仍须基线相同，不能将新内容摘要嫁接到旧pins。
# 函数用途: 识别预算耗尽而非真实来源变化。
def _same_post_generation(path, current, previous):
    state = _RESUMABLE_DIGESTS.get(path, {})
    return (current is not None and previous is not None and len(current) == len(previous) == 7
            and current[:6] == previous[:6] and isinstance(current[6], tuple) and isinstance(previous[6], tuple)
            and state.get("phase") == "post" and state.get("baseline") is not None
            and current[6][1] == previous[6][1] == state["generation"]
            and (previous[6][2] == state["baseline"] if not _is_complete_stamp(previous)
                 else previous[6][2:] == state["baseline"]))


# LLM: 同轮post验证未完可降为partial，但元数据/内容代次变化必须抛错，不能跳过读后观察。
# 函数用途: 将文件或发现目录加入当前引用视图的失效监测。
def watch_pin_source(path):
    sources = _SOURCES.get()
    if sources is not None:
        path = Path(path)
        stamp = _watched_source_stamp(path)
        if path in sources:
            previous = sources[path]
            if not _is_complete_stamp(stamp) or not _is_complete_stamp(previous):
                if not _same_post_generation(path, stamp, previous):
                    raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
            elif not _matching_stamp(path, stamp, previous):
                raise OSError("MEMORY_RETENTION_PIN_SOURCE_CHANGED")
        sources[path] = stamp


# LLM: 仅重置本次扫描的观察集合，不重置current摘要预算；重试和目标重建共用该入口。
# 函数用途: 丢弃失败尝试的旧来源并重新绑定owner与策略。
def reset_watched_pin_sources(home):
    sources = _SOURCES.get()
    if isinstance(sources, dict):
        sources.clear()
        watch_pin_source(home.owner_home_dir)
        watch_pin_source(home.owner_retention_json)


# LLM: 仅在调用方已验证同inode非缩短的目标FD起点时使用，不解除读中变化或JSON损坏检查。
# 函数用途: 将允许的打开前追加重新登记为本次来源起点。
def rebaseline_watched_pin_source(path):
    sources = _SOURCES.get()
    if sources is not None:
        sources.pop(Path(path), None)
        watch_pin_source(path)


# LLM: 对象仅在一次apply内使用；旧视图必须逐源重核，变化/错误完整重建且保留原complete门，不跨owner缓存。
# 类用途: 在同阶段复用引用解析结果，保留执行前新引用与坏源的关闭式防线。
class ReferencePinSnapshot:
    # LLM: 构造不读写文件；待发布pins与来源成对暂存，临时块对照与FD只属于该snapshot。
    # 函数用途: 绑定本次owner和严格扫描器，准备有界热源块缓存。
    def __init__(self, home, roots, now, builder):
        self.home, self.roots, self.now, self.builder = home, roots, now, builder
        self.sources, self.value = {}, None
        self.pending = None
        self.contents = {"files": {}, "fds": {}, "bytes": 0, "digest_bytes": {}, "verified": {}, "recheck": set()}
        weakref.finalize(self, _close_source_descriptors, self.contents["fds"])

    # LLM: 每current重置字节预算、小文件摘要和pass；热源post只认本pass读到的片，已发布视图下一current必须重开内容验证。
    # 函数用途: 在本次引用复核中启用有界热源字节对照。
    def current(self, errors, *, target=None):
        self.contents["digest_bytes"] = {}
        self.contents["verified"] = {}
        self.contents["recheck"] = set()
        self.contents["pass_token"] = object()
        token = _SOURCE_CONTENTS.set(self.contents)
        try:
            return self._current(errors, target=target)
        finally:
            _SOURCE_CONTENTS.reset(token)

    # LLM: 只有全源完成读后验证才发布；pending仅续验不重解析，真实变化丢弃旧pins，重建不重置预算。
    # 函数用途: 逐源重核或续验暂存结果，冷源仍只查元数据。
    def _current(self, errors, *, target=None):
        previous = self.pending or ((self.value, self.sources) if self.value is not None else None)
        if previous is not None:
            result, sources = previous
            self.value = None
            self.pending = None
            try:
                stable = _refresh_sources(sources)
            except OSError:
                stable = None
            if stable:
                self.sources, self.value = sources, result
                return self.value
            if stable is False and _post_verification_pending(sources):
                self.pending = (result, dict(sources))
                raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
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
            stable = None
            if result[1]:
                try:
                    stable = _refresh_sources(sources)
                except OSError:
                    pass
        finally:
            _SOURCES.reset(token)
        self.sources = sources
        self.value = result if stable and result[1] else None
        if stable is False and result[1] and _post_verification_pending(sources):
            self.pending = (result, dict(sources))
        if self.value is None:
            raise OSError("MEMORY_RETENTION_PIN_SOURCE_UNREADABLE")
        return self.value


# LLM: 只允许全源读取成功且唯一欠账为同代post切片的结果暂存；预验partial不能携带已解析pins。
# 函数用途: 区分可以续验的待发布结果与必须重建的不完整来源。
def _post_verification_pending(sources):
    partial = [path for path, stamp in sources.items() if not _is_complete_stamp(stamp)]
    return bool(partial) and all(_RESUMABLE_DIGESTS.get(path, {}).get("phase") == "post" for path in partial)
