"""实际worker的有界stdio采集：稳定原inode、持续排空、逐字节记录保留与丢弃。"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from contextvars import Context
from pathlib import Path

from ..common.json_io import write_private_json_file_atomic_unlocked
from ..common.nofollow_fs import read_bytes_beneath
from ..tooling.process_output_capture import ProcessOutputCapture
from ..tooling.process_pipe_pump import ProcessPipePump

BOUNDED_STDIO_LIMIT_ENV = 'MY_AGENT_BOUNDED_STDIO_MAX_BYTES'
BOUNDED_STDIO_PATH_ENV = 'MY_AGENT_BOUNDED_STDIO_PATH'


def capture_path(path):
    return Path(path).with_suffix('.capture.json')


def read_capture_report(path, identity):
    """原inode与完整数值账同时核验；缺失只报告缺失，不合成完整采集证明。"""
    target = capture_path(path)
    raw = read_bytes_beneath(target.parent, (target.name,), max_bytes=8192, require_dir_fd=True)
    if raw is None:
        return None
    row = json.loads(raw)
    if (type(row) is not dict or row.get('schema') != 'bounded_process_log.v1'
            or row.get('identity') != list(identity) or type(row.get('eof')) is not bool
            or type(row.get('error_types')) is not list or len(row['error_types']) > 8
            or any(type(value) is not str for value in row['error_types'])
            or any(type(row.get(key)) is not int or row[key] < 0
                   for key in ('max_bytes', 'seen_bytes', 'retained_bytes', 'discarded_bytes'))
            or row['max_bytes'] <= 0 or row['retained_bytes'] > row['max_bytes']
            or row['seen_bytes'] != row['retained_bytes'] + row['discarded_bytes']):
        raise ValueError('bounded_process_capture_unverified')
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        info = None
    if info is not None and ((info.st_dev, info.st_ino) != tuple(identity) or info.st_size != row['retained_bytes']):
        raise ValueError('bounded_process_capture_source_changed')
    return row, raw


class _BoundedLogCapture:
    def __init__(self, path, descriptor, limit):
        self.path, self.descriptor, self.limit = path, descriptor, limit
        info = os.fstat(descriptor)
        self.identity = (info.st_dev, info.st_ino)
        self.seen, self.retained = info.st_size, min(info.st_size, limit)
        self.eof, self.errors = set(), {}
        self._next_publish = 0.0
        self._stop = threading.Event()
        # 父启动口给的是非APPEND、唯一launch的新FD；原文件不换inode，初始化前已有输出也纳入计量。
        os.ftruncate(descriptor, self.retained)
        os.lseek(descriptor, self.retained, os.SEEK_SET)
        self._publish(force=True)

    def stopping(self):
        return False  # 只由真实EOF/读错结束，不因达到容量停止排空或给子进程制造SIGPIPE。

    def _publish(self, *, force=False):
        now = time.monotonic()
        if not force and now < self._next_publish:
            return
        # 即使元数据写失败也不按输出块重试fsync；首写、首次新错误和最终EOF强制发表。
        self._next_publish = now + 1.0
        try:
            info = self.path.lstat()
            if ((info.st_dev, info.st_ino) != self.identity or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size != self.retained):
                self.errors.setdefault('path', 'SourceChanged')
                return
            write_private_json_file_atomic_unlocked(capture_path(self.path), {
                'schema': 'bounded_process_log.v1', 'identity': list(self.identity), 'max_bytes': self.limit,
                'seen_bytes': self.seen, 'retained_bytes': self.retained,
                'discarded_bytes': self.seen - self.retained,
                'eof': len(self.eof) == 2, 'error_types': list(dict.fromkeys(self.errors.values()))[:8],
            })
        except (OSError, ValueError) as error:
            self.errors.setdefault('metadata', type(error).__name__)

    def record_data(self, name, data):
        self.seen += len(data)
        remaining = data[:max(0, self.limit - self.retained)]
        try:
            while remaining:
                written = os.write(self.descriptor, remaining)
                if written <= 0:
                    raise OSError('bounded log write made no progress')
                self.retained += written
                remaining = remaining[written:]
        except OSError as error:
            if 'sink' not in self.errors:
                self.errors['sink'] = type(error).__name__
                self._publish(force=True)
                return
        self._publish()

    def record_eof(self, name):
        self.eof.add(name)

    def record_error(self, name, error):
        if name not in self.errors:
            self.errors[name] = type(error).__name__
            self._publish(force=True)

    def record_ended(self, name):
        pass

    def close(self):
        try:
            self._publish(force=True)
        finally:
            os.close(self.descriptor)  # 原stdout租约持至真实输出EOF及末次计量发表。


def _flush_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except (OSError, ValueError):
            pass


def _redirect_stdio(descriptor):
    """CRT和原生标准句柄保持一致；Windows后代的默认继承不能绕回原无限日志。"""
    os.dup2(descriptor, 1)
    os.dup2(descriptor, 2)
    if os.name == 'nt':
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.SetStdHandle.argtypes = (wintypes.DWORD, wintypes.HANDLE)
        kernel.SetStdHandle.restype = wintypes.BOOL
        kernel.GetStdHandle.argtypes = (wintypes.DWORD,)
        kernel.GetStdHandle.restype = wintypes.HANDLE
        for number, code in ((1, -11), (2, -12)):
            msvcrt.setmode(number, os.O_BINARY)
            handle = msvcrt.get_osfhandle(number)
            os.set_handle_inheritable(handle, True)
            if not kernel.SetStdHandle(code & 0xffffffff, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            if kernel.GetStdHandle(code & 0xffffffff) != handle:
                raise OSError('Windows standard output handle was not redirected')


@contextmanager
def bounded_stdio_from_environment():
    """仅实际进程退出边界使用；结束时关本进程PIPE写端，继承输出仍由本worker采集至EOF。

    无配置、0、不匹配的TTY/管道均沿原stdio；实际worker之外不启动线程。
    """
    try:
        limit = int(os.environ.get(BOUNDED_STDIO_LIMIT_ENV, '0'))
        path = Path(os.environ.get(BOUNDED_STDIO_PATH_ENV, ''))
    except (ValueError, TypeError):
        limit, path = 0, None
    if limit <= 0 or path is None or not path.is_absolute():
        yield
        return
    try:
        expected = path.lstat()
        matched = (stat.S_ISREG(expected.st_mode) and expected.st_nlink == 1
                   and all(os.path.samestat(expected, os.fstat(fd)) for fd in (1, 2)))
    except OSError:
        matched = False
    if not matched:
        yield
        return
    _flush_stdio()
    descriptor = os.dup(1)
    reader = writer = null = None
    capture = pump = thread = stream = None
    redirected = False
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.setmode(descriptor, os.O_BINARY)
        capture = _BoundedLogCapture(path, descriptor, limit)
        descriptor = None  # capture接管，线程finally关闭；启动失败由本作用域关闭。
        null = os.open(os.devnull, os.O_WRONLY)
        reader, writer = os.pipe()
        stream = os.fdopen(reader, 'rb', buffering=0)
        reader = None
        if os.name == 'posix':
            pump = ProcessPipePump(capture, {'stdout': stream, 'stderr': None})
        if pump is None:
            capture.record_eof('stderr')
        def collect():
            try:
                if pump is not None:
                    pump.run()
                else:
                    # Windows沿已有阻塞reader；两流已合一，不在管道上注册POSIX selector。
                    ProcessOutputCapture._read(capture, 'stdout', stream, blocking=True)
            finally:
                capture.close()
        # 每个真实durable worker一条，非daemon保证主逻辑结束后继承stdout仍有原reader；不挂在Gateway父进程。
        thread = threading.Thread(target=Context().run, args=(collect,), name='bounded-worker-output', daemon=False)
        thread.start()
        redirected = True  # 任一步失败也须关闭部分重定向的PIPE写端。
        _redirect_stdio(writer)
        os.close(writer)
        writer = None
        try:
            yield
        except BaseException as error:
            if not isinstance(error, SystemExit):
                traceback.print_exc(file=sys.stderr)
            elif isinstance(error.code, str):
                print(error.code, file=sys.stderr)
            raise
    finally:
        original_error = sys.exception()
        cleanup_error = None
        if redirected:
            try:
                _flush_stdio()
                _redirect_stdio(null)
            except BaseException as error:
                cleanup_error = error
                # 原生句柄同步失败也先关闭本进程CRT写端，让reader完成EOF并回收。
                for number in (1, 2):
                    try:
                        os.close(number)
                    except OSError:
                        pass
        if null is not None:
            os.close(null)
        if writer is not None:
            os.close(writer)
        if reader is not None:
            os.close(reader)
        if thread is not None and thread.ident is not None:
            thread.join()  # 等真实输出EOF；不猜继承输出者退出，不提前关reader。
        elif capture is not None:
            if pump is not None:
                pump._close_streams()
                pump.close()
            elif stream is not None:
                stream.close()
            capture.close()
        elif descriptor is not None:
            os.close(descriptor)
        if cleanup_error is not None:
            if original_error is None:
                raise cleanup_error
            original_error.bounded_stdio_cleanup_error = cleanup_error
