"""browser-lite 停止浏览器时等本 profile 子进程再清 profile；用假 /proc 进程表，不启动真实浏览器。"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from scripts.build_plugin_api import ROOT

sys.path.insert(0, str(ROOT / "plugins/browser-lite/src"))
from browser_lite import launcher  # noqa: E402

_OURS = os.getpgrp()
_OTHER = _OURS + 1


# 函数用途: 在假 /proc 里登记一个进程；传一个字符串即 Chrome 改写后的单串标题，传多个即 NUL 分隔的原始 argv。
#   group=None 表示没有 stat 文件（读不到）。
def _process(proc: Path, pid: int, *argv: str, group: int | None = _OURS) -> None:
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(b"\0".join(os.fsencode(item) for item in argv) + b"\0")
    if group is not None:
        _stat(proc, pid, group)


# 函数用途: 写假 /proc/<pid>/stat；comm 在括号里，可含空格和括号，pgrp 是 ')' 之后第 3 个字段。
def _stat(proc: Path, pid: int, group: int, comm: str = "chrome") -> None:
    (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S 1 {group} {group} 0 -1 4194560 0 0\n", encoding="utf-8")


# 函数用途: 准备 profile、假 /proc 和“用户自己的 Chrome”“前缀相同的别的 profile”“读不到的进程”三类不匹配的干扰项。
def _setup(tmp_path: Path) -> tuple[Path, Path, str]:
    profile, proc = tmp_path / "data" / "profile", tmp_path / "proc"
    (profile / "Default").mkdir(parents=True)
    argument = f"--user-data-dir={profile}"
    _process(proc, 6666, "/usr/bin/google-chrome", "--user-data-dir=/home/user/.config/google-chrome")
    _process(proc, 5555, f"/opt/google/chrome/chrome --type=utility {argument}2 --lang=en-US")
    _process(proc, 5556, "/opt/google/chrome/chrome", f"{argument}/nested")
    (proc / "7777").mkdir()
    return profile, proc, argument


# 函数用途: 构造主进程已退出的浏览器对象，让 stop 直接进入等子进程和清理阶段。
def _exited_browser(profile: Path) -> launcher.BrowserProcess:
    browser = launcher.BrowserProcess(Path("/bin/true"), profile, 1.0)
    browser.process = SimpleNamespace(poll=lambda: 0)
    return browser


def test_stop_waits_for_helpers_that_outlive_main_without_killing(tmp_path, monkeypatch):
    profile, proc, argument = _setup(tmp_path)
    _process(proc, 4321, f"/opt/google/chrome/chrome --type=utility --utility-sub-type=network.mojom.NetworkService {argument}")
    # argv 匹配但不在插件进程组：照样要等，只是不能杀。
    _process(proc, 4323, f"/opt/google/chrome/chrome --type=zygote {argument}", group=_OTHER)
    kills = []
    monkeypatch.setattr(launcher, "_PROC_ROOT", proc)
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))

    # CI 上实际残留的就是网络服务在主进程退出后写下的这个文件，连同被它重新建出的 Default 目录。
    def helpers_exit_late():
        time.sleep(0.3)
        (profile / "Default").mkdir(parents=True, exist_ok=True)
        (profile / "Default" / "Network Persistent State").write_text("{}", encoding="utf-8")
        shutil.rmtree(proc / "4321")
        time.sleep(0.2)
        shutil.rmtree(proc / "4323")

    writer = threading.Thread(target=helpers_exit_late)
    started = time.monotonic()
    writer.start()
    _exited_browser(profile).stop()
    writer.join()
    assert time.monotonic() - started >= 0.45
    assert kills == []
    assert list(profile.iterdir()) == []


def test_stop_sigkills_only_same_group_exact_helpers_after_timeout(tmp_path, monkeypatch):
    profile, proc, argument = _setup(tmp_path)
    _process(proc, 4321, "/opt/google/chrome/chrome", "--type=utility", argument)
    # comm 含空格和括号，还伪造了一段“S 1 999”：必须从最后一个 ')' 之后取 pgrp。
    _process(proc, 4322, f"/opt/google/chrome/chrome --type=renderer {argument} --lang=en-US", group=None)
    _stat(proc, 4322, _OURS, "x) S 1 999 (y")
    _process(proc, 4323, f"/opt/google/chrome/chrome --type=zygote {argument}", group=_OTHER)
    _process(proc, 4324, "/opt/google/chrome/chrome", argument, group=None)
    _process(proc, 4325, "grep", "--", argument, group=_OTHER)
    _process(proc, 4326, f"/usr/bin/google-chrome {argument} backup", group=_OTHER)
    (profile / "Default" / "Network Persistent State").write_text("{}", encoding="utf-8")
    kills = []

    def kill(pid, sig):
        kills.append((pid, sig))
        shutil.rmtree(proc / str(pid))

    monkeypatch.setattr(launcher, "_PROC_ROOT", proc)
    monkeypatch.setattr(launcher, "_HELPER_EXIT_SECONDS", 0.2)
    monkeypatch.setattr(launcher.os, "kill", kill)
    started = time.monotonic()
    _exited_browser(profile).stop()
    assert sorted(kills) == [(4321, signal.SIGKILL), (4322, signal.SIGKILL)]
    assert list(profile.iterdir()) == []
    assert time.monotonic() - started < 3


def test_stop_without_proc_clears_immediately(tmp_path, monkeypatch):
    profile, _proc, _argument = _setup(tmp_path)
    (profile / "Default" / "Preferences").write_text("{}", encoding="utf-8")
    kills = []
    monkeypatch.setattr(launcher, "_PROC_ROOT", tmp_path / "no-proc")
    monkeypatch.setattr(launcher.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    started = time.monotonic()
    _exited_browser(profile).stop()
    assert kills == [] and list(profile.iterdir()) == []
    assert time.monotonic() - started < 1
