"""FD断言只在已确认线程/进程退出后使用；仅隔离本owner的可重建扫描缓存。"""

from __future__ import annotations

import gc
import os
import stat
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FdSnapshot:
    count: int
    targets: dict


def close_owner_quota_scan(owner_home: Path) -> None:
    from agent_py_agent.agent.user_space import owner_quota as quota

    root = Path(owner_home).resolve()
    # 同原扫描器一样先owner锁再cache锁；只取本PID、本owner，关闭在cache锁外。
    with quota._locked_owner_quota(root / quota._QUOTA_LOCK_BASENAME):
        with quota._locked_quota_scan_cache(blocking=True):
            scan = quota._QUOTA_SCANS.pop((os.getpid(), str(root)), None)
        if scan is not None:
            scan["iterator"].close()


def capture_fd_snapshot() -> FdSnapshot:
    root = Path("/proc/self/fd") if Path("/proc/self/fd").is_dir() else Path("/dev/fd")
    names = os.listdir(root)
    targets = {}
    for name in names:
        try:
            info = os.fstat(int(name))
            try:
                target = os.readlink(root / name)
            except OSError:
                target = ""
                if sys.platform == "darwin" and (
                    stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
                ):
                    import fcntl

                    try:
                        target = (
                            fcntl.fcntl(int(name), 50, b"\0" * 1024)
                            .split(b"\0", 1)[0]
                            .decode("utf-8", "replace")
                        )
                    except OSError:
                        pass
            targets[name] = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), target)
        except OSError:
            # listdir自己的临时目录FD已退出，或并发回收；不将它伪造成存活资源。
            pass
    return FdSnapshot(len(names), targets)


def fd_baseline(owner_home: Path | None = None) -> FdSnapshot:
    # 只在基线前归还前case循环垃圾；终点绝不靠GC掩盖本case漏关。
    gc.collect()
    if owner_home is not None:
        close_owner_quota_scan(owner_home)
    return capture_fd_snapshot()


def assert_fd_unchanged(before: FdSnapshot, owner_home: Path | None = None) -> None:
    if owner_home is not None:
        close_owner_quota_scan(owner_home)
    after = capture_fd_snapshot()
    added = {key: value for key, value in after.targets.items() if before.targets.get(key) != value}
    removed = {
        key: value for key, value in before.targets.items() if after.targets.get(key) != value
    }
    new_resources = Counter(after.targets.values()) - Counter(before.targets.values())
    assert after.count - before.count == 0 and not new_resources, {
        "delta": after.count - before.count,
        "added": added,
        "removed": removed,
        "new_resources": dict(new_resources),
    }
