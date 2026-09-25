# LLM: v6 非 Python 包的环境准备只做"核对平台与运行时指纹 → 按摘要排他解包 → 设权限 → 读回复核"，绝不执行包内文件
#   或解释器；候选目录、配额、期限、原操作授权与 Python 包共用同一套机制（plugin_environment），不另建状态账。
#   改动须同步 plugin_runtime_facts、plugin_runtime、plugin_skills 与 test_plugin_any_language。
# 模块用途: 把随包文件安全地放进插件的独立环境目录，供启用验收和后续启动使用。

from __future__ import annotations

import hashlib
import io
import os
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .common.nofollow_fs import open_directory_beneath, read_bytes_beneath
from .plugin_entry import FILES_DIRECTORY, PluginFile
from .plugin_environment import (
    EnvironmentBuildLimits,
    PreparedPluginEnvironment,
    check_environment_candidate,
    create_environment_candidate,
)
from .plugin_environment_process import (
    EnvironmentPreparationError,
    PluginEnvironmentOperation,
    check_preparation_deadline,
)
from .plugin_manifest import PluginPackageError
from .plugin_package import (
    PackageReadLimits,
    PluginPackageSnapshot,
    read_zip_member,
    validate_zip_members,
)
from .plugin_runtime_facts import PluginRuntimeError, resolve_plugin_runtime, write_runtime_pin
from .user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .user_space.owner_resolver import OwnerHomeResult

_EXECUTABLE_MODE = 0o500
_DATA_MODE = 0o400


# LLM: 内容来自已核对摘要的安装快照；shebang 只用于确认回执展示，不参与任何机器判断。
# 类用途: 保存一个随包文件的声明与字节。
@dataclass(frozen=True)
class PluginFileSnapshot:
    declaration: PluginFile
    content: bytes = field(repr=False)

    # LLM: 只取以 #! 开头的首行（最多 200 字符、去控制字符），让用户在确认时看到脚本会用什么启动；不解析、不执行。
    # 函数用途: 返回可展示的 shebang 行，没有则为空串。
    @property
    def shebang(self) -> str:
        if not self.content.startswith(b"#!"):
            return ""
        line = self.content.split(b"\n", 1)[0][:200].decode("utf-8", "replace")
        return "".join(char for char in line if ord(char) >= 32)


# LLM: 只读安装快照里的 ZIP 字节，复用包读取器的成员校验与预算；摘要不符整包拒绝。
# 函数用途: 取出并核对 v6 包声明的全部随包文件。
def inspect_plugin_files(package: PluginPackageSnapshot) -> tuple[PluginFileSnapshot, ...]:
    limits = PackageReadLimits()
    with zipfile.ZipFile(io.BytesIO(package.archive_bytes)) as archive:
        members = validate_zip_members(archive.infolist(), limits)
        return tuple(_verified_file(archive, members[item.path], item, limits) for item in package.manifest.files)


# LLM: 有界读取单个成员并按声明摘要核对；不符时整包拒绝，不返回部分文件。
# 函数用途: 读出一个随包文件并确认内容与声明一致。
def _verified_file(archive: zipfile.ZipFile, member: zipfile.ZipInfo, item: PluginFile,
                   limits: PackageReadLimits) -> PluginFileSnapshot:
    data = read_zip_member(archive, member, limits.member_bytes)
    if hashlib.sha256(data).hexdigest() != item.sha256:
        raise PluginPackageError("digest_mismatch", "插件包成员内容摘要不匹配。")
    return PluginFileSnapshot(item, data)


# LLM: 与 Python 包同一计划、候选地址、配额与期限；运行时事实重新解析后必须等于计划指纹（解释器被替换即拒绝）。
# 函数用途: 为非 Python 包准备独立环境：解包随包文件并在解释器类型下写定位文件。有副作用：创建环境目录与文件。
def prepare_files_environment(
    owner: OwnerHomeResult, package: PluginPackageSnapshot, operation: PluginEnvironmentOperation,
    *, limits: EnvironmentBuildLimits,
) -> PreparedPluginEnvironment:
    deadline = time.monotonic() + limits.timeout_seconds
    check_preparation_deadline(deadline)
    if os.name != "posix":
        raise EnvironmentPreparationError("environment_platform")
    operation.authorize()
    plan = operation.plan
    if operation.owner != owner or package.sha256 != plan.package_sha256 or package.manifest.plugin_id != plan.plugin_id:
        raise EnvironmentPreparationError("environment_plan_conflict")
    try:
        facts = resolve_plugin_runtime(package.manifest, deadline=deadline)
    except PluginRuntimeError as exc:
        raise EnvironmentPreparationError("environment_" + exc.reason) from exc
    if facts.fingerprint != plan.interpreter_fingerprint:
        raise EnvironmentPreparationError("environment_interpreter_changed")
    files = inspect_plugin_files(package)
    reserve = 1024 * 1024 + sum(2 * len(item.content) + 1024 for item in files)
    if reserve > limits.max_bytes:
        raise EnvironmentPreparationError("environment_budget")
    candidate = owner.plugins_dir / "environments" / plan.environment_ref
    with owner_quota_enforcer_from_policy(owner.home_dir, quota_path=owner.quota_json).admission(blocking=False) as quota:
        quota.check((OwnerQuotaChange(candidate, reserve),))
        check_preparation_deadline(deadline)
        operation.authorize()
        descriptor = create_environment_candidate(owner, plan.environment_ref)
        try:
            _write_files(candidate, files, deadline)
            write_runtime_pin(candidate, facts)
            _verify_files(candidate, files, deadline)
            check_environment_candidate(candidate, descriptor, reserve, deadline)
            operation.authorize()
        finally:
            os.close(descriptor)
    return PreparedPluginEnvironment(plan.environment_ref, package.sha256, plan.interpreter_fingerprint,
                                     f"{FILES_DIRECTORY}/{package.manifest.entry.command}", ())


# LLM: 目录逐段 no-follow 创建；文件排他创建且不跟随链接，写完设为只读（可执行文件另给执行位），不留可被改写的副本。
# 函数用途: 把随包文件写进候选的 files 子目录。有副作用：写文件。
def _write_files(candidate: Path, files: tuple[PluginFileSnapshot, ...], deadline: float) -> None:
    for item in files:
        check_preparation_deadline(deadline)
        *parents, name = (FILES_DIRECTORY, *item.declaration.path.split("/"))
        parent = open_directory_beneath(candidate, tuple(parents), create=True)
        try:
            _write_file(parent, name, item)
        finally:
            os.close(parent)


# LLM: 相对已逐段 no-follow 打开的父目录排他创建，不跟随链接；写完按声明设只读或只读可执行，不留可写副本。
# 函数用途: 在指定父目录里写一个随包文件并设权限。有副作用：写文件。
def _write_file(parent: int, name: str, item: PluginFileSnapshot) -> None:
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(item.content)
        stream.flush()
        os.fchmod(stream.fileno(), _EXECUTABLE_MODE if item.declaration.executable else _DATA_MODE)


# LLM: 读回内容与权限并与声明比对，任何不符（被替换、被加执行位、变成链接）都拒绝，不留"部分成功"的环境。
# 函数用途: 复核解包结果与包声明逐字节一致。
def _verify_files(candidate: Path, files: tuple[PluginFileSnapshot, ...], deadline: float) -> None:
    for item in files:
        check_preparation_deadline(deadline)
        parts = (FILES_DIRECTORY, *item.declaration.path.split("/"))
        info = candidate.joinpath(*parts).lstat()
        expected = _EXECUTABLE_MODE if item.declaration.executable else _DATA_MODE
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != expected:
            raise EnvironmentPreparationError("environment_file_type")
        content = read_bytes_beneath(candidate, parts, max_bytes=len(item.content))
        if content is None or hashlib.sha256(content).hexdigest() != item.declaration.sha256:
            raise EnvironmentPreparationError("environment_file_digest")
