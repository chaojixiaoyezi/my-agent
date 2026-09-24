# LLM: 打开前的路径校验只读不写：先经宿主逐次读取上下文授权，再用 SDK no-follow 从文件系统根逐段打开确认是
#   单链接普通文件（符号链接、硬链接、目录、FIFO 一律拒绝），最后拒绝带可执行权限或可执行/脚本类扩展名的文件。
#   校验到调用打开程序之间仍有极短窗口，这是"交给系统默认程序"固有的边界，README 如实说明。
# 模块用途: 把用户给的工作区路径变成可以安全交给 open / xdg-open 的绝对路径。

from __future__ import annotations

import os
import stat
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_readonly_file_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

from .commands import DesktopError

# 系统默认程序会"运行"而不是"查看"这些文件：应用包与安装包（app/pkg/dmg…）、终端/Shell 脚本（command/sh…）、
# Windows 可执行与脚本（exe/bat/ps1…）、AppleScript 与自动操作（scpt/workflow…）、Java 包，以及会跳转或启动程序的
# 快捷方式（desktop/webloc/url…）。这是拒绝清单而非允许清单：其余文档类文件交给系统按类型打开。
BLOCKED_SUFFIXES = frozenset({
    ".app", ".pkg", ".mpkg", ".dmg", ".command", ".tool", ".terminal", ".sh", ".bash", ".zsh", ".csh", ".ksh",
    ".fish", ".bat", ".cmd", ".com", ".exe", ".msi", ".ps1", ".vbs", ".wsf", ".scr", ".jar", ".scpt", ".scptd",
    ".applescript", ".workflow", ".action", ".desktop", ".webloc", ".inetloc", ".fileloc", ".url", ".lnk",
})


# LLM: lexical 路径用于 no-follow 打开，resolve 只在 SDK check 内做权限判断；不能先 resolve 擦掉路径链上的链接。
#   失败分别抛 INVALID_PATH / 读取裁决码 / FILE_NOT_FOUND / BLOCKED_FILE_TYPE；链接与非普通文件由 SDK 抛 NoFollowPathError，
#   由协议入口转成 UNSAFE_PATH。fd 在全部分支关闭，本函数无副作用。
# 函数用途: 校验一个工作区文件可以交给系统默认程序打开，返回它的绝对路径。
def openable_file(context: WorkspaceReadContext, value: str) -> Path:
    if not value or ".." in Path(value).parts:
        raise DesktopError("INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    target = context.cwd / value
    decision = context.check(target)
    if not decision.allowed:
        raise DesktopError(decision.code, "目标不在本次允许读取的工作区范围内。")
    if target.suffix.lower() in BLOCKED_SUFFIXES:
        raise DesktopError("BLOCKED_FILE_TYPE", f"拒绝打开 {target.suffix} 文件：这类文件会被系统当作程序运行。")
    try:
        descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    except FileNotFoundError as exc:
        raise DesktopError("FILE_NOT_FOUND", "文件不存在。") from exc
    try:
        mode = os.fstat(descriptor).st_mode
    finally:
        os.close(descriptor)
    if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        raise DesktopError("BLOCKED_FILE_TYPE", "拒绝打开带可执行权限的文件：它可能被系统当作程序运行。")
    return target
