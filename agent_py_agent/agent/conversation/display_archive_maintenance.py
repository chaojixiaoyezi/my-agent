# LLM: 显式维护展示副本；整档验证后删页、TTL先发墓碑，open后立即承担FD清理，不读取模型或业务账本。
# 模块用途: 在原归档根有界合页和按保留策略回收原文，不自动执行生产清理。
from __future__ import annotations

import json
import math
import os
import re
import stat
import time
import uuid
from collections import OrderedDict
from contextlib import ExitStack, contextmanager
from itertools import islice
from pathlib import Path
from threading import RLock

from ..common.nofollow_fs import open_directory_beneath
from .display_archive import (
    _ARCHIVE_ID,
    _MAX_PAGE_BYTES,
    DISPLAY_BUNDLE_SCHEMA,
    DisplayArchiveError,
    _bundle_limit,
    _fdopen_owned,
    _validate_bundle,
    _validate_page_rows,
    validate_display_archive_reference,
)

_PAGE_NAME = re.compile(r"(?:0|[1-9][0-9]*)\.json\Z")
# 展示归档维护将保留天数换算为秒所用的每日秒数。
_DISPLAY_ARCHIVE_DAY_SECONDS = 86400
# 每个处理名额最多枚举16个目录项，跳过与非UUID名称同样消耗扫描预算。
_DISPLAY_ARCHIVE_SCAN_MULTIPLIER_ENTRIES = 16
# 小批维护至少允许枚举64项，避免少量未到期归档挡住本批有效工作。
_DISPLAY_ARCHIVE_MIN_SCAN_BUDGET_ENTRIES = 64

# 只保留有限个目录/操作的内存进度；新进程从归档根父目录的游标文件恢复，不向归档目录写游标文件。
_scan_cursors: OrderedDict[tuple[int, int, str], int] = OrderedDict()
# 续扫中的目录迭代器只在本进程有效，位置须与_scan_cursors一致才可接着读，避免每次重开后从头丢弃旧项。
_live_scans: OrderedDict[tuple[int, int, str], tuple[ExitStack, object, int]] = OrderedDict()
_scan_cursor_lock = RLock()
_CURSOR_FILE = ".display_archives.cursor.json"


# LLM: 维护只接受Store或明确的会话存储根；不猜Home路径，根缺失不新建。
# 函数用途: 取得显示归档根，兼容直接传入display_archives目录。
def _root(value) -> Path:
    storage = getattr(value, "storage", None)
    root = Path(storage.root if storage is not None else value)
    return root if root.name == "display_archives" else root / "display_archives"


# LLM: 非有限时间与负预算在任何文件修改前拒绝，max为0允许显式停用本次维护。
# 函数用途: 统一校验调用方的时钟、天数和处理上限。
def _options(now, days, maximum):
    now = time.time() if now is None else now
    if (type(now) not in (int, float) or not math.isfinite(now)
            or any(type(day) not in (int, float) or not math.isfinite(day) or day < 0 for day in days)
            or type(maximum) is not int or maximum < 0):
        raise ValueError("维护时间、期限或批次上限无效")
    return now


# LLM: 仅POSIX严格dirfd维护；在根目录inode上非阻塞flock串行两种维护，不增每档锁文件。
# 函数用途: 固定归档根并取得本次维护锁，退出释放全部描述符。
@contextmanager
def _locked_root(root):
    import fcntl
    fd = open_directory_beneath(root, (), require_dir_fd=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield fd
    finally:
        os.close(fd)


# LLM: 从固定父FD打开并核一次fstat；open成功后任一校验异常都关闭FD，成功才转交调用者。
# 函数用途: 固定一个待维护的UUID目录。
def _open_directory(parent, name):
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise OSError("归档目录形状无效")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        opened = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("归档目录已变更")
        return fd
    except BaseException:
        os.close(fd)
        raise


# LLM: 普通单链接叶前后stat一致且有界；fdopen构造前也承担关闭责任，返回stat供删除复核。
# 函数用途: 在同一归档描述符下读取JSON与其文件身份。
def _read(fd, name, limit):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    with _fdopen_owned(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("归档叶形状或大小无效")
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if len(raw) > limit or _signature(before) != _signature(after):
        raise ValueError("归档叶读取期间变化")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("归档JSON必须是对象")
    return value, before


# LLM: 精确inode、字节数和修改时间用于删除前复核，不按正文或文件名猜身份。
# 函数用途: 构造普通叶的变更签名。
def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


# LLM: 同目录0600临时叶刷盘后replace；fdopen失败关闭裸FD，失败不删原页并清理临时名。
# 函数用途: 原子发表合页或过期墓碑并清理自己的临时文件。
def _replace(fd, name, raw):
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with _fdopen_owned(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            old = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            old = None
        if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_nlink != 1):
            raise OSError("拒绝替换链接或特殊文件")
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


# LLM: 删除只针对已读回身份且仍普通单链接的叶；缺失是幂等成功，变化则保留。
# 函数用途: 清理被完整替代的单页或已过期载荷。
def _unlink(fd, name, expected):
    try:
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or _signature(current) != _signature(expected):
        raise OSError("归档叶删除前变化")
    os.unlink(name, dir_fd=fd)


# LLM: 磁盘manifest允许TTL附加字段，但调用引用仍严格六字段；页数与计数必须为非负整数。
# 函数用途: 校验当前归档身份和索引。
def _manifest(fd, archive_id):
    value, metadata = _read(fd, "manifest.json", 4096)
    reference = {key: value[key] for key in ("schema", "archive_id", "thread_id", "page_count", "row_count", "char_count")}
    validate_display_archive_reference(reference)
    if (value["archive_id"] != archive_id or type(value["page_count"]) is not int or value["page_count"] < 1
            or any(type(value[key]) is not int or value[key] < 0 for key in ("row_count", "char_count"))):
        raise ValueError("归档索引无效")
    return value, metadata


# LLM: 删除目录前核根下该名称仍指向本次打开inode，rmdir本身只接受空目录，不跟随链接。
# 函数用途: 安全回收已清空的UUID目录并刷盘。
def _remove_directory(root, fd, archive_id):
    actual = os.stat(archive_id, dir_fd=root, follow_symlinks=False)
    opened = os.fstat(fd)
    if not stat.S_ISDIR(actual.st_mode) or (actual.st_dev, actual.st_ino) != (opened.st_dev, opened.st_ino):
        raise OSError("存根目录删除前变化")
    os.rmdir(archive_id, dir_fd=root)
    os.fsync(root)


# LLM: 单档合页大小受读接口上限约束；已有bundle仅在逐页匹配后用于崩溃续清理。
# 函数用途: 无损发表合页并删除已替代的独立页，返回是否回收了页文件。
def _compact(fd, manifest):
    count = manifest["page_count"]
    limit = _bundle_limit(count)
    if count > limit // 2:
        raise ValueError("合页页数超出有界容量")
    try:
        bundle, _ = _read(fd, "pages_bundle.json", limit)
    except FileNotFoundError:
        bundle = None
    pages = _validate_bundle(bundle, manifest) if bundle is not None else []
    originals = []
    total_bytes = 0
    for index in range(count):
        try:
            value, metadata = _read(fd, f"{index}.json", _MAX_PAGE_BYTES)
        except FileNotFoundError:
            if bundle is None:
                raise
            continue
        rows = value.get("rows")
        _validate_page_rows(rows)
        total_bytes += metadata.st_size
        if total_bytes > limit:
            raise ValueError("原页累计超过合页预算")
        if bundle is not None and pages[index] != rows:
            raise ValueError("原页与合页不同")
        if bundle is None:
            pages.append(rows)
        originals.append((f"{index}.json", metadata))
    if not originals:
        return False
    if bundle is None:
        bundle = {"schema": DISPLAY_BUNDLE_SCHEMA, "archive_id": manifest["archive_id"], "thread_id": manifest["thread_id"], "pages": pages}
        raw = json.dumps(bundle, ensure_ascii=False, separators=(",", ":")).encode()
        if len(raw) > limit:
            raise ValueError("合页超过读取上限")
        _replace(fd, "pages_bundle.json", raw)
    readback, _ = _read(fd, "pages_bundle.json", limit)
    if _validate_bundle(readback, manifest) != pages:
        raise ValueError("合页读回不一致")
    for name, metadata in originals:
        _unlink(fd, name, metadata)
    os.fsync(fd)
    return True


# LLM: 只检查当前窗口内的目录项，沿原dirfd与逐档错误合同处理；成功名额耗尽后不多取下一项。
#   续扫迭代器可能给出已被删除的旧名，按已枚举计入预算但不算检查或错误。
# 函数用途: 扫描一个有界窗口，返回实际枚举数、是否达到处理上限及已消失项数。
def _scan_entries(root, entries, budget, maximum, action, result):
    inspected = vanished = 0
    for entry in islice(entries, budget):
        inspected += 1
        if not _ARCHIVE_ID.fullmatch(entry.name):
            continue
        try:
            fd = _open_directory(root, entry.name)
        except FileNotFoundError:
            vanished += 1
            continue
        except OSError:
            result["scanned"] += 1
            result["errors"] += 1
            continue
        result["scanned"] += 1
        try:
            key = action(root, fd, entry.name)
            result[key or "skipped"] += 1
        except (OSError, ValueError, KeyError, DisplayArchiveError):
            result["errors"] += 1
        finally:
            os.close(fd)
        if result["compacted"] + result["expired"] + result["removed"] >= maximum:
            return inspected, True, vanished
    return inspected, False, vanished


# LLM: 迭代器用独立打开的目录描述，不复制持flock的根FD，跨调用保留也不延长维护锁；ExitStack承担scandir关闭责任。
# 函数用途: 为已锁归档根打开一个可跨调用续读的目录迭代器。
def _open_scan(root):
    descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=root)
    scans = ExitStack()
    try:
        entries = iter(scans.enter_context(os.scandir(descriptor)))
    except BaseException:
        scans.close()
        raise
    finally:
        os.close(descriptor)
    return scans, entries


# LLM: 游标只写归档根父目录下的普通单链接点文件，且父目录中的display_archives须仍是已锁根inode；坏、缺或身份不符一律视为0。
#   保存失败只放弃跨进程续点，不改变本次维护结果，也不向归档目录写任何文件。
# 函数用途: 为新进程读取（offset为None）或保存某维护操作的续扫偏移。
def _persisted_cursor(root_path, root_info, operation, offset=None):
    identity = [int(root_info.st_dev), int(root_info.st_ino)]
    try:
        parent = open_directory_beneath(root_path.parent, (), require_dir_fd=True)
    except (OSError, ValueError):
        return 0
    try:
        current = os.stat(root_path.name, dir_fd=parent, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != identity:
            return 0
        try:
            saved, _ = _read(parent, _CURSOR_FILE, 4096)
        except (FileNotFoundError, ValueError):
            saved = {}
        value = saved.get(operation)
        loaded = (value[2] if isinstance(value, list) and len(value) == 3 and value[:2] == identity
                  and type(value[2]) is int and value[2] >= 0 else 0)
        if offset is None or offset == loaded:
            return loaded
        saved[operation] = [*identity, offset]
        _replace(parent, _CURSOR_FILE, json.dumps(saved, separators=(",", ":")).encode())
        return offset
    except (OSError, ValueError):
        return 0
    finally:
        os.close(parent)


# LLM: 原根flock保护同目录维护；游标按真实根inode及操作隔离，同进程保留活迭代器续读，每个枚举项都计入预算，回绕共用同一预算。
#   内存LRU最多128项、活迭代器最多8个；活迭代器缺失（新进程或被淘汰）时按游标一次性重放前缀后再续扫。
# 函数用途: 小批维护从上次位置续扫，避免固定前缀挡住后续可处理归档，也不让续扫代价随偏移线性增长。
def _maintain(value, maximum, action):
    result = {"scanned": 0, "compacted": 0, "expired": 0, "removed": 0, "skipped": 0, "errors": 0}
    if maximum == 0:
        return result
    scan_budget = max(_DISPLAY_ARCHIVE_MIN_SCAN_BUDGET_ENTRIES, maximum * _DISPLAY_ARCHIVE_SCAN_MULTIPLIER_ENTRIES)
    root_path = _root(value)
    try:
        with _locked_root(root_path) as root:
            root_info = os.fstat(root)
            # 两个闭包都名为action，限定名才区分合页和TTL；身份取已锁住的FD，不重走路径。
            operation = action.__qualname__
            cursor_key = (int(root_info.st_dev), int(root_info.st_ino), operation)
            with _scan_cursor_lock:
                offset = _scan_cursors.get(cursor_key)
                live = _live_scans.pop(cursor_key, None)
            if live is not None and live[2] != offset:
                live[0].close()
                live = None
            if offset is None:
                offset = _persisted_cursor(root_path, root_info, operation)
            scans, entries = live[:2] if live is not None else (None, None)
            try:
                if scans is None:
                    scans, entries = _open_scan(root)
                    if offset and sum(1 for _ in islice(entries, offset)) < offset:
                        # 目录缩短到游标之前时重开，不把空尾部当作完整的一轮检查。
                        scans.close()
                        offset = 0
                        scans, entries = _open_scan(root)
                inspected, hit_limit, vanished = _scan_entries(root, entries, scan_budget, maximum, action, result)
                position = max(0, offset + inspected - result["removed"] - vanished)
                if inspected < scan_budget and not hit_limit:
                    # 走到目录尾：关闭旧迭代器，从头只补到本次起点，回绕与正常检查共用本次预算。
                    scans.close()
                    scans, position = None, 0
                    if offset:
                        removed_before_wrap = result["removed"]
                        limit = min(offset, scan_budget - inspected)
                        scans, entries = _open_scan(root)
                        wrapped, hit_limit, vanished = _scan_entries(root, entries, limit, maximum, action, result)
                        position = max(0, wrapped - (result["removed"] - removed_before_wrap) - vanished)
                        if wrapped < limit and not hit_limit:
                            scans.close()
                            scans, position = None, 0
            except BaseException:
                if scans is not None:
                    scans.close()
                raise
            with _scan_cursor_lock:
                _scan_cursors[cursor_key] = position
                _scan_cursors.move_to_end(cursor_key)
                while len(_scan_cursors) > 128:
                    evicted = _live_scans.pop(_scan_cursors.popitem(last=False)[0], None)
                    if evicted is not None:
                        evicted[0].close()
                if scans is not None:
                    _live_scans[cursor_key] = (scans, entries, position)
                    while len(_live_scans) > 8:
                        _live_scans.popitem(last=False)[1][0].close()
            _persisted_cursor(root_path, root_info, operation, position)
    except FileNotFoundError:
        pass
    except (OSError, ImportError):
        result["errors"] += 1
    return result


# LLM: 仅处理manifest修改时间满阈值的已发布归档；过期档不合页，失败不删除唯一副本。
# 函数用途: 把超过一天的多页展示无损合成一个私有bundle。
def compact_display_archives(store_or_root, *, now: float | None = None, min_age_days: float = 1.0, max_archives: int = 1000) -> dict[str, int]:
    now = _options(now, (min_age_days,), max_archives)
    # LLM: 回调运行于原根锁内，只修改本次已打开UUID目录。
    # 函数用途: 判断单档年龄后执行合页。
    def action(root, fd, archive_id):
        manifest, metadata = _manifest(fd, archive_id)
        if manifest.get("expired") is True or metadata.st_mtime > now - min_age_days * _DISPLAY_ARCHIVE_DAY_SECONDS:
            return None
        return "compacted" if _compact(fd, manifest) else None
    return _maintain(store_or_root, max_archives, action)


# LLM: pin整档保护；14天先刷盘墓碑再删载荷，墓碑按expired_at另保留30天，绝不递归删除未知文件。
# 函数用途: 显式回收过期展示正文，再清除达到存根期限的空档。
def prune_expired_display_archives(store_or_root, *, now: float | None = None, ttl_days: float = 14.0, pinned_archive_ids: set[str] | None = None, remove_expired_dirs_after_days: float = 30.0, max_archives: int = 1000) -> dict[str, int]:
    now = _options(now, (ttl_days, remove_expired_dirs_after_days), max_archives)
    pins = frozenset(pinned_archive_ids or ())
    # LLM: 先预检所有已知载荷；墓碑已发表但删除中断时重跑可继续收尾，不触碰链接。
    # 函数用途: 对单档完成过期或存根删除。
    def action(root, fd, archive_id):
        if archive_id in pins:
            return None
        try:
            manifest, metadata = _manifest(fd, archive_id)
        except FileNotFoundError:
            # 进程在最后unlink/rmdir之间崩溃时仅剩空目录；另保留完整存根期限，不能删新producer。
            if not os.listdir(fd) and os.fstat(fd).st_mtime <= now - remove_expired_dirs_after_days * _DISPLAY_ARCHIVE_DAY_SECONDS:
                _remove_directory(root, fd, archive_id)
                return "removed"
            return None
        expired = manifest.get("expired") is True
        if not expired and metadata.st_mtime > now - ttl_days * _DISPLAY_ARCHIVE_DAY_SECONDS:
            return None
        payloads = []
        for name in os.listdir(fd):
            if name == "pages_bundle.json" or _PAGE_NAME.fullmatch(name):
                leaf = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(leaf.st_mode) or leaf.st_nlink != 1:
                    raise OSError("过期载荷不是普通单链接文件")
                payloads.append((name, leaf))
        if not expired:
            manifest = {**manifest, "expired": True, "expired_at": now}
            _replace(fd, "manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode())
        for name, leaf in payloads:
            _unlink(fd, name, leaf)
        os.fsync(fd)
        expired_at = manifest.get("expired_at")
        if (expired and type(expired_at) in (int, float) and math.isfinite(expired_at)
                and expired_at <= now - remove_expired_dirs_after_days * _DISPLAY_ARCHIVE_DAY_SECONDS
                and os.listdir(fd) == ["manifest.json"]):
            _unlink(fd, "manifest.json", metadata)
            try:
                _remove_directory(root, fd, archive_id)
            except OSError:
                # 普通rmdir故障恢复同一墓碑，让下一轮直接续清；真正进程崩溃由上面的空目录期限兜底。
                if not os.listdir(fd):
                    _replace(fd, "manifest.json", json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode())
                raise
            return "removed"
        return "expired" if not expired else None
    return _maintain(store_or_root, max_archives, action)
