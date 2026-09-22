# LLM: 这里只计算标准 wheel 的目标与核对磁盘；真正安装仍由 pip 执行，不加载入口或处理环境状态。
# 模块用途: 在独立 venv 内阻止依赖覆盖解释器、安装器和彼此文件，并在安装后只读核对原内容。

from __future__ import annotations

import configparser
import csv
import hashlib
import io
import os
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .common.nofollow_fs import read_bytes_beneath
from .plugin_manifest import PluginPackageError
from .plugin_package import archive_path_key
from .plugin_wheels import PluginWheelSnapshot, WheelFile, verify_wheel_record_digest


# LLM: 路径只来自启用前的受信解释器探测，不接受包指定宿主目录；headers 遵循 venv 的 pip 安装布局。
# 类用途: 固定本次 Python 环境的标准安装目录，供文件覆盖检查和读回使用。
@dataclass(frozen=True)
class WheelInstallLayout:
    root: Path
    purelib: Path
    platlib: Path
    scripts: Path
    data: Path
    headers: Path

    # LLM: 每个安装根都必须处于固定 venv 内，不能以 resolve 后的新路径吞掉符号链接或越界。
    # 函数用途: 在接收解释器探测结果时阻止任何目标写入宿主或核心环境。
    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError("环境根必须为绝对路径")
        for target in (self.purelib, self.platlib, self.scripts, self.data, self.headers):
            _relative_target(self.root, target)


# LLM: 记录原成员到安装目标的确定映射，不含活动状态或执行许可；生成脚本由安装器负责且不在此运行。
# 类用途: 保存安装后需要只读核对的成员及目标。
@dataclass(frozen=True)
class WheelInstallTarget:
    wheel: PluginWheelSnapshot
    member: WheelFile
    relative_path: tuple[str, ...]
    script: bool


# LLM: 检查整个固定集合再调用安装器，大小写别名、文件/目录冲突及已存在文件都拒绝；共享 namespace 空目录可以共存。
# 函数用途: 为所有 wheel 生成无覆盖的文件计划，保护 venv 引导文件与安装器模块。
def plan_wheel_installation(wheels, layout: WheelInstallLayout) -> tuple[WheelInstallTarget, ...]:
    planned = []
    destinations = set()
    protected = _protected_packages(layout)
    for wheel in wheels:
        for member in wheel.files:
            target, script = _member_target(wheel, member.path, layout)
            relative = _admit_target(target, layout, destinations, protected)
            planned.append(WheelInstallTarget(wheel, member, relative, script))
        for name in _script_names(wheel.entry_points):
            _admit_target(layout.scripts / name, layout, destinations, protected)
    _reject_parent_file_collisions(destinations)
    return tuple(planned)


# LLM: 只接受同一发行的 .data 与标准布局键，未知键明确失败，不回退到工作目录；根目录名称不能冒充另一发行。
# 函数用途: 按 wheel 安装规范计算一个成员的真实写入目标。
def _member_target(wheel, name: str, layout) -> tuple[Path, bool]:
    parts = PurePosixPath(name).parts
    if not parts[0].endswith(".data"):
        return (layout.purelib if wheel.purelib else layout.platlib).joinpath(*parts), False
    if parts[0] != wheel.dist_info.removesuffix(".dist-info") + ".data" or len(parts) < 3:
        raise PluginPackageError("wheel_layout", "插件依赖的安装目录无效。")
    scheme = {
        "purelib": layout.purelib, "platlib": layout.platlib,
        "scripts": layout.scripts, "data": layout.data, "headers": layout.headers / wheel.name,
    }
    root = scheme.get(parts[1])
    if root is None:
        raise PluginPackageError("wheel_layout", "插件依赖使用了不支持的安装布局。")
    return root.joinpath(*parts[2:]), parts[1] == "scripts"


# LLM: 私有环境中已有的顶层模块属于引导环境；阻止同名模块/目录及子路径注入，不把 namespace 目录当同一个文件。
# 函数用途: 为安装器与引导包生成需要保护的模块名称。
def _protected_packages(layout) -> set[tuple[Path, str]]:
    return {
        (root, archive_path_key(child.name.removesuffix(".py")))
        for root in {layout.purelib, layout.platlib}
        if root.is_dir()
        for child in root.iterdir()
    }


# LLM: 不跟随已有链接，也不允许父目录是文件；检查在整个集合写入前完成，不能用 pip 的覆盖成功当无冲突。
# 函数用途: 将一个文件目标加入安装计划，拒绝解释器、安装器及跨 wheel 的覆盖。
def _admit_target(target, layout, destinations, protected) -> tuple[str, ...]:
    relative = _relative_target(layout.root, target)
    folded = tuple(archive_path_key(part) for part in relative)
    if folded in destinations or os.path.lexists(target):
        raise PluginPackageError("wheel_collision", "插件依赖会覆盖已有文件或其它依赖。")
    for root, name in protected:
        if target.is_relative_to(root) and archive_path_key(target.relative_to(root).parts[0].removesuffix(".py")) == name:
            raise PluginPackageError("wheel_collision", "插件依赖会修改 Python 引导包或安装器。")
    current = layout.root
    for part in relative[:-1]:
        current /= part
        if os.path.lexists(current) and (current.is_symlink() or not current.is_dir()):
            raise PluginPackageError("wheel_collision", "插件依赖目标的父目录无效。")
    destinations.add(folded)
    return relative


# LLM: 此函数只判定规范相对位置，不通过 resolve 改写传入地址；身份与权限仍由宿主负责。
# 函数用途: 拒绝任何走出固定环境的写入位置。
def _relative_target(root: Path, target: Path) -> tuple[str, ...]:
    try:
        parts = target.relative_to(root).parts
    except ValueError as exc:
        raise PluginPackageError("wheel_layout", "插件安装目标必须位于独立环境。") from exc
    if any(part in {"", ".", ".."} for part in parts):
        raise PluginPackageError("wheel_layout", "插件安装目标不是规范路径。")
    return parts


# LLM: 大小写归一后的父级文件和子文件不能同时安装；不以安装顺序决定哪个 wheel 获胜。
# 函数用途: 在安装前拒绝跨依赖的文件与目录冲突。
def _reject_parent_file_collisions(destinations: set[tuple[str, ...]]) -> None:
    if any(parts[:count] in destinations for parts in destinations for count in range(1, len(parts))):
        raise PluginPackageError("wheel_collision", "插件依赖的文件与目录发生冲突。")


# LLM: entry_points 是静态声明，只读取控制台/GUI 脚本名；不调用 EntryPoint.load，不插值环境变量。
# 函数用途: 将 pip 会生成的脚本也纳入覆盖检查，拒绝路径形式的脚本名。
def _script_names(content: bytes) -> tuple[str, ...]:
    if not content:
        return ()
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(content.decode("utf-8"))
        if parser.defaults():
            raise ValueError("脚本声明不接受 DEFAULT")
        names = tuple(name for group in ("console_scripts", "gui_scripts") if parser.has_section(group) for name in parser[group])
        if any(not name or name in {".", ".."} or any(char in name for char in "/\\:\x00\r\n") for name in names):
            raise ValueError("脚本名不是普通文件名")
        return names
    except (ValueError, configparser.Error, UnicodeError) as exc:
        raise PluginPackageError("wheel_entry_points", "插件依赖的脚本声明无效。") from exc


# LLM: 宿主读取文件，不启动安装后的 Python；RECORD 独立核对生成项，脚本只允许解释器首行改变，期限由调用方注入。
# 函数用途: 验证 pip 确实将已检查的内容安装到固定环境，导入陷阱和 .pth 不会在此执行。
def verify_wheel_installation(plan, layout: WheelInstallLayout, python: Path, *, checkpoint=None) -> None:
    for target in plan:
        if checkpoint is not None:
            checkpoint()
        if target.member.path == f"{target.wheel.dist_info}/RECORD":
            continue
        actual = read_bytes_beneath(
            layout.root, target.relative_path, max_bytes=target.member.size + 8192, require_dir_fd=True,
        )
        if actual is None:
            raise PluginPackageError("wheel_installation", "插件依赖安装后缺少文件。")
        if hashlib.sha256(actual).hexdigest() == target.member.sha256:
            continue
        if target.script and _script_rewrite_matches(target, actual, python):
            continue
        raise PluginPackageError("wheel_installation", "插件依赖安装后内容不一致。")
    for wheel in {target.wheel.name: target.wheel for target in plan}.values():
        if checkpoint is not None:
            checkpoint()
        _verify_installed_record(wheel, plan, layout, checkpoint)


# LLM: 安装后的 RECORD 会增加包装脚本与安装器记录，预算按实际允许目标和每行开销推导，不信记录自报路径或长度。
# 函数用途: 校验全部安装记录仍属于已批准的文件计划，新增脚本较多时不因固定小余量误拒绝。
def _verify_installed_record(wheel, plan, layout, checkpoint) -> None:
    selected = [target for target in plan if target.wheel.name == wheel.name]
    record = next(target for target in selected if target.member.path == f"{wheel.dist_info}/RECORD")
    allowed = {target.relative_path: target.member.size + 8192 for target in selected}
    signatures = {
        target.relative_path for target in selected
        if target.member.path in {f"{wheel.dist_info}/RECORD.jws", f"{wheel.dist_info}/RECORD.p7s"}
    }
    base = layout.purelib if wheel.purelib else layout.platlib
    for name in _script_names(wheel.entry_points):
        allowed[_relative_target(layout.root, layout.scripts / name)] = len(wheel.entry_points) + 8192
    required = set(allowed)
    for name in ("INSTALLER", "REQUESTED", "direct_url.json"):
        allowed[_relative_target(layout.root, base / wheel.dist_info / name)] = 16384
    limit = 65536 + sum(
        2 * len(os.path.relpath(layout.root.joinpath(*relative), base).encode("utf-8")) + 256
        for relative in allowed
    )
    try:
        raw = read_bytes_beneath(layout.root, record.relative_path, max_bytes=limit, require_dir_fd=True)
        if raw is None:
            raise ValueError("安装记录缺失")
        seen = set()
        for row in csv.reader(io.StringIO(raw.decode("utf-8"), newline=""), strict=True):
            if checkpoint is not None:
                checkpoint()
            if len(row) != 3:
                raise ValueError("安装记录格式无效")
            relative = _installed_record_path(row[0], base, layout.root)
            if relative not in allowed or relative in seen:
                raise ValueError("安装记录包含未知或重复目标")
            seen.add(relative)
            if relative == record.relative_path:
                if row[1:] != ["", ""]:
                    raise ValueError("安装记录不得自报摘要")
                continue
            content = read_bytes_beneath(layout.root, relative, max_bytes=allowed[relative], require_dir_fd=True)
            if content is None:
                raise ValueError("安装记录所指文件缺失")
            if relative in signatures and row[1:] == ["", ""]:
                continue  # 签名不自证摘要；前面的源内容读回已核对其完整字节。
            verify_wheel_record_digest(content, row[1], row[2])
        if not required <= seen:
            raise ValueError("安装记录未包含完整计划")
    except (ValueError, UnicodeError, OSError, csv.Error) as exc:
        raise PluginPackageError("wheel_installation", "插件依赖的安装记录或文件校验失败。") from exc


# LLM: pip 可写相对 site-packages 的上级路径，但规范化后仍须落在固定 venv 内；不接受绝对地址或其它平台路径。
# 函数用途: 将安装记录中的路径映射回受管环境，拒绝越界或歧义写法。
def _installed_record_path(name: str, base: Path, root: Path) -> tuple[str, ...]:
    if not name or PurePosixPath(name).is_absolute() or any(char in name for char in "\\:\x00"):
        raise ValueError("安装记录路径无效")
    return _relative_target(root, Path(os.path.normpath(base / name)))


# LLM: 只有 wheel 标准的 #!python/#!pythonw 首行可由 pip 改写；不能以脚本身份放过其它内容变化。
# 函数用途: 对安装器生成的解释器首行做精确核对，其余脚本文本保持原样。
def _script_rewrite_matches(target, actual: bytes, python: Path) -> bool:
    with zipfile.ZipFile(io.BytesIO(target.wheel.content)) as archive:
        original = archive.read(target.member.path)
    first, separator, rest = original.partition(b"\n")
    return bool(separator and first.rstrip(b"\r") in {b"#!python", b"#!pythonw"}
                and actual == b"#!" + os.fsencode(python) + b"\n" + rest)
