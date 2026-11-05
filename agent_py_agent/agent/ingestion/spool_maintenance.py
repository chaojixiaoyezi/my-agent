"""Bounded, durable archive-before-switch maintenance for guarantee spools.

The caller owns the watch lock and lease. Sequence numbers are monotonic for a
watch, including across generations; the archive tail is the durable dedupe key.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..common.json_io import write_private_json_file_atomic

# 背压期间 spool 维护单片最多复制的字节数。
SPOOL_MAINTENANCE_SLICE_BYTES = 8 * 1024 * 1024
# 背压期间 spool 维护单片墙钟预算毫秒数。
SPOOL_MAINTENANCE_SLICE_MS = 50


@dataclass(frozen=True)
class SpoolRotationInputs:
    spool: Path
    archive: Path | None
    progress: Path
    generation: int
    acked_seq: int
    inflight: bool
    min_free_bytes: int


class _Deferred(Exception):
    pass


# LLM: All reports have the frozen shape; no exception text or file contents escape.
# 函数用途: 统一维护回执，失败时不声称腾出了空间。
def _report(
    status: str, reason: str = "", *, scanned_bytes: int = 0,
    copied_bytes: int = 0, freed_bytes: int = 0, backlog_bytes: int = 0,
    new_generation: int | None = None,
) -> dict:
    return {"status": status, "reason": reason, "scanned_bytes": scanned_bytes,
            "copied_bytes": copied_bytes, "freed_bytes": freed_bytes,
            "backlog_bytes": backlog_bytes, "new_generation": new_generation}


# LLM: Descriptor opens reject symlinks/nonregular files, including replaced temp paths.
# 函数用途: 只打开当前普通文件，防止维护跟随软链接或卡在管道上。
def _open_file(path: Path, flags: int):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("rotation path is not regular")
        return os.fdopen(fd, "r+b" if flags & os.O_RDWR else "rb")
    except BaseException:
        os.close(fd)
        raise


# LLM: Archive rows are append-only and spool_seq increases across rotations.
# 函数用途: 从有界尾窗读取最后一个归档序号，重开时不重复复制已落盘的行。
def _archive_tail_seq(handle, max_bytes: int) -> int:
    size = os.fstat(handle.fileno()).st_size
    if not size:
        return 0
    start = max(0, size - max_bytes)
    handle.seek(start)
    tail = handle.read(max_bytes)
    if not tail.endswith(b"\n"):
        raise _Deferred("archive_tail_incomplete")
    lines = tail.splitlines()
    if start:
        if len(lines) < 2:
            raise _Deferred("archive_tail_too_large")
        lines = lines[1:]
    row = json.loads(lines[-1])
    seq = row.get("spool_seq") if isinstance(row, dict) else None
    if type(seq) is not int or seq <= 0:
        raise ValueError("invalid archive sequence")
    return seq


# LLM: Only a checkpoint owned by this spool identity can authorize archive truncation.
# 函数用途: 建立复制起点，或识别已经完成 replace、尚未保存世代的恢复点。
def _load_progress(inputs: SpoolRotationInputs, spool_stat, max_bytes: int) -> dict:
    if inputs.progress.exists():
        with _open_file(inputs.progress, os.O_RDONLY) as handle:
            progress = json.load(handle)
        if not isinstance(progress, dict):
            raise ValueError("invalid rotation progress")
        same = (progress.get("spool_dev"), progress.get("spool_ino")) == (
            spool_stat.st_dev, spool_stat.st_ino)
        switched = (progress.get("phase") == "switching" and not same and
                    (progress.get("replacement_dev"), progress.get("replacement_ino")) ==
                    (spool_stat.st_dev, spool_stat.st_ino))
        generations = {inputs.generation, inputs.generation - 1} if switched else {inputs.generation}
        if progress.get("version") != 1 or progress.get("generation") not in generations or not (same or switched):
            finish_spool_rotation(inputs.progress)
            raise _Deferred("stale_progress")
        if progress.get("phase") not in {"copying", "switching"}:
            raise ValueError("invalid rotation phase")
        _validate_progress(progress)
        return progress
    if inputs.acked_seq <= 0:
        raise _Deferred("nothing_acked")
    if inputs.archive is None:
        raise _Deferred("no_archive")
    if shutil.disk_usage(inputs.spool.parent).free <= inputs.min_free_bytes + max_bytes:
        raise _Deferred("low_disk")
    with _open_file(inputs.archive, os.O_CREAT | os.O_RDWR) as archive:
        archive_stat = os.fstat(archive.fileno())
        last_seq = _archive_tail_seq(archive, max_bytes)
    progress = {"version": 1, "phase": "copying", "generation": inputs.generation,
                "spool_dev": spool_stat.st_dev, "spool_ino": spool_stat.st_ino,
                "snapshot_size": spool_stat.st_size, "acked_seq": inputs.acked_seq,
                "copied": 0, "cut": None, "skipped": 0,
                "archive_dev": archive_stat.st_dev, "archive_ino": archive_stat.st_ino,
                "archive_base_size": archive_stat.st_size, "last_archived_seq": last_seq,
                "chunk_sha256": []}
    write_private_json_file_atomic(inputs.progress, progress)
    return progress


# LLM: A readable JSON object is not sufficient proof to authorize archive truncation.
# 函数用途: 拒绝负数、越界和坏类型的恢复点，避免坏进度裁掉已确认归档。
def _validate_progress(progress: dict) -> None:
    numeric = ("generation", "spool_dev", "spool_ino", "snapshot_size", "acked_seq",
               "copied", "skipped", "archive_dev", "archive_ino", "archive_base_size", "last_archived_seq")
    if any(type(progress.get(key)) is not int or progress[key] < 0 for key in numeric):
        raise ValueError("invalid rotation checkpoint")
    if not 0 <= progress["skipped"] <= progress["copied"] <= progress["snapshot_size"]:
        raise ValueError("invalid rotation offsets")
    cut = progress.get("cut")
    if cut is not None and (type(cut) is not int or not progress["copied"] <= cut <= progress["snapshot_size"]):
        raise ValueError("invalid rotation cut")
    if type(progress.get("tail_copied", 0)) is not int or progress.get("tail_copied", 0) < 0:
        raise ValueError("invalid tail checkpoint")
    if not isinstance(progress.get("chunk_sha256"), list):
        raise ValueError("invalid rotation digests")


# LLM: skipped is source checkpoint bytes already represented in the durable archive.
# 函数用途: 去重后仍精确计算归档应该有多长，只回退本次未记账的追加字节。
def _verified_archive(inputs: SpoolRotationInputs, progress: dict):
    if inputs.archive is None:
        raise ValueError("missing archive")
    try:
        archive = _open_file(inputs.archive, os.O_RDWR)
    except FileNotFoundError:
        raise _Deferred("archive_identity_changed") from None
    try:
        current = os.fstat(archive.fileno())
        expected = progress["archive_base_size"] + progress["copied"] - progress["skipped"]
        if (current.st_dev, current.st_ino) != (progress["archive_dev"], progress["archive_ino"]) or current.st_size < expected:
            raise _Deferred("archive_identity_changed")
        if current.st_size > expected:
            if progress["phase"] != "copying":
                raise _Deferred("archive_identity_changed")
            archive.truncate(expected)
            archive.flush()
            os.fsync(archive.fileno())
        archive.seek(expected)
        return archive
    except BaseException:
        archive.close()
        raise


# LLM: A budget boundary is resumable, not the semantic cut; retain the snapshot's final row.
# 函数用途: 有界读取完整的已签收前缀，未签收、坏行和最后一行都留热盘。
def _copy_chunk(inputs: SpoolRotationInputs, progress: dict, max_bytes: int, deadline: float) -> tuple[bytes, int, int]:
    chunk = bytearray()
    scanned = skipped = 0
    with _open_file(inputs.spool, os.O_RDONLY) as spool:
        spool.seek(progress["copied"])
        while scanned < max_bytes and time.monotonic() < deadline:
            start = spool.tell()
            line = spool.readline(max_bytes - scanned)
            scanned += len(line)
            if not line or not line.endswith(b"\n"):
                # A complete row larger than a slice is left hot; never split it.
                if not line or spool.tell() >= progress["snapshot_size"]:
                    progress["cut"] = start
                elif start == progress["copied"] and len(line) == max_bytes:
                    raise _Deferred("row_exceeds_slice")
                break
            try:
                row = json.loads(line)
                seq = row.get("spool_seq") if isinstance(row, dict) else None
            except (ValueError, UnicodeDecodeError):
                seq = None
            if (spool.tell() >= progress["snapshot_size"] or type(seq) is not int or
                    seq <= 0 or seq > min(progress["acked_seq"], inputs.acked_seq)):
                progress["cut"] = start
                break
            if seq <= progress["last_archived_seq"]:
                skipped += len(line)
            else:
                chunk.extend(line)
    return bytes(chunk), skipped, scanned


# LLM: A verified, fsynced append precedes checkpoint advancement; unrecorded bytes are recoverable.
# 函数用途: 追加并回读核验本片归档，确认后才保存复制进度。
def _append_chunk(inputs: SpoolRotationInputs, progress: dict, max_bytes: int, deadline: float) -> dict:
    with _verified_archive(inputs, progress) as archive:
        chunk, skipped, scanned = _copy_chunk(inputs, progress, max_bytes, deadline)
        start = archive.tell()
        archive.write(chunk)
        archive.flush()
        os.fsync(archive.fileno())
        archive.seek(start)
        digest = sha256(chunk).hexdigest()
        if sha256(archive.read(len(chunk))).hexdigest() != digest:
            return _report("failed", "archive_verify_failed", scanned_bytes=scanned)
        progress["copied"] += len(chunk) + skipped
        progress["skipped"] += skipped
        if chunk:
            progress["chunk_sha256"].append(digest)
        write_private_json_file_atomic(inputs.progress, progress)
    return _report("copying", scanned_bytes=scanned, copied_bytes=len(chunk))


# LLM: The temporary suffix is private to this rotation; tail copying also resumes in bounded slices.
# 函数用途: 分片重建热盘尾部，复制期间新增的完整行同样保留下来。
def _stage_tail(inputs: SpoolRotationInputs, progress: dict, max_bytes: int) -> bool:
    temp = inputs.spool.with_suffix(".ndjson.rotate.tmp")
    done = progress.get("tail_copied", 0)
    with _open_file(inputs.spool, os.O_RDONLY) as spool, _open_file(temp, os.O_CREAT | os.O_RDWR) as target:
        target_stat = os.fstat(target.fileno())
        if progress.get("replacement_ino") is not None and (target_stat.st_dev, target_stat.st_ino) != (progress["replacement_dev"], progress["replacement_ino"]):
            raise _Deferred("switch_temp_changed")
        if os.fstat(target.fileno()).st_size < done:
            raise _Deferred("switch_temp_changed")
        target.truncate(done)
        target.seek(done)
        spool.seek(progress["cut"] + done)
        chunk = spool.read(max_bytes)
        target.write(chunk)
        target.flush()
        os.fsync(target.fileno())
        done += len(chunk)
        progress["tail_copied"] = done
        progress["replacement_dev"] = os.fstat(target.fileno()).st_dev
        progress["replacement_ino"] = os.fstat(target.fileno()).st_ino
        write_private_json_file_atomic(inputs.progress, progress)
        return progress["cut"] + done == os.fstat(spool.fileno()).st_size


# LLM: POSIX directory fsync makes the replace durable before the caller persists the generation.
# 函数用途: 把重命名本身落盘；不支持目录同步的平台只忽略明确的不支持错误。
def _sync_parent(path: Path) -> None:
    import errno

    try:
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


# LLM: A switching checkpoint records the replacement inode, so recovery cannot mistake a foreign replace for ours.
# 函数用途: 最后复核租约与文件身份后切换；已切换但未保存世代时返回同一个新世代。
def _switch(inputs: SpoolRotationInputs, progress: dict, lease_current: Callable[[], bool], max_bytes: int, deadline: float) -> dict:
    current = inputs.spool.stat()
    original = (progress["spool_dev"], progress["spool_ino"])
    with _verified_archive(inputs, progress):
        pass
    if (current.st_dev, current.st_ino) != original:
        if (current.st_dev, current.st_ino) != (progress.get("replacement_dev"), progress.get("replacement_ino")):
            return _report("failed", "spool_identity_changed")
        _sync_parent(inputs.spool)
        return _report("switched", new_generation=progress["generation"] + 1, freed_bytes=progress["cut"])
    if inputs.inflight or not lease_current():
        return _report("deferred", "inflight" if inputs.inflight else "lease_lost")
    if progress["cut"] == 0:
        finish_spool_rotation(inputs.progress)
        return _report("not_ready", "no_archivable_prefix")
    with _verified_archive(inputs, progress):
        pass
    if time.monotonic() >= deadline:
        return _report("copying")
    progress["phase"] = "switching"
    write_private_json_file_atomic(inputs.progress, progress)
    if not _stage_tail(inputs, progress, max_bytes):
        return _report("copying")
    # Re-check immediately before the only destructive operation.
    after = inputs.spool.stat()
    if not lease_current():
        return _report("deferred", "lease_lost")
    if (after.st_dev, after.st_ino) != original:
        return _report("failed", "spool_identity_changed")
    if after.st_size != progress["cut"] + progress["tail_copied"]:
        return _report("copying")
    with _verified_archive(inputs, progress):
        pass
    os.replace(inputs.spool.with_suffix(".ndjson.rotate.tmp"), inputs.spool)
    _sync_parent(inputs.spool)
    return _report("switched", new_generation=progress["generation"] + 1, freed_bytes=progress["cut"])


# LLM: The watch-lock/lease owner calls this once per tick; no harvester imports or reader mutations.
# 函数用途: 运行一片可恢复归档维护，失败始终保留热盘和明确回执。
def run_spool_rotation_slice(
    inputs: SpoolRotationInputs, *, lease_current: Callable[[], bool],
    max_bytes: int = SPOOL_MAINTENANCE_SLICE_BYTES,
    max_ms: float = SPOOL_MAINTENANCE_SLICE_MS,
) -> dict:
    if inputs.inflight:
        return _report("deferred", "inflight")
    if not lease_current():
        return _report("deferred", "lease_lost")
    if max_bytes <= 0 or max_ms <= 0:
        return _report("deferred", "empty_budget")
    deadline = time.monotonic() + max_ms / 1000.0
    try:
        current = inputs.spool.stat()
        progress = _load_progress(inputs, current, max_bytes)
        report = _report("copying")
        if progress["phase"] == "copying":
            report = _append_chunk(inputs, progress, max_bytes, deadline)
            if report["status"] == "failed":
                return report
        if progress["cut"] is not None and progress["copied"] == progress["cut"]:
            remaining = max_bytes - report["scanned_bytes"]
            if remaining > 0:
                switched = _switch(inputs, progress, lease_current, remaining, deadline)
                report.update(switched, copied_bytes=report["copied_bytes"], scanned_bytes=report["scanned_bytes"])
        report["backlog_bytes"] = max(0, current.st_size - progress["copied"])
        return report
    except _Deferred as exc:
        reason = str(exc)
        status = "failed" if reason in {"archive_identity_changed", "switch_temp_changed"} else "deferred"
        if reason == "nothing_acked":
            status = "not_ready"
        return _report(status, reason)
    except FileNotFoundError:
        return _report("idle", "missing_file")
    except (OSError, ValueError, KeyError, TypeError):
        return _report("failed", "rotation_io_error")


# LLM: Called only after the switched generation has been persisted; missing progress is idempotent.
# 函数用途: 保存世代成功后清掉恢复点，重复调用不会报错。
def finish_spool_rotation(progress: Path) -> None:
    progress.unlink(missing_ok=True)
