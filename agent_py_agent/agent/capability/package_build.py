# LLM: 能力包（plugin_package.v7）与文件型插件包（v6，声明订阅/权限时 v8）的唯一打包实现：清单、摘要、ZIP 成员与复验都在这里，
#   scripts/build_capability_package.py、scripts/build_plugin_files_package.py 只是读文件 + 调这里 + 写产物的薄壳，
#   learnpack 的 package_build 工具也调这里。构建不执行、不导入任何随包文件，不安装、不改安装表；同一输入得到同一包字节。
#   改清单字段、成员顺序、时间戳或权限位会改变包 sha256，须同步 test_package_build 的固定摘要用例和两个脚本的测试。
# 模块用途: 把声明和已读出的文件字节打成可复现的包字节，并用宿主同一个读包校验器复验，失败不产出。
from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..capability_package_manifest import (
    CAPABILITY_PACKAGE_SCHEMA,
    MAX_CAPABILITY_FILES,
    validate_capability_path,
)
from ..common.nofollow_fs import read_bytes_beneath
from ..plugin_manifest import PLUGIN_PACKAGE_SCHEMA_V6, PLUGIN_PACKAGE_SCHEMA_V8
from ..plugin_package import PackageReadLimits, inspect_plugin_package

KIND_CAPABILITY_PACK = "capability_pack"
KIND_PLUGIN = "plugin"
# 能力包声明必须恰好包含这些字段（schema_version、package_kind、文件摘要由构建生成，声明不能自带）。
_PACK_FIELDS = frozenset({"plugin_id", "version", "summary", "capability", "files", "settings_schema"})
# 声明里出现这些键就打成 v8（事件订阅、收紧钩子、权限需求），否则 v6。
_V8_KEYS = frozenset({"events", "tool_gates", "permissions"})
# ZIP 成员固定时间戳：同一输入得到同一包字节。
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


# LLM: 可预期的构建失败，code 是登记过的错误码（PACKAGE_BUILD_*），message 是给人看的中文原因；继承 ValueError
#   以保持两个脚本原有的异常类型。
# 类用途: 打包失败时携带结构化错误码。
class PackageBuildError(ValueError):
    # 函数用途: 记下错误码与中文原因。
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# LLM: 已复验的包字节与身份；sha256 只证明完整性，不证明作者可信或允许执行。files 是包内成员路径（不含 plugin.json）。
# 类用途: 一次成功打包的结果。
@dataclass(frozen=True)
class BuiltPackage:
    kind: str
    package_id: str
    version: str
    payload: bytes
    sha256: str
    files: tuple[str, ...]


# LLM: 逐个按"不跟随任何链接"读取根目录下的成员，单文件与总量都受 PackageReadLimits 约束；路径先过能力资源路径规则。
#   只读文件，不判断调用方有没有读权限——权限由调用方（工具层的路径策略）先裁决。
# 函数用途: 把声明里列出的相对路径读成 {路径: 字节}，供 build_* 使用。
def read_declared_files(root: Path, paths: list[str], limits: PackageReadLimits | None = None) -> dict[str, bytes]:
    limits = limits or PackageReadLimits()
    resolved, contents, total = Path(root).resolve(), {}, 0
    for path in paths:
        validate_capability_path(path)
        if path in contents:
            raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", "能力资源重复")
        try:
            content = read_bytes_beneath(resolved, PurePosixPath(path).parts,
                                         max_bytes=min(limits.member_bytes, limits.expanded_bytes - total))
        except (OSError, ValueError) as exc:
            raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", "能力资源必须是预算内且无链接的普通文件") from exc
        if content is None:
            raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", "能力资源不存在")
        total += len(content)
        if len(content) > limits.member_bytes or total > limits.expanded_bytes:
            raise PackageBuildError("PACKAGE_BUILD_FILE_INVALID", "能力资源超过读取预算")
        contents[path] = content
    return contents


# LLM: 声明字段必须恰好是 _PACK_FIELDS，files 每项只写 path，数量 1..MAX_CAPABILITY_FILES；只看结构，不读文件。纯函数。
# 函数用途: 校验能力包声明的外形并返回要读的文件路径（按声明顺序）。
def capability_pack_paths(declaration: object) -> list[str]:
    if not isinstance(declaration, dict) or set(declaration) != _PACK_FIELDS:
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "能力包声明字段不完整或含生成字段")
    items = declaration["files"]
    if (not isinstance(items, list) or not 1 <= len(items) <= MAX_CAPABILITY_FILES
            or any(not isinstance(item, dict) or set(item) != {"path"} for item in items)):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "能力包 files 每项只声明 path")
    return [item["path"] for item in items]


# LLM: contents 的键必须与声明的 files 一致（通常来自 read_declared_files）；清单补上协议版本、包类型和逐文件摘要（无执行位）。
#   成功返回已复验的包，失败抛 PackageBuildError 或校验器的 PluginPackageError。纯函数。
# 函数用途: 打一个能力包（v7 纯内容包）。
def build_capability_pack(declaration: dict, contents: dict[str, bytes]) -> BuiltPackage:
    if capability_pack_paths(declaration) != list(contents):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "能力包 files 与读到的文件不一致")
    manifest = {**declaration, "schema_version": CAPABILITY_PACKAGE_SCHEMA, "package_kind": "capability",
                "files": [{"path": path, "sha256": hashlib.sha256(content).hexdigest(), "executable": False}
                          for path, content in sorted(contents.items())]}
    members = {"plugin.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), 0o644),
               **{path: (content, 0o644) for path, content in contents.items()}}
    # 能力包所有成员（含 plugin.json）整体按名字排序写入，与历史产物逐字节一致。
    return _finish(KIND_CAPABILITY_PACK, _zip(sorted(members), members), require_entry=False)


# LLM: 声明不能自带 schema_version；platforms 在声明与参数二选一；files 每项只写 path 与 executable（摘要由构建生成）。
#   只看结构，不读文件。纯函数。
# 函数用途: 校验文件型插件声明的外形并返回要读的文件路径（按声明顺序）。
def files_plugin_paths(declaration: object, platforms: tuple[str, ...] = ()) -> list[str]:
    if not isinstance(declaration, dict) or "schema_version" in declaration:
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "声明必须是对象，且不能自带协议版本")
    if platforms and "platforms" in declaration:
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "平台只能在声明或 --platform 中二选一")
    items = declaration.get("files")
    if not isinstance(items, list) or any(not isinstance(item, dict) or set(item) != {"path", "executable"} for item in items):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "files 每项只能写 path 与 executable，摘要由脚本生成")
    return [item["path"] for item in items]


# LLM: contents 的键必须与声明的 files 一致；带 events/tool_gates/permissions 任一键时打成 v8（空订阅也照 v8 校验），
#   否则 v6；产物必须是带入口的文件型插件包。纯函数。
# 函数用途: 打一个文件型插件包（任意语言，v6/v8）。
def build_files_plugin(declaration: dict, contents: dict[str, bytes], platforms: tuple[str, ...] = ()) -> BuiltPackage:
    if sorted(files_plugin_paths(declaration, platforms)) != sorted(contents):
        raise PackageBuildError("PACKAGE_BUILD_DECLARATION_INVALID", "插件 files 与读到的文件不一致")
    items = declaration["files"]
    manifest = {"panels": [], "skills": [], "host_api": [], **_subscription_fields(declaration), **declaration,
                "files": [{**item, "sha256": hashlib.sha256(contents[item["path"]]).hexdigest()} for item in items]}
    if platforms:
        manifest["platforms"] = list(platforms)
    executables = {item["path"] for item in items if item["executable"] is True}
    members = {"plugin.json": (json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode(), 0o644),
               **{path: (contents[path], 0o755 if path in executables else 0o644) for path in contents}}
    # 插件包先写 plugin.json，其余按名字排序，与历史产物逐字节一致。
    return _finish(KIND_PLUGIN, _zip(["plugin.json", *sorted(contents)], members), require_entry=True)


# LLM: 显式键选择 v8，空订阅也须校验而不降级；缺省新字段不放宽未知字段。纯函数。
# 函数用途: 为插件声明选择 v6/v8，并补齐可省略的订阅与默认断网需求。
def _subscription_fields(declaration: dict) -> dict:
    if _V8_KEYS & declaration.keys():
        return {"schema_version": PLUGIN_PACKAGE_SCHEMA_V8, "events": [], "tool_gates": [], "permissions": {}}
    return {"schema_version": PLUGIN_PACKAGE_SCHEMA_V6}


# LLM: 成员按调用方给的顺序写（两种包的历史顺序不同，改了会变 sha256）；不写目录成员（宿主读包器拒绝目录项），
#   权限位只方便解压查看，宿主以清单里的 executable 为准。纯函数。
# 函数用途: 以固定时间戳与普通文件类型生成 ZIP 字节。
def _zip(order: list[str], members: dict[str, tuple[bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in order:
            content, mode = members[name]
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(info, content)
    return buffer.getvalue()


# LLM: 用宿主安装时同一个读包校验器复验产物，校验器的 PluginPackageError 原样抛出（脚本历史行为，工具层自己转码）；
#   插件包还要求有文件入口。
# 函数用途: 复验包字节并组装 BuiltPackage。
def _finish(kind: str, payload: bytes, *, require_entry: bool) -> BuiltPackage:
    manifest = inspect_plugin_package(payload).manifest
    if require_entry and manifest.entry is None:
        raise PackageBuildError("PACKAGE_BUILD_OUTPUT_INVALID", "产物不是 v6/v8 文件入口插件包")
    files = tuple(item.path for item in manifest.files)
    return BuiltPackage(kind, manifest.plugin_id, manifest.version, payload, hashlib.sha256(payload).hexdigest(), files)


__all__ = [
    "KIND_CAPABILITY_PACK",
    "KIND_PLUGIN",
    "BuiltPackage",
    "PackageBuildError",
    "build_capability_pack",
    "build_files_plugin",
    "capability_pack_paths",
    "files_plugin_paths",
    "read_declared_files",
]
