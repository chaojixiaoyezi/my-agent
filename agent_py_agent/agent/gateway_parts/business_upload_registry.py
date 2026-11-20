"""原业务上传JSONL的有界完整核验；派生续作状态不拥有登记或权限。"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..common.directory_lock import locked_private_directory
from ..common.nofollow_fs import file_descriptor_change_ns, open_readonly_file_beneath
from ..local_storage.owner_read_index import (
    OwnerReadIndex,
    OwnerReadUnavailable,
    _ensure_keyed_table,
)
from ..plugin_host.errors import HostAPIError

_ROOT = ('artifacts', 'business')
_SOURCE = (*_ROOT, *REGISTRY_RELATIVE_PATH.parts)
_WATERMARK = 'upload_registry_watermark'
_RECEIPT = 'upload_registry_receipt'
_TABLE_GENERATION = 'domain_generation:ori_upload_registry'
_SEAL = object()
# 单次上传登记核验请求默认向前推进读取的分步上限次数。
_UPLOAD_REGISTRY_STEPS_COUNT = 4
# 单条上传登记 JSONL 记录允许读取并解析的最大字节数（2 MiB）。
_MAX_RECORD_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class RegistryRecordProof:
    owner: str
    artifact_id: str
    source_stamp: tuple
    offset: int
    length: int
    sha256: str
    _seal: object


@dataclass(frozen=True)
class UploadLookup:
    record: object | None
    registry_size: int
    proof: RegistryRecordProof | None
    pending: bool = False
    watermark: int | None = None


def _host_error(code: str, status: int, *, message: str, reason: str) -> HostAPIError:
    """构造带结构化原因标识的宿主异常，避免上层解析异常文案做控制流判断。"""
    error = HostAPIError(code, status, message=message)
    error.registry_reason = reason
    return error


def _stamp(fd):
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            file_descriptor_change_ns(fd, info), info.st_mode)


def _read(fd, offset, count):
    os.lseek(fd, offset, os.SEEK_SET)
    chunks = []
    while count:
        value = os.read(fd, min(65536, count))
        if not value:
            break
        chunks.append(value)
        count -= len(value)
    return b''.join(chunks)


def _bound_checkpoint(db, stamp):
    """返回严格绑定到该源代的检查点；字段缺失、换代或不自洽时返回 None。"""
    metadata = dict(db.execute(
        'SELECT key,value FROM ori_meta WHERE key IN (?,?,?)',
        (_WATERMARK, _RECEIPT, _TABLE_GENERATION),
    ))
    try:
        watermark = json.loads(metadata[_WATERMARK])
        receipt = json.loads(metadata[_RECEIPT])
        offset = watermark['offset']
        pending = base64.b64decode(receipt['pending'], validate=True)
        largest = receipt['largest_row']
        if (
            receipt['table_generation'] == metadata[_TABLE_GENERATION]
            and receipt['source_stamp'] == list(stamp)
            and (watermark['st_dev'], watermark['st_ino']) == stamp[:2]
            and isinstance(watermark['generation'], str) and watermark['generation']
            and receipt['generation'] == watermark['generation']
            and type(offset) is int and 0 <= offset <= stamp[2]
            and len(pending) <= min(offset, _MAX_RECORD_BYTES)
            and (offset < stamp[2] or not pending)
            and type(largest) is int and 0 <= largest <= _MAX_RECORD_BYTES
            and type(receipt['largest_row_offset']) is int and 0 <= receipt['largest_row_offset'] <= offset
        ):
            return watermark, receipt, pending
    except (KeyError, ValueError, TypeError):
        pass
    return None


def _read_checkpoint(db, stamp):
    bound = _bound_checkpoint(db, stamp)
    if bound is not None:
        return (*bound, False)

    # 仅重建上传投影。其它 ORI 领域和既有源检查点保持原代。
    if db.execute('SELECT 1 FROM ori_upload_registry LIMIT 1').fetchone() is not None:
        db.execute('DROP TABLE ori_upload_registry')
        _ensure_keyed_table(db, 'ori_upload_registry')
    table_generation = db.execute(
        'SELECT value FROM ori_meta WHERE key=?', (_TABLE_GENERATION,),
    ).fetchone()[0]
    generation = secrets.token_hex(16)
    return (
        {'st_dev': stamp[0], 'st_ino': stamp[1], 'offset': 0, 'generation': generation},
        {'generation': generation, 'source_stamp': list(stamp), 'table_generation': table_generation,
         'coverage': 'partial', 'rebuilt_at': time.time(), 'largest_row': 0, 'largest_row_offset': 0},
        b'',
        True,
    )


def _write_checkpoint(db, watermark, receipt, pending, size):
    receipt['coverage'] = 'complete' if watermark['offset'] == size else 'partial'
    receipt['pending'] = base64.b64encode(pending).decode('ascii')
    db.executemany(
        'INSERT INTO ori_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        ((_WATERMARK, json.dumps(watermark)), (_RECEIPT, json.dumps(receipt))),
    )


def _line_budget(offset, limit):
    return _host_error(
        'HOST_NOT_READY', 503, reason='line_budget',
        message=f'原上传登记行超过{limit}字节核验上限（offset={offset}）；未读取内容，请核对该行后重试。',
    )


def _consume(db, watermark, receipt, pending, chunk, *, eof, limit):
    start = watermark['offset'] - len(pending)
    lines = (pending + chunk).splitlines(keepends=True)
    pending = b''
    for index, line in enumerate(lines):
        if len(line) > limit:
            raise _line_budget(start, limit)
        if index == len(lines) - 1 and not line.endswith(b'\n') and not eof:
            pending = line
            break
        if len(line) > receipt['largest_row']:
            receipt['largest_row'] = len(line)
            receipt['largest_row_offset'] = start
        if line.strip():
            try:
                record = _record_from_payload(json.loads(line.decode('utf-8')))
                if record is None:
                    raise ValueError('invalid record')
            except (UnicodeError, ValueError, TypeError) as error:
                raise _host_error(
                    'STORE_UNAVAILABLE', 503, reason='unreadable',
                    message=(f'原上传登记offset={start}含无效或未完成JSON行（{type(error).__name__}）；'
                             '未读取内容，修复原登记后同参数重试。'),
                ) from error
            db.execute(
                'INSERT INTO ori_upload_registry(artifact_id,byte_offset,length,row_sha,generation) VALUES (?,?,?,?,?) '
                'ON CONFLICT(artifact_id) DO UPDATE SET byte_offset=excluded.byte_offset,length=excluded.length,'
                'row_sha=excluded.row_sha,generation=excluded.generation',
                (record.artifact_id, start, len(line), hashlib.sha256(line).hexdigest(), watermark['generation']),
            )
        start += len(line)
    return pending


def _advance(db, fd, stamp, *, max_bytes, max_steps, max_total_bytes, record_limit):
    watermark, receipt, pending, reset = _read_checkpoint(db, stamp)
    if receipt['largest_row'] > record_limit:
        raise _line_budget(receipt['largest_row_offset'], record_limit)
    if len(pending) > record_limit:
        raise _line_budget(watermark['offset'] - len(pending), record_limit)
    progressed = 0
    for _ in range(max_steps):
        remaining = min(max_bytes, stamp[2] - watermark['offset'])
        if max_total_bytes is not None:
            remaining = min(remaining, max_total_bytes - progressed)
        if not remaining:
            break
        chunk = _read(fd, watermark['offset'], remaining)
        if len(chunk) != remaining:
            raise _host_error('HOST_NOT_READY', 503, reason='short_read',
                message='原上传登记读取提前结束；未读取内容，请核对源文件后同参数重试。')
        end = watermark['offset'] + len(chunk)
        pending = _consume(db, watermark, receipt, pending, chunk, eof=end == stamp[2], limit=record_limit)
        watermark['offset'] = end
        progressed += len(chunk)
    if reset or progressed:
        _write_checkpoint(db, watermark, receipt, pending, stamp[2])
    return watermark, progressed


class UploadExtensionWitness:
    """原子发布边界上的前缀连续性见证：只由持写锁的规范写入方创建，不授予登记或读取权限。"""

    def __init__(self, owner: Path, previous_stamp: tuple):
        self._owner = owner
        self._previous = previous_stamp
        self._confirmed = False

    def confirm(self, previous: bytes) -> bool:
        # 写入方读出的旧字节必须正是见证时的源代全部字节，且结束在完整行上，
        # 否则旧投影里按 EOF 收尾的末行会与新行拼接，不能沿用。
        self._confirmed = False
        if len(previous) != self._previous[2] or (previous and not previous.endswith(b'\n')):
            return False
        try:
            fd = open_readonly_file_beneath(self._owner, _SOURCE)
        except OSError:
            return False
        try:
            self._confirmed = _stamp(fd) == self._previous
        finally:
            os.close(fd)
        return self._confirmed

    def rebind(self, published_size: int) -> bool:
        """把绑定在旧源代上的上传投影改绑到刚发布的新源代；进度、残行和行代原样保留。"""
        if not self._confirmed or published_size <= self._previous[2]:
            return False
        try:
            fd = open_readonly_file_beneath(self._owner, _SOURCE)
            try:
                stamp = _stamp(fd)
                if stamp[2] != published_size:
                    return False
                with OwnerReadIndex(self._owner).upload_registry_transaction() as db:
                    bound = _bound_checkpoint(db, self._previous)
                    if bound is None:
                        return False
                    watermark, receipt, pending = bound
                    watermark.update(st_dev=stamp[0], st_ino=stamp[1])
                    receipt['source_stamp'] = list(stamp)
                    _write_checkpoint(db, watermark, receipt, pending, stamp[2])
                    return True
            finally:
                os.close(fd)
        except (OSError, ValueError, sqlite3.Error, OwnerReadUnavailable) as error:
            # 规范登记已成功；投影改绑失败只让下次查询按原规则重建，不把发布谎报为失败。
            logging.getLogger(__name__).warning(
                'UPLOAD_PROJECTION_REBIND_FAILED canonical_registry_committed=true error_type=%s '
                'next_action=rebuild_on_next_lookup', type(error).__name__)
            return False


def upload_extension_witness(anchor, parts) -> UploadExtensionWitness | None:
    """仅对 owner 业务上传登记返回见证；其它登记或源缺失时返回 None。调用方须持业务写锁。"""
    anchor = Path(anchor).absolute()
    if tuple(parts) != tuple(REGISTRY_RELATIVE_PATH.parts) or anchor.parts[-len(_ROOT):] != _ROOT:
        return None
    owner = anchor.parents[len(_ROOT) - 1]
    try:
        fd = open_readonly_file_beneath(owner, _SOURCE)
    except OSError:
        return None
    try:
        return UploadExtensionWitness(owner, _stamp(fd))
    except OSError:
        return None
    finally:
        os.close(fd)


def _source_matches(owner, fd, stamp):
    if _stamp(fd) != stamp:
        return False
    try:
        current = open_readonly_file_beneath(owner, _SOURCE)
    except FileNotFoundError:
        return False
    try:
        return _stamp(current) == stamp
    finally:
        os.close(current)


def lookup_business_upload(
    owner_home,
    artifact_id,
    *,
    max_bytes=2 * 1024 * 1024,
    writer_locked=False,
    max_steps=_UPLOAD_REGISTRY_STEPS_COUNT,
    max_total_bytes=None,
    return_pending=False,
    max_record_bytes=None,
):
    """在原请求预算内推进共享水位；完整覆盖后按主键取址并复核原行。"""
    owner = Path(owner_home).absolute()
    if type(max_bytes) is not int or max_bytes < 1 or max_bytes > 2 * 1024 * 1024:
        raise ValueError('business registry block budget out of range')
    if (
        type(max_steps) is not int
        or isinstance(max_steps, bool)
        or not 1 <= max_steps <= _UPLOAD_REGISTRY_STEPS_COUNT
    ):
        raise ValueError('business registry step budget out of range')
    if max_total_bytes is not None and (
        type(max_total_bytes) is not int
        or isinstance(max_total_bytes, bool)
        or max_total_bytes < 1
        or max_total_bytes > max_bytes * _UPLOAD_REGISTRY_STEPS_COUNT
    ):
        raise ValueError('business registry total byte budget out of range')
    if type(return_pending) is not bool:
        raise ValueError('invalid return_pending')
    if max_record_bytes is not None and (
        type(max_record_bytes) is not int
        or isinstance(max_record_bytes, bool)
        or max_record_bytes < 1
        or max_record_bytes > _MAX_RECORD_BYTES
    ):
        raise ValueError('business registry record byte budget out of range')
    record_limit = _MAX_RECORD_BYTES if max_record_bytes is None else max_record_bytes
    # 空Owner只确认原源缺失，不为只读查ID创建canonical业务目录。
    try:
        existing = open_readonly_file_beneath(owner, _SOURCE)
    except FileNotFoundError:
        return UploadLookup(None, 0, None)
    else:
        os.close(existing)
    boundary = nullcontext() if writer_locked else locked_private_directory(
        owner, relative_parts=_ROOT, lock_name='.business-artifacts.lock')
    with boundary:
        try:
            fd = open_readonly_file_beneath(owner, _SOURCE)
        except FileNotFoundError:
            return UploadLookup(None, 0, None)
        try:
            stamp = _stamp(fd)
            conflict = None
            try:
                with OwnerReadIndex(owner).upload_registry_transaction() as db:
                    watermark, progressed = _advance(
                        db, fd, stamp, max_bytes=max_bytes, max_steps=max_steps,
                        max_total_bytes=max_total_bytes, record_limit=record_limit,
                    )
                    offset = watermark['offset']
                    if not _source_matches(owner, fd, stamp):
                        raise _host_error('HOST_NOT_READY', 503, reason='source_changed',
                            message='原上传登记在核验期间变化；未读取内容，请对同一ID继续核验。')
                    if offset == stamp[2]:
                        hit = db.execute(
                            'SELECT byte_offset,length,row_sha,generation FROM ori_upload_registry WHERE artifact_id=?',
                            (artifact_id,),
                        ).fetchone()
                        if hit is None:
                            return UploadLookup(None, stamp[2], None)
                        pos, length, row_sha, generation = hit
                        proof = RegistryRecordProof(str(owner), artifact_id, stamp, pos, length, row_sha, _SEAL)
                        try:
                            if generation != watermark['generation'] or not _valid_extent(proof):
                                raise _invalid_proof()
                            if max_total_bytes is not None:
                                if length > max_total_bytes:
                                    raise _line_budget(pos, max_total_bytes)
                                if progressed + length > max_total_bytes:
                                    proof = None
                            if proof is not None:
                                record = _verify_row(owner, fd, proof)
                                return UploadLookup(record, stamp[2], proof)
                        except HostAPIError as error:
                            if error.code != 'STATE_CONFLICT':
                                raise
                            # 保留原失败并撤销覆盖证明，下次按原始登记重建。
                            db.execute('DELETE FROM ori_meta WHERE key IN (?,?)', (_WATERMARK, _RECEIPT))
                            conflict = error
            except OwnerReadUnavailable as error:
                reason = error.coverage['reason']
                raise _host_error('HOST_NOT_READY', 503, reason=reason,
                    message=f'上传登记索引暂不可用（{reason}）；未读取内容，请对同一ID重试或核对所有者读索引。') from error
            if conflict is not None:
                raise conflict
            if return_pending:
                return UploadLookup(None, stamp[2], None, pending=True, watermark=offset)
            error = _host_error(
                'HOST_NOT_READY', 503, reason='scan_in_progress',
                message=(f'原上传登记核验可续：本次推进{progressed}字节，共享水位 offset={offset}/{stamp[2]}；'
                         '未读取内容，请保持同一artifact_id原参数再次调用，不重传或换ID。'),
            )
            error.registry_watermark = offset
            raise error
        finally:
            os.close(fd)


def _invalid_proof():
    return HostAPIError('STATE_CONFLICT', 409,
        message='原登记页证明失效；未读取内容，请用同一正式资产版本从首读重取，不扩大范围。')


def _valid_extent(proof):
    return (
        type(proof.offset) is int and proof.offset >= 0
        and type(proof.length) is int and 0 < proof.length <= _MAX_RECORD_BYTES
        and proof.offset + proof.length <= proof.source_stamp[2]
        and isinstance(proof.sha256, str)
    )


def _verify_row(owner, fd, proof):
    if not _valid_extent(proof):
        raise _invalid_proof()
    if _stamp(fd) != proof.source_stamp:
        raise HostAPIError('STATE_CONFLICT', 409,
            message='原上传登记已换代；未读取内容，请保持同一资产版本从首读重新核验。')
    raw = _read(fd, proof.offset, proof.length)
    if (len(raw) != proof.length or hashlib.sha256(raw).hexdigest() != proof.sha256
            or not _source_matches(owner, fd, proof.source_stamp)):
        raise HostAPIError('STATE_CONFLICT', 409,
            message='原上传登记行已改变；未读取内容，请核对原资产版本并从首读重新核验。')
    try:
        record = _record_from_payload(json.loads(raw.decode('utf-8')))
    except (ValueError, TypeError) as error:
        raise _invalid_proof() from error
    if record is None or record.artifact_id != proof.artifact_id:
        raise _invalid_proof()
    return record


def verify_record_proof(owner_home, proof):
    """续页只复核原登记确切行与源代；调用方仍须重验资产归属、版本及正文块。"""
    owner = Path(owner_home).absolute()
    if (not isinstance(proof, RegistryRecordProof) or proof._seal is not _SEAL
            or proof.owner != str(owner)):
        raise _invalid_proof()
    try:
        fd = open_readonly_file_beneath(owner, _SOURCE)
    except FileNotFoundError as error:
        raise HostAPIError('STATE_CONFLICT', 409,
            message='原上传登记已不存在；未读取内容，请核对原资产和来源，不得沿旧游标读取。') from error
    try:
        return _verify_row(owner, fd, proof)
    finally:
        os.close(fd)
