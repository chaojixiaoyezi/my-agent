"""隔离 Watchman 变更流；只有已证明的 inotify cookie 顺序可授予增量覆盖。"""
from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_QUERY_BYTES = 16 * 1024 * 1024
_SYNC_MS = 5000
_CONFIG = b'{"ignore_dirs":[],"ignore_vcs":[]}\n'


def _configuration_matches(path):
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != len(_CONFIG):
            return False
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as source:
            opened = os.fstat(source.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_size) != (info.st_dev, info.st_ino, info.st_size):
                return False
            return source.read(len(_CONFIG) + 1) == _CONFIG
    except OSError:
        return False


@dataclass(frozen=True)
class OwnerChangeBatch:
    clock: str
    paths: tuple[dict, ...]
    complete: bool
    reason: str


class OwnerChangeJournal:
    """一个 worker 一条私有 daemon；每个 warm slot 只有一条可独立撤销的 watch。"""

    def __init__(self):
        self._process = None
        self._directory = None
        self._roots = {}
        self._lock = threading.RLock()
        atexit.register(self.close)

    @staticmethod
    def require_available():
        if sys.platform == 'linux':
            command = shutil.which('watchman')
            if command is None:
                raise RuntimeError('规模 owner 快照需要 Watchman，请在 Linux worker 镜像安装 watchman')
            try:
                subprocess.run([command, '--version'], check=True, timeout=10, capture_output=True)
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError('规模 owner Watchman 启动检查失败，请检查发行版本及运行库') from exc

    def _start(self):
        if self._process is not None and self._process.poll() is None:
            return
        self.close()
        command = shutil.which('watchman')
        if command is None:
            raise OSError('watchman unavailable')
        self._directory = Path(tempfile.mkdtemp(prefix='my-agent-observer-'))
        os.chmod(self._directory, 0o700)
        self._socket = self._directory / 'sock'
        args = [command, '--foreground', '--no-save-state', '--sockname', str(self._socket),
                '--statefile', str(self._directory / 'state'), '--logfile', str(self._directory / 'log'),
                '--pidfile', str(self._directory / 'pid'), '--log-level=0']
        launcher = [sys.executable, str(Path(__file__).absolute()), str(os.getpid()), *args]
        self._process = subprocess.Popen(launcher, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise OSError('private watchman exited during startup')
            try:
                self._request(['version', {'required': ['clock-sync-timeout', 'relative_root']}])
                return
            except OSError:
                time.sleep(0.02)
        raise OSError('private watchman startup timed out')

    def _request(self, command):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(10)
            channel.connect(str(self._socket))
            channel.sendall(json.dumps(command).encode() + b'\n')
            with channel.makefile('rb') as response:
                raw = response.readline(_QUERY_BYTES + 1)
        if len(raw) > _QUERY_BYTES or not raw.endswith(b'\n'):
            raise OSError('observer response exceeds byte budget')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise OSError('observer response is unreadable') from exc
        if type(result) is not dict or 'error' in result:
            raise OSError('observer command failed')
        return result

    def start(self):
        if sys.platform == 'linux':
            self.require_available()
            with self._lock:
                self._start()

    def query(self, root: Path, since: str = '', *, control_root: Path | None = None) -> OwnerChangeBatch:
        # Apple 官方不保证 FSEvents+FlushSync 的先前事件覆盖；不拿 settle/debounce 代替证明。
        if sys.platform != 'linux':
            return OwnerChangeBatch('', (), False, 'observer_ordering_unproven')
        root = root.absolute()
        control = control_root.absolute() if control_root is not None else root
        if not root.is_relative_to(control):
            return OwnerChangeBatch('', (), False, 'observer_scope_conflict')
        with self._lock:
            try:
                self._start()
                config = control / '.watchmanconfig'
                if root not in self._roots:
                    config.parent.mkdir(parents=True, exist_ok=True)
                    if config.exists() and not _configuration_matches(config):
                        return OwnerChangeBatch('', (), False, 'observer_configuration_conflict')
                    if not config.exists():
                        descriptor = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        with os.fdopen(descriptor, 'wb') as handle:
                            handle.write(_CONFIG)
                    self._roots[root] = (control, None)
                    watched = self._request(['watch', str(control)])
                    self._roots[root] = (control, watched.get('watcher'))
                elif self._roots[root][0] != control:
                    return OwnerChangeBatch('', (), False, 'observer_scope_conflict')
                elif not _configuration_matches(config):
                    self.release(root)
                    return OwnerChangeBatch('', (), False, 'observer_configuration_changed')
                if self._roots[root][1] != 'inotify':
                    return OwnerChangeBatch('', (), False, 'observer_ordering_unproven')
                query = {'fields': ['name', 'exists', 'type', 'new'], 'sync_timeout': _SYNC_MS,
                         'empty_on_fresh_instance': True}
                if since:
                    query['since'] = since
                if root != control:
                    query['relative_root'] = root.relative_to(control).as_posix()
                result = self._request(['query', str(control), query])
                clock, files = result.get('clock'), result.get('files')
                fresh = result.get('is_fresh_instance')
                if (type(clock) is not str or not clock.startswith('c:') or type(files) is not list
                        or type(fresh) is not bool):
                    return OwnerChangeBatch('', (), False, 'observer_invalid_coverage')
                for item in files:
                    if (type(item) is not dict or type(item.get('name')) is not str
                            or type(item.get('exists')) is not bool):
                        return OwnerChangeBatch('', (), False, 'observer_invalid_path')
                    name = PurePosixPath(item['name'])
                    if name.is_absolute() or any(part in {'..', '.'} for part in name.parts) or '\x00' in item['name']:
                        return OwnerChangeBatch('', (), False, 'observer_invalid_path')
                if result.get('warning') or result.get('canceled'):
                    self.release(root)
                    return OwnerChangeBatch(clock, (), False, 'observer_recrawl_or_canceled')
                return OwnerChangeBatch(clock, tuple(files), bool(since) and not fresh,
                                        'observer_fresh_instance' if not since or fresh else '')
            except (OSError, ValueError, KeyError):
                self.release(root)
                return OwnerChangeBatch('', (), False, 'observer_unavailable')

    def release(self, root: Path):
        with self._lock:
            root = root.absolute()
            scope = self._roots.pop(root, None)
            if scope is not None and not any(other[0] == scope[0] for other in self._roots.values()):
                try:
                    self._request(['watch-del', str(scope[0])])
                except OSError:
                    pass

    def close(self):
        with self._lock:
            process = self._process
            self._process = None
            self._roots.clear()
            if process is not None and process.poll() is None:
                try:
                    self._request(['shutdown-server'])
                    process.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
            if self._directory is not None:
                shutil.rmtree(self._directory, ignore_errors=True)
                self._directory = None


def _exec_bound_child(parent, command):
    # 独立单线程解释器里设父死亡信号；不在多线程宿主使用 preexec_fn。
    import ctypes
    import signal

    library = ctypes.CDLL(None, use_errno=True)
    if library.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        raise SystemExit(126)
    os.execv(command[0], command)


if __name__ == '__main__':
    if sys.platform != 'linux' or len(sys.argv) < 3:
        raise SystemExit(2)
    _exec_bound_child(int(sys.argv[1]), sys.argv[2:])
