# LLM: Conveyor M00 / R3-G0 + Layer-2 M00-fix（F2/F3/F4）：SpawnPopen 在 Darwin 上直接以 libc posix_spawn 启动目标
#   程序（无中间进程），代替 fork 大进程；Linux 及契约外选项走原 Popen。可观察行为必须与 subprocess.Popen 一致：
#   退出码、管道、communicate、cwd、env（逐字节，含壳层会改写的变量与 SIP 会剥掉的 DYLD_*；env=None 时含 os.putenv
#   的修改）、start_new_session/process_group、pass_fds；子进程 fd 恰为 {0,1,2} ∪ pass_fds，包括其它线程在启动窗口里
#   新开的可继承 fd（128 路并发启动）；环境赋值不进入任何 argv；exec 失败（ENOEXEC 等）与启动中被删的 cwd 在父进程
#   按 Popen 的类型/errno/filename 抛出，而真实启动后以 126 退出的程序仍是正常回执；_execute_child 签名与 3.14 一致。
#   close_fds=False 的普通 Popen 对照必须在同一 fd 断言上失败，证明断言真能发现泄漏。环境探针与派生探针只记录
#   变量名和 SHA-256 摘要，断言失败时也不会把继承环境里的值打印出来。
# 模块用途: 固定 agent/common/process_spawn.py 的派生合同（C1.5），只起本机短命子进程，不读 git 历史。
from __future__ import annotations

import errno
import hashlib
import inspect
import json
import os
import select
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import threading
import time

import pytest

from agent_py_agent.agent.common import process_spawn
from agent_py_agent.agent.common.process_spawn import SpawnPopen

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="posix_spawn 路径只在 Darwin/Linux 启用")

# 合同路径经 process_spawn._posix_spawn 派生的平台（Linux 由 CPython 的 vfork 路径承担，不经它）。
SPAWNS_THROUGH_MODULE = sys.platform == "darwin"
# 3.14 POSIX Popen._execute_child 的参数名（C1.5 钉住；3.11–3.14 同形）。
PINNED_EXECUTE_CHILD_PARAMETERS = (
    "self", "args", "executable", "preexec_fn", "close_fds", "pass_fds", "cwd", "env", "startupinfo",
    "creationflags", "shell", "p2cread", "p2cwrite", "c2pread", "c2pwrite", "errread", "errwrite",
    "restore_signals", "gid", "gids", "uid", "umask", "start_new_session", "process_group",
)
# 父进程里复制出的可继承高位 fd 号（两位数，覆盖旧跳板只关 3–9 号的边界）。
HIGH_INHERITABLE_FD = 87
# 启动窗口里新开的可继承 fd 的起始号（远离 3–9，也避开测试进程已有的低位 fd）。
LATE_FD_FLOOR = 200
# 每个子进程的等待上限秒数。
CHILD_TIMEOUT_SECONDS = 20
# 并发启动数：与验收配置 gateway_http_max_request_workers=128 相同（C0 规则 8）。
CONCURRENT_LAUNCHES = 128
# 泄漏对照的启动数（close_fds=False 的普通 Popen）。
LEAKING_CONTROL_LAUNCHES = 4
# 后开写端的读端等待 EOF 的秒数：无子进程持有时立即 EOF，持有时一直不到。
LEAK_PROBE_SECONDS = 1.0
# Mach-O 64 位魔数、ARM64 CPU 类型与 MH_OBJECT 文件类型（<mach-o/loader.h>）：合法头但不是可执行映像。
MACHO_MAGIC_64 = 0xFEEDFACF
MACHO_CPU_TYPE_ARM64 = 0x0100000C
MACHO_FILETYPE_OBJECT = 1
# ELF64 小端头字段：ELFCLASS64、ELFDATA2LSB、EV_CURRENT、ET_REL（可重定位目标文件）、头长 64 字节。
ELF_CLASS_64 = 2
ELF_DATA_LSB = 1
ELF_VERSION_CURRENT = 1
ELF_TYPE_RELOCATABLE = 1
ELF_HEADER_BYTES = 64

# 子进程探针：按 argv[1] 报告 fd 集合、环境、cwd、会话/进程组或回显 stdin。-I -S 保证解释器启动不额外开文件。
_CHILD = r'''
import hashlib, json, os, sys
mode = sys.argv[1]
if mode == "fds":
    root = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    found = []
    for name in os.listdir(root):
        try:
            os.fstat(int(name))
        except OSError:
            continue
        found.append(int(name))
    print(json.dumps(sorted(found)))
elif mode == "env":
    print(json.dumps(sorted([k.decode("latin-1"), hashlib.sha256(v).hexdigest()] for k, v in os.environb.items())))
elif mode == "cwd":
    print(os.getcwd())
elif mode == "ids":
    print(json.dumps([os.getpid(), os.getsid(0), os.getpgid(0), sys.orig_argv[0]]))
elif mode == "echo":
    data = sys.stdin.buffer.read()
    sys.stdout.buffer.write(data.upper())
    sys.stderr.write("err:" + str(len(data)))
    sys.exit(int(sys.argv[2]))
elif mode == "write-fd":
    os.write(int(sys.argv[2]), b"through-pass-fd")
elif mode == "kill-self":
    os.kill(os.getpid(), 15)
'''


# LLM: 先 os.fsencode 再取摘要，str 与 bytes 都不会让探针在派生路径上抛错——探针帧一旦出现在回溯里，pytest 会把
#   它的实参（argv/env 原文）打印出来。
# 函数用途: 字符串或字节串的 SHA-256 摘要；环境值只以摘要比较，从不明文出现在断言与日志里。
def _digest(data: str | bytes) -> str:
    return hashlib.sha256(os.fsencode(data)).hexdigest()


# 函数用途: 组装探针子进程的命令行。
def _probe(*extra: str) -> list[str]:
    return [sys.executable, "-I", "-S", "-c", _CHILD, *extra]


# 函数用途: 用给定 Popen 类运行一次并返回 (退出码, stdout, stderr)。
def _run(popen_cls, args, **kwargs) -> tuple[int, bytes, bytes]:
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.PIPE)
    with popen_cls(args, **kwargs) as proc:
        out, err = proc.communicate(timeout=CHILD_TIMEOUT_SECONDS)
    return proc.returncode, out, err


# 函数用途: 运行并返回成功结果或父进程侧异常的可比较形态，供选项回落路径做差分。
def _result(popen_cls, args, **kwargs):
    try:
        return ("ok", *_run(popen_cls, args, **kwargs))
    except (OSError, subprocess.SubprocessError) as exc:
        return ("raise", type(exc), getattr(exc, "errno", None))


# 函数用途: 从 start 起找一个当前未打开的 fd 号，避免 dup2 覆盖测试进程已有的 fd。
def _free_fd(start: int) -> int:
    candidate = start
    while True:
        try:
            os.fstat(candidate)
        except OSError:
            return candidate
        candidate += 1


# 函数用途: 运行后返回抛出的父进程侧异常的可比较形态（类型、errno、filename、strerror）；未抛出返回 None。
def _spawn_error(popen_cls, args, **kwargs):
    try:
        with popen_cls(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs) as proc:
            proc.wait(timeout=CHILD_TIMEOUT_SECONDS)
    except OSError as exc:
        return type(exc), exc.errno, exc.filename, exc.strerror
    return None


# 函数用途: fd 卫生断言：子进程看到的 fd 必须恰为 {0,1,2} ∪ pass_fds。
def _assert_exact_fds(found: list[int], pass_fds: tuple[int, ...]) -> None:
    assert sorted(found) == sorted({0, 1, 2, *pass_fds}), found


# 函数用途: 从 fd 读满 size 字节（每个子进程写回 1 字节）；超时或 EOF 时返回已读到的部分。
def _read_exactly(fd: int, size: int) -> bytes:
    deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
    data = b""
    while len(data) < size:
        ready, _, _ = select.select([fd], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(fd, size - len(data))
        if not chunk:
            break
        data += chunk
    return data


# 函数用途: 合法魔数但内核拒绝执行的目标文件头（Darwin 为 Mach-O MH_OBJECT，Linux 为 ELF ET_REL）。
def _object_file_header() -> bytes:
    if sys.platform == "darwin":
        return struct.pack("<IiiIIIII", MACHO_MAGIC_64, MACHO_CPU_TYPE_ARM64, 0, MACHO_FILETYPE_OBJECT, 0, 0, 0, 0)
    ident = b"\x7fELF" + bytes((ELF_CLASS_64, ELF_DATA_LSB, ELF_VERSION_CURRENT)) + bytes(9)
    return struct.pack("<16sHHIQQQIHHHHHH", ident, ELF_TYPE_RELOCATABLE, 0, ELF_VERSION_CURRENT,
                       0, 0, 0, 0, ELF_HEADER_BYTES, 0, 0, 0, 0, 0)


# LLM: 把注入点放在 "subprocess.Popen" 审计事件：任何实现都在父进程侧准备（含旧实现的 fd 快照）之后、子进程创建之前
#   发出它，所以在这里开的 fd 恰好落进启动窗口。只对本线程标记过的启动生效，审计钩子由 monkeypatch 还原。
# 函数用途: 在指定启动的审计点上执行回调。
def _audit_point(monkeypatch, callback):
    armed = threading.local()
    real_audit = sys.audit

    def audit(event, *args):
        real_audit(event, *args)
        if event == "subprocess.Popen" and getattr(armed, "on", False):
            armed.on = False
            callback(*args)

    monkeypatch.setattr(sys, "audit", audit)
    return armed


# LLM: count 路启动全部停在审计点（栅栏同时证明父进程侧准备没有被串行化），此时另开一个可继承的管道写端
#   （号 ≥ LATE_FD_FLOOR），放行后各自创建子进程。子进程先经 pass_fds 写回 1 字节，再阻塞在读 stdin；父进程关掉
#   自己的写端后，读端立即 EOF 说明没有任何子进程继承它。返回 (经 pass_fds 收到的字节, 后开 fd 是否未泄漏)。
# 函数用途: 并发启动并探测启动窗口里新开的可继承 fd 是否到达子进程。
def _launch_with_late_fd(monkeypatch, popen_cls, count: int, *, use_pass_fd: bool, **kwargs) -> tuple[bytes, bool]:
    barrier = threading.Barrier(count + 1, timeout=CHILD_TIMEOUT_SECONDS)
    released = threading.Event()

    def hold(*_audit_args) -> None:
        barrier.wait()
        released.wait(CHILD_TIMEOUT_SECONDS)

    armed = _audit_point(monkeypatch, hold)
    pass_read, pass_write = os.pipe()
    late_read = late_write = -1
    script = f"printf x > /dev/fd/{pass_write}; read line" if use_pass_fd else "read line"
    extra = {"pass_fds": (pass_write,)} if use_pass_fd else {}
    procs, errors, lock = [], [], threading.Lock()

    def launch() -> None:
        armed.on = True
        try:
            proc = popen_cls(["/bin/sh", "-c", script], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **extra, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - 失败计入断言
            with lock:
                errors.append(exc)
        else:
            with lock:
                procs.append(proc)

    threads = [threading.Thread(target=launch) for _ in range(count)]
    try:
        for thread in threads:
            thread.start()
        barrier.wait()
        late_read, staged = os.pipe()
        late_write = os.dup2(staged, _free_fd(LATE_FD_FLOOR), inheritable=True)
        os.close(staged)
        released.set()
        for thread in threads:
            thread.join(CHILD_TIMEOUT_SECONDS)
        assert not errors and len(procs) == count, errors[:3]
        received = _read_exactly(pass_read, count) if use_pass_fd else b""
        os.close(late_write)
        late_write = -1
        ready, _, _ = select.select([late_read], [], [], LEAK_PROBE_SECONDS)
        closed_everywhere = bool(ready) and os.read(late_read, 1) == b""
        return received, closed_everywhere
    finally:
        released.set()
        for proc in procs:
            proc.stdin.close()
        for proc in procs:
            proc.wait(timeout=CHILD_TIMEOUT_SECONDS)
        for fd in (pass_read, pass_write, late_read, late_write):
            if fd != -1:
                os.close(fd)


# 函数用途: 记录 process_spawn._posix_spawn 调用的探针（argv 逐项摘要、envp 整体摘要，None 表示继承），透传真实调用。
@pytest.fixture
def spawn_calls(monkeypatch):
    calls = []
    real = process_spawn._posix_spawn

    def spy(path, argv, env, **kwargs):
        calls.append((path, [_digest(part) for part in argv], None if env is None else _digest(b"\0".join(env)),
                      kwargs))
        return real(path, argv, env, **kwargs)

    monkeypatch.setattr(process_spawn, "_posix_spawn", spy)
    return calls


# 函数用途: 合同路径在本平台应经 _posix_spawn 发生的派生次数。
def _expected_spawns(count: int) -> int:
    return count if SPAWNS_THROUGH_MODULE else 0


def test_execute_child_signature_is_pinned():
    ours = tuple(inspect.signature(SpawnPopen._execute_child).parameters)
    stock = tuple(inspect.signature(subprocess.Popen._execute_child).parameters)
    assert ours == PINNED_EXECUTE_CHILD_PARAMETERS == stock


def test_exit_code_pipes_and_communicate_match_popen(spawn_calls):
    for code in (0, 3):
        assert _run(SpawnPopen, _probe("echo", str(code)), stdin=subprocess.PIPE) == \
            _run(subprocess.Popen, _probe("echo", str(code)), stdin=subprocess.PIPE)
    with SpawnPopen(_probe("echo", "5"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT) as proc:
        merged, _ = proc.communicate(b"abc", timeout=CHILD_TIMEOUT_SECONDS)
    assert (proc.returncode, merged) == (5, b"ABCerr:3")
    assert _run(SpawnPopen, _probe("kill-self"))[0] == _run(subprocess.Popen, _probe("kill-self"))[0] == -15
    text = _run(SpawnPopen, 'printf "a\\nb" && echo " $0"', shell=True, text=True)
    assert text == _run(subprocess.Popen, 'printf "a\\nb" && echo " $0"', shell=True, text=True)
    assert len(spawn_calls) == _expected_spawns(5)


def test_env_is_byte_exact_including_shell_managed_and_dyld_names(spawn_calls):
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C", "PWD": "/nonexistent-pwd", "OLDPWD": "/old",
        "IFS": "x:y", "PS1": "$ ", "PS2": "> ", "OPTIND": "9", "PPID": "1", "LINENO": "3", "_": "/underscore",
        "SHELLOPTS": "xtrace", "BASHOPTS": "extglob", "ENV": "/nonexistent-env", "BASH_ENV": "/nonexistent-bash-env",
        "CDPATH": "/tmp", "POSIXLY_CORRECT": "1", "RANDOM": "4", "BASH_FUNC_m00%%": "() { echo hi; }",
        "keep": "k", "f": "f", "n": "n", "odd name": "spaces in name", "UNICODE_值": "中文😀", "EMPTY": "",
        "DYLD_LIBRARY_PATH": "/nonexistent-m00-dyld", "MULTI": "line1\nline2",
    }
    ours = _run(SpawnPopen, _probe("env"), env=env)
    assert ours == _run(subprocess.Popen, _probe("env"), env=env)
    seen = dict(json.loads(ours[1]))
    # 探针回报 latin-1 逐字节解码的变量名与原始值字节的 SHA-256，这里按同一规则算出期望值。
    for key, value in env.items():
        assert seen.get(os.fsencode(key).decode("latin-1")) == _digest(os.fsencode(value)), key
    # SHLVL 缺省时不得替子进程补出来；inherit（env=None）时与父进程环境一致。
    assert "SHLVL" not in seen
    assert _run(SpawnPopen, _probe("env")) == _run(subprocess.Popen, _probe("env"))
    assert len(spawn_calls) == _expected_spawns(2)


def test_environment_never_rides_in_spawn_argv(spawn_calls):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "M00_FIX_LABEL": "argv-must-not-carry-this"}
    ours = _run(SpawnPopen, _probe("env"), env=env)
    assert ours == _run(subprocess.Popen, _probe("env"), env=env)
    assignments = [os.fsencode(key) + b"=" + os.fsencode(value) for key, value in env.items()]
    for _path, argv, _env, _kwargs in spawn_calls:
        assert {_digest(item) for item in assignments}.isdisjoint(argv)
    # Darwin 上唯一一次派生就是目标程序本身：argv 与调用方给的完全相同，环境按原顺序只走 envp。
    assert [call[1] for call in spawn_calls] == \
        [[_digest(os.fsencode(part)) for part in _probe("env")]] * _expected_spawns(1)
    assert [call[2] for call in spawn_calls] == [_digest(b"\0".join(assignments))] * _expected_spawns(1)


def test_env_none_keeps_native_putenv_changes(spawn_calls):
    name, value = "M00_FIX_PUTENV_ONLY", "native-environ"
    assert name not in os.environ
    os.putenv(name, value)
    try:
        assert name not in os.environ
        ours = _run(SpawnPopen, _probe("env"))
        stock = _run(subprocess.Popen, _probe("env"))
    finally:
        os.unsetenv(name)
    assert [name, _digest(value.encode())] in json.loads(stock[1])
    assert ours == stock
    # env=None 时不另建 envp，直接交出 libc 当前的 environ。
    assert [call[2] for call in spawn_calls] == [None] * _expected_spawns(1)


def test_cwd_forms_match_popen(tmp_path, monkeypatch, spawn_calls):
    real_dir = tmp_path / "real dir"
    (real_dir / "-").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    for cwd in (real_dir, str(real_dir), os.fsencode(real_dir), "real dir", "real dir/-", "link", "link/../link"):
        ours = _run(SpawnPopen, _probe("cwd"), cwd=cwd, env={"CDPATH": str(tmp_path)})
        assert ours == _run(subprocess.Popen, _probe("cwd"), cwd=cwd, env={"CDPATH": str(tmp_path)}), cwd
        assert ours[0] == 0
    # 相对可执行路径按新 cwd 解析（与 Popen 子进程先 chdir 再 exec 一致）。
    (real_dir / "probe-link").symlink_to(sys.executable)
    relative = ["./probe-link", *_probe("cwd")[1:]]
    assert _run(SpawnPopen, relative, cwd=real_dir) == _run(subprocess.Popen, relative, cwd=real_dir)
    assert len(spawn_calls) == _expected_spawns(8)


def test_new_session_process_group_and_argv0(spawn_calls):
    pid, sid, pgid, argv0 = json.loads(_run(SpawnPopen, _probe("ids"), start_new_session=True)[1])
    assert sid == pid == pgid and sid != os.getsid(0)
    assert argv0 == sys.executable
    pid, sid, pgid, _argv0 = json.loads(_run(SpawnPopen, _probe("ids"), process_group=0)[1])
    assert pgid == pid and sid == os.getsid(0)
    shell_path = {"PATH": "/bin:/usr/bin"}
    assert _run(SpawnPopen, ["sh", "-c", 'echo "$0"'], env=shell_path) == \
        _run(subprocess.Popen, ["sh", "-c", 'echo "$0"'], env=shell_path) == (0, b"sh\n", b"")
    assert len(spawn_calls) == _expected_spawns(3)
    if SPAWNS_THROUGH_MODULE:
        assert spawn_calls[0][3]["setsid"] is True
        assert spawn_calls[1][3]["setpgroup"] == 0
        assert {signal.SIGPIPE, signal.SIGXFSZ} <= set(spawn_calls[0][3]["setsigdef"])


def test_explicit_executable_keeps_argv0_like_popen(spawn_calls):
    args = ["m00-argv0-alias", *_probe("ids")[1:]]
    ours = _run(SpawnPopen, args, executable=sys.executable)
    stock = _run(subprocess.Popen, args, executable=sys.executable)
    assert ours[0] == stock[0] == 0
    assert json.loads(ours[1])[3] == json.loads(stock[1])[3] == "m00-argv0-alias"


def test_pass_fds_reach_the_child(spawn_calls):
    read_end, write_end = os.pipe()
    try:
        assert not os.get_inheritable(write_end)
        assert _run(SpawnPopen, _probe("write-fd", str(write_end)), pass_fds=(write_end,))[0] == 0
        os.close(write_end)
        write_end = -1
        assert os.read(read_end, 64) == b"through-pass-fd"
    finally:
        os.close(read_end)
        if write_end != -1:
            os.close(write_end)
    assert len(spawn_calls) == _expected_spawns(1)


def test_child_fds_are_exactly_std_plus_pass_fds(tmp_path, spawn_calls):
    database = sqlite3.connect(tmp_path / "m00.db")
    database.execute("create table t(x)")
    database.commit()
    listener = socket.create_server(("127.0.0.1", 0))
    read_end, write_end = os.pipe()
    inheritable = os.dup(read_end)
    os.set_inheritable(inheritable, True)
    high = os.dup2(write_end, _free_fd(HIGH_INHERITABLE_FD), inheritable=True)
    passed = os.dup(write_end)
    try:
        found = json.loads(_run(SpawnPopen, _probe("fds"), pass_fds=(passed, high))[1])
        _assert_exact_fds(found, (passed, high))
        found = json.loads(_run(SpawnPopen, _probe("fds"))[1])
        _assert_exact_fds(found, ())
        assert len(spawn_calls) == _expected_spawns(2)
        # 对照：不关 fd 的普通 Popen 必须在同一断言上失败，证明断言真能发现泄漏。
        leaked = json.loads(_run(subprocess.Popen, _probe("fds"), close_fds=False)[1])
        assert {inheritable, high} <= set(leaked)
        with pytest.raises(AssertionError):
            _assert_exact_fds(leaked, ())
    finally:
        for fd in (read_end, write_end, inheritable, high, passed):
            os.close(fd)
        listener.close()
        database.close()


def test_late_inheritable_fd_never_reaches_concurrent_children(monkeypatch):
    received, closed_everywhere = _launch_with_late_fd(monkeypatch, SpawnPopen, CONCURRENT_LAUNCHES, use_pass_fd=True)
    assert received == b"x" * CONCURRENT_LAUNCHES
    assert closed_everywhere


def test_late_fd_probe_detects_a_leaking_launcher(monkeypatch):
    _received, closed_everywhere = _launch_with_late_fd(monkeypatch, subprocess.Popen, LEAKING_CONTROL_LAUNCHES,
                                                        use_pass_fd=False, close_fds=False)
    assert not closed_everywhere


def test_exec_format_error_raises_in_parent_like_popen(tmp_path, spawn_calls):
    image = tmp_path / "m00-object.o"
    image.write_bytes(_object_file_header())
    image.chmod(image.stat().st_mode | stat.S_IXUSR)
    expected = (OSError, errno.ENOEXEC, str(image), os.strerror(errno.ENOEXEC))
    assert _spawn_error(subprocess.Popen, [str(image)]) == expected
    assert _spawn_error(SpawnPopen, [str(image)]) == expected
    # 真正启动并以 126 退出的程序仍是正常回执，与 exec 失败可区分。
    exit_126 = ["/bin/sh", "-c", "exit 126"]
    assert _run(SpawnPopen, exit_126) == _run(subprocess.Popen, exit_126) == (126, b"", b"")
    assert len(spawn_calls) == _expected_spawns(2)


def test_cwd_removed_during_launch_raises_like_popen(tmp_path, monkeypatch):
    victims = []
    armed = _audit_point(monkeypatch, lambda *_args: victims[-1].rmdir())
    for popen_cls in (subprocess.Popen, SpawnPopen):
        victims.append(tmp_path / f"cwd-{popen_cls.__name__}")
        victims[-1].mkdir()
        armed.on = True
        assert _spawn_error(popen_cls, _probe("cwd"), cwd=victims[-1]) == \
            (FileNotFoundError, errno.ENOENT, victims[-1], os.strerror(errno.ENOENT))
        assert not victims[-1].exists()


@pytest.mark.parametrize("case", [
    "missing_cwd", "file_cwd", "missing_absolute", "missing_on_path", "not_executable", "directory",
    "unknown_format", "missing_interpreter", "path_skips_non_executable",
])
def test_parent_side_exceptions_match_popen(case, tmp_path):
    plain_file = tmp_path / "plain.txt"
    plain_file.write_text("x", encoding="utf-8")
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"\x00\x01not an executable format")
    garbage.chmod(garbage.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "script.sh"
    script.write_text("#!/nonexistent/m00-interpreter\necho hi\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "m00-tool").write_text("no execute bit", encoding="utf-8")
    (second / "m00-tool").write_bytes(garbage.read_bytes())
    (second / "m00-tool").chmod(garbage.stat().st_mode)
    args, kwargs = {
        "missing_cwd": (_probe("cwd"), {"cwd": tmp_path / "missing"}),
        "file_cwd": (_probe("cwd"), {"cwd": plain_file}),
        "missing_absolute": ([str(tmp_path / "missing-binary")], {}),
        "missing_on_path": (["m00-definitely-missing-command"], {"env": {"PATH": str(tmp_path)}}),
        "not_executable": ([str(plain_file)], {}),
        "directory": ([str(tmp_path)], {}),
        "unknown_format": ([str(garbage)], {}),
        "missing_interpreter": ([str(script)], {}),
        # PATH 上第一个候选不可执行（EACCES），第二个格式错误（ENOEXEC）：Popen 报第一个非 ENOENT 错误。
        "path_skips_non_executable": (["m00-tool"], {"env": {"PATH": f"{first}:{second}"}}),
    }[case]
    ours = _spawn_error(SpawnPopen, args, **kwargs)
    assert ours is not None and ours == _spawn_error(subprocess.Popen, args, **kwargs), ours


@pytest.mark.parametrize("option", ["preexec_fn", "umask", "close_fds", "start_and_group", "join_group"])
def test_options_outside_the_spawn_contract_use_stock_popen(option, spawn_calls):
    args, kwargs = _probe("cwd"), {}
    if option == "preexec_fn":
        kwargs["preexec_fn"] = lambda: None
    elif option == "umask":
        kwargs["umask"] = 0o22
    elif option == "close_fds":
        kwargs["close_fds"] = False
    elif option == "start_and_group":
        kwargs.update(start_new_session=True, process_group=0)
    else:
        kwargs["process_group"] = os.getpgid(0)
    ours = _result(SpawnPopen, args, **kwargs)
    assert ours == _result(subprocess.Popen, args, **kwargs)
    assert ours[0] == ("raise" if option == "start_and_group" else "ok")
    assert not spawn_calls
