# LLM: 工作区只读；每个目标先用 serve 时冻结的读取上下文 check，再用 SDK no-follow 从文件系统根逐段打开，
#   链接、多链接文件和非普通对象一律拒绝。HTTP 线程并发调用本模块，函数不保存任何跨请求状态。
# 模块用途: 定义业务错误，提供服务根目录授权、请求路径解析、目录枚举和有界/流式文件读取。

from __future__ import annotations

import os
import stat
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_directory_beneath, open_readonly_file_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

LIST_LIMIT = 2000


# LLM: status 是给 HTTP 层用的状态码（MCP 工具层只取 code/message）；正文是中文说明，不含原始异常或文件内容。
# 类用途: 让工具入口和网页请求把失败转成结构化错误，不终止插件进程或服务线程。
class BoardError(ValueError):
    # LLM: 本异常不表示宿主操作状态，不能用它重试已发生的调用或修改权限。
    # 函数用途: 保存 HTTP 状态码、稳定错误码及中文展示说明。
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


# LLM: lexical 路径用于打开，resolve 只在 SDK check 内做权限判断；根目录必须能经严格 no-follow 打开为目录，
#   打开后立即关闭，不持有 fd。只读，无副作用。
# 函数用途: serve 调用时确认要浏览的目录在本次读取范围内且是真实目录，返回其词法绝对路径。
def authorized_root(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise BoardError(400, "INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    root = context.cwd / value
    decision = context.check(root)
    if not decision.allowed:
        raise BoardError(403, decision.code, "目标不在本次允许读取的工作区范围内。")
    try:
        descriptor = open_directory_beneath(Path(root.anchor), root.parts[1:], require_dir_fd=True)
    except FileNotFoundError as exc:
        raise BoardError(404, "NOT_FOUND", "目录不存在。") from exc
    os.close(descriptor)
    return root


# LLM: 请求路径只能是 root 下的相对路径：拒绝空字节、绝对路径和 .. 组件，再用冻结上下文 check；
#   返回 (词法目标, 规范化相对路径)。不打开文件，调用方必须随后走 no-follow 打开。
# 函数用途: 把网页查询参数 p 解析为 serve 目录之下、且仍在读取范围内的目标路径。
def request_target(context: WorkspaceReadContext, root: Path, value: str) -> tuple[Path, str]:
    relative = Path(value)
    if "\x00" in value or relative.is_absolute() or ".." in relative.parts:
        raise BoardError(400, "INVALID_PATH", "路径必须是浏览目录下的相对路径，不能含 .. 或绝对路径。")
    target = root.joinpath(*relative.parts)
    decision = context.check(target)
    if not decision.allowed:
        raise BoardError(403, decision.code, "目标不在本次允许读取的范围内。")
    return target, relative.as_posix() if relative.parts else ""


# LLM: 目录 fd 经严格 no-follow 打开；子项用 lstat 语义，不跟随链接。链接/设备/多链接文件和被上下文拒绝的项
#   只标记为不可打开，不暴露内容。条目数有上限，超出时 truncated=True。只读，无副作用。
# 函数用途: 枚举一个目录的直接子项，返回按"目录在前、名称排序"的条目列表。
def list_directory(context: WorkspaceReadContext, target: Path) -> tuple[list[dict], bool]:
    descriptor = open_directory_beneath(Path(target.anchor), target.parts[1:], require_dir_fd=True)
    entries, truncated = [], False
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                if len(entries) >= LIST_LIMIT:
                    truncated = True
                    break
                info = entry.stat(follow_symlinks=False)
                directory = stat.S_ISDIR(info.st_mode)
                regular = stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                allowed = (directory or regular) and context.check(target / entry.name).allowed
                kind = "directory" if directory else "file" if regular else "other"
                entries.append({"name": entry.name, "kind": kind, "size": info.st_size, "openable": allowed})
    finally:
        os.close(descriptor)
    entries.sort(key=lambda item: (item["kind"] != "directory", item["name"]))
    return entries, truncated


# LLM: 返回的 fd 由调用方关闭；链接和非普通文件由 SDK 抛 NoFollowPathError。
# 函数用途: 以严格 no-follow 只读方式打开一个普通文件，供流式输出原始字节。
def open_file(target: Path) -> int:
    return open_readonly_file_beneath(Path(target.anchor), target.parts[1:])


# LLM: 实际读取量受 limit 限制（不只信 stat）；fd 在全部分支关闭。只读，无副作用。
# 函数用途: 读取文件开头至多 limit 字节，返回 (内容, 文件大小, 是否截断)。
def read_head(target: Path, limit: int) -> tuple[bytes, int, bool]:
    descriptor = open_file(target)
    try:
        size = os.fstat(descriptor).st_size
        pieces, total = [], 0
        while total < limit and (chunk := os.read(descriptor, min(65536, limit - total))):
            pieces.append(chunk)
            total += len(chunk)
        truncated = total < size or bool(os.read(descriptor, 1))
    finally:
        os.close(descriptor)
    return b"".join(pieces), size, truncated
