"""只读分片核对当前 claim 目录；未完成或变化中的扫描不能签发空闲事实。"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict

from ..io.bounded_directory import DirectoryCursor
from .store_claims import _read_claim_report

_LOCK = threading.RLock()
_SCANS = OrderedDict()


def _signature(path):
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns
    except FileNotFoundError:
        return None


class _Scan:
    def __init__(self, signature):
        self.signature = signature
        self.cursor = DirectoryCursor()
        self.busy = set()
        self.unknown = self.complete = False
        self.checked_at = time.monotonic()

    def close(self):
        self.cursor.close()


def claim_activity_state(storage, thread_id):
    """每拍最多128项/25ms，不依赖可被历史清理移除的 TaskLink 定位孤立租约。

    原 acquire/renew/finish 均原子换叶，因此目录版本能废弃已核对结果。
    仅保留活跃线程集合；缓存至多4个store，静止结果5秒后重新分片核对。
    """
    directory = storage.background_claims_dir
    try:
        with _LOCK:
            signature = _signature(directory)
            key = str(directory.absolute())
            scan = _SCANS.pop(key, None)
            if scan is not None and (scan.signature != signature or
                    scan.complete and time.monotonic() - scan.checked_at >= 5):
                scan.close()
                scan = None
            scan = scan or _Scan(signature)
            _SCANS[key] = scan
            while len(_SCANS) > 4:
                _SCANS.popitem(last=False)[1].close()
            deadline = time.monotonic() + .025
            if not scan.complete:
                for _ in range(128):
                    entry = scan.cursor.next_entry(directory)
                    if entry is None:
                        scan.complete = True
                        scan.checked_at = time.monotonic()
                        break
                    if entry.name.endswith('.json'):
                        if not entry.is_file(follow_symlinks=False):
                            scan.unknown = True
                        else:
                            claim, error = _read_claim_report(directory / entry.name)
                            if error or claim.get('status') not in {'running', 'claimed', 'finished', 'failed', 'cancelled'}:
                                scan.unknown = True
                            elif claim['status'] in {'running', 'claimed'}:
                                identity = claim.get('thread_id')
                                if isinstance(identity, str) and identity:
                                    scan.busy.add(identity)
                                else:
                                    scan.unknown = True
                    if time.monotonic() >= deadline:
                        break
            if _signature(directory) != signature:
                scan.close()
                _SCANS.pop(key, None)
                return 'activity_unknown'
            if thread_id in scan.busy:
                return 'foreground_active'
            if not scan.complete or scan.unknown:
                return 'activity_unknown'
            return ''
    except (OSError, ValueError, TypeError):
        return 'activity_unknown'
