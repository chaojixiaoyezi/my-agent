"""单线程账本冷热分段的公共件；与转录分段同构，原写锁内先准备、再切尾、最后发布。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

from ..common.json_io import _sync_parent_directory, write_private_text_file_atomic_unlocked
from ..common.nofollow_fs import (
    ensure_private_dir,
    open_readonly_file_beneath,
    write_bytes_atomic_beneath,
)
from ..runtime_errors import DataCorruptionError

_SEGMENT_NAME = re.compile(r"seg-([0-9]{6,})\.jsonl\Z")


def archive_dir_for(active: Path) -> Path:
    return active.parent / "archive" / active.stem


def file_stamp(path: Path) -> tuple | None:
    """只接受普通单链接文件；缺叶可自愈，链接和特殊文件不能当作缺失。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise DataCorruptionError("thread ledger must be a single regular file")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def archive_stamp(active: Path) -> tuple:
    """常规汇总只检查两个代次；目录增删使段缺失可见，不遍历历史。"""
    directory = archive_dir_for(active)
    try:
        info = directory.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISDIR(info.st_mode):
        raise DataCorruptionError("thread ledger archive must be a real directory")
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns), file_stamp(directory / "manifest.json")


def _open(path: Path):
    return os.fdopen(open_readonly_file_beneath(path.parent, (path.name,)), "rb")


def _measure(path: Path, end: int | None = None) -> dict:
    digest, rows, size, last = hashlib.sha256(), 0, 0, b""
    with _open(path) as handle:
        while end is None or size < end:
            raw = handle.read(65536 if end is None else min(65536, end - size))
            if not raw:
                break
            digest.update(raw)
            rows += raw.count(b"\n")
            size += len(raw)
            last = raw[-1:]
    if end is not None and (size != end or last != b"\n"):
        raise DataCorruptionError("thread ledger seal is not a complete row boundary")
    return {"rows": rows + int(bool(last and last != b"\n")), "bytes": size, "sha256": digest.hexdigest()}


def _empty() -> dict:
    return {"schema": "thread_ledger_segments.v1", "segments": [], "coverage": "complete"}


def _validate_entry(entry, previous):
    match = _SEGMENT_NAME.fullmatch(entry["file"])
    if (not match or type(entry["seq"]) is not int or int(match[1]) != entry["seq"]
            or entry["seq"] <= previous or type(entry["bytes"]) is not int or entry["bytes"] < 0
            or type(entry["rows"]) is not int or entry["rows"] < 0 or "carry" not in entry
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
        raise ValueError("invalid ledger segment entry")


def _rebuild(directory: Path) -> dict:
    """仅清单缺失/损坏时冷重建；丢失的 carry 永远保持未知。"""
    result = _empty()
    if not directory.exists():
        return result
    entries = []
    for path in directory.glob("seg-*.jsonl"):
        match = _SEGMENT_NAME.fullmatch(path.name)
        if match:
            try:
                file_stamp(path)
                entries.append({"seq": int(match[1]), "file": path.name, **_measure(path), "carry": None})
            except FileNotFoundError:
                result["coverage"] = "partial"
    result["segments"] = sorted(entries, key=lambda entry: entry["seq"])
    if entries:
        result["coverage"] = "partial"
    _publish(directory, result)
    return result


def read_manifest(archive_dir: Path) -> dict:
    """读取清单并报告缺段；调用方负责用账本原锁串行化清单重建。"""
    archive_dir = Path(archive_dir)
    path = archive_dir / "manifest.json"
    try:
        file_stamp(path)
        with _open(path) as handle:
            result = json.load(handle)
        if (not isinstance(result, dict) or result.get("schema") != "thread_ledger_segments.v1"
                or result.get("coverage") not in ("complete", "partial")):
            raise ValueError("invalid ledger manifest schema")
        entries = result["segments"]
        if not isinstance(entries, list):
            raise ValueError("invalid ledger segment list")
        previous = 0
        for entry in entries:
            _validate_entry(entry, previous)
            previous = entry["seq"]
        pending = result.get("pending")
        if pending is not None:
            _validate_entry(pending, previous)
            source = pending["source_identity"]
            if (not isinstance(source, list) or len(source) != 5 or any(type(value) is not int for value in source)
                    or type(pending["tail_bytes"]) is not int or pending["tail_bytes"] < 0
                    or source[2] != pending["bytes"] + pending["tail_bytes"]
                    or not re.fullmatch(r"[0-9a-f]{64}", pending["tail_sha256"])):
                raise ValueError("invalid ledger prepared entry")
        # rename 后未发布且没有 prepared 的旧中断不能静默遗漏孤段。
        if not result.get("pending") and (archive_dir / f"seg-{previous + 1:06d}.jsonl").exists():
            result = _rebuild(archive_dir)
    except FileNotFoundError:
        result = _rebuild(archive_dir)
    except (ValueError, TypeError, KeyError, AttributeError):
        result = _rebuild(archive_dir)
    errors = []
    previous = 0
    for entry in result["segments"]:
        current = file_stamp(archive_dir / entry["file"])
        if entry["seq"] != previous + 1 or current is None or current[2] != entry["bytes"]:
            errors.append({"coverage": "partial", "path": str(archive_dir / entry["file"]),
                           "message": "thread ledger segment is missing or changed"})
        previous = entry["seq"]
    if errors:
        result = {**result, "coverage": "partial", "load_errors": errors}
    return result


def iter_segment_paths(archive_dir: Path) -> list[Path]:
    return [archive_dir / entry["file"] for entry in read_manifest(archive_dir)["segments"]]


def _publish(directory: Path, manifest: dict) -> None:
    # 固定小首行让普通追加 O(1) 检查 prepared；段列表仅在封段、历史读取或冷恢复时解析。
    header = {"schema": manifest["schema"], "coverage": manifest["coverage"], "pending": manifest.get("pending"),
              "last_seq": manifest["segments"][-1]["seq"] if manifest["segments"] else 0}
    content = json.dumps(header, ensure_ascii=False)[:-1] + ',\n"segments": '
    content += json.dumps(manifest["segments"], ensure_ascii=False) + "}\n"
    write_private_text_file_atomic_unlocked(directory / "manifest.json", content)


def read_manifest_header(archive_dir: Path) -> dict | None:
    """有限近期窗口只读固定首行导航；旧格式、缺叶或 prepared 交回完整恢复口。"""
    try:
        with _open(archive_dir / "manifest.json") as handle:
            raw = handle.readline(512)
        if raw.endswith(b",\n"):
            header = json.loads(raw.rstrip()[:-1] + b"}")
            if (header.get("schema") == "thread_ledger_segments.v1" and "pending" in header
                    and header.get("coverage") in ("complete", "partial")
                    and type(header.get("last_seq")) is int and header["last_seq"] >= 0):
                return header
    except (FileNotFoundError, ValueError):
        pass
    return None


def recover_pending(active: Path) -> None:
    """必须已持原写锁；prepared 的旧/新尾封印决定恢复，不从文本猜测状态。"""
    directory = archive_dir_for(active)
    if not directory.exists():
        return
    header = read_manifest_header(directory)
    if header is not None and header["pending"] is None:
        return
    manifest = read_manifest(directory)
    entry = manifest.get("pending")
    if entry is None:
        return
    target = directory / entry["file"]
    original = entry["source_identity"]
    current = file_stamp(active)
    if file_stamp(target) is None:
        if current != tuple(original):
            raise DataCorruptionError("thread ledger prepared source is unreadable; coverage=partial")
        # 尚未移动 canonical，可撤回准备记录并在下次封段重新执行。
        manifest.pop("pending")
        _publish(directory, manifest)
        return
    if _measure(target) != {key: entry[key] for key in ("rows", "bytes", "sha256")}:
        raise DataCorruptionError("thread ledger prepared segment is unreadable; coverage=partial")
    if current == tuple(original):
        with _open(active) as handle:
            handle.seek(entry["bytes"])
            tail = handle.read(entry["tail_bytes"] + 1)
        if len(tail) != entry["tail_bytes"] or hashlib.sha256(tail).hexdigest() != entry["tail_sha256"]:
            raise DataCorruptionError("thread ledger prepared tail is unreadable; coverage=partial")
        write_bytes_atomic_beneath(active.parent, (active.name,), tail)
    elif current is None:
        if entry["tail_bytes"]:
            raise DataCorruptionError("thread ledger prepared tail is missing; coverage=partial")
    elif current[2] != entry["tail_bytes"] or _measure(active)["sha256"] != entry["tail_sha256"]:
        raise DataCorruptionError("thread ledger prepared tail changed; coverage=partial")
    manifest["segments"].append(entry)
    manifest.pop("pending")
    _publish(directory, manifest)


def _copy_prefix(active: Path, target: Path, end: int) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".seal-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output, _open(active) as source:
            remaining = end
            while remaining:
                raw = source.read(min(65536, remaining))
                if not raw:
                    raise DataCorruptionError("thread ledger changed during sealing")
                output.write(raw)
                remaining -= len(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        _sync_parent_directory(target)
    finally:
        temporary.unlink(missing_ok=True)


def seal_if_due(active: Path, *, max_bytes: int, carry=None, sealable_prefix_end: int | None = None) -> dict | None:
    """调用方必须已持该账本原写锁；只重写不超过 max_bytes 的尾部，未知前缀不封存。"""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    recover_pending(active)
    source = file_stamp(active)
    if source is None or source[2] < max_bytes:
        return None
    end = source[2] if sealable_prefix_end is None else sealable_prefix_end
    if type(end) is not int or end < 0 or end > source[2]:
        raise ValueError("invalid sealable prefix")
    if not end or source[2] - end > max_bytes:
        return None
    if end == source[2] and stat.S_IMODE(active.lstat().st_mode) != 0o600:
        os.chmod(active, 0o600, follow_symlinks=False)
        source = file_stamp(active)
    measurement = _measure(active, end)
    with _open(active) as handle:
        handle.seek(end)
        tail = handle.read(max_bytes + 1)
    directory = archive_dir_for(active)
    ensure_private_dir(directory)
    manifest = read_manifest(directory)
    sequence = manifest["segments"][-1]["seq"] + 1 if manifest["segments"] else 1
    entry = {"seq": sequence, "file": f"seg-{sequence:06d}.jsonl", **measurement, "carry": carry,
             "source_identity": list(source), "tail_bytes": len(tail), "tail_sha256": hashlib.sha256(tail).hexdigest()}
    target = directory / entry["file"]
    if target.exists():
        raise DataCorruptionError("thread ledger segment already exists")
    manifest["pending"] = entry
    _publish(directory, manifest)
    if end == source[2]:
        descriptor = os.open(active, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(active, target)
        _sync_parent_directory(active)
        _sync_parent_directory(target)
    else:
        _copy_prefix(active, target, end)
        write_bytes_atomic_beneath(active.parent, (active.name,), tail)
    manifest["segments"].append(entry)
    manifest.pop("pending")
    _publish(directory, manifest)
    return entry
