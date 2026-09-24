# LLM: 只调用本机系统程序（osascript/open/pbcopy，notify-send/xdg-open/wl-copy/xclip），不联网、不调用模型。
#   用户文本只作为独立 argv 或 stdin 传给子进程，从不拼进命令行或 AppleScript 源码；子进程不 start_new_session，
#   宿主回收插件进程组时一并回收；超时 kill 并等待退出。平台和程序缺失明确报"不可用"，不降级到别的程序。
# 模块用途: 定义本插件业务错误，按平台定位系统程序，并以超时运行通知、打开、剪贴板命令。

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# 通知脚本固定不变，从 stdin 交给 `osascript -`；标题和内容只经 argv 进入 run handler，
# AppleScript 把它们当作字符串值，引号、反斜杠、`& do shell script` 之类都不会被当成代码执行。
NOTIFY_SCRIPT = "on run argv\n\tdisplay notification (item 2 of argv) with title (item 1 of argv)\nend run\n"


# LLM: code 是稳定的业务失败分类；extra 只放结构化事实（实际程序、退出码、超时秒数），协议入口原样并入错误结果；
#   正文是给用户看的中文说明，不含 stderr 或用户文本。
# 类用途: 让协议入口把参数、路径、程序缺失或执行失败返回为工具结果，不终止整个插件进程。
class DesktopError(ValueError):
    # LLM: 本异常不表示宿主操作状态，不能用它重试已发生的调用或修改权限。
    # 函数用途: 保存错误码、中文说明和可选的附加结构化字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


# LLM: 一次要执行的完整命令；argv[0] 已是解析后的可执行文件，stdin 为 None 时子进程标准输入接空设备。
# 类用途: 在"按平台组装命令"和"运行命令"之间传递结构化事实。
@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]
    stdin: bytes | None = None


# LLM: 设置非空时只认该值（按 shutil.which 规则检查可执行），不再回退自动探测；为空时按 candidates 顺序在 PATH 查找。
#   找不到抛 DESKTOP_UNAVAILABLE，message 以"<功能>不可用："开头，调用方据 code 判断，不解析文案。
# 函数用途: 确定某项功能要调用的系统程序。
def locate(configured: str, candidates: tuple[str, ...], feature: str, setting: str) -> str:
    for name in (configured,) if configured else candidates:
        found = shutil.which(name)
        if found:
            return found
    wanted = configured or " / ".join(candidates)
    raise DesktopError("DESKTOP_UNAVAILABLE", f"{feature}不可用：未找到 {wanted}（可在设置 {setting} 指定）",
                       platform=sys.platform)


# LLM: 只有 macOS 与 Linux 有实现；其他平台明确不可用，不猜测等价程序。
# 函数用途: 对当前平台不支持的功能抛出统一的不可用错误。
def unsupported(feature: str) -> DesktopError:
    return DesktopError("DESKTOP_UNAVAILABLE", f"{feature}不可用：当前平台 {sys.platform} 不支持", platform=sys.platform)


# LLM: macOS 固定脚本走 stdin、文本走 argv；Linux notify-send 用 `--` 截断选项解析，防止以 - 开头的标题被当成参数。
# 函数用途: 组装发送系统通知的命令。
def notify_command(settings: dict, title: str, message: str) -> Command:
    if sys.platform == "darwin":
        exe = locate(settings["osascript_path"], ("osascript",), "桌面通知", "osascript_path")
        return Command((exe, "-", title, message), NOTIFY_SCRIPT.encode("utf-8"))
    if sys.platform.startswith("linux"):
        exe = locate(settings["notify_send_path"], ("notify-send",), "桌面通知", "notify_send_path")
        return Command((exe, "--", title, message))
    raise unsupported("桌面通知")


# LLM: target 必须是调用方已完成授权与文件类型校验的绝对路径；以 / 开头，不会被打开程序当成选项。
# 函数用途: 组装用系统默认程序打开文件的命令。
def open_command(settings: dict, target: Path) -> Command:
    if sys.platform == "darwin":
        names = ("open",)
    elif sys.platform.startswith("linux"):
        names = ("xdg-open",)
    else:
        raise unsupported("打开文件")
    return Command((locate(settings["open_path"], names, "打开文件", "open_path"), str(target)))


# LLM: 文本只经 stdin 传入。Linux 在 Wayland 会话优先 wl-copy，否则 xclip；xclip 需要显式选中 clipboard 选区，
#   按实际程序文件名决定是否附加该参数（设置指定的程序同样适用）。
# 函数用途: 组装把文本写入系统剪贴板的命令。
def clipboard_command(settings: dict, text: str) -> Command:
    if sys.platform == "darwin":
        names: tuple[str, ...] = ("pbcopy",)
    elif sys.platform.startswith("linux"):
        names = ("wl-copy", "xclip") if os.environ.get("WAYLAND_DISPLAY") else ("xclip", "wl-copy")
    else:
        raise unsupported("剪贴板")
    exe = locate(settings["clipboard_path"], names, "剪贴板", "clipboard_path")
    extra = ("-selection", "clipboard") if Path(exe).name.startswith("xclip") else ()
    return Command((exe, *extra), text.encode("utf-8"))


# LLM: 有副作用：启动一个系统程序（弹通知、打开应用或改剪贴板）。stdout/stderr 接空设备，避免 xclip 这类常驻子进程
#   占住管道使调用挂起；macOS 未设置 locale 时补 UTF-8，否则 pbcopy 会丢弃非 ASCII 文本。超时 kill 后 wait 回收，
#   抛 COMMAND_TIMEOUT；非零退出抛 COMMAND_FAILED；两者都带实际程序。
# 函数用途: 以超时运行一条命令，成功时返回实际程序与退出码。
def run(command: Command, timeout: int) -> dict:
    program = command.argv[0]
    env = dict(os.environ)
    if sys.platform == "darwin" and not any(env.get(key) for key in ("LC_ALL", "LC_CTYPE", "LANG")):
        env["LANG"] = "en_US.UTF-8"
    try:
        process = subprocess.Popen(command.argv, stdin=subprocess.DEVNULL if command.stdin is None else subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    except OSError as exc:
        raise DesktopError("COMMAND_FAILED", f"无法启动 {program}，请检查对应设置。", program=program) from exc
    try:
        process.communicate(command.stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise DesktopError("COMMAND_TIMEOUT", f"{program} 超过 {timeout} 秒未结束，已终止该进程。",
                           program=program, timeout_seconds=timeout) from None
    if process.returncode != 0:
        raise DesktopError("COMMAND_FAILED", f"{program} 执行失败（退出码 {process.returncode}）。",
                           program=program, exit_code=process.returncode)
    return {"program": program, "exit_code": process.returncode}
