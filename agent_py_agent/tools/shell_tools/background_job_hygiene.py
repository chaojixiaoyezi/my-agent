# LLM: 显式维护jobs_root；与权威Store共锁并隔离复核准确inode/回执字节，变更前重查全部共享引用，不探测PID。
# 模块用途: 安全维护退出日志与内外置回执，运行态、pin、共享引用及并发替换均保守保留。
from __future__ import annotations

import errno
import json
import math
import os
import stat
import time
import uuid
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path

from ...agent.common.directory_lock import locked_private_directory
from ...agent.common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
)
from ...agent.tooling.process_session_commit import recover_process_commit
from ...agent.tooling.process_session_store import process_session_store_root

_DAY = 86400
_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "killed", "exited", "not_started"}
_LOG_SUFFIXES = {".log", ".out", ".err"}
_MARKER = b"\n...[truncated by 3d background_job_hygiene]...\n"
_MAX_METADATA_BYTES = 1024 * 1024


# LLM: 隔离对象保留表示部分提交未自动解决；调用者必须显式恢复，不能按普通重试删除它。
# 类用途: 让维护返回可识别的人工恢复责任计数。
class _RecoveryRequired(OSError):
    pass


# LLM: 只解析本机固定libc符号，缺原子不覆盖能力时失败关闭，不退回check-then-rename或硬链接。
# 函数用途: 取得Linux/macOS的目录FD排他rename原语。
@lru_cache(maxsize=1)
def _exclusive_rename_api():
    import ctypes
    import sys

    library = ctypes.CDLL(None, use_errno=True)
    # Linux RENAME_NOREPLACE=1；Darwin RENAME_EXCL=4，两者都由内核原子拒绝已有目标。
    symbol, flag = ("renameatx_np", 4) if sys.platform == "darwin" else ("renameat2", 1)
    function = getattr(library, symbol, None)
    if function is None:
        raise OSError(errno.ENOTSUP, "缺少原子不覆盖rename")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    return function, flag


# LLM: 发表和失败恢复均用同一parent FD的原子不覆盖rename；即使不协作写者在末次检查后重建名字也不能覆写。
# 函数用途: 将已隔离文件或完整新文件移动到确实空闲的原名字。
def _rename_exclusive(fd, source, target):
    import ctypes

    function, flag = _exclusive_rename_api()
    if function(fd, os.fsencode(source), fd, os.fsencode(target), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


# LLM: 只接受有限数值或明确带时区的ISO时间；无时间仅在已有终态证据时回退mtime。
# 函数用途: 读取可比较的完成时间，不把None、布尔值或坏日期当退出证据。
def _timestamp(value):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return float(value)
    if isinstance(value, str):
        from datetime import datetime
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo is not None else None
        except ValueError:
            pass
    return None


# LLM: 终态、整数exit_code或显式完成时刻才允许维护；永不查询进程是否还在。
# 函数用途: 判定作业已有可用的退出记录。
def _exited(record):
    return (record.get("status") in _TERMINAL or type(record.get("exit_code")) is int
            or _timestamp(record.get("ended_at")) is not None or _timestamp(record.get("finished_at")) is not None)


# LLM: 调用方已选择并锁定具体Store；不递归读隐藏事务数据，遗留隔离对象使维护失败保留证据。
# 函数用途: 枚举一个回执目录内的JSON，不把隔离文件误认作新作业。
def _receipts(root, *, ignored=()):
    for name in list_names_beneath(root, ()):
        if name.startswith(".purge_") and name not in ignored:
            raise _RecoveryRequired("上轮维护隔离对象待恢复")
        if not name.startswith(".") and name.endswith(".json"):
            yield (name,)


# LLM: 回执有界单链接读取，同时捕获实际FD元数据供隔离提交核验，损坏不返回空对象。
# 函数用途: 读取回执正文、原字节与准确文件版本。
def _record(root, parts):
    descriptor = open_readonly_file_beneath(root, parts)
    with _fdopen_owned(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        raw = stream.read(_MAX_METADATA_BYTES + 1)
        after = os.fstat(stream.fileno())
    if (len(raw) > _MAX_METADATA_BYTES or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1 or _signature(before) != _signature(after)):
        raise OSError("后台回执读取期间变化或超限")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("后台回执不是JSON对象")
    return value, (raw, before)


# LLM: 内置子目录回执可引用jobs根或同目录日志，外置Store只可引用jobs根；不resolve链接或穿越。
# 函数用途: 按可信日志根解析回执输出；引用保护可保留暂时缺失的目标名字。
def _logs(root, parts, record, *, receipt_root=None, include_missing=False):
    source = root if receipt_root is None else receipt_root
    try:
        parent = source.relative_to(root).parts + parts[:-1]
    except ValueError:
        parent = ()
    stem = Path(parts[-1]).stem
    names = {(*parent, stem + suffix) for suffix in _LOG_SUFFIXES}
    for key in ("output_file", "stdout_file", "stderr_file", "log_file", "log_path"):
        value = record.get(key)
        if not value:
            continue
        if not isinstance(value, str):
            raise ValueError("日志路径不是文本")
        target = Path(value)
        if ".." in target.parts:
            raise ValueError("日志路径包含穿越")
        if target.is_absolute():
            relative = target.relative_to(root)
        else:
            relative = Path(*parent) / target
        if relative.parts[:-1] not in {(), parent} or relative.suffix not in _LOG_SUFFIXES:
            raise ValueError("日志不在受管作业目录")
        names.add(relative.parts)
    result = []
    for log in sorted(names):
        path = root.joinpath(*log)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if include_missing:
                result.append(log)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("日志不是普通单链接文件")
        # 父目录同样经过严格nofollow校验，不能只凭叶lstat判断根内归属。
        parent_fd = open_directory_beneath(root, log[:-1], require_dir_fd=True)
        os.close(parent_fd)
        result.append(log)
    return result


# LLM: fdopen成功才转移关闭责任；构造异常关闭裸FD，不能依赖文件对象尚未建立的finally。
# 函数用途: 为日志与回执读取安全封装描述符。
def _fdopen_owned(descriptor, mode):
    try:
        return os.fdopen(descriptor, mode)
    except BaseException:
        os.close(descriptor)
        raise


# LLM: seek有界头尾且前后核FD；fdopen失败同样回收描述符，UTF8边界丢弃不完整码点。
# 函数用途: 提取有界首2048字节和尾4096字节及原始大小。
def _preview(root, parts):
    descriptor = open_readonly_file_beneath(root, parts)
    with _fdopen_owned(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        head = stream.read(2048)
        stream.seek(max(0, before.st_size - 4096))
        tail = stream.read(4096)
        after = os.fstat(stream.fileno())
    if _signature(before) != _signature(after):
        raise OSError("日志读取期间变化")
    head = head.decode("utf-8", errors="ignore").encode()
    tail = tail.decode("utf-8", errors="ignore").encode()
    return head + _MARKER + tail, tail.decode(), before


# LLM: 隔离rename可能改变ctime，合同使用dev/ino/size/mtime，普通单链接类型另行硬校验。
# 函数用途: 构造并核对日志变更签名。
def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


# LLM: 持Store锁隔离旧叶再核inode和原字节；恢复用原子不覆盖rename，原名被占时保留责任且不删新对象。
# 函数用途: 固定本次要变更的文件，退出时恢复未消费的隔离文件。
@contextmanager
def _claimed(root, parts, expected, *, raw=None, kind="log"):
    fd = open_directory_beneath(root, parts[:-1], require_dir_fd=True)
    name, temporary = parts[-1], f".purge_{kind}_{uuid.uuid4().hex}"
    moved = False
    try:
        _exclusive_rename_api()
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or _signature(current) != _signature(expected):
            raise OSError("隔离前文件已变化")
        os.rename(name, temporary, src_dir_fd=fd, dst_dir_fd=fd)
        moved = True
        claimed = os.stat(temporary, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(claimed.st_mode) or claimed.st_nlink != 1 or _signature(claimed) != _signature(expected):
            raise OSError("隔离对象不是已验证版本")
        if raw is not None:
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with _fdopen_owned(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                actual = stream.read(_MAX_METADATA_BYTES + 1)
                after = os.fstat(stream.fileno())
            if (opened.st_nlink != 1 or after.st_nlink != 1 or _signature(opened) != _signature(expected)
                    or _signature(after) != _signature(expected) or actual != raw):
                raise OSError("隔离回执原字节已变化")
        yield fd, temporary
    finally:
        try:
            if moved:
                try:
                    os.stat(temporary, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    # Store写者共享锁，恢复只允许原名缺失；已有新名字时保留隔离叶供人工恢复，不覆盖新写者。
                    try:
                        os.stat(name, dir_fd=fd, follow_symlinks=False)
                    except FileNotFoundError:
                        try:
                            _rename_exclusive(fd, temporary, name)
                        except OSError as exc:
                            raise _RecoveryRequired("隔离对象未恢复") from exc
                        os.fsync(fd)
                    else:
                        raise _RecoveryRequired("原名已重建，保留隔离文件待恢复")
        finally:
            os.close(fd)


# LLM: 临时写入/发表/清理全锚定原parent FD；内核排他rename拒绝末刻重建，fsync失败保留隔离责任。
# 函数用途: 原子不覆盖地发表完整新文件，再消费已核验旧叶。
def _publish_claimed(root, parts, raw, fd, claimed):
    temporary = f".hygiene_new_{uuid.uuid4().hex}"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        with _fdopen_owned(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError("发表目标被并发重建")
        _rename_exclusive(fd, temporary, parts[-1])
        os.fsync(fd)
        os.unlink(claimed, dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


# LLM: 保留所有原字段，在共享Store锁内隔离并核原始字节/元数据，绝不覆盖已经更新的回执。
# 函数用途: 原子发表维护摘要，后续日志变更只能在此成功之后。
def _save(root, parts, record, expected):
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > _MAX_METADATA_BYTES:
        raise ValueError("摘要回执超过上限")
    with _claimed(root, parts, expected[1], raw=expected[0], kind="receipt") as (fd, claimed):
        _publish_claimed(root, parts, raw, fd, claimed)
    _, current = _record(root, parts)
    if current[0] != raw:
        raise OSError("回执发表后变化")
    return current


# LLM: 仅选择jobs根及一层非隐藏子目录和显式权威Store，不全盘发现owner；危险目录不跟随。
# 函数用途: 建立本次必须共同持锁的回执目录清单。
def _store_directories(root, external):
    roots = {root}
    for name in list_names_beneath(root, ()):
        metadata = (root / name).lstat()
        if not name.startswith(".") and stat.S_ISDIR(metadata.st_mode):
            roots.add(root / name)
    if external is not None:
        try:
            metadata = external.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError("外置回执根不是普通目录")
            roots.add(external)
    return tuple(sorted(roots))


# LLM: 本次锁集合目录身份及当前回执版本必须仍一致；重扫全部其它引用，新目录/未知归属一律拒绝。
# 函数用途: 在日志变更前同时核当前回执及所有共享引用，不能只相信初扫清单。
def _referenced(root, log, sources, identities, external, own, *, own_expected, claim_path=None):
    if _store_directories(root, external) != sources:
        raise OSError("回执目录清单已变化")
    _, actual = _record(*own)
    if actual[0] != own_expected[0] or _signature(actual[1]) != _signature(own_expected[1]):
        raise OSError("当前回执在日志操作前变化")
    for source in sources:
        current = source.lstat()
        if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identities[source]:
            raise OSError("回执目录已替换")
        ignored = (claim_path.name,) if claim_path is not None and claim_path.parent == source else ()
        for parts in _receipts(source, ignored=ignored):
            if (source, parts) == own:
                continue
            record, _ = _record(source, parts)
            if log in _logs(root, parts, record, receipt_root=source, include_missing=True):
                return True
    return False


# LLM: 持所有来源Store锁；原回执签名、隔离inode与共享引用每次变更均重核，先存摘要才消费日志。
# 函数用途: 处理准确终态版本，竞争或未知归属时保留原文件并拒绝变更。
def _maintain_job(root, parts, record, original, now, slim_days, raw_days, summary_days, protected,
                  *, receipt_root, sources, identities, external):
    metadata = original[1]
    completed = _timestamp(record.get("ended_at")) or _timestamp(record.get("finished_at")) or record.get("hygiene_completed_at") or metadata.st_mtime
    if type(completed) not in (int, float) or not math.isfinite(completed):
        raise ValueError("退出时间无效")
    age = (now - completed) / _DAY
    if age < min(slim_days, raw_days, summary_days):
        return 0, 0, 0
    logs = _logs(root, parts, record, receipt_root=receipt_root)
    own = (receipt_root, parts)
    if any(log in protected or _referenced(root, log, sources, identities, external, own, own_expected=original) for log in logs):
        return 0, 0, 0
    previews = [(log, *_preview(root, log)) for log in logs]
    update = dict(record)
    update["hygiene_completed_at"] = completed
    sizes = dict(record.get("original_log_bytes") or {})
    tails = dict(record.get("tail_preview") or {})
    updates = []
    for log, summary, tail, info in previews:
        if age >= raw_days or age >= slim_days and info.st_size > 8192:
            sizes.setdefault(log[-1], info.st_size)
            tails[log[-1]] = tail
            updates.append((log, summary, info))
    if updates:
        update.update(original_log_bytes=sizes, tail_preview=tails)
        update["raw_logs_removed_at" if age >= raw_days else "log_slimmed_at"] = now
    remove_receipt = age >= summary_days and not any(record.get(key) for key in ("artifact_refs", "artifact_ref", "output_artifact_ref"))
    if not updates and not remove_receipt:
        return 0, 0, 0
    current = _save(receipt_root, parts, update, original)
    for log, summary, info in updates:
        if _referenced(root, log, sources, identities, external, own, own_expected=current):
            raise OSError("日志已出现其它引用")
        with _claimed(root, log, info) as (fd, claimed):
            if _referenced(root, log, sources, identities, external, own,
                           own_expected=current, claim_path=root.joinpath(*log[:-1], claimed)):
                raise OSError("日志隔离后出现其它引用")
            if age >= raw_days:
                os.unlink(claimed, dir_fd=fd)
                os.fsync(fd)
            else:
                _publish_claimed(root, log, summary, fd, claimed)
    if remove_receipt:
        # 即使策略被重排，也不遗留失去归属的大日志。
        if logs and age < raw_days:
            return len(updates), 0, 0
        with _claimed(receipt_root, parts, current[1], raw=current[0], kind="receipt") as (fd, claimed):
            os.unlink(claimed, dir_fd=fd)
            os.fsync(fd)
        # Store目录保留永久锁；不能删除锁文件使已等待的写者分裂成两把锁。
        return 0, len(updates), 1
    return (0, len(updates), 0) if age >= raw_days else (len(updates), 0, 0)


# LLM: 根flock后按固定顺序取所有实际Store锁并恢复原redo；支持显式外置根/owner推导，来源新增时拒绝旧快照变更。
# 函数用途: 显式维护两种真实布局的退出作业；不自动上线清理、不查询PID，扫描不受动作额度限制。
def maintain_background_jobs_dir(jobs_root: Path, *, now: float | None = None, slim_after_days: float = 3.0, raw_log_ttl_days: float = 7.0, summary_ttl_days: float = 30.0, pinned_job_ids: set[str] | None = None, max_jobs: int = 500, receipt_store_root: Path | None = None, owner_home: Path | None = None) -> dict[str, int]:
    now = time.time() if now is None else now
    if (type(now) not in (int, float) or not math.isfinite(now)
            or any(type(day) not in (int, float) or not math.isfinite(day) or day < 0 for day in (slim_after_days, raw_log_ttl_days, summary_ttl_days))
            or type(max_jobs) is not int or max_jobs < 0):
        raise ValueError("日志维护时间、期限或上限无效")
    result = {"scanned": 0, "slimmed": 0, "logs_removed": 0, "receipts_removed": 0, "skipped": 0, "errors": 0, "recovery_required": 0}
    if not max_jobs:
        return result
    root = Path(os.path.abspath(jobs_root))
    external = Path(os.path.abspath(receipt_store_root)) if receipt_store_root is not None else None
    if owner_home is not None:
        if root.name != ".background_jobs":
            raise ValueError("owner回执推导需要workspace的.background_jobs根")
        derived = process_session_store_root(root.parent, owner_home)
        if external is not None and external != derived:
            raise ValueError("显式回执根与owner权威目录不一致")
        external = derived
    pins = frozenset(pinned_job_ids or ())
    try:
        import fcntl
        fd = open_directory_beneath(root, (), require_dir_fd=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            sources = _store_directories(root, external)
            with ExitStack() as locks:
                identities = {}
                for source in sources:
                    check = open_directory_beneath(source, (), require_dir_fd=True)
                    try:
                        info = os.fstat(check)
                        identities[source] = info.st_dev, info.st_ino
                    finally:
                        os.close(check)
                    locks.enter_context(locked_private_directory(source, lock_name=".process-sessions.lock"))
                    recover_process_commit(source)
                if _store_directories(root, external) != sources:
                    raise OSError("回执来源在取锁期间变化")
                candidates, protected, owners = [], set(), {}
                for source in sources:
                    for parts in _receipts(source):
                        record, original = _record(source, parts)
                        logs = _logs(root, parts, record, receipt_root=source)
                        for log in logs:
                            owners.setdefault(log, set()).add((source, parts))
                        pinned = bool({Path(parts[-1]).stem, source.name, record.get("job_id"), record.get("session_id")} & pins)
                        if pinned or not _exited(record):
                            protected.update(logs)
                        else:
                            candidates.append((source, parts, record, original))
                protected.update(log for log, refs in owners.items() if len(refs) > 1)
                changed_jobs = 0
                for source, parts, record, original in candidates:
                    if changed_jobs >= max_jobs:
                        break
                    result["scanned"] += 1
                    try:
                        slimmed, removed, receipts = _maintain_job(root, parts, record, original, now, slim_after_days, raw_log_ttl_days, summary_ttl_days, protected,
                            receipt_root=source, sources=sources, identities=identities, external=external)
                        result["slimmed"] += slimmed
                        result["logs_removed"] += removed
                        result["receipts_removed"] += receipts
                        result["skipped"] += not (slimmed or removed or receipts)
                        changed_jobs += bool(slimmed or removed or receipts)
                    except (OSError, ValueError, TypeError) as exc:
                        result["errors"] += 1
                        result["recovery_required"] += isinstance(exc, _RecoveryRequired)
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    except (OSError, ImportError, ValueError, TypeError) as exc:
        result["errors"] += 1
        result["recovery_required"] += isinstance(exc, _RecoveryRequired)
    return result
