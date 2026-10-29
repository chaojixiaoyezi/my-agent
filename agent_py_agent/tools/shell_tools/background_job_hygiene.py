# LLM: 仅显式处理jobs_root内有终态证据的日志副本；不探测PID、不发信号、不改进程生命周期字段。
# 模块用途: 把退出作业日志按3/7/30天缩尾、删原文及回收无引用回执，运行态与pin始终保留。
from __future__ import annotations

import json
import math
import os
import stat
import time
from pathlib import Path

from ...agent.common.nofollow_fs import (
    list_names_beneath,
    open_directory_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    unlink_file_beneath,
    write_bytes_atomic_beneath,
)

_DAY = 86400
_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "killed", "exited", "not_started"}
_LOG_SUFFIXES = {".log", ".out", ".err"}
_MARKER = b"\n...[truncated by 3d background_job_hygiene]...\n"
_MAX_METADATA_BYTES = 1024 * 1024


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


# LLM: 只枚举根及一层作业目录的JSON，目录本身经过nofollow；不递归进入未知树。
# 函数用途: 找出候选回执的受管相对路径。
def _receipts(root):
    for name in list_names_beneath(root, ()):
        metadata = (root / name).lstat()
        if stat.S_ISREG(metadata.st_mode) and name.endswith(".json"):
            yield (name,)
        elif stat.S_ISDIR(metadata.st_mode):
            for child in list_names_beneath(root, (name,)):
                if child.endswith(".json"):
                    yield name, child


# LLM: JSON受1MiB上限限制且单链接读取；返回原始字节作提交前变更检查。
# 函数用途: 读取候选作业回执，不吞损坏文件为正常空对象。
def _record(root, parts):
    raw = read_bytes_beneath(root, parts, max_bytes=_MAX_METADATA_BYTES, require_dir_fd=True)
    value = json.loads(raw) if raw is not None else None
    if not isinstance(value, dict):
        raise ValueError("后台回执不是JSON对象")
    return value, raw


# LLM: 输出路径只接受jobs_root内同层叶；不resolve链接，不接受穿越或其它层资源。
# 函数用途: 找出回执声明的日志及同名原始日志，危险引用令整项跳过。
def _logs(root, parts, record):
    parent = parts[:-1]
    stem = Path(parts[-1]).stem
    names = {stem + suffix for suffix in _LOG_SUFFIXES}
    for key in ("output_file", "stdout_file", "stderr_file", "log_file"):
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
        if relative.parts[:-1] != parent or relative.suffix not in _LOG_SUFFIXES:
            raise ValueError("日志不在原作业目录")
        names.add(relative.name)
    result = []
    for name in sorted(names):
        path = root.joinpath(*parent, name)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError("日志不是普通单链接文件")
        result.append((*parent, name))
    return result


# LLM: 只seek头尾，不把大日志全部读进内存；裁切UTF8边界时丢弃两端不完整码点。
# 函数用途: 提取有界首2048字节和尾4096字节及原始大小。
def _preview(root, parts):
    descriptor = open_readonly_file_beneath(root, parts)
    with os.fdopen(descriptor, "rb") as stream:
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


# LLM: 修改前复核叶身份、大小及时间，拒绝正在变化的日志而不是覆写活跃输出。
# 函数用途: 构造并核对日志变更签名。
def _signature(value):
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


# LLM: 保留所有原回执字段；原子写0600摘要并刷盘，后续删日志必须在此成功之后。
# 函数用途: 保存维护元数据，不创建替代进程状态账。
def _save(root, parts, record, expected):
    if read_bytes_beneath(root, parts, max_bytes=_MAX_METADATA_BYTES, require_dir_fd=True) != expected:
        raise OSError("回执提交前变化")
    raw = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > _MAX_METADATA_BYTES:
        raise ValueError("摘要回执超过上限")
    write_bytes_atomic_beneath(root, parts, raw)
    return raw


# LLM: 对整项先预取及验证，再先保存摘要后删/换日志；重跑保留最初大小，不反复裁同一日志。
# 函数用途: 处理一条已确认退出的回执并回报动作数。
def _maintain_job(root, parts, record, original, now, slim_days, raw_days, summary_days, protected):
    metadata = root.joinpath(*parts).lstat()
    completed = _timestamp(record.get("ended_at")) or _timestamp(record.get("finished_at")) or record.get("hygiene_completed_at") or metadata.st_mtime
    if type(completed) not in (int, float) or not math.isfinite(completed):
        raise ValueError("退出时间无效")
    age = (now - completed) / _DAY
    if age < min(slim_days, raw_days, summary_days):
        return 0, 0, 0
    logs = _logs(root, parts, record)
    if any(log in protected for log in logs):
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
    current = _save(root, parts, update, original)
    for log, summary, info in updates:
        latest = root.joinpath(*log).lstat()
        if not stat.S_ISREG(latest.st_mode) or latest.st_nlink != 1 or _signature(latest) != _signature(info):
            raise OSError("日志修改前变化")
        if age >= raw_days:
            unlink_file_beneath(root, log)
        else:
            write_bytes_atomic_beneath(root, log, summary)
    if remove_receipt:
        # 即使策略被重排，也不遗留失去归属的大日志。
        if logs and age < raw_days:
            return len(updates), 0, 0
        if read_bytes_beneath(root, parts, max_bytes=_MAX_METADATA_BYTES, require_dir_fd=True) != current:
            raise OSError("回执删除前变化")
        unlink_file_beneath(root, parts)
        if len(parts) == 2:
            fd = open_directory_beneath(root, (), require_dir_fd=True)
            try:
                try:
                    os.rmdir(parts[0], dir_fd=fd)
                except OSError:
                    pass
            finally:
                os.close(fd)
        return 0, len(updates), 1
    return (0, len(updates), 0) if age >= raw_days else (len(updates), 0, 0)


# LLM: 根目录flock只串行显式维护；候选回执的终态事实不变，不通过PID判断或控制进程。
# 函数用途: 有界维护退出作业，保护所有pin及未确认退出记录引用的日志。
def maintain_background_jobs_dir(jobs_root: Path, *, now: float | None = None, slim_after_days: float = 3.0, raw_log_ttl_days: float = 7.0, summary_ttl_days: float = 30.0, pinned_job_ids: set[str] | None = None, max_jobs: int = 500) -> dict[str, int]:
    now = time.time() if now is None else now
    if (type(now) not in (int, float) or not math.isfinite(now)
            or any(type(day) not in (int, float) or not math.isfinite(day) or day < 0 for day in (slim_after_days, raw_log_ttl_days, summary_ttl_days))
            or type(max_jobs) is not int or max_jobs < 0):
        raise ValueError("日志维护时间、期限或上限无效")
    result = {"scanned": 0, "slimmed": 0, "logs_removed": 0, "receipts_removed": 0, "skipped": 0, "errors": 0}
    if not max_jobs:
        return result
    root = Path(os.path.abspath(jobs_root))
    pins = frozenset(pinned_job_ids or ())
    try:
        import fcntl
        fd = open_directory_beneath(root, (), require_dir_fd=True)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            candidates, protected, owners = [], set(), {}
            for parts in _receipts(root):
                try:
                    record, raw = _record(root, parts)
                    logs = _logs(root, parts, record)
                    for log in logs:
                        owners.setdefault(log, set()).add(parts)
                    pinned = bool({Path(parts[-1]).stem, *parts[:-1], record.get("job_id"), record.get("session_id")} & pins)
                    if pinned or not _exited(record):
                        protected.update(logs)
                    else:
                        candidates.append((parts, record, raw))
                except (OSError, ValueError, TypeError):
                    result["errors"] += 1
                    # 无法识别归属时不冒险修改其它回执引用的共享输出。
                    return result
            # 非唯一归属无法由单条回执决定保留策略，整项保留，避免较老回执替新回执裁剪共享输出。
            protected.update(log for log, refs in owners.items() if len(refs) > 1)
            changed_jobs = 0
            for parts, record, raw in candidates:
                if changed_jobs >= max_jobs:
                    break
                result["scanned"] += 1
                try:
                    slimmed, removed, receipts = _maintain_job(root, parts, record, raw, now, slim_after_days, raw_log_ttl_days, summary_ttl_days, protected)
                    result["slimmed"] += slimmed
                    result["logs_removed"] += removed
                    result["receipts_removed"] += receipts
                    result["skipped"] += not (slimmed or removed or receipts)
                    changed_jobs += bool(slimmed or removed or receipts)
                except (OSError, ValueError, TypeError):
                    result["errors"] += 1
        finally:
            os.close(fd)
    except FileNotFoundError:
        pass
    except (OSError, ImportError):
        result["errors"] += 1
    return result
