# LLM: 此层纯读包内 wheel，复用公共 ZIP 预算与 PyPA 规则；不安装、导入或查询索引，调用方仍须原管理授权。
# 模块用途: 在准备独立环境前验证固定依赖集合、元数据、平台和成员内容，失败不产生部分可运行环境。

from __future__ import annotations

import base64
import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from email.parser import BytesParser
from pathlib import PurePosixPath

from packaging.markers import default_environment
from packaging.metadata import Metadata
from packaging.requirements import Requirement
from packaging.tags import parse_tag, sys_tags
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from .plugin_manifest import PluginPackageError, PluginWheel
from .plugin_package import (
    PackageReadLimits,
    PluginPackageSnapshot,
    archive_path_key,
    inspect_plugin_package,
    read_zip_member,
    validate_zip_directory,
    validate_zip_members,
)

_WHEEL_LIMITS = PackageReadLimits(members=16384, directory_bytes=4 * 1024 * 1024)


# LLM: 摘要属于归档原文，安装器可重写的 RECORD/脚本须由安装校验区别处理；不能把摘要当授权。
# 类用途: 保存一个 wheel 文件的相对名称、大小和内容指纹，供安装前后核对。
@dataclass(frozen=True)
class WheelFile:
    path: str
    size: int
    sha256: str


# LLM: 元数据只保留不可变值，源字节不从用户路径重读；requirements 由 packaging 解析，不能自写版本比较。
# 类用途: 将一个已验证 wheel 的字节、发行身份和文件清单交给独立环境安装器。
@dataclass(frozen=True)
class PluginWheelSnapshot:
    declaration: PluginWheel
    content: bytes
    name: str
    version: str
    dist_info: str
    purelib: bool
    requirements: tuple[str, ...]
    extras: frozenset[str]
    files: tuple[WheelFile, ...]
    entry_points: bytes


# LLM: 同时检查外包和内部 wheel，固定集合闭合后才返回；平台始终取当前宿主解释器，不接受包自报环境。
# 函数用途: 在任何 venv/pip 进程启动前确定所有本地依赖可在本机共同安装。
def inspect_plugin_wheels(
    package: PluginPackageSnapshot, *, limits: PackageReadLimits = _WHEEL_LIMITS,
) -> tuple[PluginWheelSnapshot, ...]:
    verified = inspect_plugin_package(package.archive_bytes)
    if verified.manifest != package.manifest:
        raise PluginPackageError("package_integrity", "插件包与固定描述不一致。")
    supported = frozenset(sys_tags())
    environment = default_environment()
    wheels = []
    total = 0
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as outer:
        for declaration in package.manifest.wheels:
            content = read_zip_member(outer, outer.getinfo(declaration.path), limits.archive_bytes)
            wheel = _inspect_wheel(declaration, content, supported, environment, limits)
            total += sum(member.size for member in wheel.files)
            if total > limits.expanded_bytes:
                raise PluginPackageError("package_limit", "插件依赖集合超过展开预算。")
            wheels.append(wheel)
    _validate_dependencies(tuple(wheels), environment)
    return tuple(wheels)


# LLM: 第三方元数据解析可抛聚合异常，统一脱敏；不捕获 KeyboardInterrupt/SystemExit，不将坏元数据视为无依赖。
# 函数用途: 验证单个 wheel 文件名、平台、标准元数据及完整成员，保留稳定错误分类。
def _inspect_wheel(declaration, content, supported, environment, limits) -> PluginWheelSnapshot:
    try:
        filename = PurePosixPath(declaration.path).name
        _check_tag_budget("-".join(filename.removesuffix(".whl").rsplit("-", 3)[-3:]), limits.members)
        name, version, _, tags = parse_wheel_filename(filename)
        if not tags & supported:
            raise PluginPackageError("wheel_platform", "插件依赖不支持当前 Python 或平台。")
        validate_zip_directory(content, limits)
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            members = validate_zip_members(archive.infolist(), limits, allow_directories=True)
            dist_info = _dist_info(members, name, version)
            metadata = Metadata.from_email(
                read_zip_member(archive, members[f"{dist_info}/METADATA"], limits.manifest_bytes),
                validate=True,
            )
            _validate_metadata(metadata, name, version, environment)
            purelib = _wheel_headers(archive, members, dist_info, tags, limits)
            files = _verify_record(archive, members, dist_info, limits)
            entry = members.get(f"{dist_info}/entry_points.txt")
            entry_points = read_zip_member(archive, entry, limits.manifest_bytes) if entry else b""
        return PluginWheelSnapshot(
            declaration, content, str(name), str(version), dist_info, purelib,
            tuple(str(item) for item in metadata.requires_dist or ()),
            frozenset(canonicalize_name(item) for item in metadata.provides_extra or ()),
            files, entry_points,
        )
    except PluginPackageError:
        raise
    except Exception as exc:
        raise PluginPackageError("invalid_wheel", "插件 wheel 内容或标准元数据无效。") from exc


# LLM: 仅一个与文件名一致的标准 dist-info 是发行身份来源；不能用任意目录的 METADATA 覆盖它。
# 函数用途: 找到 wheel 的唯一元数据目录并检查三个必需文件。
def _dist_info(members, name, version) -> str:
    roots = {path.split("/", 1)[0] for path in members if path.split("/", 1)[0].endswith(".dist-info")}
    if len(roots) != 1:
        raise ValueError("wheel 必须有唯一 dist-info")
    root = roots.pop()
    distribution, release = root.removesuffix(".dist-info").rsplit("-", 1)
    if canonicalize_name(distribution) != name or Version(release) != version:
        raise ValueError("wheel 目录身份不符")
    if any(f"{root}/{filename}" not in members for filename in ("METADATA", "WHEEL", "RECORD")):
        raise ValueError("wheel 元数据缺失")
    return root


# LLM: 所有直接 URL 都拒绝，包含未激活 marker；完整闭包由后续纯函数核对，不依赖 pip 自动补齐。
# 函数用途: 对齐文件名与标准发行信息，检查 Python 版本和不允许联网的依赖声明。
def _validate_metadata(metadata, name, version, environment) -> None:
    if canonicalize_name(metadata.name) != name or metadata.version != version:
        raise ValueError("wheel 身份不一致")
    if metadata.requires_python and not metadata.requires_python.contains(
        environment["python_full_version"], prereleases=True,
    ):
        raise PluginPackageError("wheel_python", "插件依赖要求不同的 Python 版本。")
    if any(requirement.url is not None for requirement in metadata.requires_dist or ()):
        raise PluginPackageError("wheel_url_dependency", "插件依赖不得声明直接 URL。")


# LLM: 只解析 WHEEL 协议，不执行入口；版本主号与根布局必须明确，标签与文件名保持一致。
# 函数用途: 确定安装到 purelib 或 platlib，并拒绝会让预检与安装器解释不同的头部。
def _wheel_headers(archive, members, dist_info, tags, limits) -> bool:
    headers = BytesParser().parsebytes(
        read_zip_member(archive, members[f"{dist_info}/WHEEL"], limits.manifest_bytes),
    )
    if len(headers.get_all("Wheel-Version", [])) != 1 or len(headers.get_all("Root-Is-Purelib", [])) != 1:
        raise ValueError("wheel 头部缺失或重复")
    version = Version(headers["Wheel-Version"])
    purelib = headers["Root-Is-Purelib"]
    declared_tags = set()
    for value in headers.get_all("Tag", []):
        _check_tag_budget(value, limits.members)
        declared_tags.update(parse_tag(value))
        if len(declared_tags) > limits.members:
            raise PluginPackageError("package_limit", "插件兼容标签超过读取预算。")
    if version.major != 1 or purelib not in {"true", "false"} or declared_tags != tags:
        raise ValueError("wheel 格式或标签不符")
    return purelib == "true"


# LLM: packaging 展开压缩标签会做笛卡尔积；解析前先限制组合数，规则本身仍由公共库解释，不自建平台枚举。
# 函数用途: 防止很短的标签声明在纯预检中展开为过量对象。
def _check_tag_budget(value: str, maximum: int) -> None:
    parts = value.split("-")
    if len(parts) != 3:
        raise ValueError("wheel 标签格式无效")
    count = 1
    for part in parts:
        count *= len(part.split("."))
        if count > maximum:
            raise PluginPackageError("package_limit", "插件兼容标签超过读取预算。")


# LLM: RECORD 自身不带摘要，签名文件可不在表内；其它普通成员逐一核对安全摘要与大小，不能跳过 CRC 读取。
# 函数用途: 对完整 wheel 建立可复核文件清单，拒绝越界、遗漏和伪造的安装记录。
def _verify_record(archive, members, dist_info, limits) -> tuple[WheelFile, ...]:
    record_path = f"{dist_info}/RECORD"
    raw = read_zip_member(archive, members[record_path], limits.manifest_bytes).decode("utf-8")
    rows = list(csv.reader(io.StringIO(raw, newline=""), strict=True))
    if any(len(row) != 3 for row in rows) or len({row[0] for row in rows}) != len(rows):
        raise ValueError("wheel RECORD 格式无效")
    records = {row[0]: (row[1], row[2]) for row in rows}
    files = {name: item for name, item in members.items() if not item.is_dir()}
    signatures = {f"{record_path}.jws", f"{record_path}.p7s"}
    reserved = {archive_path_key(f"{dist_info}/{name}") for name in ("INSTALLER", "REQUESTED", "direct_url.json")}
    if any(archive_path_key(name) in reserved for name in files):
        raise ValueError("wheel 不得预置宿主安装器事实")
    if set(records) - set(files) or set(files) - set(records) - signatures:
        raise ValueError("wheel RECORD 清单不一致")
    if records.get(record_path) != ("", ""):
        raise ValueError("RECORD 不得声明自身摘要")
    verified = []
    for name, member in files.items():
        content = read_zip_member(archive, member, limits.member_bytes)
        if name != record_path and name not in signatures:
            verify_wheel_record_digest(content, *records[name])
        verified.append(WheelFile(name, len(content), hashlib.sha256(content).hexdigest()))
    return tuple(verified)


# LLM: 支持标准库提供的定长强摘要，不限制为单一发行工具格式；低于 SHA-256 强度或编码不符必须拒绝。
# 函数用途: 根据 RECORD 的摘要算法和长度核对一个成员，不读取外部文件。
def verify_wheel_record_digest(content: bytes, encoded: str, size: str) -> None:
    algorithm, expected = encoded.split("=", 1)
    digest = hashlib.new(algorithm, content)
    if digest.digest_size < 32 or algorithm.lower() in {"md5", "sha1"}:
        raise ValueError("wheel 摘要算法不足")
    actual = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    if actual != expected or int(size) != len(content):
        raise ValueError("wheel RECORD 摘要或大小不符")


# LLM: 所有供应的 wheel 都会安装，故各自基础依赖都要检查；extras 只沿明确需求闭包传播，循环依赖以已处理集合终止。
# 函数用途: 验证本地集合满足当前平台的全部有效依赖，缺包、重复版本或 extras 冲突均明确失败。
def _validate_dependencies(wheels: tuple[PluginWheelSnapshot, ...], environment: dict) -> None:
    available = {wheel.name: wheel for wheel in wheels}
    if len(available) != len(wheels):
        raise PluginPackageError("wheel_duplicate", "同一发行名称不能提供多个 wheel。")
    pending = [(wheel.name, "") for wheel in wheels]
    visited = set()
    while pending:
        name, extra = pending.pop()
        if (name, extra) in visited:
            continue
        visited.add((name, extra))
        for raw in available[name].requirements:
            requirement = Requirement(raw)
            if requirement.marker and not requirement.marker.evaluate({**environment, "extra": extra}):
                continue
            dependency = available.get(canonicalize_name(requirement.name))
            if dependency is None or not requirement.specifier.contains(dependency.version, prereleases=True):
                raise PluginPackageError("wheel_dependency", "本地 wheel 集合缺少依赖或存在版本冲突。")
            extras = {canonicalize_name(item) for item in requirement.extras}
            if not extras <= dependency.extras:
                raise PluginPackageError("wheel_extra", "本地依赖未声明所需的 extras。")
            pending.extend((dependency.name, requested) for requested in extras)
