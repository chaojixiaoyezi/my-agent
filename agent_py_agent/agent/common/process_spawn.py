"""不 fork 网关大进程的子进程派生（S50-14，C1.5，R3 §5.10；Layer-2 M00-fix F2/F3/F4）。

macOS 上 subprocess.Popen 只要带 cwd、pass_fds、start_new_session 或 process_group 就走 fork；多 GB 的网关进程
每次 fork 都要复制页表，在忙线程下停顿数十毫秒（R3 §2.11）。SpawnPopen 在 Darwin 上经 ctypes 直接调用 libc 的
posix_spawn 启动目标程序本身，不经过任何中间进程：
- fd：POSIX_SPAWN_CLOEXEC_DEFAULT 让子进程只保留文件动作点名的 fd（管道 dup2 到的 0/1/2、未重定向且可继承的
  0/1/2、pass_fds），其它线程在任何时刻新开的可继承 fd 都到不了子进程（F2）；
- env：按 Popen 的规则编码为 envp，与 argv 分开交给内核；env=None 时传 libc 当前的 environ，os.putenv 的修改
  照样继承（F3）；
- exec 结果：posix_spawn 返回前内核已完成 exec，失败时同步返回 errno，父进程按 Popen 的类型、errno、filename
  抛出，也不留下需要回收的失败子进程（F4）。
Linux 上 CPython 的 Popen 已走 vfork（不复制页表），直接调用 subprocess.Popen._execute_child。Darwin 上 libc 符号
不可用、用到 preexec_fn/uid/gid/gids/umask/close_fds=False/process_group>0 等契约外选项，或参数、cwd、pass_fds 不
满足前提时同样原样调用它，这些情形的异常与 Popen 完全一致。
"""

from __future__ import annotations

import errno
import os
import signal
import stat
import subprocess
import sys

# macOS <sys/syslimits.h> 的 OPEN_MAX：libc 的 posix_spawn 文件动作拒绝不小于它的 fd 号（与 RLIMIT_NOFILE 无关），用到时改走原 Popen。
_DARWIN_OPEN_MAX_FILES = 10240
# 查找候选可执行文件时跳过、继续试下一个的 errno（与 _posixsubprocess 的 exec 循环相同）。
_NOT_FOUND_ERRNOS = frozenset((errno.ENOENT, errno.ENOTDIR))


# 类用途: Darwin <spawn.h> 的 posix_spawnattr 标志位。
class _DarwinSpawnFlags:
    # 按 posix_spawnattr_setpgroup 设置子进程的进程组。
    SETPGROUP = 0x0002
    # 按 posix_spawnattr_setsigdefault 把信号恢复为默认处置。
    SETSIGDEF = 0x0004
    # 子进程调用 setsid 新建会话。
    SETSID = 0x0400
    # 只保留文件动作点名的 fd，其余在 exec 时全部关闭。
    CLOEXEC_DEFAULT = 0x4000


# LLM: 只在 Darwin 绑定 libc 的 posix_spawn 系列符号（addinherit_np 自 10.7、addchdir_np 自 10.15 提供）。ctypes
#   不可导入、库或符号缺失、或加载被审计钩子拒绝（PermissionError 属于 OSError）时返回 None，SpawnPopen 一律走
#   原 Popen。返回 (ctypes 模块, 名字 -> 已设好 argtypes 的 libc 函数, _NSGetEnviron)。
# 函数用途: 加载 Darwin libc 的 posix_spawn 绑定。
def _load_darwin_spawn():
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        handle, sigset = ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32)
        strings = ctypes.POINTER(ctypes.c_char_p)
        signatures = {
            "posix_spawn": (ctypes.POINTER(ctypes.c_int), ctypes.c_char_p, handle, handle, strings, strings),
            "posix_spawnattr_init": (handle,),
            "posix_spawnattr_destroy": (handle,),
            "posix_spawnattr_setflags": (handle, ctypes.c_short),
            "posix_spawnattr_setpgroup": (handle, ctypes.c_int),
            "posix_spawnattr_setsigdefault": (handle, sigset),
            "posix_spawn_file_actions_init": (handle,),
            "posix_spawn_file_actions_destroy": (handle,),
            "posix_spawn_file_actions_adddup2": (handle, ctypes.c_int, ctypes.c_int),
            "posix_spawn_file_actions_addinherit_np": (handle, ctypes.c_int),
            "posix_spawn_file_actions_addchdir_np": (handle, ctypes.c_char_p),
            "sigemptyset": (sigset,),
            "sigaddset": (sigset, ctypes.c_int),
        }
        calls = {}
        for name, argtypes in signatures.items():
            function = getattr(libc, name)
            function.argtypes, function.restype = argtypes, ctypes.c_int
            calls[name] = function
        environ = libc._NSGetEnviron
        environ.argtypes, environ.restype = (), ctypes.POINTER(strings)
    except (ImportError, OSError, AttributeError):
        return None
    return ctypes, calls, environ


_DARWIN_SPAWN = _load_darwin_spawn()


# 函数用途: libc 返回非零错误码时抛出对应 OSError。
def _check(code: int) -> None:
    if code:
        raise OSError(code, os.strerror(code))


# LLM: 一次 libc posix_spawn，设 POSIX_SPAWN_CLOEXEC_DEFAULT：子进程只保留文件动作点名的 fd——dup2s 把管道接到
#   0/1/2，inherits 保留指定 fd 并清除其 close-on-exec，chdir 在 exec 前切换目录（相对的 path 按新目录解析）。
#   env 为 None 时传 libc 当前 environ，否则传给定的 NAME=VALUE 列表。失败抛 OSError(errno, strerror, path)；
#   属性与文件动作对象在 finally 里销毁。测试替换本函数观察每次派生。
# 函数用途: 直接派生目标程序并返回 pid。
def _posix_spawn(path: bytes, argv: list[bytes], env: list[bytes] | None, *, dup2s, inherits, chdir, setsid,
                 setpgroup, setsigdef) -> int:
    ctypes, calls, environ = _DARWIN_SPAWN
    attr, actions, pid = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_int()
    _check(calls["posix_spawnattr_init"](ctypes.byref(attr)))
    try:
        _check(calls["posix_spawn_file_actions_init"](ctypes.byref(actions)))
        try:
            flags = _DarwinSpawnFlags.CLOEXEC_DEFAULT
            if setsigdef:
                signals = ctypes.c_uint32()
                calls["sigemptyset"](ctypes.byref(signals))
                for signum in setsigdef:
                    calls["sigaddset"](ctypes.byref(signals), signum)
                _check(calls["posix_spawnattr_setsigdefault"](ctypes.byref(attr), ctypes.byref(signals)))
                flags |= _DarwinSpawnFlags.SETSIGDEF
            if setsid:
                flags |= _DarwinSpawnFlags.SETSID
            if setpgroup is not None:
                _check(calls["posix_spawnattr_setpgroup"](ctypes.byref(attr), setpgroup))
                flags |= _DarwinSpawnFlags.SETPGROUP
            _check(calls["posix_spawnattr_setflags"](ctypes.byref(attr), flags))
            for fd, target in dup2s:
                _check(calls["posix_spawn_file_actions_adddup2"](ctypes.byref(actions), fd, target))
            for fd in inherits:
                _check(calls["posix_spawn_file_actions_addinherit_np"](ctypes.byref(actions), fd))
            if chdir is not None:
                _check(calls["posix_spawn_file_actions_addchdir_np"](ctypes.byref(actions), chdir))
            words = (ctypes.c_char_p * (len(argv) + 1))(*argv, None)
            envp = environ().contents if env is None else (ctypes.c_char_p * (len(env) + 1))(*env, None)
            code = calls["posix_spawn"](ctypes.byref(pid), path, ctypes.byref(actions), ctypes.byref(attr),
                                        words, envp)
        finally:
            calls["posix_spawn_file_actions_destroy"](ctypes.byref(actions))
    finally:
        calls["posix_spawnattr_destroy"](ctypes.byref(attr))
    if code:
        raise OSError(code, os.strerror(code), path)
    return pid.value


# LLM: 与 Popen._execute_child 同样规整 args/shell/executable，并按 Popen 交给 _posixsubprocess 的同一规则编码：
#   argv 与 executable 逐个 os.fsencode，env 逐项编码为 NAME=VALUE，候选可执行文件按 os.get_exec_path(env) 展开。
#   Popen 会因此抛异常的输入（空 args、类型不对、名字含 "="、含 NUL、PATH 键冲突等）返回 None，由原 Popen 给出
#   原异常。返回 (审计用 executable, 审计用 args, argv, envp, 候选路径, cwd 字节)。
# 函数用途: 生成派生所需的字节参数；不适用返回 None。
def _spawn_command(args, executable, cwd, env, shell):
    if isinstance(args, (str, bytes)):
        args = [args]
    elif isinstance(args, os.PathLike):
        if shell:
            return None
        args = [args]
    else:
        args = list(args)
    if shell:
        args = ["/bin/sh", "-c", *args]
        if executable:
            args[0] = executable
    if not args:
        return None
    if executable is None:
        executable = args[0]
    try:
        argv = [os.fsencode(word) for word in args]
        program = os.fsencode(executable)
        envp = None
        if env is not None:
            envp = []
            for key, value in env.items():
                name = os.fsencode(key)
                if b"=" in name:
                    return None
                envp.append(name + b"=" + os.fsencode(value))
        if os.path.dirname(program):
            candidates = (program,)
        else:
            candidates = tuple(os.path.join(os.fsencode(entry), program) for entry in os.get_exec_path(env))
        cwd_bytes = None if cwd is None else os.fsencode(cwd)
    except (TypeError, ValueError, AttributeError):
        return None
    if any(b"\0" in part for part in (*argv, *candidates, *(envp or ()), cwd_bytes or b"")):
        return None
    return executable, args, argv, envp, candidates, cwd_bytes


# 函数用途: 预判 chdir(path) 的 errno（不存在、不是目录、不可搜索）；可以切换时返回 None。
def _chdir_error(path: bytes) -> int | None:
    try:
        mode = os.stat(path).st_mode
    except OSError as exc:
        return exc.errno
    if not stat.S_ISDIR(mode):
        return errno.ENOTDIR
    return None if os.access(path, os.X_OK) else errno.EACCES


# 函数用途: fd 在本进程打开且可继承时返回 True。
def _inheritable(fd: int) -> bool:
    try:
        return os.get_inheritable(fd)
    except OSError:
        return False


# LLM: 与 _posixsubprocess 子进程的 fd 处理逐项对应：管道子端 dup2 到 0/1/2；未重定向的 0/1/2 原样继承（父进程里
#   打开且可继承才保留，与 exec 时 close-on-exec 的效果相同）；pass_fds 全部保留并设为可继承。pass_fds 含父进程
#   管道端（Popen 子进程会关掉它们）或未打开的 fd，或任何 fd 号不小于 OPEN_MAX 时返回 None，改走原 Popen。
# 函数用途: 生成 (dup2s, inherits) 文件动作；不适用返回 None。
def _file_actions(pass_fds, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite):
    try:
        keep = sorted({int(fd) for fd in pass_fds})
        for fd in keep:
            os.get_inheritable(fd)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not {p2cwrite, c2pread, errread}.isdisjoint(keep):
        return None
    redirects = ((p2cread, 0), (c2pwrite, 1), (errwrite, 2))
    dup2s = [(fd, target) for fd, target in redirects if fd != -1]
    inherits = [target for fd, target in redirects if fd == -1 and _inheritable(target)]
    inherits += [fd for fd in keep if fd not in inherits]
    if any(fd >= _DARWIN_OPEN_MAX_FILES for fd in (*inherits, *(source for source, _target in dup2s))):
        return None
    return dup2s, inherits


# 函数用途: Popen 的 restore_signals 恢复默认处置的信号（同 _Py_RestoreSignals：SIGPIPE、SIGXFZ、SIGXFSZ 中本平台有的）。
def _restored_signals() -> tuple[int, ...]:
    return tuple(signum for signum in (getattr(signal, name, None) for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                 if signum is not None)


# 类用途: 与 subprocess.Popen 行为一致、在 Darwin 上不 fork 当前进程的 Popen 子类。
class SpawnPopen(subprocess.Popen):
    # LLM: 参数名与顺序钉住 3.14 POSIX Popen._execute_child（3.11–3.14 同形，测试断言）。契约内的调用在 Darwin 上
    #   直接 posix_spawn；其余（Linux、契约外选项、_spawn_natively 判定不适用）以原参数调用
    #   subprocess.Popen._execute_child（一次性迭代器 args 先转成等价列表）。
    # 函数用途: 创建子进程。
    def _execute_child(self, args, executable, preexec_fn, close_fds,
                       pass_fds, cwd, env,
                       startupinfo, creationflags, shell,
                       p2cread, p2cwrite,
                       c2pread, c2pwrite,
                       errread, errwrite,
                       restore_signals,
                       gid, gids, uid, umask,
                       start_new_session, process_group):
        if not isinstance(args, (str, bytes, os.PathLike)):
            args = list(args)
        if (_DARWIN_SPAWN is not None and preexec_fn is None and uid is None and gid is None and gids is None
                and umask < 0 and close_fds
                and (process_group < 0 or (process_group == 0 and not start_new_session))
                and all(fd == -1 or fd > 2 for fd in (p2cread, c2pwrite, errwrite))
                and self._spawn_natively(args, executable, pass_fds, cwd, env, shell,
                                         p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                                         restore_signals, start_new_session, process_group)):
            return
        subprocess.Popen._execute_child(self, args, executable, preexec_fn, close_fds,
                                        pass_fds, cwd, env,
                                        startupinfo, creationflags, shell,
                                        p2cread, p2cwrite,
                                        c2pread, c2pwrite,
                                        errread, errwrite,
                                        restore_signals,
                                        gid, gids, uid, umask,
                                        start_new_session, process_group)

    # LLM: 编码与检查都在审计之前完成，不适用即返回 False（尚未审计、未派生），由调用方走原 Popen；审计之后只会
    #   成功或按 Popen 的规则抛出。候选循环与 _posixsubprocess 一致：ENOENT/ENOTDIR 试下一个（先用 stat 跳过不存在
    #   的候选，省掉一次失败的派生），报告第一个其它 errno、否则最后一个，filename 为原 executable；EAGAIN（进程数
    #   上限）与 fork 失败一样立即抛出、不带 filename。全部失败后 cwd 已不可切换（启动期间被删等）时，按 Popen 子
    #   进程 chdir 失败抛出，filename 为原 cwd。每次实际派生前另发 os.posix_spawn 审计事件。
    # 函数用途: 经 libc posix_spawn 直接派生目标程序。
    def _spawn_natively(self, args, executable, pass_fds, cwd, env, shell,
                        p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                        restore_signals, start_new_session, process_group) -> bool:
        command = _spawn_command(args, executable, cwd, env, shell)
        if command is None:
            return False
        executable, args, argv, envp, candidates, cwd_bytes = command
        if cwd_bytes is not None and _chdir_error(cwd_bytes) is not None:
            return False
        actions = _file_actions(pass_fds, p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
        if actions is None:
            return False
        dup2s, inherits = actions
        options = {"dup2s": dup2s, "inherits": inherits, "chdir": cwd_bytes, "setsid": bool(start_new_session),
                   "setpgroup": 0 if process_group == 0 else None,
                   "setsigdef": _restored_signals() if restore_signals else ()}
        sys.audit("subprocess.Popen", executable, args, cwd, env)
        first = last = None
        for candidate in candidates:
            try:
                os.stat(candidate if cwd_bytes is None else os.path.join(cwd_bytes, candidate))
            except OSError as exc:
                if exc.errno in _NOT_FOUND_ERRNOS:
                    last = exc.errno
                    continue
            sys.audit("os.posix_spawn", candidate, argv, env)
            try:
                self.pid = _posix_spawn(candidate, argv, envp, **options)
            except OSError as exc:
                if exc.errno == errno.EAGAIN:
                    raise OSError(exc.errno, exc.strerror) from None
                last = exc.errno
                if first is None and last not in _NOT_FOUND_ERRNOS:
                    first = last
                continue
            self._child_created = True
            self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
            return True
        cwd_error = None if cwd_bytes is None else _chdir_error(cwd_bytes)
        if cwd_error is not None:
            raise OSError(cwd_error, os.strerror(cwd_error), cwd)
        code = last if first is None else first
        raise OSError(code, os.strerror(code), executable)


__all__ = ["SpawnPopen"]
