"""测试防线自检：shim 只记录不执行、令牌打码、PATH 前置与 webbrowser 替身在整个测试会话生效。"""
from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from types import SimpleNamespace

import pytest

from agent_py_agent.tests._desktop_open_guard import (
    REAL_DESKTOP_MARKER,
    SHIMMED_PROGRAMS,
    DesktopOpenGuard,
    failure_message,
)


def test_session_puts_shims_first_on_path_and_replaces_webbrowser(_desktop_open_guard):
    guard = _desktop_open_guard
    for name in ("open", "xdg-open", "osascript", "pbcopy", "notify-send"):
        assert shutil.which(name) == str(guard.shim_dir / name)
    assert webbrowser.open is not guard.originals["open"]


def test_shim_records_redacted_call_without_running_anything(tmp_path):
    guard = DesktopOpenGuard.install(tmp_path / "guard")
    assert {path.name for path in guard.shim_dir.iterdir()} == set(SHIMMED_PROGRAMS)
    mark = guard.mark()
    # 只给 PATH：插件 MCP 子进程经 build_safe_env 拿到的也只是白名单变量，shim 必须照样记录。
    env = {"PATH": guard.path_with_shims(os.environ.get("PATH", ""))}
    marker = tmp_path / "must-not-exist"
    result = subprocess.run(["open", "http://127.0.0.1:9/?token=SECRET123&x=1", str(marker)], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and not marker.exists()
    records = guard.take_since(mark)
    assert [record.program for record in records] == ["open"]
    assert "SECRET123" not in guard.log.read_text() and "token=<redacted>" in records[0].arguments
    assert records[0].parent_pid and guard.unattributed() == []
    message = failure_message(records, "测试 x ")
    assert "open" in message and REAL_DESKTOP_MARKER in message and "SECRET123" not in message


def test_python_webbrowser_stand_in_records_and_refuses(tmp_path):
    guard = DesktopOpenGuard.install(tmp_path / "guard")
    assert guard.python_opener("open_new_tab")("https://example.invalid/?token=abc") is False
    [record] = guard.take_since(0)
    assert record.program == "python-webbrowser.open_new_tab" and "abc" not in record.arguments


def test_guard_io_ignores_patched_path_builtins_and_os(tmp_path, monkeypatch):
    """测试常把 Path.read_text/open、builtins.open 或 os 函数换成哨兵或假内容；防线的读写必须碰不到它们。"""
    import builtins
    import pathlib

    guard = DesktopOpenGuard.install(tmp_path / "guard")
    touched = []

    def sentinel(*args, **kwargs):
        touched.append(args[:1])
        raise AssertionError("防线不该经过被测试替换的 IO 接口")

    for name in ("read_text", "read_bytes", "write_text", "open", "stat", "exists", "is_dir"):
        monkeypatch.setattr(pathlib.Path, name, sentinel)
    monkeypatch.setattr(builtins, "open", sentinel)
    for name in ("open", "read", "write", "fstat", "stat", "lseek", "close"):
        monkeypatch.setattr(os, name, sentinel)

    mark = guard.mark()
    assert guard.python_opener("open")("https://example.invalid/?token=abc") is False
    [record] = guard.take_since(mark)
    assert record.program == "python-webbrowser.open" and "abc" not in record.arguments
    assert guard.take_since(guard.mark()) == [] and guard.unattributed() == []
    assert touched == []


def test_half_written_line_waits_for_its_newline(tmp_path):
    guard = DesktopOpenGuard.install(tmp_path / "guard")
    with open(guard.log, "a", encoding="utf-8") as handle:
        handle.write("open\targs\t1\tparent")
    assert guard.take_since(0) == [] and guard.attributed == 0
    with open(guard.log, "a", encoding="utf-8") as handle:
        handle.write("\ttest\n")
    [record] = guard.take_since(0)
    assert (record.program, record.pytest_current_test) == ("open", "test")


@pytest.mark.parametrize("late_call", [True, False])
def test_session_end_reads_the_finished_guard(tmp_path, monkeypatch, late_call):
    """会话级 fixture 在最后一条测试收尾时已结束；会话结束检查必须还能读到它，晚到的记录照样让会话失败。"""
    import agent_py_agent.tests.conftest as hooks

    guard = DesktopOpenGuard.install(tmp_path / "guard")
    if late_call:
        guard.python_opener("open")("https://example.invalid/late")
    monkeypatch.setattr(hooks, "_DESKTOP_GUARD", None)
    monkeypatch.setattr(hooks, "_FINISHED_DESKTOP_GUARD", guard)
    lines: list[str] = []
    writer = SimpleNamespace(line=lines.append)
    session = SimpleNamespace(config=SimpleNamespace(get_terminal_writer=lambda: writer), exitstatus=0)
    hooks.pytest_sessionfinish(session, 0)
    if late_call:
        assert session.exitstatus == pytest.ExitCode.TESTS_FAILED and "python-webbrowser.open" in lines[0]
    else:
        assert session.exitstatus == 0 and lines == []


def test_real_desktop_marker_path_drops_only_the_shim_directory(tmp_path):
    guard = DesktopOpenGuard.install(tmp_path / "guard")
    shimmed = guard.path_with_shims(os.pathsep.join(["/usr/bin", "/bin"]))
    assert shimmed.split(os.pathsep)[0] == str(guard.shim_dir)
    assert guard.path_without_shims(shimmed) == os.pathsep.join(["/usr/bin", "/bin"])
    assert guard.path_with_shims(shimmed).count(str(guard.shim_dir)) == 1


@pytest.mark.real_desktop_programs
def test_marked_test_gets_real_path_and_webbrowser_without_calling_them(_desktop_open_guard):
    # 只核对放行后的环境，不真的调用任何桌面程序。
    assert str(_desktop_open_guard.shim_dir) not in os.environ["PATH"].split(os.pathsep)
    assert webbrowser.open is _desktop_open_guard.originals["open"]
