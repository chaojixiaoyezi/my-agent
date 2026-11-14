"""同 Gateway 进程树的物理模型运输槽；不计业务身份或外层任务。"""
from __future__ import annotations

import json
import os
import random
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..common.json_io import (
    _locked_file_path,
    locked_json_path,
    write_private_json_file_atomic_unlocked,
)
from ..common.nofollow_fs import ensure_private_dir, read_bytes_beneath

_BINDING_ENV = 'MY_AGENT_GATEWAY_TRANSPORT_RESOURCE'
_SCHEMA = 'gateway_transport_capacity.v1'
# 一个 Gateway 进程树可分配的模型运输槽数量上限。
_MAX_TRANSPORT_SLOT_COUNT = 4096
# 每个等待者每轮最多探测的槽数，防止满载时重复扫描所有槽。
_ROUND_PROBE_COUNT = 4
_SNAPSHOT_GUARD = threading.Lock()
_SNAPSHOT = None  # 仅保留当前Gateway资源的一份快照，不随owner/任务数量增长。
_START_GUARD = threading.Lock()
_next_start = 0


def _record(root: Path) -> dict:
    row = json.loads(read_bytes_beneath(root, ('capacity.json',)))
    if (not isinstance(row, dict) or row.get('schema') != _SCHEMA
            or type(row.get('limit')) is not int or type(row.get('slots')) is not int
            or not 0 <= row['limit'] <= row['slots'] <= _MAX_TRANSPORT_SLOT_COUNT):
        raise ValueError('GATEWAY_LLM_CAPACITY_INVALID: capacity.json结构无效；停止旧宿主后核对配置，不可绕过额度')
    return row


@contextmanager
def gateway_transport_binding(gateway_root: Path, limit: int):
    """单例宿主启动时固定配置；容量切换须证明所有旧物理槽都已排空。"""
    if type(limit) is not int or not 0 <= limit <= _MAX_TRANSPORT_SLOT_COUNT:
        raise ValueError('gateway_llm_inflight_limit必须为0到4096的整数')
    root = Path(gateway_root).absolute() / 'llm_transport_capacity'
    previous = os.environ.pop(_BINDING_ENV, None)
    try:
        if limit or root.exists():
            from ..common import json_io
            if json_io.fcntl is None and json_io.msvcrt is None:
                raise RuntimeError('GATEWAY_LLM_OS_LOCK_UNAVAILABLE: 此平台无跨进程OS锁，未启用资源额度；请使用受支持宿主')
            ensure_private_dir(root)
            with locked_json_path(root / 'capacity.json'):
                old = _record(root) if (root / 'capacity.json').exists() else {'limit': 0, 'slots': 0}
                slots = max(limit, old['slots'])
                # 永久锁文件不删换；同目录重启始终复用旧inode，不能用新代目录逃过旧活运输。
                with ExitStack() as held:
                    if old['limit'] != limit:
                        try:
                            for index in range(old['slots']):
                                held.enter_context(_locked_file_path(root / f'slot-{index}', blocking=False))
                        except BlockingIOError as exc:
                            raise RuntimeError('GATEWAY_LLM_CAPACITY_BUSY: 旧模型运输仍占槽，未改变容量；先停止并排空旧执行后重启') from exc
                    for index in range(old['slots'], slots):
                        with _locked_file_path(root / f'slot-{index}', blocking=False):
                            pass
                    write_private_json_file_atomic_unlocked(root / 'capacity.json',
                        {'schema': _SCHEMA, 'limit': limit, 'slots': slots})
            if limit:
                os.environ[_BINDING_ENV] = str(root)
        yield
    finally:
        os.environ.pop(_BINDING_ENV, None)
        if previous is not None:
            os.environ[_BINDING_ENV] = previous


def configured() -> bool:
    """绑定只定位资源，不携带owner、授权或业务身份。"""
    return bool(os.environ.get(_BINDING_ENV))


class GatewayTransportLease:
    def __init__(self, slot):
        self._slot = slot
        self._guard = threading.Lock()

    def release(self):
        with self._guard:
            slot, self._slot = self._slot, None
        if slot is not None:
            slot.__exit__(None, None, None)

    def __del__(self):
        self.release()


def _capacity_signature(root):
    info = (root / 'capacity.json').stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('GATEWAY_LLM_CAPACITY_INVALID: capacity.json必须是普通文件')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _capacity_snapshot(root):
    global _SNAPSHOT
    # 配置解析在OS配置锁外；缓存只是提示，取得配置锁后还要核同一文件身份。
    for _ in range(3):
        signature = _capacity_signature(root)
        with _SNAPSHOT_GUARD:
            cached = _SNAPSHOT
        if cached is not None and cached[0] == root and cached[1] == signature:
            return cached[1:]
        row = _record(root)
        if _capacity_signature(root) == signature:
            with _SNAPSHOT_GUARD:
                _SNAPSHOT = root, signature, row
            return signature, row
    raise BlockingIOError('Gateway模型容量配置正在切换，未申请运输')


@dataclass
class _ProbeCursor:
    next_index: int | None = None

    def indices(self, limit):
        global _next_start
        if self.next_index is None:
            # 各进程起点不同，同进程新等待者也轮转；已有等待者独立推进，不被新人重置。
            with _START_GUARD:
                self.next_index = (os.getpid() + _next_start) % limit
                _next_start = (_next_start + _ROUND_PROBE_COUNT) % _MAX_TRANSPORT_SLOT_COUNT
        start = self.next_index % limit
        size = min(_ROUND_PROBE_COUNT, limit)
        self.next_index = (start + size) % limit
        return ((start + offset) % limit for offset in range(size))


def _try_slot(root: Path, cursor):
    signature, row = _capacity_snapshot(root)
    # 每轮至多4个OS槽操作；配置锁内不解析文件、不随capacity全扫。
    with locked_json_path(root / 'capacity.json', blocking=False):
        if _capacity_signature(root) != signature:
            raise BlockingIOError('Gateway模型容量已切换，重新读取正式配置')
        if row['limit'] == 0:
            return False
        for index in cursor.indices(row['limit']):
            slot = _locked_file_path(root / f'slot-{index}', blocking=False)
            try:
                slot.__enter__()
            except BlockingIOError:
                continue
            return GatewayTransportLease(slot)
    return None


def acquire(deadline: float, notify=None):
    if not configured():
        return None
    from ..backends.errors import ProviderQueueTimeoutError
    from ..concurrency.interrupt import is_interrupted, wait_interruptibly
    root = Path(os.environ[_BINDING_ENV])
    started = time.monotonic()
    announced = False
    cursor = _ProbeCursor()
    jitter = random.Random(os.getpid() ^ time.monotonic_ns())
    delay = .005
    while True:
        if is_interrupted():
            raise InterruptedError('Gateway模型运输排队被停止；尚未发送，不重放此前工具')
        if time.monotonic() >= deadline:
            raise ProviderQueueTimeoutError('GATEWAY_LLM_CAPACITY_WAIT_TIMEOUT: Gateway模型运输槽已满；本次尚未发送，请稍后继续，禁止重放已完成工具')
        try:
            lease = _try_slot(root, cursor)
        except BlockingIOError:
            lease = None
        if lease is False:
            return None
        if lease is not None:
            if is_interrupted():
                lease.release()
                raise InterruptedError('Gateway模型运输获槽前被停止；尚未发送')
            return lease
        if not announced and time.monotonic() - started >= 2 and callable(notify):
            notify({'phase': 'waiting', 'admission_wait_reason': 'gateway_llm_capacity',
                    'wait_seconds': round(time.monotonic() - started, 3)})
            announced = True
        # 有界退避加抖动，避免进程树同时每50ms争同一配置锁；低cap最多60ms再看释放。
        wait_interruptibly(min(delay * jitter.uniform(.8, 1.2), max(0, deadline - time.monotonic())))
        delay = min(.05, delay * 2)


def counts() -> dict:
    """只读固定槽投影，不创建历史任务账；短竞争保守记作占用。"""
    if not configured():
        return {'enabled': False, 'limit': 0, 'active': 0}
    root = Path(os.environ[_BINDING_ENV])
    with locked_json_path(root / 'capacity.json'):
        row = _record(root)
        active = 0
        for index in range(row['limit']):
            try:
                with _locked_file_path(root / f'slot-{index}', blocking=False):
                    pass
            except BlockingIOError:
                active += 1
        return {'enabled': row['limit'] > 0, 'limit': row['limit'], 'active': active}
