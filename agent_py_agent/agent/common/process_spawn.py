"""不 fork 网关大进程的子进程派生（S50-14，C1.5，R3 §5.10）。

macOS 上 subprocess.Popen 只要带 cwd、pass_fds、start_new_session 或 process_group 就走 fork；多 GB 的网关进程
每次 fork 都要复制页表，在忙线程下停顿数十毫秒（R3 §2.11）。SpawnPopen 在 Darwin/Linux 上改用 os.posix_spawn
启动 /bin/sh 跳板：跳板以空环境运行，cd 到 cwd、关闭 3–9 号残留 fd，再 exec /usr/bin/env -i，由 env 按参数里的
NAME=VALUE 逐字节建出与 Popen 相同的环境并 exec 目标程序（argv 原样）。可继承的高位 fd 由父进程文件动作关闭。

父进程侧先做完 Popen 会在子进程里失败的全部检查（cwd、可执行文件查找与格式、pass_fds、fd 号上限等）；任何一项
不满足，或调用用到 preexec_fn/uid/gid/gids/umask/close_fds=False 等契约外选项时，原样调用
subprocess.Popen._execute_child，所以这些情形的异常类型、errno、filename 与 Popen 完全一致。
"""

from __future__ import annotations

import itertools
import os
import signal
import stat
import subprocess
import sys

_SHELL = "/bin/sh"
_ENV_BINARY = "/usr/bin/env"
_posix_spawn = getattr(os, "posix_spawn", None)
# macOS <sys/syslimits.h> 的 OPEN_MAX：posix_spawn 文件动作拒绝不小于它的 fd 号（与 RLIMIT_NOFILE 无关），用到时改走原 Popen。
_DARWIN_OPEN_MAX_FILES = 10240
# #! 首行最多检查的字节数（Linux BINPRM_BUF_SIZE；macOS 内核上限 512）：首行落在其内时两种内核解析一致，否则改走原 Popen。
_SHEBANG_LINE_LIMIT_BYTES = 256

# 原生可执行文件头：Mach-O（32/64 位、两种字节序）与通用二进制（fat/fat64）；ELF。
_NATIVE_MAGICS = {
    "darwin": (b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",
               b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"),
    "linux": (b"\x7fELF",),
}

# LLM: 参数依次为 $1=cwd（空串表示不切换；相对路径已由父进程加 ./ 前缀，避开 CDPATH 与 "-"）、$2=保留 fd 表，
#   其后是 "--"、NAME=VALUE 赋值、命令字与其余参数。cd -P 与 chdir 一样按物理路径解析，失败退出 126；dash 不认
#   多位数 fd 重定向，所以循环只关 3–9 号（更高号的可继承 fd 已由父进程文件动作关闭）；最后 env -i 清空环境、只按
#   赋值建环境并 execvp 命令字（裸名按新环境的 PATH 查找，argv[0] 原样保留）。
_TRAMPOLINE = (
    'case $1 in \'\') ;; *) cd -P -- "$1" || exit 126;; esac; '
    'keep=" 0 1 2 $2 "; shift 2; '
    'for f in /dev/fd/*; do n=${f##*/}; case "$keep" in *" $n "*) ;; *) '
    'case $n in [3-9]) eval "exec $n>&-" 2>/dev/null;; esac;; esac; done; '
    f'exec {_ENV_BINARY} -i "$@"'
)


# 函数用途: 平台与运行时是否具备 posix_spawn 跳板的前提（Darwin/Linux 非 Android、可用 posix_spawn、/bin/sh 与 env 可执行）。
def _platform_ready() -> bool:
    return (sys.platform in _NATIVE_MAGICS and not hasattr(sys, "getandroidapilevel")
            and _posix_spawn is not None and getattr(subprocess, "_USE_POSIX_SPAWN", False)
            and os.access(_SHELL, os.X_OK) and os.access(_ENV_BINARY, os.X_OK))


# 函数用途: 读文件开头若干字节；打不开或读失败返回 None。
def _read_head(path: bytes) -> bytes | None:
    try:
        with open(path, "rb") as handle:
            return handle.read(_SHEBANG_LINE_LIMIT_BYTES)
    except OSError:
        return None


# 函数用途: 文件头是否为本平台原生可执行格式。
def _is_native(head: bytes) -> bool:
    return head.startswith(_NATIVE_MAGICS[sys.platform])


# LLM: 内核 exec 必然成功的格式才可交给 env：原生二进制，或 #! 解释器为绝对路径、本身是原生二进制的脚本。其余
#   （未知格式、无 #! 的脚本）在 Popen 下报 ENOEXEC，而 execvp 会改用 sh 解释，两者不同，于是改走原 Popen。
#   解释器按内核规则以空格/制表符截断（不剥 \r，CRLF 脚本的解释器路径在两条路径上同样不存在）。
# 函数用途: 判断选中的可执行文件能否经跳板运行。
def _exec_format_ok(path: bytes) -> bool:
    head = _read_head(path)
    if head is None:
        return False
    if _is_native(head):
        return True
    newline = head.find(b"\n")
    if not head.startswith(b"#!") or newline < 0:
        return False
    interpreter = head[2:newline].replace(b"\t", b" ").lstrip(b" ").split(b" ", 1)[0]
    if not interpreter.startswith(b"/"):
        return False
    try:
        mode = os.stat(interpreter).st_mode
    except OSError:
        return False
    if not stat.S_ISREG(mode) or not os.access(interpreter, os.X_OK):
        return False
    interpreter_head = _read_head(interpreter)
    return interpreter_head is not None and _is_native(interpreter_head)


# LLM: 与 Popen 子进程的逐个 execve 一致：不存在（ENOENT/ENOTDIR）跳过，存在但非普通文件或不可执行（execve 报 EACCES）
#   跳过；其它 stat 错误下 BSD/glibc execvp 会停止查找而 Popen 继续，结果可能不同，返回 None 改走原 Popen。
#   相对候选按子进程 chdir 之后的 cwd 解析。
# 函数用途: 选出 Popen 会实际 exec 的第一个候选；找不到或无法确定返回 None。
def _choose_executable(executable: bytes, env, cwd: bytes | None) -> bytes | None:
    if os.path.dirname(executable):
        candidates = (executable,)
    else:
        candidates = tuple(os.path.join(os.fsencode(entry), executable) for entry in os.get_exec_path(env))
    for candidate in candidates:
        full = candidate if cwd is None or candidate.startswith(b"/") else os.path.join(cwd, candidate)
        try:
            mode = os.stat(full).st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            return None
        if stat.S_ISREG(mode) and os.access(full, os.X_OK):
            return full
    return None


# LLM: 与 Popen 相同地规整 args/shell/executable；env 赋值按 Popen 的 fsencode 规则转换，名字为空、含 "="、重复（str
#   与 bytes 混用）或任何参数含 NUL 时改走原 Popen（由它给出原异常）。env=None 时取 os.environb 快照。
# 函数用途: 生成跳板 argv（不含 fd 部分）与审计事件参数；不适用返回 None。
def _spawn_command(args, executable, cwd, env, shell):
    if isinstance(args, (str, bytes)):
        argv = [args]
    elif isinstance(args, os.PathLike):
        if shell:
            return None
        argv = [args]
    else:
        argv = list(args)
    if shell:
        argv = ["/bin/sh", "-c", *argv]
        if executable:
            argv[0] = executable
    if not argv:
        return None
    audit_executable = argv[0] if executable is None else executable
    environ = os.environb if env is None else env
    try:
        words = [os.fsencode(word) for word in argv]
        command = os.fsencode(audit_executable)
        cwd_bytes = None if cwd is None else os.fsencode(cwd)
        assignments, names = [], set()
        for key, value in environ.items():
            name, text = os.fsencode(key), os.fsencode(value)
            if not name or b"=" in name or name in names:
                return None
            names.add(name)
            assignments.append(name + b"=" + text)
        has_path = b"PATH" in names
    except (TypeError, ValueError):
        return None
    if (command != words[0] or not command or b"=" in command
            or any(b"\0" in part for part in (*words, *assignments, cwd_bytes or b""))):
        return None
    if not os.path.dirname(command) and not has_path:
        return None
    return audit_executable, argv, cwd_bytes, command, assignments, words[1:]


# 函数用途: 枚举本进程当前打开的 fd 号；无法枚举返回 None。
def _open_fds() -> list[int] | None:
    for root in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(root)
        except OSError:
            continue
        return [int(name) for name in names if name.isdigit()]
    return None


# 函数用途: 文件动作可用的 fd 号上限（不含）；无法确定返回 None。
def _file_action_fd_limit() -> int | None:
    try:
        limit = os.sysconf("SC_OPEN_MAX")
    except (OSError, ValueError):
        return None
    if limit <= 0:
        return None
    return min(limit, _DARWIN_OPEN_MAX_FILES) if sys.platform == "darwin" else limit


# 类用途: 与 subprocess.Popen 行为一致、在 Darwin/Linux 上不 fork 当前进程的 Popen 子类。
class SpawnPopen(subprocess.Popen):
    # LLM: 参数名与顺序钉住 3.14 POSIX Popen._execute_child（3.11–3.14 同形，测试断言）。契约内的调用走跳板；其余或
    #   父进程侧检查不通过时，以原参数调用 subprocess.Popen._execute_child（一次性迭代器 args 先转成等价列表）。
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
        if (preexec_fn is None and uid is None and gid is None and gids is None and umask < 0 and close_fds
                and not (start_new_session and process_group >= 0)
                and all(fd == -1 or fd > 2 for fd in (p2cread, c2pwrite, errwrite))
                and _platform_ready()
                and self._spawn_via_trampoline(args, executable, pass_fds, cwd, env, shell,
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

    # LLM: 检查全部通过才审计并派生，返回 True；任何检查不通过返回 False（尚未审计、未派生），由调用方走原 Popen。
    #   文件动作顺序：非可继承的 pass_fds 经临时号 dup2 来回以清除 close-on-exec；按 Popen._posix_spawn 关闭父端、
    #   dup2 管道到 0/1/2；其余可继承 fd 先 dup2 锚点再关闭（macOS 关闭未打开的 fd 会令 posix_spawn 失败）。
    # 函数用途: 经 /bin/sh 跳板 posix_spawn 子进程。
    def _spawn_via_trampoline(self, args, executable, pass_fds, cwd, env, shell,
                              p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite,
                              restore_signals, start_new_session, process_group) -> bool:
        command = _spawn_command(args, executable, cwd, env, shell)
        if command is None:
            return False
        audit_executable, audit_args, cwd_bytes, word, assignments, rest = command
        if cwd_bytes is not None:
            try:
                mode = os.stat(cwd_bytes).st_mode
            except OSError:
                return False
            if not stat.S_ISDIR(mode) or not os.access(cwd_bytes, os.X_OK):
                return False
        chosen = _choose_executable(word, env, cwd_bytes)
        if chosen is None or not _exec_format_ok(chosen):
            return False
        keep = sorted({int(fd) for fd in pass_fds})
        try:
            uncloexec = [fd for fd in keep if fd > 2 and not os.get_inheritable(fd)]
        except OSError:
            return False
        open_fds, limit = _open_fds(), _file_action_fd_limit()
        if open_fds is None or limit is None:
            return False
        kept = set(keep)
        to_close = []
        for fd in open_fds:
            if fd > 2 and fd not in kept:
                try:
                    if os.get_inheritable(fd):
                        to_close.append(fd)
                except OSError:
                    continue
        pipes = [fd for fd in (p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite) if fd != -1]
        anchor = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC) if to_close else None
        try:
            anchors = () if anchor is None else (anchor,)
            to_close = [fd for fd in to_close if fd not in anchors]
            # 临时号取当前最小的空闲号：之后被别的线程占用也只会覆盖子进程里的那份副本。
            taken = {*open_fds, *keep, *pipes, *anchors}
            spare = next(fd for fd in itertools.count(3) if fd not in taken) if uncloexec else None
            action_fds = [*uncloexec, *pipes, *to_close, *anchors, *(() if spare is None else (spare,))]
            if any(fd >= limit for fd in action_fds):
                return False
            file_actions = []
            for fd in uncloexec:
                file_actions += [(os.POSIX_SPAWN_DUP2, fd, spare), (os.POSIX_SPAWN_DUP2, spare, fd),
                                 (os.POSIX_SPAWN_CLOSE, spare)]
            file_actions += [(os.POSIX_SPAWN_CLOSE, fd) for fd in (p2cwrite, c2pread, errread) if fd != -1]
            file_actions += [(os.POSIX_SPAWN_DUP2, fd, target)
                             for fd, target in ((p2cread, 0), (c2pwrite, 1), (errwrite, 2)) if fd != -1]
            for fd in to_close:
                file_actions += [(os.POSIX_SPAWN_DUP2, anchor, fd), (os.POSIX_SPAWN_CLOSE, fd)]
            kwargs = {}
            if restore_signals:
                kwargs["setsigdef"] = [signum for signum in (getattr(signal, name, None)
                                                             for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ"))
                                       if signum is not None]
            if start_new_session:
                kwargs["setsid"] = True
            if process_group >= 0:
                kwargs["setpgroup"] = process_group
            if file_actions:
                kwargs["file_actions"] = file_actions
            cwd_arg = b"" if cwd_bytes is None else cwd_bytes if cwd_bytes.startswith(b"/") else b"./" + cwd_bytes
            argv = [_SHELL, "-c", _TRAMPOLINE, "sh", cwd_arg, " ".join(map(str, keep)), "--",
                    *assignments, word, *rest]
            sys.audit("subprocess.Popen", audit_executable, audit_args, cwd, env)
            self.pid = _posix_spawn(_SHELL, argv, {}, **kwargs)
        finally:
            if anchor is not None:
                os.close(anchor)
        self._child_created = True
        self._close_pipe_fds(p2cread, p2cwrite, c2pread, c2pwrite, errread, errwrite)
        return True


__all__ = ["SpawnPopen"]
