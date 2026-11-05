"""原生daemon的诊断日志轮转；不在打印热路径加锁或扫描。

每个写进程定时检查活动路径，在永久flock侧锁内封卷、重开FD 1/2并退休旧卷。
单卷可超出阈值一个检查间隔内的实际写入量；封卷至少保留两个间隔供其它写者重开。
Windows不安装线程。这里不处理正式审计、用户资产或其它日志名。
"""
from __future__ import annotations

import logging
import os
import re
import stat
import sys
import threading
import time
from pathlib import Path

# 原生 daemon 日志单卷封口前的最大字节数。
DAEMON_LOG_VOLUME_MAX_BYTES = 16 * 1024 * 1024
# 每个服务日志根允许保留的总字节数。
DAEMON_LOG_TOTAL_MAX_BYTES = 256 * 1024 * 1024
# 已封口 daemon 日志卷的最长保留天数。
DAEMON_LOG_RETENTION_DAYS = 30
# daemon 检查日志卷长度与换代的间隔秒数。
DAEMON_LOG_CHECK_INTERVAL_SECONDS = 30.0

_LOGGER = logging.getLogger(__name__)
_INSTALL_PID = os.getpid()
_INSTALL_LOCK = threading.Lock()
_INSTALL_THREAD: threading.Thread | None = None


# LLM: 仅实际stdout与指定日志同inode才安装；fork后重建本进程守卫，终端/管道不重定向；无导入期I/O。
# 函数用途: 为真实daemon安装一个定期轮转线程，重复调用不叠加线程。
def install_daemon_log_rotation(log_path: str | None) -> bool:
    global _INSTALL_PID, _INSTALL_LOCK, _INSTALL_THREAD
    if os.name == 'nt' or not log_path:
        return False
    try:
        if _identity(os.fstat(1)) != _identity(os.stat(log_path)):
            return False
    except OSError:
        return False
    pid = os.getpid()
    if pid != _INSTALL_PID:
        # fork只留下调用者线程，不能沿用父进程可能被其它线程持有的安装锁。
        _INSTALL_PID, _INSTALL_LOCK, _INSTALL_THREAD = pid, threading.Lock(), None
    with _INSTALL_LOCK:
        if _INSTALL_THREAD is not None and _INSTALL_THREAD.is_alive():
            return True
        thread = threading.Thread(target=_rotation_loop, args=(Path(log_path),),
                                  name='daemon-log-rotation', daemon=True)
        thread.start()
        _INSTALL_THREAD = thread
    return True


# LLM: 诊断设施失败不终止业务；每小时至多一条告警，原异常不被宣称已成功处理。
# 函数用途: 在后台周期性尝试轮转与重开，不给每次stdout写入增加工作。
def _rotation_loop(log_path):
    next_warning = 0.0
    while True:
        try:
            rotate_log_volumes_once(log_path, now=time.time())
        except Exception:
            if time.monotonic() >= next_warning:
                next_warning = time.monotonic() + 3600
                _LOGGER.warning('daemon日志轮转失败，保留原日志并等待下一拍', exc_info=True)
        time.sleep(DAEMON_LOG_CHECK_INTERVAL_SECONDS)


# LLM: 仅比较内核文件身份，不从路径字符串或长度推测重开是否完成。
# 函数用途: 提取文件的设备及inode二元组。
def _identity(info):
    return info.st_dev, info.st_ino


# LLM: 任一流落后或FD已无效均需恢复；尤其不能把第一次dup2成功后的部分提交误认作两条流都已重开。
# 函数用途: 核对本进程stdout和stderr是否都指向当前活动日志。
def _stdio_matches(info):
    try:
        return all(_identity(os.fstat(descriptor)) == _identity(info) for descriptor in (1, 2))
    except OSError:
        return False


# LLM: 只在永久日志锁内调用；两条流先flush，再替换进程FD；不修改Python流对象或其它FD。
# 函数用途: 把本进程stdout/stderr接到当前追加卷，崩溃后路径缺失时新建。
def _reopen_stdio(log_path):
    sys.stdout.flush()
    sys.stderr.flush()
    descriptor = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        os.dup2(descriptor, 1)
        os.dup2(descriptor, 2)
    finally:
        if descriptor not in (1, 2):
            os.close(descriptor)


# LLM: 名字精确匹配时间戳封卷，lstat排除链接及非普通文件；活动卷、其它日志与永久锁均不退休。
# 函数用途: 删除已过重开宽限的最旧封卷并返回真实计量。
def _prune_sealed(log_path, now):
    pattern = re.compile(re.escape(log_path.name) + r'\.(\d{19})$')
    volumes = []
    for path in log_path.parent.iterdir():
        match = pattern.fullmatch(path.name)
        if match is None:
            continue
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            volumes.append((int(match[1]), path, info.st_size))
    total = log_path.stat().st_size + sum(size for _, _, size in volumes)
    deleted = freed = 0
    for stamp, path, size in sorted(volumes):
        age = now - stamp / 1e9
        if age < 2 * DAEMON_LOG_CHECK_INTERVAL_SECONDS:
            continue
        if total <= DAEMON_LOG_TOTAL_MAX_BYTES and age <= DAEMON_LOG_RETENTION_DAYS * 86400:
            continue
        path.unlink()
        deleted += 1
        freed += size
        total -= size
    return deleted, freed, total


# LLM: .rotate.lock永不删除，非阻塞flock后重核锁inode；各写者比较自身FD并重开，不只rename假装完成。
# 函数用途: 执行一拍daemon封卷、重开与退休，返回真实删除字节；锁忙零副作用留待下一拍。
def rotate_log_volumes_once(log_path: Path, *, now: float) -> dict:
    report = {'rotated': False, 'reopened': False, 'deleted': 0, 'freed_bytes': 0, 'total_bytes': 0}
    if os.name == 'nt':
        return report
    import fcntl

    log_path = Path(log_path)
    lock_path = Path(str(log_path) + '.rotate.lock')
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return report
        acquired = True
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or _identity(info) != _identity(lock_path.lstat())):
            raise OSError('daemon rotation lock identity changed')
        os.fchmod(descriptor, 0o600)
        try:
            active = log_path.stat()
        except FileNotFoundError:
            active = None
        if active is not None and active.st_size >= DAEMON_LOG_VOLUME_MAX_BYTES:
            sys.stdout.flush()
            sys.stderr.flush()
            stamp = time.time_ns()
            while Path(f'{log_path}.{stamp:019d}').exists():
                stamp += 1
            os.rename(log_path, f'{log_path}.{stamp:019d}')
            report['rotated'] = True
            active = None
        if active is None or not _stdio_matches(active):
            _reopen_stdio(log_path)
            report['reopened'] = True
        report['deleted'], report['freed_bytes'], report['total_bytes'] = _prune_sealed(log_path, now)
        return report
    finally:
        try:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
