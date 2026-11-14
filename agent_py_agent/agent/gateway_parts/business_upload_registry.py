"""原业务上传JSONL的有界完整核验；派生续作状态不拥有登记或权限。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path

from ..artifacts.registry import REGISTRY_RELATIVE_PATH, _record_from_payload
from ..common.directory_lock import locked_private_directory
from ..common.json_io import locked_json_path
from ..common.nofollow_fs import (
    file_descriptor_change_ns,
    open_private_lock_beneath,
    open_readonly_file_beneath,
)
from ..io.sqlite_scope import connect_projection_sqlite, scoped_connection
from ..plugin_host.errors import HostAPIError

_ROOT = ('artifacts', 'business')
_SOURCE = (*_ROOT, *REGISTRY_RELATIVE_PATH.parts)
_INDEX = (*_ROOT, 'data', 'artifacts', '.business-upload-progress.sqlite3')
_KEY = secrets.token_bytes(32)
_SEAL = object()
# 单次上传登记核验请求默认向前推进读取的分步上限次数。
_UPLOAD_REGISTRY_STEPS_COUNT = 4
# SQLite 上传续作进度表最多保留的近期 artifact 记录条数。
_UPLOAD_PROGRESS_LIMIT_COUNT = 8
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


def _chain(previous, chunk):
    return hashlib.sha256(bytes.fromhex(previous) + hashlib.sha256(chunk).digest()).hexdigest()


def _pack(state):
    data = json.dumps(state, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    return data, hmac.new(_KEY, data.encode(), hashlib.sha256).hexdigest()


@contextmanager
def _connection(owner):
    path = owner.joinpath(*_INDEX)
    def factory():
        db = connect_projection_sqlite(path, timeout=5)
        try:
            db.execute('PRAGMA auto_vacuum=INCREMENTAL')
            db.execute('PRAGMA journal_size_limit=1048576')
            db.execute('CREATE TABLE IF NOT EXISTS progress (artifact_id TEXT PRIMARY KEY, '
                       'state TEXT NOT NULL, signature TEXT NOT NULL, touched REAL NOT NULL)')
            return db
        except BaseException:
            db.close()
            raise
    with locked_json_path(path):
        fd = open_private_lock_beneath(owner, _INDEX)
        os.close(fd)
        with scoped_connection(path, False, factory) as db:
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise


def _initial(owner, artifact_id, stamp, block):
    return {'owner': str(owner), 'artifact_id': artifact_id, 'stamp': list(stamp),
            'block': block, 'phase': 'collect', 'offset': 0, 'chain': '00' * 32,
            'collected_chain': '', 'pending': '', 'latest': None}


def _load(db, owner, artifact_id, stamp, block):
    row = db.execute('SELECT substr(state,1,8388608),substr(signature,1,128),length(state) FROM progress WHERE artifact_id=?', (artifact_id,)).fetchone()
    if row is not None:
        raw, signature, size = row
        if size <= 8388608 and isinstance(raw, str) and isinstance(signature, str) and hmac.compare_digest(hmac.new(_KEY, raw.encode(), hashlib.sha256).hexdigest(), signature):
            state = json.loads(raw)
            if (state['owner'] == str(owner) and state['artifact_id'] == artifact_id
                    and state['stamp'] == list(stamp) and state['block'] == block):
                return state
    # 重启、源换代或派生状态被改写只重建进度，不把索引当登记权威。
    return _initial(owner, artifact_id, stamp, block)


def _save(db, artifact_id, state):
    raw, signature = _pack(state)
    db.execute('INSERT INTO progress VALUES (?,?,?,?) ON CONFLICT(artifact_id) DO UPDATE SET '
               'state=excluded.state,signature=excluded.signature,touched=excluded.touched',
               (artifact_id, raw, signature, time.time()))
    db.execute('DELETE FROM progress WHERE artifact_id NOT IN '
               '(SELECT artifact_id FROM progress ORDER BY touched DESC LIMIT ?)', (_UPLOAD_PROGRESS_LIMIT_COUNT,))


def _consume(state, chunk, *, eof):
    previous = base64.b64decode(state['pending'])
    start = state['offset'] - len(previous)
    data = previous + chunk
    lines = data.splitlines(keepends=True)
    pending = b''
    for index, line in enumerate(lines):
        if index == len(lines) - 1 and not line.endswith(b'\n') and not eof:
            pending = line
            break
        if len(line) > _MAX_RECORD_BYTES:
            raise HostAPIError('HOST_NOT_READY', 503, message=(
                f'原登记单行超过{_MAX_RECORD_BYTES}字节核验上限（offset={start}）；未读取内容，请核对该行，不得盲重试。'))
        if line.strip():
            try:
                record = _record_from_payload(json.loads(line.decode('utf-8')))
                if record is None:
                    raise ValueError('invalid record')
            except (UnicodeError, ValueError, TypeError) as error:
                raise HostAPIError('STORE_UNAVAILABLE', 503, message=(
                    f'原上传登记offset={start}含无效或未完成JSON行（{type(error).__name__}）；未读取内容，修复原登记后同参数重试。')) from error
            if record.artifact_id == state['artifact_id']:
                # 物理末行胜出；时间戳相等或回退均不改变权威行顺序。
                state['latest'] = {'offset': start, 'raw': base64.b64encode(line).decode('ascii')}
        start += len(line)
    if len(pending) > _MAX_RECORD_BYTES:
        raise HostAPIError('HOST_NOT_READY', 503, message='原上传登记单行超过读取上限；未读取内容，请核对该行，不得盲重试。')
    state['pending'] = base64.b64encode(pending).decode('ascii')


def lookup_business_upload(owner_home, artifact_id, *, max_bytes=2 * 1024 * 1024, writer_locked=False):
    """每步不超原预算，单请求自动四步；无可信发布代时完整双遍，不抽样签latest。"""
    owner = Path(owner_home).absolute()
    if type(max_bytes) is not int or max_bytes < 1 or max_bytes > 2 * 1024 * 1024:
        raise ValueError('business registry block budget out of range')
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
            with _connection(owner) as db:
                state = _load(db, owner, artifact_id, stamp, max_bytes)
                progressed = 0
                for _step in range(_UPLOAD_REGISTRY_STEPS_COUNT):
                    if _stamp(fd) != stamp:
                        raise HostAPIError('HOST_NOT_READY', 503, message='原上传登记在核验期间换代；未读取内容，请对同一ID重新核验，不得换ID重传。')
                    chunk = _read(fd, state['offset'], min(max_bytes, stamp[2] - state['offset']))
                    eof = state['offset'] + len(chunk) == stamp[2]
                    if not chunk and not eof:
                        raise HostAPIError('HOST_NOT_READY', 503, message='原上传登记读取提前结束；未读取内容，请核对源文件后同参数重试。')
                    if state['phase'] == 'collect':
                        _consume(state, chunk, eof=eof)
                    state['chain'] = _chain(state['chain'], chunk)
                    state['offset'] += len(chunk)
                    progressed += len(chunk)
                    if _stamp(fd) != stamp:
                        raise HostAPIError('HOST_NOT_READY', 503, message='原上传登记在核验期间变化；未读取内容，请对同一ID继续核验。')
                    if eof and state['phase'] == 'collect':
                        state.update(phase='verify', collected_chain=state['chain'], offset=0, chain='00' * 32)
                    elif eof:
                        if state['chain'] != state['collected_chain']:
                            state = _initial(owner, artifact_id, stamp, max_bytes)
                            continue
                        latest = state['latest']
                        db.execute('DELETE FROM progress WHERE artifact_id=?', (artifact_id,))
                        if latest is None:
                            return UploadLookup(None, stamp[2], None)
                        raw = base64.b64decode(latest['raw'])
                        record = _record_from_payload(json.loads(raw.decode('utf-8')))
                        proof = RegistryRecordProof(str(owner), artifact_id, stamp, latest['offset'],
                                                    len(raw), hashlib.sha256(raw).hexdigest(), _SEAL)
                        return UploadLookup(record, stamp[2], proof)
                _save(db, artifact_id, state)
            # 返回进度前已提交派生checkpoint；503不是同一总量上限的永久拒绝。
            raise HostAPIError('HOST_NOT_READY', 503, message=(
                f'原上传登记核验可续：本次推进{progressed}字节，{state["phase"]} offset={state["offset"]}/{stamp[2]}；'
                '未读取内容，请保持同一artifact_id原参数再次调用，宿主将继续原游标，不重传或换ID。'))
        finally:
            os.close(fd)


def verify_record_proof(owner_home, proof):
    """续页只复核原登记确切行与源代；调用方仍必须重验当前资产/空间和正文块。"""
    owner = Path(owner_home).absolute()
    if (not isinstance(proof, RegistryRecordProof) or proof._seal is not _SEAL
            or proof.owner != str(owner) or proof.offset < 0 or not 0 < proof.length <= 2 * 1024 * 1024):
        raise HostAPIError('STATE_CONFLICT', 409, message='原登记页证明失效；未读取内容，请用同一正式资产版本从首读重取，不扩大范围。')
    try:
        fd = open_readonly_file_beneath(owner, _SOURCE)
    except FileNotFoundError as error:
        raise HostAPIError('STATE_CONFLICT', 409, message='原上传登记已不存在；未读取内容，请核对原资产和来源，不得沿旧游标读取。') from error
    try:
        if _stamp(fd) != proof.source_stamp:
            raise HostAPIError('STATE_CONFLICT', 409, message='原上传登记已换代；未读取内容，请保持同一资产版本从首读重新核验。')
        raw = _read(fd, proof.offset, proof.length)
        if hashlib.sha256(raw).hexdigest() != proof.sha256 or _stamp(fd) != proof.source_stamp:
            raise HostAPIError('STATE_CONFLICT', 409, message='原上传登记行已改变；未读取内容，请核对原资产版本并从首读重新核验。')
        record = _record_from_payload(json.loads(raw.decode('utf-8')))
        if record is None or record.artifact_id != proof.artifact_id:
            raise HostAPIError('STATE_CONFLICT', 409, message='原上传登记身份不再对应原页；未读取内容，请从同一资产版本首读重取。')
        return record
    finally:
        os.close(fd)
