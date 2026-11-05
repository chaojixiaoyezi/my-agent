# LLM: 只消费原进程目录的退役责任；目录锁有界，变更经原协调写门，未知状态保留，不扫描 job 名推断归属。
# 模块用途: 按游标维护进程终态、孤儿侧车和已经登记的后台日志。
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from ..common.nofollow_fs import open_directory_beneath
from ..gateway_parts.io import cleanup_orphan_lock_files
from ..user_space.storage_lanes import (
    STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT,
    STORAGE_LANE_SLICE_BYTES,
    STORAGE_LANE_SLICE_ENTRIES,
    STORAGE_LANE_SLICE_MS,
    StorageLaneBudget,
    StorageLaneRequest,
    owner_retention_override,
)
from .process_session_records import PROCESS_TERMINAL_STATUSES, validate_session_id
from .process_session_store import (
    ProcessSessionStore,
    _activation_exit_confirmed,
    _completion_obligation_finished,
    _record_ttl_expired,
    process_session_store_root,
)

# 已结束后台命令日志的默认保留天数。
BACKGROUND_JOB_LOG_RETENTION_DAYS = 7
# 日志退役台账已消费前缀达到该字节数时尝试压缩。
BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES = 1 * 1024 * 1024
# 对同一进程目录执行终态 TTL 裁剪的最短间隔秒数。
_PROCESS_SESSION_LANE_PRUNE_INTERVAL_SECONDS = 3600
# 等待原进程目录锁的最长毫秒数，超时按锁忙留待下一片。
_PROCESS_SESSION_LANE_LOCK_WAIT_MS = 20


# LLM: 回调只约束原锁准入，不打断已经开始的原子提交。
# 函数用途: 为进程目录锁生成有界等待检查。
def _bounded_wait(deadline):
    def check():
        if time.monotonic() >= deadline:
            raise BlockingIOError("process_session_lock_busy")
    return check


# LLM: 公共 list_records 不接收 wait_check；只在本通道适配其原 transaction，不改全局 Store 合同或缓存。
# 类用途: 让便宜投影未命中后的真实读取也使用有界目录锁。
class _LaneStore(ProcessSessionStore):
    def transaction(self, *, wait_check=None):
        return super().transaction(wait_check=wait_check or _bounded_wait(
            time.monotonic() + _PROCESS_SESSION_LANE_LOCK_WAIT_MS / 1000))


# LLM: 从绝对锚逐层 lstat，拒绝链接、父级穿越及非目录；从不创建路径。
# 函数用途: 检查通道即将进入的完整目录链。
def _real_directories(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        return False
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if not stat.S_ISDIR(os.lstat(current).st_mode):
            return False
    return True


# LLM: 缺失与身份变化仅结束这条责任，不授权删除新文件；路径须来自合法台账且父链不含链接。
# 函数用途: 取符合规范日志名、根及原始 inode 身份的普通单链接文件状态。
def _log_identity(row):
    path = Path(row["output_file"])
    if (not path.is_absolute() or ".." in path.parts or path.parent.name != ".background_jobs"
            or str(path.parent) != row["log_root"] or not re.fullmatch(r"job-\d+\.log", path.name)):
        return None
    try:
        if not _real_directories(path.parent):
            return None
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_dev, info.st_ino) != (row["dev"], row["ino"])):
        return None
    return info


# LLM: 只接受冻结 schema 及有限数值时间；坏行不能被当成过期，也不能跳过后丢失责任。
# 函数用途: 解码一条完整台账行并验证游标可消费的最小合同。
def _decode_row(raw):
    row = json.loads(raw)
    if not isinstance(row, dict) or row.get("schema") != "background_log_retirement.v1":
        raise ValueError("background_log_retirement_schema_unknown")
    validate_session_id(row.get("session_id"))
    if any(type(row.get(key)) is not str or not row[key] for key in ("output_file", "log_root")):
        raise ValueError("background_log_retirement_path_invalid")
    if any(type(row.get(key)) is not int or row[key] < 0 for key in ("dev", "ino")):
        raise ValueError("background_log_retirement_identity_invalid")
    finished = row.get("finished_at")
    if type(finished) not in (int, float) or not math.isfinite(finished) or finished < 0:
        raise ValueError("background_log_retirement_time_unknown")
    return row


# LLM: 目录和叶均在打开前后核验；返回的二进制句柄由调用方关闭，拒绝链接及多链接文件。
# 函数用途: 安全打开可按字节续读的日志退役台账。
def _open_ledger(root):
    if not _real_directories(root):
        raise OSError("process_session_root_unsafe")
    path = root / ".log_retirement.jsonl"
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("background_log_retirement_ledger_unsafe")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened, current = os.fstat(descriptor), path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise OSError("background_log_retirement_ledger_changed")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


# LLM: 此对象只活一片，回执不含私有绝对路径；cursor 仍由上游维护权威发布。
# 类用途: 归集通道预算、阶段续点和实际回收计量。
class _Pass:
    def __init__(self, request, root):
        self.request, self.root = request, root
        source = request.budget
        self.budget = StorageLaneBudget(
            min(STORAGE_LANE_SLICE_ENTRIES, source.max_items) if source.max_items is not None else STORAGE_LANE_SLICE_ENTRIES,
            min(STORAGE_LANE_SLICE_BYTES, source.max_bytes) if source.max_bytes is not None else STORAGE_LANE_SLICE_BYTES,
            min(source.deadline if source.deadline is not None else math.inf, time.monotonic() + STORAGE_LANE_SLICE_MS / 1000),
        )
        self.result = {"cursor": {"next_prune_at": request.cursor.get("next_prune_at", 0.0),
                                  "log_offset": request.cursor.get("log_offset", 0),
                                  "phase": request.cursor.get("phase", "prune")},
                       "complete": False, "items": 0, "bytes": 0, "deleted_items": 0,
                       "freed_bytes": 0, "skipped": {}, "backlog": 0, "oldest_due_age_seconds": 0.0, "errors": []}
        cursor = self.result["cursor"]
        next_prune, offset, phase = cursor["next_prune_at"], cursor["log_offset"], cursor["phase"]
        self.ledger_generation = None
        if type(phase) is str and "@" in phase:
            phase, generation = phase.split("@", 1)
            try:
                dev, ino = generation.split(":")
                self.ledger_generation = (int(dev), int(ino))
                if min(self.ledger_generation) < 0:
                    phase = "invalid"
            except (ValueError, TypeError):
                phase = "invalid"
        self.phase = phase
        self.invalid_cursor = (type(next_prune) not in (int, float) or not math.isfinite(next_prune) or next_prune < 0
                               or type(offset) is not int or offset < 0
                               or type(phase) is not str or phase not in {"prune", "launch_locks", "root_locks", "logs", "compact"})
        if self.invalid_cursor:
            self.result["cursor"] = {"next_prune_at": 0.0, "log_offset": 0, "phase": "prune"}
        elif self.ledger_generation is None and offset:
            # 无代际的旧续点只能重放，不能把当前台账前缀直接认作已消费。
            self.result["cursor"].update(log_offset=0, phase="logs")
            self.phase = "logs"
            self.skip("ledger_generation_unknown")

    # 原 cursor 仍只有三个字段；phase 的结构化后缀绑定台账 inode，防恢复后误用旧 offset。
    def set_phase(self, phase):
        self.phase = phase
        suffix = "" if self.ledger_generation is None else f"@{self.ledger_generation[0]}:{self.ledger_generation[1]}"
        self.result["cursor"]["phase"] = phase + suffix

    def available(self):
        return not self.budget.exhausted(self.result["items"], self.result["bytes"])

    def skip(self, code, count=1):
        skipped = self.result["skipped"]
        skipped[code] = skipped.get(code, 0) + count

    def error(self, code):
        if len(self.result["errors"]) < STORAGE_LANE_RECEIPT_MAX_ERRORS_COUNT:
            self.result["errors"].append({"code": code})

    def transaction(self):
        deadline = time.monotonic() + _PROCESS_SESSION_LANE_LOCK_WAIT_MS / 1000
        return ProcessSessionStore(self.root).transaction(wait_check=_bounded_wait(deadline))


# LLM: 初读在协调写门外；仅合格 TTL 候选触发原 prune，通知目标缺消费读口时保留。
# 函数用途: 每小时通过原目录事务清理已结清的终态记录。
def _prune(p):
    if p.request.now < p.result["cursor"]["next_prune_at"]:
        return True
    records, errors = _LaneStore(p.root).list_records(cheap_checks=True)
    if errors:
        for error in errors:
            if error.get("error_type") == "BlockingIOError":
                p.skip("lock_busy")
            else:
                p.skip("process_state_unknown")
                p.error("process_session_scan_failed")
        return False
    candidates = [row for row in records
                  if row["status"] in PROCESS_TERMINAL_STATUSES
                  and not row.get("retain_until_consumed")
                  and (row.get("activation_scope") is None or (
                      not row.get("stop_requested") and _activation_exit_confirmed(row)))
                  and _completion_obligation_finished(row, None)
                  and _record_ttl_expired(row, 86400.0, p.request.now)]
    if candidates:
        # 阶段已在扫描前准入；原 prune 不能拆分，不能在慢扫后反复放弃而永久无法清积压。
        sizes = {}
        for row in candidates:
            try:
                sizes[row["session_id"]] = os.lstat(p.root / (row["session_id"] + ".json")).st_size
            except FileNotFoundError:
                continue

        def operation():
            with p.transaction() as transaction:
                return transaction.prune_finished(None, max_age_seconds=86400.0, now=p.request.now)

        removed = p.request.run_write(operation)
        p.result["deleted_items"] += len(removed)
        p.result["freed_bytes"] += sum(sizes.get(sid, 0) for sid in removed)
        p.result["items"] += 1  # 原 prune 是一个不可分割的目录事务批次。
    p.result["cursor"]["next_prune_at"] = p.request.now + _PROCESS_SESSION_LANE_PRUNE_INTERVAL_SECONDS
    return True


# LLM: 只做一次目录枚举；候选必须是陈旧、无数据的普通侧车，永久目录锁不参与。
# 函数用途: 在写门外判断是否真的有孤儿锁需要清扫。
def _orphan_candidates(directory, now):
    if not _real_directories(directory):
        return 0
    count = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if entry.name == ".process-sessions.lock" or not entry.name.endswith(".lock"):
                continue
            if not Path(entry.name[:-5]).suffix:
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or now - info.st_mtime < 86400:
                continue
            try:
                os.lstat(directory / entry.name[:-5])
            except FileNotFoundError:
                count += 1
    return count


# LLM: 旧积压使用原试锁/身份协议的一次批次；有 dir_fd 才固定完整父链，portable 仍走原跨平台清扫器。
# 函数用途: 有真实候选时经协调写门清扫一个目录的孤儿锁。
def _locks(p, directory):
    try:
        count = _orphan_candidates(directory, p.request.now)
    except FileNotFoundError:
        return True
    if not count:
        return True
    p.result["backlog"] += count  # 首轮遗留积压在清扫前计量，不把它误报为稳态单条工作。
    # 与 prune 一样只在阶段入口检查预算，首轮旧积压按规格作为一个原子维护批次。
    def operation():
        with p.transaction():
            if not _real_directories(directory):
                raise OSError("process_session_lock_directory_unsafe")
            if os.open in os.supports_dir_fd and os.stat in os.supports_dir_fd:
                descriptor = open_directory_beneath(directory.anchor, directory.parts[1:])
                try:
                    return cleanup_orphan_lock_files(directory, now=p.request.now, data_sidecars_only=True,
                                                     directory_fd=descriptor)
                finally:
                    os.close(descriptor)
            return cleanup_orphan_lock_files(directory, now=p.request.now, data_sidecars_only=True)

    p.result["deleted_items"] += p.request.run_write(operation)
    p.result["items"] += 1
    return True


# LLM: 台账写入成功但 metadata 删除失败时责任尚未生效；任何同名记录（含坏文件）都阻止删日志。
# 函数用途: 保守检查准确 session 定位文件是否仍在。
def _record_present(root, session_id):
    try:
        os.lstat(root / (session_id + ".json"))
    except FileNotFoundError:
        return False
    return True


# LLM: fsync 不改变目录内容；缺失日志重试也同步父目录，避免先丢台账而掉电后恢复已删名字。
# 函数用途: 将规范日志目录已发生的删除同步到磁盘，不跟随链接。
def _sync_log_parent(row):
    if os.name == "nt":
        return  # Windows 没有可移植的目录 fsync，保留原平台 unlink 语义。
    path = Path(row["output_file"])
    if (not path.is_absolute() or ".." in path.parts or path.parent.name != ".background_jobs"
            or str(path.parent) != row["log_root"] or not re.fullmatch(r"job-\d+\.log", path.name)):
        return
    try:
        if not _real_directories(path.parent):
            return
        if os.open in os.supports_dir_fd:
            descriptor = open_directory_beneath(path.anchor, path.parts[1:-1])
        else:
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# LLM: 最后一次身份核验与 unlink 共用固定父 FD；portable 无 dir_fd 时复核原父链与单链接叶，不伪称同等竞态强度。
# 函数用途: 删除仍绑定台账原始身份的一个日志，返回实际释放字节。
def _unlink_log(row):
    path = Path(row["output_file"])
    if os.open in os.supports_dir_fd and os.unlink in os.supports_dir_fd and os.stat in os.supports_dir_fd:
        descriptor = open_directory_beneath(path.anchor, path.parts[1:-1])
        try:
            info = os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (row["dev"], row["ino"])):
                return None
            os.unlink(path.name, dir_fd=descriptor)
            os.fsync(descriptor)
            return info.st_size
        finally:
            os.close(descriptor)
    info = _log_identity(row)
    if info is None:
        return None
    os.unlink(path)
    _sync_log_parent(row)
    return info.st_size


# LLM: 复核在原目录事务内，唯一变更在协调写门内；记录仍在、文件替换、锁忙都不能丢掉未完成的责任。
# 函数用途: 最终复核一条日志退休责任并执行一次身份绑定的删除。
def _delete_log(p, row):
    def operation():
        with p.transaction():
            if _record_present(p.root, row["session_id"]):
                return "protected", 0
            if _log_identity(row) is None:
                return "changed", 0
            try:
                size = _unlink_log(row)
            except FileNotFoundError:
                return "changed", 0
            return ("changed", 0) if size is None else ("deleted", size)
    return p.request.run_write(operation)


# LLM: 只消费完整行；未来、坏行、失败及预算边界都保留当前字节续点，不扫描未登记的日志。
# 函数用途: 在条目、字节和时间上限内逐条退休日志。
def _logs(p):
    offset = p.result["cursor"]["log_offset"]
    try:
        handle = _open_ledger(p.root)
    except FileNotFoundError:
        p.result["cursor"]["log_offset"] = 0
        return True
    days = owner_retention_override(p.request.home, "background_job_log_days", BACKGROUND_JOB_LOG_RETENTION_DAYS)
    with handle:
        original = os.fstat(handle.fileno())
        generation = (original.st_dev, original.st_ino)
        if p.ledger_generation is not None and p.ledger_generation != generation:
            p.skip("ledger_changed")
            offset = p.result["cursor"]["log_offset"] = 0
        p.ledger_generation = generation
        p.set_phase("logs")
        if original.st_size < offset:
            p.skip("ledger_shrank")
            p.result["cursor"]["log_offset"] = 0
            return False
        handle.seek(offset)
        while p.available():
            remaining = p.budget.max_bytes - p.result["bytes"]
            raw = handle.readline(remaining)
            p.result["bytes"] += len(raw)
            if not raw or not raw.endswith(b"\n"):
                return len(raw) < remaining
            p.result["items"] += 1
            current = (p.root / ".log_retirement.jsonl").lstat()
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                p.skip("ledger_changed")
                p.result["cursor"]["log_offset"] = 0
                p.ledger_generation = (current.st_dev, current.st_ino)
                p.set_phase("logs")
                return False
            try:
                row = _decode_row(raw)
            except (ValueError, TypeError, UnicodeError):
                p.skip("log_state_unknown")
                p.error("background_log_retirement_invalid")
                return False
            due = row["finished_at"] + days * 86400
            if p.request.now < due:
                return True
            p.result["oldest_due_age_seconds"] = max(0.0, p.request.now - due)
            if _record_present(p.root, row["session_id"]):
                p.skip("session_record_present")
                p.result["backlog"] += 1
                return False
            if _log_identity(row) is None:
                try:
                    _sync_log_parent(row)
                except OSError:
                    p.error("background_log_unlink_failed")
                    return False
                outcome, size = "changed", 0
            else:
                try:
                    outcome, size = _delete_log(p, row)
                except BlockingIOError:
                    raise
                except OSError:
                    p.error("background_log_unlink_failed")
                    return False
            if outcome == "protected":
                p.skip("session_record_present")
                return False
            if outcome == "changed":
                p.skip("log_identity_changed")
            else:
                p.result["deleted_items"] += 1
                p.result["freed_bytes"] += size
            p.result["cursor"]["log_offset"] = handle.tell()
            p.result["oldest_due_age_seconds"] = 0.0
    return False


# LLM: 与追加同一目录锁；复制原始尾字节而非重新序列化；尾部超过本片预算时先继续消费，不做无界复制。
# 函数用途: 将较长的已消费台账前缀原子压缩，保留未消费尾部的精确字节。
def _compact(p):
    offset = p.result["cursor"]["log_offset"]
    if offset < BACKGROUND_LOG_RETIREMENT_COMPACT_BYTES:
        return True
    path = p.root / ".log_retirement.jsonl"
    info = path.lstat()
    generation = (info.st_dev, info.st_ino)
    if p.ledger_generation is not None and p.ledger_generation != generation:
        p.skip("ledger_changed")
        p.result["cursor"]["log_offset"] = 0
        p.ledger_generation = generation
        p.set_phase("logs")
        return False
    p.ledger_generation = generation
    p.set_phase("compact")
    if info.st_size < offset:
        p.skip("ledger_shrank")
        p.result["cursor"]["log_offset"] = 0
        return False
    remaining = p.budget.max_bytes - p.result["bytes"]
    if info.st_size - offset > p.budget.max_bytes:
        p.set_phase("logs")
        return False
    if not p.available() or info.st_size - offset > remaining:
        return False

    def operation():
        with p.transaction(), _open_ledger(p.root) as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != p.ledger_generation:
                return None
            if os.fstat(handle.fileno()).st_size < offset:
                return None
            handle.seek(offset)
            tail = handle.read(remaining + 1)
            if len(tail) > remaining:
                return None
            descriptor, name = tempfile.mkstemp(prefix=".log_retirement-", suffix=".tmp", dir=p.root)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(tail)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, path)
                current = path.lstat()
                p.ledger_generation = (current.st_dev, current.st_ino)
                if os.name != "nt":
                    parent = os.open(p.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                    try:
                        os.fsync(parent)
                    finally:
                        os.close(parent)
            finally:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass  # best-effort: 成功 replace 后临时名字本就不再存在。
            return len(tail)

    copied = p.request.run_write(operation)
    if copied is None:
        p.skip("ledger_changed")
        p.result["cursor"]["log_offset"] = 0
        p.ledger_generation = None
        p.set_phase("logs")
        return False
    p.result["bytes"] += copied
    p.result["items"] += 1
    p.result["cursor"]["log_offset"] = 0
    return True


# LLM: 只有原 owner 进程目录和台账授权的外部 workspace 日志参与；空 owner、legal_hold、无候选不调用写门。
# 函数用途: 依次推进 TTL、锁侧车、日志退休和台账压缩，并返回可验证的维护回执。
def maintain_process_sessions_lane(request: StorageLaneRequest) -> dict:
    root = process_session_store_root(request.home.owner_home_dir, request.home.owner_home_dir)
    p = _Pass(request, root)
    if request.legal_hold:
        return {**p.result, "complete": True, "deferred_reason": "legal_hold"}
    if p.invalid_cursor or type(request.now) not in (int, float) or not math.isfinite(request.now):
        p.skip("maintenance_input_unknown")
        return p.result
    try:
        try:
            root_info = root.lstat()
        except FileNotFoundError:
            return {**p.result, "complete": True}
        if (not stat.S_ISDIR(root_info.st_mode) or not _real_directories(Path(request.home.owner_home_dir))
                or not _real_directories(root)):
            p.skip("unsafe_path")
            return p.result
        phase = p.phase
        if phase == "prune":
            if not p.available() or not _prune(p):
                return p.result
            p.set_phase("launch_locks")
            phase = p.phase
        for step, directory, following in (("launch_locks", root / ".launches", "root_locks"),
                                            ("root_locks", root, "logs")):
            if phase == step:
                if not p.available() or not _locks(p, directory):
                    return p.result
                p.set_phase(following)
                phase = p.phase
        if phase == "logs":
            if not p.available() or not _logs(p):
                return p.result
            p.set_phase("compact")
            phase = p.phase
        if phase == "compact":
            if not _compact(p):
                return p.result
            p.set_phase("prune")
            p.result["complete"] = True
    except BlockingIOError:
        p.skip("lock_busy")
    except (OSError, ValueError, TypeError, RuntimeError):
        p.skip("process_state_unknown")
        p.error("process_session_maintenance_failed")
    return p.result
