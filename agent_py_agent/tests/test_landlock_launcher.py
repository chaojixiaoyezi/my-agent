"""G5（Gateway 本机信任第 (1) 层，Linux Landlock 启动器）。

两类用例：
- 平台无关（任意平台可跑）：系统调用号查表、启动器 argv 解析、启动器失败 fail-closed 不 exec / 成功才 exec、
  _wrap_with_landlock 按就绪与否包启动器或记 unavailable、非 Linux 的就绪探测返回 NOT_LINUX。
- 仅 Linux（车道真跑）：就绪探测在车道为 ready；经启动器施加 Landlock 后，子进程连被拒端口 EACCES、连其它端口通、
  IPv4 映射也被拒（Landlock 按端口不看地址）；unavailable 分支命令照跑。

安全要点：apply_connect_tcp_deny / restrict_self 会永久限制调用进程，所以所有“真施加”只在子进程里做（经启动器），
绝不在 pytest 进程里调。真实施加只连本进程起的随机端口临时监听，不碰任何真实服务。
"""

from __future__ import annotations

import errno
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.attempt import landlock_launcher as ll
from agent_py_agent.agent.attempt.sandbox import (
    AttemptExecutionSandbox,
    AttemptSandboxSpec,
    landlock_net_readiness,
)

IS_LINUX = sys.platform.startswith("linux")
needs_linux = pytest.mark.skipif(not IS_LINUX, reason="Linux Landlock")
_LAUNCHER = Path(ll.__file__)


# ------------------------------------------------------------ 平台无关


def test_syscall_numbers_known_and_unknown():
    assert ll.syscall_numbers("x86_64") == (444, 445, 446)
    assert ll.syscall_numbers("aarch64") == (444, 445, 446)
    assert ll.syscall_numbers("mips") is None


@pytest.mark.parametrize("argv,ports,command", [
    (["--deny", "8420", "--", "bwrap", "x"], {8420}, ["bwrap", "x"]),
    (["--deny", "8420,9000", "--", "cmd"], {8420, 9000}, ["cmd"]),
])
def test_parse_launcher_argv_valid(argv, ports, command):
    assert ll.parse_launcher_argv(argv) == (ports, command)


@pytest.mark.parametrize("argv", [
    [], ["--deny", "8420"], ["--deny", "8420", "--"],  # 缺命令
    ["nope", "8420", "--", "cmd"], ["--deny", "abc", "--", "cmd"],  # 格式/非数字
    ["--deny", "", "--", "cmd"],  # 空 --deny：什么都不拒，按参数不对
    ["--deny", "70000", "--", "cmd"], ["--deny", "-1", "--", "cmd"],  # 越界端口
    ["--deny", "8420", "9000", "--", "cmd"],  # --deny 后多于一个参数
])
def test_parse_launcher_argv_bad(argv):
    assert ll.parse_launcher_argv(argv)[0] is None


def test_main_fail_closed_on_apply_error(monkeypatch, capsys):
    execd = []
    monkeypatch.setattr(ll.os, "execv", lambda *a: execd.append(a))
    monkeypatch.setattr(ll, "apply_connect_tcp_deny", lambda *a, **k: (_ for _ in ()).throw(ll.LandlockApplyError("X")))
    assert ll.main(["--deny", "8420", "--", "/bin/true"]) == ll.APPLY_FAILED_EXIT_CODE
    assert execd == [], "施加失败必须 fail-closed，绝不 exec"
    # stderr 第一行是带 error_code 的结构化标记（机器可识别）
    assert ll.is_apply_failed(ll.APPLY_FAILED_EXIT_CODE, capsys.readouterr().err)


def test_main_fail_closed_on_unavailable(monkeypatch):
    execd = []
    monkeypatch.setattr(ll.os, "execv", lambda *a: execd.append(a))
    monkeypatch.setattr(ll, "apply_connect_tcp_deny", lambda *a, **k: (_ for _ in ()).throw(ll.LandlockUnavailable("Y")))
    assert ll.main(["--deny", "8420", "--", "/bin/true"]) == ll.APPLY_FAILED_EXIT_CODE
    assert execd == []


def test_main_fail_closed_on_bad_argv(monkeypatch):
    execd = []
    monkeypatch.setattr(ll.os, "execv", lambda *a: execd.append(a))
    assert ll.main(["garbage"]) == ll.BAD_ARGV_EXIT_CODE
    assert execd == []


def test_is_apply_failed_only_matches_marker_and_code():
    import json as _json
    marker = _json.dumps({"error_code": ll.APPLY_FAILED_ERROR_CODE, "reason": "X"})
    assert ll.is_apply_failed(ll.APPLY_FAILED_EXIT_CODE, marker + "\n其它行")
    assert not ll.is_apply_failed(0, marker)                       # 退出码不对
    assert not ll.is_apply_failed(ll.APPLY_FAILED_EXIT_CODE, "命令自己的报错")  # 没有标记
    assert not ll.is_apply_failed(ll.APPLY_FAILED_EXIT_CODE, "")


def test_main_execs_command_on_success(monkeypatch):
    execd = []
    monkeypatch.setattr(ll, "apply_connect_tcp_deny", lambda *a, **k: None)
    monkeypatch.setattr(ll.os, "execv", lambda path, args: execd.append((path, args)))
    ll.main(["--deny", "8420", "--", "/bin/echo", "hi"])
    assert execd == [("/bin/echo", ["/bin/echo", "hi"])]


def _spec(tmp_path: Path, ports: tuple[int, ...]) -> AttemptSandboxSpec:
    root = tmp_path / "s"
    root.mkdir(exist_ok=True)
    return AttemptSandboxSpec(attempt_view=root, staging_root=root, shared_workspace=root,
                             owner_home=root, deny_gateway_ports=ports)


def test_wrap_with_landlock_wraps_when_ready(tmp_path, monkeypatch):
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, (8420,)))
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.landlock_net_readiness",
                        lambda _p: (True, "LANDLOCK_READY"))
    wrapped = sandbox._wrap_with_landlock(["bwrap", "--ro-bind", "/", "/"])
    assert wrapped[0] == sys.executable and wrapped[1] == "-I" and wrapped[2] == str(_LAUNCHER)
    assert wrapped[3:6] == ["--deny", "8420", "--"]
    assert wrapped[6:] == ["bwrap", "--ro-bind", "/", "/"]


def test_wrap_with_landlock_plain_when_unavailable(tmp_path, monkeypatch):
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, (8420,)))
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.landlock_net_readiness",
                        lambda _p: (False, "LANDLOCK_NET_UNSUPPORTED"))
    base = ["bwrap", "--ro-bind", "/", "/"]
    assert sandbox._wrap_with_landlock(base) == base


def test_wrap_with_landlock_noop_without_ports(tmp_path):
    sandbox = AttemptExecutionSandbox(_spec(tmp_path, ()))
    base = ["bwrap", "x"]
    assert sandbox._wrap_with_landlock(base) is base
    assert sandbox.gateway_isolation_fact() == "not_applicable"


def test_isolation_status_linux_follows_readiness(monkeypatch):
    # Linux 的 isolation 事实跟 landlock_net_readiness 走，和 _wrap 的包裹决定同一个探测（就绪 applied、否则 unavailable:<原因>）。
    from agent_py_agent.agent.attempt.sandbox import gateway_isolation_status

    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.landlock_net_readiness",
                        lambda _p: (True, "LANDLOCK_READY"))
    assert gateway_isolation_status((8420,), system="Linux") == "applied"
    monkeypatch.setattr("agent_py_agent.agent.attempt.sandbox.landlock_net_readiness",
                        lambda _p: (False, "BWRAP_SETUID"))
    assert gateway_isolation_status((8420,), system="Linux") == "unavailable:BWRAP_SETUID"


def test_readiness_not_linux_off_linux():
    if IS_LINUX:
        pytest.skip("本用例验证非 Linux 的短路")
    assert landlock_net_readiness(None) == (False, "NOT_LINUX")


# ------------------------------------------------------------ 仅 Linux（车道真跑）

_CONNECT = (
    "import json,socket,sys\n"
    "def c(h,p):\n"
    " f=socket.AF_INET6 if ':' in h else socket.AF_INET\n"
    " s=socket.socket(f,socket.SOCK_STREAM); s.settimeout(2)\n"
    " try:\n  s.connect((h,p)); return {'ok':True}\n"
    " except OSError as e: return {'ok':False,'errno':e.errno}\n"
    " finally: s.close()\n"
    "b,o=int(sys.argv[1]),int(sys.argv[2])\n"
    "print(json.dumps({'blocked':c('127.0.0.1',b),'blocked_mapped':c('::ffff:127.0.0.1',b),'other':c('127.0.0.1',o)}))\n"
)


def _listener() -> tuple[socket.socket, int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    threading.Thread(target=lambda: [srv.accept()[0].close() for _ in range(20)], daemon=True).start()
    return srv, srv.getsockname()[1]


@needs_linux
def test_landlock_readiness_ready_on_lane():
    ready, reason = landlock_net_readiness(None)
    if not ready:
        pytest.skip(f"车道 Landlock 不就绪：{reason}")
    assert reason == "LANDLOCK_READY"


@needs_linux
def test_launcher_denies_blocked_port_allows_others():
    ready, reason = landlock_net_readiness(None)
    if not ready:
        pytest.skip(f"车道 Landlock 不就绪：{reason}")
    blocked_srv, blocked = _listener()
    other_srv, other = _listener()
    try:
        argv = [sys.executable, str(_LAUNCHER), "--deny", str(blocked), "--",
                sys.executable, "-c", _CONNECT, str(blocked), str(other)]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        report = json.loads(out.stdout.strip())
    finally:
        blocked_srv.close()
        other_srv.close()
    assert report["blocked"]["ok"] is False and report["blocked"]["errno"] == errno.EACCES
    assert report["blocked_mapped"]["ok"] is False, "IPv4 映射也要被拒（Landlock 按端口不看地址）"
    assert report["other"]["ok"] is True, "其它端口照常可连"


@needs_linux
def test_launcher_restriction_reaches_through_bwrap():
    ready, reason = landlock_net_readiness(None)
    if not ready:
        pytest.skip(f"车道 Landlock 不就绪：{reason}")
    from agent_py_agent.agent.tooling.sandbox import find_bwrap

    bwrap = find_bwrap()
    if not bwrap:
        pytest.skip("车道没有 bwrap")
    blocked_srv, blocked = _listener()
    other_srv, other = _listener()
    try:
        inner = [sys.executable, "-c", _CONNECT, str(blocked), str(other)]
        bwrap_argv = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
                      "--share-net", "--unshare-pid", "--die-with-parent", "--", *inner]
        argv = [sys.executable, str(_LAUNCHER), "--deny", str(blocked), "--", *bwrap_argv]
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        report = json.loads(out.stdout.strip())
    finally:
        blocked_srv.close()
        other_srv.close()
    assert report["blocked"]["ok"] is False and report["blocked"]["errno"] == errno.EACCES
    assert report["other"]["ok"] is True


def test_shell_maps_launcher_apply_failure_to_host_error_code(tmp_path):
    """启动器 fail-closed（Landlock 施加失败）时，shell 工具要把它标成 GATEWAY_ISOLATION_APPLY_FAILED，
    不和命令自己的非零退出码混淆（ae 复审）。"""
    import subprocess
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.shell import _run_shell_process_text

    marker = json.dumps({"error_code": ll.APPLY_FAILED_ERROR_CODE, "reason": "RESTRICT_SELF_FAILED"})
    fake_result = subprocess.CompletedProcess(["bwrap"], ll.APPLY_FAILED_EXIT_CODE, "", marker + "\n")
    tool = SimpleNamespace(max_output_chars=4000, _run_command=lambda *a, **k: fake_result)

    _output, succeeded, error_code, facts = _run_shell_process_text(
        tool, "python3 -c pass", tmp_path, 30, None, None, None)
    assert succeeded is False
    assert error_code == "GATEWAY_ISOLATION_APPLY_FAILED"
    assert facts["return_code"] == ll.APPLY_FAILED_EXIT_CODE


def test_shell_leaves_ordinary_nonzero_as_command_failed(tmp_path):
    import subprocess
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling.shell import _run_shell_process_text

    fake_result = subprocess.CompletedProcess(["x"], 1, "", "ordinary failure")
    tool = SimpleNamespace(max_output_chars=4000, _run_command=lambda *a, **k: fake_result)
    _output, succeeded, error_code, _facts = _run_shell_process_text(
        tool, "false", tmp_path, 30, None, None, None)
    assert succeeded is False and error_code == "COMMAND_FAILED"
