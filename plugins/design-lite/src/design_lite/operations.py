# LLM: 两个工具的文件流程，都会写工作区：create 只用写入上下文；edit 先经读取上下文 + SDK no-follow 读回，
#   再经写入上下文 check → anchor 结果必须等于 lexical 路径 → write_bytes_atomic_beneath 原子替换。
#   不在这里决定模板或字段内容（见 templates.py），也不从 cwd 以外补路径根。
# 模块用途: 实现生成设计文件和修改设计文件字段的具体步骤与结果结构。

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import (
    NoFollowPathError,
    open_readonly_file_beneath,
    write_bytes_atomic_beneath,
)
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext
from my_agent_plugin_api.workspace_write_context import WorkspaceWriteContext

from .templates import (
    DesignError,
    apply_fields,
    field_value,
    locate_fields,
    normalize_color,
    render,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
_CHUNK = 65536


# LLM: 只做字面检查（非空、无 NUL、无 .. 组件、后缀 .html），返回 lexical 绝对路径；授权由调用方的上下文 check 裁决。
# 函数用途: 把用户给的路径拼到当前工作目录下并做基本合法性检查。
def lexical_target(cwd: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise DesignError("INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    target = cwd / value
    if target.suffix.lower() != ".html":
        raise DesignError("INVALID_PATH", "设计文件必须以 .html 结尾。")
    return target


# LLM: 目标相对 cwd 时返回相对路径，否则返回绝对路径；只用于展示，不参与授权。
# 函数用途: 生成结果里给用户看的路径。
def display_path(cwd: Path, target: Path) -> str:
    return str(target.relative_to(cwd)) if target.is_relative_to(cwd) else str(target)


# LLM: 有副作用：原子写入工作区文件（父目录不存在时按 0o755 创建）。check 拒绝时透传宿主同源错误码；
#   anchor 结果与 lexical 路径不同说明路径链含链接，按不安全路径拒绝。
# 函数用途: 经写入上下文裁决后把内容安全写到目标路径。
def write_checked(write: WorkspaceWriteContext, target: Path, content: bytes, mode: int) -> None:
    decision = write.check(target)
    if not decision.allowed:
        raise DesignError(decision.code, "目标不在本次允许写入的工作区范围内。")
    root, parts = write.anchor(target)
    if root.joinpath(*parts) != target:
        raise NoFollowPathError("write anchor differs from lexical target")
    write_bytes_atomic_beneath(root, parts, content, directory_mode=0o755, file_mode=mode)


# LLM: 有副作用：写工作区。已存在（含链接、目录）默认拒绝；overwrite=True 时仍由 SDK 拒绝替换链接或非普通文件。
#   存在检查与替换之间不加锁，并发创建同名文件属已知的尽力检查。
# 函数用途: 按模板生成一个新的 HTML 设计文件。
def create_design(write: WorkspaceWriteContext, version: str, arguments: dict) -> dict:
    target = lexical_target(write.cwd, arguments["output"])
    decision = write.check(target)
    if not decision.allowed:
        raise DesignError(decision.code, "目标不在本次允许写入的工作区范围内。")
    text = render(arguments["template"], version, arguments["title"], arguments.get("subtitle") or "",
                  arguments.get("color"))
    existed = os.path.lexists(target)
    if existed and not arguments["overwrite"]:
        raise DesignError("OUTPUT_EXISTS", "输出文件已存在；如确认要覆盖，请加 --overwrite。")
    content = text.encode("utf-8")
    write_checked(write, target, content, 0o644)
    fields = locate_fields(text)
    return {"path": display_path(write.cwd, target), "template": arguments["template"], "overwritten": existed,
            "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
            "fields": {name: field_value(name, found) for name, found in fields.items() if found}}


# LLM: 从文件系统根逐段 no-follow 打开（链接、硬链接、非普通文件由 SDK 拒绝），读取量有界，fd 在全部分支关闭。
# 函数用途: 读取工作区设计文件的字节和权限位。
def read_design(target: Path) -> tuple[bytes, int]:
    try:
        descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    except FileNotFoundError as exc:
        raise DesignError("FILE_NOT_FOUND", "文件不存在。") from exc
    try:
        mode = os.fstat(descriptor).st_mode & 0o7777
        pieces, total = [], 0
        while chunk := os.read(descriptor, _CHUNK):
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise DesignError("FILE_TOO_LARGE", f"文件超过 {MAX_FILE_BYTES} 字节上限，不像 design-lite 生成的文件。")
            pieces.append(chunk)
        return b"".join(pieces), mode
    finally:
        os.close(descriptor)


# LLM: 有副作用：覆盖工作区文件并保持原权限位。只认本插件生成的文件，只替换请求的已声明字段，其余字节不变；
#   读回与写回之间不做比较交换，期间他人改动会被覆盖（与 savepoint-lite 的 expect 不同，这里是已知限制）。
# 函数用途: 修改设计文件的标题、副标题或颜色，并返回每个字段的新旧值。
def edit_design(read: WorkspaceReadContext, write: WorkspaceWriteContext, arguments: dict) -> dict:
    changes = {name: arguments[name] for name in ("title", "subtitle", "color") if arguments.get(name) is not None}
    if not changes:
        raise DesignError("NO_FIELDS", "至少要给出 --title、--subtitle、--color 中的一个。")
    if "color" in changes:
        changes["color"] = normalize_color(changes["color"])
    target = lexical_target(read.cwd, arguments["path"])
    decision = read.check(target)
    if not decision.allowed:
        raise DesignError(decision.code, "目标不在本次允许读取的工作区范围内。")
    content, mode = read_design(target)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DesignError("NOT_DESIGN_FILE", "文件不是 UTF-8 文本，不是 design-lite 生成的文件。") from exc
    found = locate_fields(text)
    missing = [name for name in changes if not found[name]]
    if missing:
        raise DesignError("FIELD_MISSING", f"文件里没有可修改的字段：{'、'.join(missing)}。", fields=missing)
    report = [{"field": name, "old": field_value(name, found[name]), "new": value} for name, value in changes.items()]
    updated = apply_fields(text, found, changes).encode("utf-8")
    write_checked(write, target, updated, mode)
    for item in report:
        item["changed"] = item["old"] != item["new"]
    return {"path": display_path(read.cwd, target), "fields": report, "bytes": len(updated),
            "sha256": hashlib.sha256(updated).hexdigest()}
