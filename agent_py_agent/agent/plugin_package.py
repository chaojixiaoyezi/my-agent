# LLM: 读取器只处理已由调用方授权的本地来源，不安装、不导入、不启动插件；完整字节快照供后续安装使用。
# 模块用途: 有界读取 ZIP、拒绝危险成员并核对 wheel 摘要，让包验证和保存不受来源文件变化影响。

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .plugin_manifest import PluginManifest, PluginPackageError


# LLM: 预算只属于本地包读取，不扩展执行权限；调用方若开放配置须同步 YAML、dataclass 与安装入口。
# 类用途: 限制归档、展开内容、单成员和描述文件占用，防止一次安装耗尽宿主内存。
@dataclass(frozen=True)
class PackageReadLimits:
    archive_bytes: int = 64 * 1024 * 1024
    expanded_bytes: int = 128 * 1024 * 1024
    member_bytes: int = 64 * 1024 * 1024
    manifest_bytes: int = 512 * 1024
    directory_bytes: int = 1024 * 1024
    members: int = 128

    # LLM: 这里不是能力路由限制，零值不能代表无限读取；预算必须是显式正整数。
    # 函数用途: 拒绝无效预算，避免布尔值或负值绕开读取上限。
    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value <= 0
            for value in (
                self.archive_bytes,
                self.expanded_bytes,
                self.member_bytes,
                self.manifest_bytes,
                self.directory_bytes,
                self.members,
            )
        ):
            raise ValueError("插件包读取预算必须是正整数")


_DEFAULT_LIMITS = PackageReadLimits()


# LLM: bytes 和 manifest 一起固定校验时内容；sha256 是完整性标识，不证明作者可信或允许执行。
# 类用途: 将已经检查过的包交给后续安装服务，后续不能再次打开原来源代替这些字节。
@dataclass(frozen=True)
class PluginPackageSnapshot:
    manifest: PluginManifest
    archive_bytes: bytes

    # LLM: 摘要始终读取本快照的全部原始字节，不依据文件名、mtime 或 manifest 自报值。
    # 函数用途: 为候选包的内容地址和重复安装判断提供稳定标识。
    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.archive_bytes).hexdigest()


# LLM: 来源路径的权限必须由宿主入口先核对；本函数仅读取普通文件，错误不打印路径或包正文。
# 函数用途: 读取一个本地候选包并返回不可变快照，不创建文件或调用插件实现。
def read_plugin_package(
    source: Path,
    *,
    limits: PackageReadLimits = _DEFAULT_LIMITS,
) -> PluginPackageSnapshot:
    try:
        content = _read_source(source, limits.archive_bytes)
    except OSError as exc:
        raise PluginPackageError("source_unavailable", "无法读取插件包来源。") from exc
    return inspect_plugin_package(content, limits=limits)


# LLM: 用非阻塞打开再按描述符确认普通文件，避免 FIFO 卡死；读取次数和字节预算不受自报大小影响。
# 函数用途: 从已经授权的来源取得一份有界字节快照，底层 I/O 异常由公开入口脱敏。
def _read_source(source: Path, limit: int) -> bytes:
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise PluginPackageError("invalid_source", "插件包来源必须是普通文件。")
        _check_limit(metadata.st_size, limit)
        return stream.read(limit + 1)


# LLM: 安装保存必须使用本次已检查的字节；不能用这个纯校验入口冒充来源路径授权或真正安装。
# 函数用途: 核对静态描述、归档结构和逐文件摘要，并保留完整包快照。
def inspect_plugin_package(
    content: bytes,
    *,
    limits: PackageReadLimits = _DEFAULT_LIMITS,
) -> PluginPackageSnapshot:
    if not isinstance(content, bytes):
        raise PluginPackageError("invalid_archive", "插件包必须是不可变字节。")
    _check_limit(len(content), limits.archive_bytes)
    _validate_zip_directory(content, limits)
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            manifest = _verified_manifest(archive, limits)
            return PluginPackageSnapshot(manifest, content)
    except (
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        EOFError,
        NotImplementedError,
        RuntimeError,
        OSError,
        UnicodeError,
        zlib.error,
    ) as exc:
        raise PluginPackageError("invalid_archive", "插件包归档损坏或使用了不支持的格式。") from exc


# LLM: 声明、归档成员和摘要必须三方精确匹配；失败时不返回部分工具目录或可安装候选。
# 函数用途: 从已预检的归档读取描述并验证全部 wheel 字节，不展开到磁盘。
def _verified_manifest(archive: zipfile.ZipFile, limits: PackageReadLimits) -> PluginManifest:
    members = _validate_members(archive.infolist(), limits)
    if "plugin.json" not in members:
        raise PluginPackageError("invalid_archive", "插件包缺少描述文件。")
    manifest = _read_manifest(_read_member(archive, members["plugin.json"], limits.manifest_bytes))
    expected = {"plugin.json", *(wheel.path for wheel in manifest.wheels)}
    if set(members) != expected:
        raise PluginPackageError("invalid_archive", "插件包成员与描述不一致。")
    for wheel in manifest.wheels:
        data = _read_member(archive, members[wheel.path], limits.member_bytes)
        if hashlib.sha256(data).hexdigest() != wheel.sha256:
            raise PluginPackageError("digest_mismatch", "插件 wheel 内容摘要不匹配。")
    return manifest


# LLM: ZipFile 构造会分配所有目录对象；先有界遍历标准 ZIP 目录，实际计数不能只信 EOCD 自报值。
# 函数用途: 在交给标准库前限制元数据内存，只接收首期协议的单卷非 ZIP64 尾部；正文仍由 zipfile 校验。
def _validate_zip_directory(content: bytes, limits: PackageReadLimits) -> None:
    end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
    if end < 0 or end + 22 > len(content):
        raise PluginPackageError("invalid_archive", "插件包 ZIP 目录无效。")
    _, disk, start_disk, disk_count, declared_count, size, offset, comment_size = (
        struct.unpack_from("<4s4H2LH", content, end)
    )
    if (
        disk
        or start_disk
        or disk_count != declared_count
        or end + 22 + comment_size != len(content)
        or (end >= 20 and content[end - 20 : end - 16] == b"PK\x06\x07")
        or declared_count == 65535
        or size == 0xFFFFFFFF
        or offset == 0xFFFFFFFF
        or offset + size != end
    ):
        raise PluginPackageError("invalid_archive", "插件包须为单卷、非 ZIP64 的标准归档。")
    _check_limit(size, limits.directory_bytes)
    _check_limit(declared_count, limits.members)
    cursor = offset
    count = 0
    while cursor < end:
        count += 1
        _check_limit(count, limits.members)
        if cursor + 46 > end or content[cursor : cursor + 4] != b"PK\x01\x02":
            raise PluginPackageError("invalid_archive", "插件包 ZIP 目录条目无效。")
        name_size, extra_size, member_comment_size, member_disk = struct.unpack_from(
            "<4H", content, cursor + 28
        )
        if member_disk or not name_size:
            raise PluginPackageError("invalid_archive", "插件包 ZIP 目录成员无效。")
        cursor += 46 + name_size + extra_size + member_comment_size
    if cursor != end or count != declared_count:
        raise PluginPackageError("invalid_archive", "插件包 ZIP 目录计数或边界不一致。")


# LLM: 校验在任何成员展开前完成；同名、大小写别名、链接和非普通文件都不能进入安装候选。
# 函数用途: 检查 ZIP 目录及总预算，返回仅含明确普通文件的精确名称表。
def _validate_members(
    items: list[zipfile.ZipInfo], limits: PackageReadLimits
) -> dict[str, zipfile.ZipInfo]:
    _check_limit(len(items), limits.members)
    result = {}
    folded = set()
    total = 0
    for item in items:
        name = item.filename
        path = PurePosixPath(name)
        mode = item.external_attr >> 16
        if (
            not name
            or name != item.orig_filename
            or name != path.as_posix()
            or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or name.casefold() in folded
            or item.is_dir()
            or item.flag_bits & 1
            or item.external_attr & 0x10
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        ):
            raise PluginPackageError("invalid_archive", "插件包包含无效、重复或非普通文件成员。")
        _check_limit(item.file_size, limits.member_bytes)
        total += item.file_size
        _check_limit(total, limits.expanded_bytes)
        result[name] = item
        folded.add(name.casefold())
    return result


# LLM: 不只相信 ZIP 自报大小；读取也加一字节硬上限，并让 zipfile 完成 CRC/重叠检测。
# 函数用途: 在预算内展开一个已验证成员，不将其写入文件系统。
def _read_member(archive: zipfile.ZipFile, member: zipfile.ZipInfo, limit: int) -> bytes:
    _check_limit(member.file_size, limit)
    with archive.open(member) as stream:
        content = stream.read(limit + 1)
    _check_limit(len(content), limit)
    if len(content) != member.file_size:
        raise PluginPackageError("invalid_archive", "插件包成员长度不一致。")
    return content


# LLM: manifest 必须是唯一键、有限数值的 UTF-8 JSON；嵌套重名同样拒绝，不能以最后一项覆盖身份。
# 函数用途: 将描述文件转换为不可变声明，并统一报告损坏描述。
def _read_manifest(content: bytes) -> PluginManifest:
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_unique_fields,
            parse_constant=_invalid_constant,
        )
        return PluginManifest.from_payload(payload)
    except (UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise PluginPackageError("invalid_manifest", "插件包描述无效。") from exc


# LLM: 字段重复不能靠 JSON 默认覆盖规则裁决，尤其不能让已校验值与展示值不一致。
# 函数用途: 拒绝每层对象中重复的 JSON 键。
def _unique_fields(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("插件包描述字段重复")
        result[key] = value
    return result


# LLM: NaN/Infinity 不属于包协议 JSON，必须在 schema 校验前拒绝。
# 函数用途: 关闭 Python JSON 解码器默认允许的非有限数值扩展。
def _invalid_constant(value: str) -> object:
    raise ValueError("插件包描述数值无效")


# LLM: 大小异常只返回稳定分类，不回显包路径或正文；不能因超限降级为无界读取。
# 函数用途: 对归档和展开预算执行同一上限检查。
def _check_limit(actual: int, limit: int) -> None:
    if actual > limit:
        raise PluginPackageError("package_limit", "插件包超过读取预算。")
