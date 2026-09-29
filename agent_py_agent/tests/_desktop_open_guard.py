"""测试会话的桌面程序防线：任何测试都不能真的打开用户的浏览器、文件、通知或剪贴板。

conftest 在会话开始时把 shim 目录放到 PATH 最前面。shim 同名替换 open、xdg-open、osascript、pbcopy 等程序，
只记录一行（令牌打码），不执行任何动作。每条测试结束时检查自己期间新增的记录，有就让该测试失败；
测试之外（后台进程晚到）的记录在会话结束时统一报出并让会话失败。
确实需要真实程序的测试必须显式加 `real_desktop_programs` marker，这时该测试的 PATH 去掉 shim、Python 内的
webbrowser 恢复原实现，期间的记录不算违规。
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

REAL_DESKTOP_MARKER = "real_desktop_programs"
# 会打开浏览器/文件/应用的程序；Python webbrowser 在 Linux/WSL 上也按这些名字查找。
OPENERS = ("open", "xdg-open", "gio", "gnome-open", "kde-open", "kde-open5", "wslview",
           "sensible-browser", "x-www-browser", "www-browser")
# 其它会在用户桌面留下可见副作用或读到用户私有数据的程序：AppleScript（macOS 的 webbrowser 就经它打开，
# 也能弹窗、控制应用）、系统通知、剪贴板读写。
DESKTOP_SIDE_EFFECTS = ("osascript", "notify-send", "terminal-notifier",
                        "pbcopy", "pbpaste", "wl-copy", "wl-paste", "xclip", "xsel")
SHIMMED_PROGRAMS = OPENERS + DESKTOP_SIDE_EFFECTS
_WEBBROWSER_FUNCTIONS = ("open", "open_new", "open_new_tab")

# shim 只追加一行制表符分隔的记录后返回 0：程序名、打码后的参数、父进程命令、当前测试名。
# 记录文件路径在安装时写死进脚本：插件 MCP 子进程经 build_safe_env 只继承 PATH 等白名单变量，不能靠环境变量找日志。
# 不读 stdin（调用方可能一直不关 stdin），不启动任何程序；令牌、密钥类参数值一律打码。
_SHIM = r"""#!/bin/sh
log=__GUARD_LOG__
redact() { sed -E 's/((token|access_token|secret|key|password)=)[^&[:space:]]+/\1<redacted>/g'; }
name=$(basename "$0")
args=$(printf '%s ' "$@" | redact | tr '\t\n' '  ' | cut -c1-240)
parent=$(ps -o command= -p "$PPID" 2>/dev/null | redact | tr '\t\n' '  ' | cut -c1-200)
printf '%s\t%s\t%s\t%s\t%s\n' "$name" "$args" "$PPID" "$parent" "${PYTEST_CURRENT_TEST:-}" >> "$log"
exit 0
"""


# 类用途: 一条被拦下的桌面程序调用。
@dataclass(frozen=True)
class GuardRecord:
    program: str
    arguments: str
    parent_pid: str
    parent_command: str
    pytest_current_test: str

    # 函数用途: 生成给失败信息用的一行说明。
    def describe(self) -> str:
        current = f"；PYTEST_CURRENT_TEST={self.pytest_current_test}" if self.pytest_current_test else ""
        return (f"{self.program} {self.arguments}".rstrip()
                + f"（调用方 pid={self.parent_pid}：{self.parent_command or '未知'}{current}）")


# 类用途: 管理 shim 目录、记录文件和“哪些记录已经归属到某条测试”的游标。
@dataclass
class DesktopOpenGuard:
    shim_dir: Path
    log: Path
    attributed: int = 0
    originals: dict[str, object] = field(default_factory=dict)

    # 函数用途: 在给定目录写出全部 shim 与空记录文件；不改动环境变量。
    @classmethod
    def install(cls, root: Path) -> DesktopOpenGuard:
        shim_dir = root / "bin"
        shim_dir.mkdir(parents=True, exist_ok=True)
        log = root / "calls.tsv"
        log.write_text("", encoding="utf-8")
        script = _SHIM.replace("__GUARD_LOG__", shlex.quote(str(log)))
        for name in SHIMMED_PROGRAMS:
            path = shim_dir / name
            path.write_text(script, encoding="utf-8")
            path.chmod(stat.S_IRWXU)
        return cls(shim_dir, log, originals={name: getattr(webbrowser, name) for name in _WEBBROWSER_FUNCTIONS})

    # 函数用途: 返回把 shim 目录放在最前面的 PATH。
    def path_with_shims(self, path: str) -> str:
        parts = [item for item in path.split(os.pathsep) if item and item != str(self.shim_dir)]
        return os.pathsep.join([str(self.shim_dir), *parts])

    # 函数用途: 返回去掉 shim 目录后的 PATH，给声明了真实调用的测试使用。
    def path_without_shims(self, path: str) -> str:
        return os.pathsep.join(item for item in path.split(os.pathsep) if item and item != str(self.shim_dir))

    # 函数用途: 生成替换 webbrowser.open* 的函数：只记录一行并返回 False，不打开任何东西。
    def python_opener(self, name: str):
        def refuse(url, *args, **kwargs):
            with self.log.open("a", encoding="utf-8") as handle:
                handle.write(f"python-webbrowser.{name}\t{_redact(str(url))[:240]}\t{os.getpid()}\t"
                             f"pytest 进程内调用\t{os.environ.get('PYTEST_CURRENT_TEST', '')}\n")
            return False
        return refuse

    # 函数用途: 返回记录文件当前的行数，作为一条测试开始时的游标。
    def mark(self) -> int:
        return len(self._lines())

    # 函数用途: 取出游标之后的新记录，并把它们标为已归属（后续会话结束检查不再重复报）。
    def take_since(self, mark: int) -> list[GuardRecord]:
        lines = self._lines()
        self.attributed = max(self.attributed, len(lines))
        return [_parse(line) for line in lines[mark:]]

    # 函数用途: 取出所有还没归属到任何测试的记录（会话结束时使用）。
    def unattributed(self) -> list[GuardRecord]:
        lines = self._lines()
        return [_parse(line) for line in lines[self.attributed:]]

    # 函数用途: 读取记录文件的全部非空行；文件被外部删掉时按空处理。
    def _lines(self) -> list[str]:
        try:
            return [line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []


# 函数用途: 把令牌、密钥类查询参数的值打码。
def _redact(text: str) -> str:
    return re.sub(r"((?:token|access_token|secret|key|password)=)[^&\s]+", r"\1<redacted>", text)


# 函数用途: 解析一行记录；字段不足时用空串补齐，不因格式问题漏报。
def _parse(line: str) -> GuardRecord:
    parts = (line.split("\t") + [""] * 5)[:5]
    return GuardRecord(*parts)


# 函数用途: 把一组记录拼成失败信息。
def failure_message(records: list[GuardRecord], where: str) -> str:
    rows = "\n".join(f"  - {record.describe()}" for record in records)
    return (f"{where}调用了会影响用户桌面的程序（已被测试防线拦下，没有真正执行）：\n{rows}\n"
            f"测试不能打开用户的浏览器、文件、通知或剪贴板；请在测试里关掉相应开关、注入假程序，"
            f"确需真实调用时显式加 @pytest.mark.{REAL_DESKTOP_MARKER}。")
