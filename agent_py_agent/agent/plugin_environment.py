# LLM: 这是启用前的内部准备器，不发布激活或目录；调用方须原宿主授权/执行器，结果写原操作账，不能扫描目录判断成功。
# 模块用途: 从固定本地 wheel 在 owner 的最终地址创建独立 Python 环境，源码开发阶段尚未接到启用命令。

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common.nofollow_fs import open_directory_beneath
from .common.strict_json import load_strict_json
from .plugin_environment_process import (
    EnvironmentPreparationError,
    check_preparation_deadline,
    preparation_environment,
    run_environment_process,
)
from .plugin_package import PluginPackageSnapshot
from .plugin_wheel_layout import (
    WheelInstallLayout,
    plan_wheel_installation,
    verify_wheel_installation,
)
from .plugin_wheels import inspect_plugin_wheels
from .tooling.cancellation import raise_if_cancelled
from .user_space.owner_quota import OwnerQuotaChange, owner_quota_enforcer_from_policy
from .user_space.owner_resolver import OwnerHomeResult

_PROBE = """import json, sys, sysconfig
from pathlib import Path
p = sysconfig.get_paths()
print(json.dumps(dict(purelib=p['purelib'], platlib=p['platlib'], scripts=p['scripts'],
 data=p['data'], headers=str(Path(sys.prefix) / 'include' / 'site' / f'python{sys.version_info.major}.{sys.version_info.minor}'),
 prefix=sys.prefix, base_prefix=sys.base_prefix, version=list(sys.version_info[:3]))))
"""


# LLM: 此预算只控制宿主准备，不是插件业务权限；配置化时须同步原配置入口，不能让包自行增大。
# 类用途: 限制一次离线环境准备的总等待与最大预留空间。
@dataclass(frozen=True)
class EnvironmentBuildLimits:
    timeout_seconds: float = 120.0
    max_bytes: int = 512 * 1024 * 1024

    # LLM: 预算必须有限且为正，不能用零或 NaN 表示无界准备。
    # 函数用途: 在产生文件或进程前拒绝错误预算。
    def __post_init__(self) -> None:
        if type(self.timeout_seconds) not in {int, float} or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("准备时间必须为有限正数")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("准备空间必须为正整数")


# LLM: 结果是原工具操作的一部分，不是启用状态表；相对引用须由原 owner 解析，恢复时不能改绑解释器或目录。
# 类用途: 把已经完成内容验证的环境交给后续配置和激活提交，不写个人绝对路径到安装表。
@dataclass(frozen=True)
class PreparedPluginEnvironment:
    environment_ref: str
    package_sha256: str
    interpreter_fingerprint: str
    python_relative_path: str
    distributions: tuple[tuple[str, str], ...]


_DEFAULT_LIMITS = EnvironmentBuildLimits()


# LLM: 原配额锁非阻塞准入后覆盖有期限的准备，未持插件业务锁；每操作只建一次候选，失败不修改安装/启用事实。
# 函数用途: 从不可变包在独立最终地址安装本地依赖，返回验证结果；不启动插件、MCP 或重启 Gateway。
def prepare_plugin_environment(
    owner: OwnerHomeResult, package: PluginPackageSnapshot, operation_id: str,
    *, limits: EnvironmentBuildLimits = _DEFAULT_LIMITS,
) -> PreparedPluginEnvironment:
    deadline = time.monotonic() + limits.timeout_seconds
    check_preparation_deadline(deadline)
    if os.name != "posix":
        raise EnvironmentPreparationError("environment_platform")
    if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 256:
        raise ValueError("环境准备必须绑定原宿主操作身份")
    wheels = inspect_plugin_wheels(package)
    check_preparation_deadline(deadline)
    fingerprint = _interpreter_fingerprint(deadline)
    reference = hashlib.sha256(json.dumps([operation_id, package.sha256, fingerprint]).encode()).hexdigest()
    candidate = owner.plugins_dir / "environments" / reference
    reserve = _preparation_capacity(wheels)
    if reserve > limits.max_bytes:
        raise EnvironmentPreparationError("environment_budget")
    with owner_quota_enforcer_from_policy(owner.home_dir, quota_path=owner.quota_json).admission(blocking=False) as quota:
        quota.check((OwnerQuotaChange(candidate, reserve),))
        check_preparation_deadline(deadline)
        descriptor = _create_candidate(owner, reference)
        try:
            _prepare_candidate(candidate, descriptor, wheels, deadline, reserve)
        finally:
            os.close(descriptor)
    return PreparedPluginEnvironment(
        reference, package.sha256, fingerprint, "python/bin/python",
        tuple((wheel.name, wheel.version) for wheel in wheels),
    )


# LLM: 同一源包在不同宿主 Python 上不能共享候选；只返回摘要，不持久化用户路径。
# 函数用途: 固定当前解释器版本、实现和实际程序内容，后续启用可据此拒绝过期环境。
def _interpreter_fingerprint(deadline: float | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps([sys.version, sys.implementation.cache_tag, os.path.realpath(sys.executable)]).encode())
    with open(sys.executable, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None:
                check_preparation_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


# LLM: 保守预留包括解压、安装临时副本、入口包装与标准引导文件；在原 quota 锁内使用，不能当第二套持久配额。
# 函数用途: 在开始创建目录前检查 owner 是否有足够准备空间，避免安装后才发现配额不足。
def _preparation_capacity(wheels) -> int:
    return 64 * 1024 * 1024 + sum(
        3 * sum(member.size for member in wheel.files) + 2 * len(wheel.content)
        + len(wheel.entry_points) * 64 + len(wheel.files) * 1024
        for wheel in wheels
    )


# LLM: 原 owner 根下逐段 no-follow，最终目录必须不存在；已有候选即使不完整也不能被重试覆盖或当作成功。
# 函数用途: 为本次操作在最终地址排他创建私有候选，保留目录描述符供后续身份核对。
def _create_candidate(owner, reference: str) -> int:
    parts = (*owner.plugins_dir.relative_to(owner.root).parts, "environments")
    parent = open_directory_beneath(owner.root, parts, create=True)
    try:
        try:
            os.mkdir(reference, mode=0o700, dir_fd=parent)
        except FileExistsError as exc:
            raise EnvironmentPreparationError("environment_exists") from exc
        return os.open(reference, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    finally:
        os.close(parent)


# LLM: venv 从第一刻就在最终地址，pip 只消费固定哈希；探测发生在安装前，安装后绝不再启动该 Python 做自检。
# 函数用途: 依次建立引导环境、核对文件计划、离线安装并从宿主读回内容。
def _prepare_candidate(candidate, descriptor, wheels, deadline, reserve) -> None:
    temporary = candidate / "temporary"
    temporary.mkdir(mode=0o700)
    environment = preparation_environment(temporary)
    root = candidate / "python"
    _check_candidate(candidate, descriptor, reserve, deadline)
    run_environment_process(
        (sys.executable, "-I", "-m", "venv", "--symlinks", str(root)),
        cwd=candidate, environment=environment, deadline=deadline,
    )
    python = root / "bin" / "python"
    layout = _probe_layout(python, root, candidate, environment, deadline)
    bootstrap = _bootstrap_files(root, deadline)
    plan = plan_wheel_installation(wheels, layout)
    check_preparation_deadline(deadline)
    requirements = _write_wheels(candidate, wheels, deadline)
    _check_candidate(candidate, descriptor, reserve, deadline)
    run_environment_process(
        (str(python), "-I", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input", "--no-cache-dir",
         "install", "--no-index", "--no-deps", "--only-binary=:all:", "--no-compile", "--require-hashes", "-r", str(requirements)),
        cwd=candidate, environment=environment, deadline=deadline,
    )
    _check_candidate(candidate, descriptor, reserve, deadline)
    if any(_file_fingerprint(root / relative, deadline) != original for relative, original in bootstrap.items()):
        raise EnvironmentPreparationError("bootstrap_modified")
    verify_wheel_installation(plan, layout, python, checkpoint=lambda: check_preparation_deadline(deadline))
    check_preparation_deadline(deadline)


# LLM: 输出来自固定的未装插件解释器脚本；必须确认 prefix 和版本，不能接受包提供的 JSON 或其它解释器布局。
# 函数用途: 在执行任何 wheel 安装前读取标准安装路径，全部目标需留在独立环境内。
def _probe_layout(python, root, candidate, environment, deadline) -> WheelInstallLayout:
    output = run_environment_process(
        (str(python), "-I", "-c", _PROBE), cwd=candidate, environment=environment, deadline=deadline, capture=True,
    )
    payload = load_strict_json(output)
    if payload["prefix"] != str(root) or payload["base_prefix"] == str(root) or payload["version"] != list(sys.version_info[:3]):
        raise EnvironmentPreparationError("environment_interpreter")
    return WheelInstallLayout(root, *(Path(payload[key]) for key in ("purelib", "platlib", "scripts", "data", "headers")))


# LLM: 输入文件由宿主排他写入，不接受用户 requirements 文本；直接本地 URI 与 SHA-256 禁止 pip 寻找替代下载。
# 函数用途: 将原快照里的 wheel 写到当前私有候选，生成一次离线安装清单。
def _write_wheels(candidate, wheels, deadline) -> Path:
    directory = candidate / "wheels"
    directory.mkdir(mode=0o700)
    requirements = []
    for wheel in wheels:
        check_preparation_deadline(deadline)
        target = directory / Path(wheel.declaration.path).name
        with target.open("xb") as stream:
            stream.write(wheel.content)
        requirements.append(f"{target.as_uri()} --hash=sha256:{wheel.declaration.sha256}\n")
    target = candidate / "requirements.txt"
    with target.open("x", encoding="utf-8") as stream:
        stream.write("".join(requirements))
    return target


# LLM: 原引导环境的文件与链接分别记录，不能跟随 lib64 链接重复扫描或把包导入作为完整性检测。
# 函数用途: 记录 pip 安装前的解释器和安装器文件，便于安装后证明它们未被修改。
def _bootstrap_files(root, deadline) -> dict[Path, str]:
    return {
        path.relative_to(root): _file_fingerprint(path, deadline)
        for path in root.rglob("*") if path.is_symlink() or path.is_file()
    }


# LLM: 只读普通文件或链接文本，特殊文件拒绝；摘要不包含文件正文或个人路径。
# 函数用途: 对引导文件生成内容指纹，不执行其中代码。
def _file_fingerprint(path: Path, deadline) -> str:
    check_preparation_deadline(deadline)
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        return "link:" + hashlib.sha256(os.fsencode(path.readlink())).hexdigest()
    if not stat.S_ISREG(mode):
        raise EnvironmentPreparationError("environment_file_type")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_preparation_deadline(deadline)
            digest.update(chunk)
    return digest.hexdigest()


# LLM: 候选身份与空间在每次外部命令前后核对；这不是 OS 沙箱，目录存在或进程退出都不能替代最终安装事实提交。
# 函数用途: 防止准备器继续使用已被替换的目录，并在返回结果前检查实际空间仍在原预留内。
def _check_candidate(candidate, descriptor, reserve, deadline) -> None:
    check_preparation_deadline(deadline)
    expected = os.fstat(descriptor)
    actual = candidate.lstat()
    if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino) or not stat.S_ISDIR(actual.st_mode):
        raise EnvironmentPreparationError("environment_identity")
    used = 0
    for path in candidate.rglob("*"):
        check_preparation_deadline(deadline)
        if not path.is_dir() or path.is_symlink():
            used += path.lstat().st_size
    if used > reserve:
        raise EnvironmentPreparationError("environment_budget")
