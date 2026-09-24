# LLM: 工作区只读；路径每次经宿主逐次读取上下文授权，再用 SDK no-follow 从文件系统根逐段打开，
#   读取量受 max_image_bytes 限制（同时检查 stat 与实际读取量），本模块从不写工作区。
# 模块用途: 定义本插件的业务错误，并提供授权后的有界图片字节读取。

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_readonly_file_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

_CHUNK = 65536


# LLM: code 是稳定的业务失败分类；extra 只放结构化事实（如图片元数据、已安装语言），协议入口原样并入错误结果；
#   正文是给用户看的中文说明，不含原始异常、stderr 或文件内容。
# 类用途: 让协议入口把读取或 OCR 失败返回为工具结果，不终止整个插件进程。
class ImageTextError(ValueError):
    # LLM: 本异常不表示宿主操作状态，不能用它重试已发生的调用或修改权限。
    # 函数用途: 保存错误码、中文说明和可选的附加结构化字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


# LLM: 一次读取的结构化事实；display 只用于展示，不参与授权。
# 类用途: 保存读到的图片字节、sha256 与展示路径。
@dataclass(frozen=True)
class ImageBytes:
    display: str
    content: bytes
    sha256: str


# LLM: lexical 路径用于打开，resolve 只在 SDK check 内做权限判断；不能先 resolve 擦掉路径链上的链接。
# 函数用途: 组合当前工作区路径，拒绝空路径与上溯组件，再按读取上下文检查读取资格。
def authorized_path(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise ImageTextError("INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    target = context.cwd / value
    decision = context.check(target)
    if not decision.allowed:
        raise ImageTextError(decision.code, "目标不在本次允许读取的工作区范围内。")
    return target


# LLM: 链接、多链接文件、非普通文件由 SDK no-follow 打开拒绝（抛 NoFollowPathError）；前后 fstat 身份不一致视为
#   读取期间变化；fd 在全部分支关闭。只读，无副作用。
# 函数用途: 在授权后读取整张图片（不超过上限），返回字节和 sha256。
def read_image(context: WorkspaceReadContext, value: str, *, limit: int) -> ImageBytes:
    target = authorized_path(context, value)
    try:
        descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    except FileNotFoundError as exc:
        raise ImageTextError("FILE_NOT_FOUND", "文件不存在。") from exc
    try:
        before = os.fstat(descriptor)
        if before.st_size > limit:
            raise ImageTextError("FILE_TOO_LARGE", f"图片超过读取上限 {limit} 字节（设置 max_image_bytes）。")
        pieces, total = [], 0
        while chunk := os.read(descriptor, _CHUNK):
            total += len(chunk)
            if total > limit:
                raise ImageTextError("FILE_TOO_LARGE", f"图片超过读取上限 {limit} 字节（设置 max_image_bytes）。")
            pieces.append(chunk)
        after = os.fstat(descriptor)
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, total, after.st_mtime_ns):
            raise ImageTextError("FILE_CHANGED", "文件在读取期间发生变化，请重试。")
    finally:
        os.close(descriptor)
    content = b"".join(pieces)
    display = str(target.relative_to(context.cwd)) if target.is_relative_to(context.cwd) else str(target)
    return ImageBytes(display, content, hashlib.sha256(content).hexdigest())
