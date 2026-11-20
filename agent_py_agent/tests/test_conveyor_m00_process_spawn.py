# LLM: Conveyor M00 / R3-G0：SpawnPopen 在 Darwin/Linux 上用 posix_spawn(/bin/sh 跳板) 代替 fork 大进程，
#   可观察行为必须与 subprocess.Popen 一致：退出码、管道、communicate、cwd、env（逐字节，含壳层会改写的变量与
#   SIP 会剥掉的 DYLD_*）、start_new_session/process_group、pass_fds；子进程 fd 恰为 {0,1,2} ∪ pass_fds（父进程
#   同时开着 sqlite、socket、管道与可继承高位 fd）；父进程侧缺 cwd/可执行文件的异常与 Popen 同类型同字段；
#   _execute_child 签名与 3.14 钉住的一致。close_fds=False 的普通 Popen 对照必须在同一 fd 断言上失败。
# 模块用途: 固定 agent/common/process_spawn.py 的派生合同（C1.5），只起本机短命子进程，不读 git 历史。
from __future__ import annotations

import inspect
import json
import os
import signal
import socket
import sqlite3
import stat
import subprocess
import sys

import pytest

from agent_py_agent.agent.common import process_spawn
from agent_py_agent.agent.common.process_spawn import SpawnPopen

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="posix_spawn 路径只在 Darwin/Linux 启用")

# 3.14 POSIX Popen._execute_child 的参数名（C1.5 钉住；3.11–3.14 同形）。
PINNED_EXECUTE_CHILD_PARAMETERS = (
    "self", "args", "executable", "preexec_fn", "close_fds", "pass_fds", "cwd", "env", "startupinfo",
    "creationflags", "shell", "p2cread", "p2cwrite", "c2pread", "c2pwrite", "errread", "errwrite",
    "restore_signals", "gid", "gids", "uid", "umask", "start_new_session", "process_group",
)
# 父进程里复制出的可继承高位 fd 号（两位数，覆盖 dash 只认一位 fd 重定向的边界）。
HIGH_INHERITABLE_FD = 87
# 每个子进程的等待上限秒数。
CHILD_TIMEOUT_SECONDS = 20

# 子进程探针：按 argv[1] 报告 fd 集合、环境、cwd、会话/进程组或回显 stdin。-I -S 保证解释器启动不额外开文件。
_CHILD = r'''
import json, os, sys
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
    print(json.dumps(sorted((k.decode("latin-1"), v.decode("latin-1")) for k, v in os.environb.items())))
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


# 函数用途: 运行后返回抛出的父进程侧异常的可比较形态（类型、errno、filename、strerror）。
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


# 函数用途: 记录 posix_spawn 调用的探针，透传真实调用。
@pytest.fixture
def spawn_calls(monkeypatch):
    calls = []
    real = process_spawn._posix_spawn

    def spy(path, argv, env, **kwargs):
        calls.append((path, list(argv), env, kwargs))
        return real(path, argv, env, **kwargs)

    monkeypatch.setattr(process_spawn, "_posix_spawn", spy)
    return calls


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
    assert len(spawn_calls) == 5
    assert all(call[0] == process_spawn._SHELL and call[2] == {} for call in spawn_calls)


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
    # 探针按 latin-1 逐字节解码环境，这里按同一规则还原期望值，比较的是原始字节。
    for key, value in env.items():
        assert seen.get(os.fsencode(key).decode("latin-1")) == os.fsencode(value).decode("latin-1"), key
    # SHLVL 缺省时壳层不得替子进程补出来；inherit（env=None）时与父进程环境一致。
    assert "SHLVL" not in seen
    assert _run(SpawnPopen, _probe("env")) == _run(subprocess.Popen, _probe("env"))
    assert len(spawn_calls) == 2


def test_cwd_forms_match_popen(tmp_path, monkeypatch, spawn_calls):
    real_dir = tmp_path / "real dir"
    (real_dir / "-").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real_dir, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    for cwd in (real_dir, str(real_dir), os.fsencode(real_dir), "real dir", "real dir/-", "link", "link/../link"):
        ours = _run(SpawnPopen, _probe("cwd"), cwd=cwd, env={"CDPATH": str(tmp_path)})
        assert ours == _run(subprocess.Popen, _probe("cwd"), cwd=cwd, env={"CDPATH": str(tmp_path)}), cwd
        assert ours[0] == 0
    assert len(spawn_calls) == 7


def test_new_session_process_group_and_argv0(spawn_calls):
    pid, sid, pgid, argv0 = json.loads(_run(SpawnPopen, _probe("ids"), start_new_session=True)[1])
    assert sid == pid == pgid and sid != os.getsid(0)
    assert argv0 == sys.executable
    pid, sid, pgid, _argv0 = json.loads(_run(SpawnPopen, _probe("ids"), process_group=0)[1])
    assert pgid == pid and sid == os.getsid(0)
    shell_path = {"PATH": "/bin:/usr/bin"}
    assert _run(SpawnPopen, ["sh", "-c", 'echo "$0"'], env=shell_path) == \
        _run(subprocess.Popen, ["sh", "-c", 'echo "$0"'], env=shell_path) == (0, b"sh\n", b"")
    assert spawn_calls[0][3]["setsid"] is True
    assert spawn_calls[1][3]["setpgroup"] == 0
    assert {signal.SIGPIPE, signal.SIGXFSZ} <= set(spawn_calls[0][3]["setsigdef"])


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
    assert len(spawn_calls) == 1


@pytest.mark.parametrize("shell", ["default", "/bin/dash"])
def test_child_fds_are_exactly_std_plus_pass_fds(shell, tmp_path, monkeypatch, spawn_calls):
    if shell != "default":
        if not os.path.exists(shell):
            pytest.skip(f"{shell} not installed")
        monkeypatch.setattr(process_spawn, "_SHELL", shell)
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
        assert len(spawn_calls) == 2
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


@pytest.mark.parametrize("case", [
    "missing_cwd", "file_cwd", "missing_absolute", "missing_on_path", "not_executable", "directory",
    "unknown_format", "missing_interpreter",
])
def test_parent_side_exceptions_match_popen(case, tmp_path, spawn_calls):
    plain_file = tmp_path / "plain.txt"
    plain_file.write_text("x", encoding="utf-8")
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"\x00\x01not an executable format")
    garbage.chmod(garbage.stat().st_mode | stat.S_IXUSR)
    script = tmp_path / "script.sh"
    script.write_text("#!/nonexistent/m00-interpreter\necho hi\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    args, kwargs = {
        "missing_cwd": (_probe("cwd"), {"cwd": tmp_path / "missing"}),
        "file_cwd": (_probe("cwd"), {"cwd": plain_file}),
        "missing_absolute": ([str(tmp_path / "missing-binary")], {}),
        "missing_on_path": (["m00-definitely-missing-command"], {"env": {"PATH": str(tmp_path)}}),
        "not_executable": ([str(plain_file)], {}),
        "directory": ([str(tmp_path)], {}),
        "unknown_format": ([str(garbage)], {}),
        "missing_interpreter": ([str(script)], {}),
    }[case]
    ours = _spawn_error(SpawnPopen, args, **kwargs)
    assert ours is not None and ours == _spawn_error(subprocess.Popen, args, **kwargs), ours
    assert not spawn_calls


@pytest.mark.parametrize("option", ["preexec_fn", "umask", "close_fds", "explicit_executable", "start_and_group"])
def test_options_outside_the_spawn_contract_use_stock_popen(option, spawn_calls):
    args, kwargs = _probe("cwd"), {}
    if option == "preexec_fn":
        kwargs["preexec_fn"] = lambda: None
    elif option == "umask":
        kwargs["umask"] = 0o22
    elif option == "close_fds":
        kwargs["close_fds"] = False
    elif option == "explicit_executable":
        args = ["m00-argv0-alias", *args[1:]]
        kwargs["executable"] = sys.executable
    else:
        kwargs.update(start_new_session=True, process_group=0)
    ours = _result(SpawnPopen, args, **kwargs)
    assert ours == _result(subprocess.Popen, args, **kwargs)
    assert ours[0] == ("raise" if option == "start_and_group" else "ok")
    assert not spawn_calls
