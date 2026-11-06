"""SP1-W2: foreground-only, birth-checked and monotone process-tree demotion."""
from __future__ import annotations

import errno
import inspect
import json
import os
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import fields
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.tooling import process_registry as registry
from agent_py_agent.agent.tooling import shell

ROOT, CHILD = 910001, 910002


# LLM: 缺失接口在断言处报出契约缺口，不靠ImportError冒充红测，也不碰宿主真实PID。
# 函数用途: 获取本任务要求的进程树降权入口。
@pytest.fixture
def renice():
    operation = getattr(registry, "renice_process_tree", None)
    assert callable(operation), "SP1-W2 requires renice_process_tree"
    return operation


# LLM: 所有身份与调度调用均为精确替身；真实进程行为另由独立用例覆盖。
# 函数用途: 建立已验证根和后代，记录setpriority的实际调用意图。
@pytest.fixture
def tree(monkeypatch):
    identities = {ROOT: "root-birth", CHILD: "child-birth"}
    priorities = {ROOT: 5, CHILD: 5}
    snapshot = Mock(side_effect=lambda _pid: (dict(identities), True))
    monkeypatch.setattr(registry, "_process_tree_snapshot", snapshot)
    monkeypatch.setattr(registry, "_same_process", lambda pid, token: bool(token) and identities.get(pid) == token)
    monkeypatch.setattr(registry.os, "getpriority", lambda _which, pid: priorities[pid], raising=False)
    writes = Mock(side_effect=lambda _which, pid, value: priorities.__setitem__(pid, value))
    monkeypatch.setattr(registry.os, "setpriority", writes, raising=False)
    monkeypatch.setattr(registry.os, "PRIO_PROCESS", 0, raising=False)
    return SimpleNamespace(identities=identities, priorities=priorities, snapshot=snapshot, writes=writes)


# LLM: 必须复核每个成员的出生身份，只在目标nice更大时写，调用次数不充当资源状态。
# 函数用途: 验证根与子进程都实际变为目标nice且重复调用不再写。
def test_tree_demotes_root_and_child_once(renice, tree):
    assert renice(ROOT, "root-birth", 15) == 2
    assert tree.priorities == {ROOT: 15, CHILD: 15}
    assert tree.writes.call_count == 2
    assert renice(ROOT, "root-birth", 15) == 0
    assert tree.writes.call_count == 2


# LLM: 未知身份不能降级为PID存活判断；坏根在扫描前拒绝，不借扫描结果替代启动身份。
# 函数用途: 验证缺失或不匹配的出生身份不扫描、不改优先级。
@pytest.mark.parametrize("birth", [None, "", "other-birth"])
def test_unknown_or_reused_root_is_untouched(renice, tree, birth):
    assert renice(ROOT, birth, 15) == 0
    tree.snapshot.assert_not_called()
    tree.writes.assert_not_called()


# LLM: 树扫描本身可能跨过根退出/复用；扫描后的根身份仍须匹配原启动token。
# 函数用途: 反证扫描期间根PID复用不得影响任何快照成员。
def test_root_reused_during_scan_is_untouched(renice, tree):
    def scan(_pid):
        snapshot = dict(tree.identities)
        tree.identities[ROOT] = "replacement"
        return snapshot, True
    tree.snapshot.side_effect = scan
    assert renice(ROOT, "root-birth", 15) == 0
    tree.writes.assert_not_called()


# LLM: nice读取不是身份凭证；在即将写入前再次核验，子PID复用时不得使用旧快照写新进程。
# 函数用途: 验证成员在读取nice后换出生身份时被跳过。
def test_child_reused_after_priority_read_is_untouched(renice, tree, monkeypatch):
    def read(_which, pid):
        if pid == CHILD:
            tree.identities[pid] = "reused-child"
        return 5
    monkeypatch.setattr(registry.os, "getpriority", read)
    assert renice(ROOT, "root-birth", 15) == 1
    assert [call.args[1] for call in tree.writes.call_args_list] == [ROOT]


# LLM: 目标nice相等或更低都不能写，尤其不能把已经更低优先级的进程提回去。
# 函数用途: 验证已经是15或19的成员保持原值。
@pytest.mark.parametrize("current", [15, 19])
def test_never_raises_an_existing_lower_priority(renice, tree, current):
    tree.priorities.update({ROOT: current, CHILD: current})
    assert renice(ROOT, "root-birth", 15) == 0
    assert set(tree.priorities.values()) == {current}
    tree.writes.assert_not_called()


# LLM: 平台API不可用或进程退出/拒绝权限只影响尽力降权，不得使前台命令失败。
# 函数用途: 验证读取和写入nice的常见OS错误被忽略。
@pytest.mark.parametrize("stage", ["getpriority", "setpriority"])
@pytest.mark.parametrize("error", [ProcessLookupError(), PermissionError(), OSError(errno.ESRCH, "gone"), OSError(errno.EPERM, "denied")])
def test_process_exit_and_permission_errors_are_nonfatal(renice, tree, monkeypatch, stage, error):
    monkeypatch.setattr(registry.os, stage, Mock(side_effect=error))
    assert renice(ROOT, "root-birth", 15) == 0


# LLM: 不能对不支持POSIX优先级的主机调用缺失API，更不能借此改变命令终态。
# 函数用途: 验证不支持setpriority的平台直接不降权。
def test_missing_priority_api_is_a_noop(renice, tree, monkeypatch):
    monkeypatch.delattr(registry.os, "setpriority")
    assert renice(ROOT, "root-birth", 15) == 0
    tree.snapshot.assert_not_called()


# LLM: 仅替换等待时钟/采集观察，不替换被测循环；不使用真实sleep赌调度速度。
# 函数用途: 为T/2T、EOF仍活着、禁用和取消构造确定性等待输入。
def _wait_fixture(monkeypatch, *, finish_at, eof_at=None, birth="root-birth"):
    clock = SimpleNamespace(now=0.0)
    calls = []
    capture = SimpleNamespace(generation=0)
    capture.done = lambda: clock.now >= (finish_at if eof_at is None else eof_at)
    capture.wait_changed = lambda seconds, **_kwargs: setattr(clock, "now", clock.now + seconds)
    capture.finish = Mock(return_value=True)
    capture.result = lambda: ("original-out", "original-error", {"complete": True})
    monkeypatch.setattr(shell, "time", SimpleNamespace(monotonic=lambda: clock.now,
                        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    monkeypatch.setattr(shell, "capture_process_birth_token", lambda _pid: birth)
    monkeypatch.setattr(shell, "_start_foreground_capture", lambda *_args: capture)
    monkeypatch.setattr(shell, "register_cancellation_callback", lambda *_args: nullcontext())
    monkeypatch.setattr(shell, "is_interrupted", lambda: False)
    monkeypatch.setattr(shell, "cancellation_requested", lambda: False)
    receipt = registry.ProcessTerminationReceipt("already_gone", True, 7, 1)
    monkeypatch.setattr(shell, "finish_foreground_process", lambda *_args, **_kwargs:
                        (7, receipt) if clock.now >= finish_at else (None, None))
    monkeypatch.setattr(shell, "renice_process_tree", lambda *args: calls.append((clock.now, args)), raising=False)
    kill = Mock(return_value=receipt)
    monkeypatch.setattr(shell, "_kill_process_group", kill)
    return SimpleNamespace(clock=clock, capture=capture, calls=calls, kill=kill)


# LLM: API不存在时先断言契约，不以TypeError或漏跑循环冒充有效红测。
# 函数用途: 确认等待入口接受两项只供宿主透传的降权参数。
def _require_wait_contract():
    assert {"demote_after_seconds", "demoted_nice"} <= inspect.signature(shell._communicate_process).parameters.keys()


# LLM: 管道EOF不能取消自适应时钟；短命令/0关闭不触发，长命令只在T和2T各一次。
# 函数用途: 精确锁定最大两次扫描及原结果/清理事实。
@pytest.mark.parametrize("finish_at,eof_at,threshold,expected", [
    (3.2, None, 1, [1, 2]), (3.2, 0, 1, [1, 2]),
    (0.2, None, 1, []), (3.2, None, 0, []),
])
def test_wait_demotes_only_at_two_deadlines(monkeypatch, finish_at, eof_at, threshold, expected):
    _require_wait_contract()
    state = _wait_fixture(monkeypatch, finish_at=finish_at, eof_at=eof_at)
    result = shell._communicate_process(SimpleNamespace(pid=ROOT), command="foreground", timeout=10,
                                        demote_after_seconds=threshold, demoted_nice=15)
    assert [when for when, _args in state.calls] == pytest.approx(expected, abs=0.02)
    assert all(args == (ROOT, "root-birth", 15) for _when, args in state.calls)
    assert (result.returncode, result.stdout, result.stderr) == (7, "original-out", "original-error")
    assert result.termination.confirmed
    state.kill.assert_not_called()


# LLM: 出生身份未知时不启动任何扫描，保持原命令退出/清理结果的独立性。
# 函数用途: 验证缺身份的长前台命令不会调用降权。
def test_wait_without_birth_never_demotes(monkeypatch):
    _require_wait_contract()
    state = _wait_fixture(monkeypatch, finish_at=3.2, birth="")
    assert shell._communicate_process(SimpleNamespace(pid=ROOT), command="unknown", timeout=10,
                                      demote_after_seconds=1).returncode == 7
    assert state.calls == []


# LLM: 取消和超时仍先于降权，沿用原终止、管道排空及错误对象。
# 函数用途: 验证自适应循环不会吞掉或延迟前台取消/超时。
@pytest.mark.parametrize("interrupted", [False, True])
def test_timeout_and_cancel_keep_original_cleanup(monkeypatch, interrupted):
    _require_wait_contract()
    state = _wait_fixture(monkeypatch, finish_at=5)
    if interrupted:
        monkeypatch.setattr(shell, "is_interrupted", lambda: state.clock.now >= 0.2)
    error = shell.CommandInterruptedError if interrupted else shell.CommandTimeoutError
    with pytest.raises(error):
        shell._communicate_process(SimpleNamespace(pid=ROOT), command="bounded", timeout=0.5,
                                   demote_after_seconds=1)
    state.kill.assert_called_once_with(SimpleNamespace(pid=ROOT), "root-birth")
    assert state.capture.finish.called and state.calls == []


# LLM: 只对本用例创建并冻结出生身份的独立进程树执行nice变更；失败也使用同身份清理。
# 函数用途: 用真实父子进程读回OS优先级，区分长命令、短命令和0禁用。
@pytest.mark.skipif(not hasattr(os, "setpriority"), reason="POSIX priority API required")
@pytest.mark.parametrize("threshold,duration,demoted", [(1, 1.4, True), (5, 0.05, False), (0, 1.3, False)])
def test_real_foreground_parent_and_child_priorities(threshold, duration, demoted):
    _require_wait_contract()
    measured = (
        "import json,os,time; "
        "before=os.getpriority(os.PRIO_PROCESS,0); "
        f"time.sleep({duration!r}); "
        "print(json.dumps({'role':'child','before':before,'after':os.getpriority(os.PRIO_PROCESS,0)}),flush=True)"
    )
    program = (
        "import json,os,subprocess,sys,time\n"
        "before=os.getpriority(os.PRIO_PROCESS,0)\n"
        f"child=subprocess.Popen([sys.executable,'-c',{measured!r}])\n"
        f"time.sleep({duration!r})\n"
        "print(json.dumps({'role':'root','before':before,'after':os.getpriority(os.PRIO_PROCESS,0)}),flush=True)\n"
        "child.wait()\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", program], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    birth = registry.capture_process_birth_token(proc.pid)
    try:
        assert birth
        result = shell._communicate_process(proc, command="real-parent-and-child", timeout=10,
                                            demote_after_seconds=threshold, demoted_nice=15)
        assert result.returncode == 0 and result.stderr == ""
        rows = {row["role"]: row for line in result.stdout.splitlines() if (row := json.loads(line))}
        assert set(rows) == {"root", "child"}
        for row in rows.values():
            if demoted:
                assert row["after"] >= max(row["before"], 15), rows
            else:
                assert row["after"] == row["before"], rows
        assert result.termination.confirmed
    finally:
        if proc.poll() is None:
            registry.terminate_process_tree(proc.pid, proc, expected_birth_token=birth)
        proc.wait(timeout=5)


# LLM: 参数只来自ShellToolOptions，两个前台调用点均须原样透传；0保持旧调用形状，后台/PTY不借此接线。
# 函数用途: 反证配置虽已保存但前台等待仍只吃默认0的漏接线。
@pytest.mark.parametrize("sandboxed", [True, False])
@pytest.mark.parametrize("threshold", [0, 7])
def test_foreground_paths_plumb_options(tmp_path, monkeypatch, sandboxed, threshold):
    assert {"demote_after_seconds", "demoted_nice"} <= {item.name for item in fields(shell.ShellToolOptions)}
    tool = shell.ShellTool(tmp_path, options=shell.ShellToolOptions(demote_after_seconds=threshold, demoted_nice=13))
    assert (tool.demote_after_seconds, tool.demoted_nice) == (threshold, 13)
    proc = SimpleNamespace(pid=ROOT)
    completed = subprocess.CompletedProcess("command", 0, "same-output", "")
    communicate = Mock(return_value=completed)
    monkeypatch.setattr(shell, "_communicate_process", communicate)
    if sandboxed:
        monkeypatch.setattr(shell, "_sandbox_exec", lambda *_args, **_kwargs: (["synthetic"], False))
        monkeypatch.setattr(shell, "_start_attempt_shell_process", lambda *_args: proc)
        result = shell._run_attempt_sandboxed_shell_command(tool, "command", tmp_path, 5, None, None, None)
    else:
        monkeypatch.setattr(shell.os, "name", "nt")
        monkeypatch.setattr(shell, "_subprocess_text_env", lambda *_args, **_kwargs: {})
        monkeypatch.setattr(shell.subprocess, "Popen", lambda *_args, **_kwargs: proc)
        result = shell._run_shell_command(tool, "command", tmp_path, 5, None, None, None)
    expected = {"command": "command", "timeout": 5}
    if threshold:
        expected.update(demote_after_seconds=threshold, demoted_nice=13)
    communicate.assert_called_once_with(proc, **expected)
    assert result is completed


# LLM: 两个键是数值部署项，不是新布尔开关；默认值和严格类型/范围均应来自同一配置权威。
# 函数用途: 验证新增配置默认及直接构造的边界拒绝。
@pytest.mark.parametrize("key,default,upper", [
    ("tool_shell_demote_after_seconds", 1, 3600), ("tool_shell_demoted_nice", 15, 19),
])
def test_config_defaults_and_strict_bounds(key, default, upper):
    assert key in {item.name for item in fields(AgentConfig)}
    assert getattr(AgentConfig(), key) == default
    for valid in (0, upper):
        assert getattr(AgentConfig(**{key: valid}), key) == valid
    for invalid in (-1, upper + 1, True, 1.5, "1"):
        with pytest.raises(ValueError, match=key):
            AgentConfig(**{key: invalid})
