"""任务轮次的唯一追加源；写者复用原 telemetry owner 锁，读者使用原 OwnerReadIndex。

JSON 水位不是跨文件事务证明。先提交的轮次可在摘要失败后精确重放；公开详情披露差异。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

from ..common.nofollow_fs import (
    append_text_beneath,
    open_directory_beneath,
    open_private_lock_beneath,
    open_readonly_file_beneath,
    read_bytes_beneath,
    read_text_beneath,
    write_bytes_atomic_beneath,
    write_text_atomic_beneath,
)
from ..local_storage.owner_read_index import (
    _INT64_MAX,
    _OWNER_INDEX_MAX_PAGE_ROWS,
    _OWNER_INDEX_MAX_ROW_BYTES,
    OwnerReadIndex,
    OwnerReadUnavailable,
    require_complete,
)
from ..workflows.models import redact_bearer_values as redact
from .telemetry_ledger import (
    TELEMETRY_TOTAL_MAX_BYTES,
    TELEMETRY_VOLUME_MAX_BYTES,
    _encode,
    _lock,
    _number,
)

_SCHEMA = "task_rounds.v1"


def _frame(generation, sequence, row):
    return dict(schema=_SCHEMA, generation=generation, sequence=sequence, row=row)


# 长度由固定格式和摘要身份推导，不是新的容量或轮询参数。
_HEADER_BYTES = len(_encode(_frame("0" * 64, 0, None)).encode("utf-8"))


def _parts(task_id):
    from .task_call_inspector import _path

    path = _path(task_id)
    return (*path[:-1], path[-1][:-5] + ".rounds.jsonl")


def _validate_round(row):
    from .task_call_inspector import _usage

    if not isinstance(row, dict):
        raise ValueError("invalid task round")
    _usage(row.get("token_usage", row))
    if "duration_ms" in row:
        _number(row["duration_ms"])


def _validate_frame(frame):
    if (not isinstance(frame, dict) or frame.get("schema") != _SCHEMA
            or not isinstance(frame.get("generation"), str) or len(frame["generation"]) != 64
            or type(frame.get("sequence")) is not int or not 0 <= frame["sequence"] <= _INT64_MAX):
        raise ValueError("invalid task round frame")
    if frame["sequence"] == 0:
        if frame.get("row") is not None:
            raise ValueError("invalid task round header")
    else:
        _validate_round(frame.get("row"))


def _snapshot(home, task_id, parts=None):
    descriptor = open_readonly_file_beneath(home, parts or _parts(task_id))
    try:
        info = os.fstat(descriptor)
        raw = os.pread(descriptor, _HEADER_BYTES, 0)
        if len(raw) != _HEADER_BYTES:
            raise OSError("short task round header read")
        header = json.loads(raw)
        _validate_frame(header)
        if header["sequence"] != 0 or not raw.endswith(b"\n"):
            raise ValueError("invalid task round header")
        return header["generation"], [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    finally:
        os.close(descriptor)


def _reference(home, task_id, count, previous=None):
    generation, stamp = _snapshot(home, task_id)
    return {**(previous or {}), **dict(generation=generation, count=count, stamp=stamp)}


def _segment_parts(task_id, generation):
    if not isinstance(generation, str) or len(generation) != 64 or any(c not in '0123456789abcdef' for c in generation):
        raise ValueError('invalid task round segment generation')
    parts = _parts(task_id)
    return (*parts[:-1], parts[-1][:-6] + '.' + generation + '.jsonl')


def _rotate_rounds(home, metadata):
    """保留旧代源后切活动卷；元数据落盘前中断可从原 generation 精确恢复。"""
    reference = metadata['_rounds']
    task_id = metadata['task_id']
    target = _segment_parts(task_id, reference['generation'])
    try:
        generation, stamp = _snapshot(home, task_id)
    except FileNotFoundError:
        generation, stamp = None, None
    segments = list(reference.get('segments', []))
    if generation == reference['generation'] and stamp[2] < TELEMETRY_VOLUME_MAX_BYTES:
        return
    if generation == reference['generation']:
        if os.rename in os.supports_dir_fd:
            descriptor = open_directory_beneath(home, _parts(task_id)[:-1])
            try:
                os.rename(_parts(task_id)[-1], target[-1], src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        else:
            from ..common.nofollow_fs import _portable_directory
            directory = _portable_directory(home, _parts(task_id)[:-1], create=False)
            os.rename(directory / _parts(task_id)[-1], directory / target[-1])
        generation = None
    # 只认元数据原代次对应的封卷，不枚举目录或推测其它历史。
    sealed_generation, sealed_stamp = _snapshot(home, task_id, target)
    if sealed_generation != reference['generation'] or sealed_stamp[:3] != reference['stamp'][:3]:
        raise ValueError('task round rotation source conflict')
    if not generation:
        write_text_atomic_beneath(home, _parts(task_id), _encode(_frame(secrets.token_hex(32), 0, None)))
    segments.append(dict(generation=sealed_generation, stamp=sealed_stamp))
    metadata['_rounds'] = _reference(home, task_id, reference['count'], {**reference, 'segments': segments})
    _write_metadata(home, metadata)



def prepare_task_rounds(home, row, *, replace=False):
    """仅在原 writer 锁内迁移/显式替换轮次，返回尚待 JSON 原子提交的摘要与 CAS 水位。"""
    from .task_call_inspector import _sum_usage

    previous = row.get("_rounds")
    if previous and not replace:
        return row
    rounds = row["rounds"]
    task_id = row["task_id"]
    migration = hashlib.sha256(_encode(dict(task_id=task_id, rounds=rounds)).encode("utf-8")).hexdigest()
    generation = secrets.token_hex(32) if previous else migration
    frames = [_frame(generation, 0, None)]
    frames.extend(_frame(generation, sequence, value) for sequence, value in enumerate(rounds, 1))
    for frame in frames:
        _validate_frame(frame)
        if len(_encode(frame).encode("utf-8")) > _OWNER_INDEX_MAX_ROW_BYTES:
            raise ValueError("task round exceeds existing index row budget")
    blob = "".join(_encode(frame) for frame in frames)
    # 迁移输入的摘要就是代次：JSON 提交前中断时只能复用完全相同的源，不能再次追加旧轮次。
    existing = read_bytes_beneath(home, _parts(task_id)) if not previous else None
    if existing is not None and existing != blob.encode("utf-8"):
        raise ValueError("task round migration CAS conflict")
    if existing is None:
        write_text_atomic_beneath(home, _parts(task_id), blob)
    return {**row, "rounds": [], "token_usage": _sum_usage(rounds),
            "_rounds": _reference(home, task_id, len(rounds))}


def _write_metadata(home, row):
    from .task_call_inspector import TaskCallInspector

    TaskCallInspector(home, owner_id=row["owner_id"])._save(row, sync_index=False, include_rounds=False)


def _apply_round(home, row, data):
    from .task_call_inspector import _usage

    usage = _usage(data.get("token_usage", data))
    row["token_usage"] = {key: row["token_usage"][key] + usage[key] for key in usage}
    row["root_run_id"] = row.get("root_run_id") or str(data.get("root_run_id") or "")
    row["_rounds"] = _reference(home, row["task_id"], row["_rounds"]["count"] + 1, row["_rounds"])


def append_task_round(owner_home: Path, task_id: str, row: dict[str, object]) -> None:
    """公开追加入口自己持原 owner 锁，不要求调用方复制锁管理。"""
    with _lock(Path(owner_home).absolute()):
        _append_task_round_unlocked(owner_home, task_id, row)


def _append_task_round_unlocked(owner_home, task_id, row):
    """在调用方持有的原 owner 锁内追加一轮；水位之后最多有本次单行事务的恢复量。"""
    from .task_call_inspector import _path

    _validate_round(row)
    raw = read_text_beneath(owner_home, _path(task_id))
    if raw is None:
        raise KeyError(task_id)
    metadata, row = redact(json.loads(raw)), redact(dict(row))
    if metadata.get("schema") != "task_call_telemetry.v1" or metadata.get("task_id") != task_id:
        raise ValueError("invalid task record")
    if not metadata.get("_rounds"):
        metadata = prepare_task_rounds(owner_home, metadata)
        _write_metadata(owner_home, metadata)
    reference = metadata['_rounds']
    total = reference['stamp'][2] + sum(segment['stamp'][2] for segment in reference.get('segments', []))
    if total >= TELEMETRY_TOTAL_MAX_BYTES:
        metadata['_rounds']['capacity_limited'] = True
        _write_metadata(owner_home, metadata)
        raise ValueError('task round capacity_limited; existing evidence retained')
    _rotate_rounds(owner_home, metadata)
    generation, stamp = _snapshot(owner_home, task_id)
    reference = metadata["_rounds"]
    if (generation != reference["generation"] or stamp[:2] != reference["stamp"][:2]
            or stamp[2] < reference["stamp"][2]):
        raise ValueError("task round source changed")
    if stamp != reference["stamp"]:
        start = reference["stamp"][2]
        size = stamp[2] - start
        if not 0 < size <= _OWNER_INDEX_MAX_ROW_BYTES:
            raise ValueError("task round recovery exceeds one record")
        descriptor = open_readonly_file_beneath(owner_home, _parts(task_id))
        try:
            pending = os.pread(descriptor, size, start)
        finally:
            os.close(descriptor)
        if len(pending) != size:
            raise OSError("short task round recovery read")
        try:
            recovered = json.loads(pending)
            _validate_frame(recovered)
        except (ValueError, UnicodeError):
            if pending.endswith(b"\n"):
                raise ValueError("invalid committed task round") from None
            sidecar = (*_parts(task_id)[:-1], _parts(task_id)[-1] + ".torn-" + hashlib.sha256(pending).hexdigest())
            write_bytes_atomic_beneath(owner_home, sidecar, pending)
            descriptor = open_private_lock_beneath(owner_home, _parts(task_id))
            try:
                info = os.fstat(descriptor)
                if [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns] != stamp:
                    raise ValueError("task round changed during recovery")
                os.ftruncate(descriptor, start)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            metadata["_rounds"] = _reference(owner_home, task_id, reference["count"], reference)
            recovered = None
        else:
            if recovered["generation"] != generation or recovered["sequence"] != reference["count"] + 1:
                raise ValueError("task round recovery CAS conflict")
            # 即使前次在 fsync 中失败，恢复水位前也先确立耐久的完整行。
            append_text_beneath(owner_home, _parts(task_id), "" if pending.endswith(b"\n") else "\n")
            _apply_round(owner_home, metadata, recovered["row"])
        _write_metadata(owner_home, metadata)
        if recovered and row.get("round_id") is not None and row.get("round_id") == recovered["row"].get("round_id"):
            if row != recovered["row"]:
                raise ValueError("task round replay conflicts")
            return
    frame = _frame(generation, metadata["_rounds"]["count"] + 1, row)
    _validate_frame(frame)
    blob = _encode(frame)
    if len(blob.encode("utf-8")) > _OWNER_INDEX_MAX_ROW_BYTES:
        raise ValueError("task round exceeds existing index row budget")
    append_text_beneath(owner_home, _parts(task_id), blob)
    _apply_round(owner_home, metadata, row)
    _write_metadata(owner_home, metadata)


def read_task_rounds(owner_home: Path, task_id: str) -> list[dict[str, object]]:
    """显式详情才推进/遍历完整读投影；坏源或无法完成的预算不能伪装成空历史。"""
    from .task_call_inspector import _path

    metadata = json.loads(read_text_beneath(owner_home, _path(task_id)))
    sources = [_segment_parts(task_id, item['generation']) for item in metadata['_rounds'].get('segments', [])]
    sources.append(_parts(task_id))
    rows, sequence = [], 1
    for parts in sources:
        _snapshot(owner_home, task_id, parts)
        index = OwnerReadIndex(owner_home)
        view = index.advance('task_rounds', parts, validator=_validate_frame)
        while view['status'] == 'partial':
            progress = index._advance_progress(view)
            view = index.advance('task_rounds', parts, validator=_validate_frame)
            if index._advance_progress(view) <= progress:
                break
        require_complete({'coverage': view})
        cursor = None
        while True:
            page = require_complete(index.query(view, limit=_OWNER_INDEX_MAX_PAGE_ROWS, after_cursor=cursor))
            for frame in page['rows']:
                if not frame['sequence']:
                    continue
                if frame['sequence'] != sequence:
                    raise OwnerReadUnavailable(dict(status='unavailable', reason='round_sequence_conflict'))
                rows.append(frame['row'])
                sequence += 1
            cursor = page['next_cursor']
            if cursor is None:
                break
    return rows


def hydrate_task_rounds(home, row):
    """按需组合完整详情，摘要落后或来源变化只签发明确的 partial。"""
    from .task_call_inspector import _append_model_run, _sum_usage

    reference = row.pop("_rounds", None)
    if reference and reference.get('retired'):
        row['rounds'] = []
        row['rounds_coverage'] = dict(status='partial', reason='task_rounds_retired')
        return row
    if reference is None:
        try:
            _snapshot(home, row["task_id"])
        except FileNotFoundError:
            return row
        row["rounds_coverage"] = dict(status="partial", reason="migration_pending")
        return row
    try:
        before = _snapshot(home, row["task_id"])
        rounds = read_task_rounds(home, row["task_id"])
        after = _snapshot(home, row["task_id"])
    except (OwnerReadUnavailable, FileNotFoundError, ValueError) as error:
        row["rounds"] = []
        row["rounds_coverage"] = getattr(error, "coverage", dict(status="unavailable", reason="rounds_source_missing_or_invalid"))
        return row
    row["rounds"], row["token_usage"] = rounds, _sum_usage(rounds)
    by_run = {run["run_id"]: run for run in row["runs"] if run.get("run_id")}
    for data in rounds:
        _append_model_run(row, data, runs_by_id=by_run)
    complete = (before == after == (reference["generation"], reference["stamp"]) and len(rounds) == reference["count"])
    row["rounds_coverage"] = dict(status="complete" if complete else "partial", reason="covered" if complete else "metadata_lag")
    return row


def missing_task_metadata(home, task_id, owner_id):
    """首次 JSON 提交失败也不能把已存在的轮次源报成没有任务，不推测缺失的生命周期。"""
    from .task_call_inspector import TaskCallRecord

    try:
        _snapshot(home, task_id)
    except FileNotFoundError:
        return None
    row = TaskCallRecord(task_id, owner_id=owner_id).to_dict()
    row.update(status=None, rounds_coverage=dict(status="unavailable", reason="task_metadata_missing"))
    return row


def read_task_rounds_page(owner_home: Path, task_id: str, *, limit: int = 20, cursor: str = '') -> dict[str, object]:
    """精确任务的只读轮次页；首行头部仅是投影元数据，不对外当模型轮次。"""
    import base64

    from .task_call_inspector import _path

    metadata = json.loads(read_text_beneath(owner_home, _path(task_id)))
    reference = metadata.get('_rounds', {})
    if reference.get('retired'):
        return dict(rows=[], total_count=None, scanned=0, next_cursor=None,
                    coverage=dict(status='partial', reason='task_rounds_retired'))
    sources = [_segment_parts(task_id, item['generation']) for item in reference.get('segments', [])]
    sources.append(_parts(task_id))
    source, inner = 0, ''
    if cursor:
        try:
            if not isinstance(cursor, str) or len(cursor) > 32768:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            source, inner = value['source'], value['inner']
            if (set(value) != {'source', 'inner', 'task', 'home'} or value['task'] != task_id
                    or value['home'] != hashlib.sha256(str(Path(owner_home).absolute()).encode()).hexdigest()
                    or type(source) is not int or not 0 <= source < len(sources) or not isinstance(inner, str)):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ValueError('invalid task round cursor') from None
    page = OwnerReadIndex(owner_home).query_readonly_page(
        'task_rounds', sources[source], validator=_validate_frame, limit=limit, cursor=inner,
        filters={'since': 1})
    page['rows'] = [frame['row'] for frame in page['rows'] if frame['sequence'] > 0]
    next_inner = page['next_cursor']
    if not next_inner and page['coverage']['status'] == 'complete':
        source += 1
    if next_inner or source < len(sources) and page['coverage']['status'] == 'complete':
        page['next_cursor'] = base64.urlsafe_b64encode(_encode(dict(source=source, inner=next_inner or '',
            task=task_id, home=hashlib.sha256(str(Path(owner_home).absolute()).encode()).hexdigest())).encode()).decode()
    page['coverage']['capacity_limited'] = bool(reference.get('capacity_limited'))
    return page
