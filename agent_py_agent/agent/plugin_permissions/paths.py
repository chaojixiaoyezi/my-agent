# LLM: 只检查管理员明确选择的路径；不猜依赖、不授 OS 权限，不能由读 HOME 扩成宿主控制面授权。
# 模块用途: 冻结有限路径身份和额外程序内容，供确认与启动前复核漂移。
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from pathlib import Path

from ..path_access_policy import (
    HOST_CONFIG_HOME_PARTS,
    HOST_CONFIG_OWNER_PARTS,
    HOST_CREDENTIAL_HOME_PARTS,
    PathAccessPolicy,
    host_config_root_for_path,
    host_readonly_paths,
)


# LLM: 仅投影可信布局、中央配置目录和原 schema 来源声明；广视图也保护其他 owner 的宿主配置，不读取秘密正文。
# 函数用途: 汇总插件永不可见的宿主配置目录和明确文件来源。
def host_exclusive_paths(home_paths, config=None) -> tuple[Path, ...]:
    root = Path(home_paths.root).resolve()
    paths = [root.joinpath(*parts) for parts in (*HOST_CONFIG_HOME_PARTS, *HOST_CREDENTIAL_HOME_PARTS)]
    paths.extend(path for path in host_readonly_paths(root) if host_config_root_for_path(path, root) is not None)
    paths.append(root / "secrets")
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home is not None:
        paths.extend(Path(owner_home).joinpath(*parts) for parts in HOST_CONFIG_OWNER_PARTS)
    config_path = getattr(config, "config_path", "")
    if config_path:
        paths.append(Path(config_path).expanduser())
    if is_dataclass(config) and not isinstance(config, type):
        sources = getattr(config, "config_sources", {})
        for item in fields(config):
            if not item.metadata.get("secret_ref", False):
                continue
            source = sources.get(item.name, {}) if isinstance(sources, Mapping) else {}
            references = (getattr(config, item.name), source.get("source") if isinstance(source, Mapping) else None)
            for value in references:
                text = value.strip() if isinstance(value, str) else ""
                if text.startswith("file:"):
                    paths.append(Path(text[5:].strip()).expanduser())
    return tuple(dict.fromkeys(path.resolve() for path in paths))


# LLM: 阻止受保护根及其祖先被宽授权覆盖；系统根不是“程序前缀”的可用别名。
# 函数用途: 检查有限授权根，避免以整个目录绕开控制面保护。
def validate_permission_root(path: Path, forbidden_roots) -> None:
    broad = {"/", "/Users", "/home", "/private", "/tmp", "/var", "/usr", "/opt", "/System"}
    if str(path) in broad:
        raise ValueError("授权根过宽，请选择具体目录或程序")
    blocked = tuple(Path(root).resolve() for root in forbidden_roots)
    if any(path == root or path in root.parents or root in path.parents for root in blocked):
        raise ValueError("不能授权宿主控制目录或其祖先")


# 程序文件摘要每次读入的块大小（字节）。
_DIGEST_CHUNK_BYTES = 1024 * 1024


# LLM: 不展开相对路径、~ 或 ..；解析到别名或链接时拒绝，程序文件摘要以流式读取固定。
# 函数用途: 读取路径身份，不运行程序或读取目录内的业务正文。
def permission_path(value: str, forbidden_roots, *, program: bool = False):
    from .grants import PermissionPath

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("授权路径无效")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ValueError("授权路径必须是无链接、无别名的绝对地址")
    validate_permission_root(path, forbidden_roots)
    stat = path.stat()
    if not path.is_dir() and not (program and path.is_file()):
        raise ValueError("读写根必须为目录，额外依赖可为普通文件或目录前缀")
    digest = ""
    if program and path.is_file():
        # 项目支持 Python 3.10，hashlib.file_digest 是 3.11 才有的（3.10 上整条插件启用会以 AttributeError 失败，CI 修复
        # 2026-10-07），这里按块流式读，结果相同。
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(_DIGEST_CHUNK_BYTES), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
    return PermissionPath(str(path), stat.st_dev, stat.st_ino, digest)


# LLM: 先求请求与宿主有效范围的交集，再冻结 inode/程序摘要；不把收窄后的程序文件扩成父目录。
# 函数用途: 冻结一组有效有限根，未知形状及过大输入先拒绝。
def permission_paths(values: object, forbidden_roots, *, program: bool = False,
                     scope_ceiling: PathAccessPolicy | None = None, write: bool = False) -> tuple:
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("授权路径必须是最多 32 项的列表")
    roots = [root for value in values for root in _permission_intersection(value, scope_ceiling, write=write)]
    paths = [permission_path(str(root), forbidden_roots, program=program) for root in roots]
    unique = {item.path: item for item in paths}
    return tuple(unique[key] for key in sorted(unique))


# LLM: 原路径先通过无链接/无别名校验；墙外相离请求没有交集，父请求只能收窄到墙内，不能上提父目录。
# 函数用途: 按原中央路径策略计算可授权根；危险子树不能因父目录允许而整体放行。
def _permission_intersection(value, ceiling: PathAccessPolicy | None, *, write: bool) -> tuple[Path, ...]:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("授权路径无效")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path.resolve(strict=True) != path:
        raise ValueError("授权路径必须是无链接、无别名的绝对地址")
    if ceiling is None:
        return (path,)
    candidates = (path,)
    if ceiling.owner_scope_root is not None:
        upper = [ceiling.owner_scope_root]
        if not write and ceiling.agent_home_root is not None:
            upper.append(ceiling.agent_home_root / "shared")
        candidates = tuple(dict.fromkeys(
            path if path.is_relative_to(root) else root
            for root in upper if path.is_relative_to(root) or root.is_relative_to(path)
        ))
    # 目录根是范围上界，不是对其中 persona/宿主状态文件的逐项写授权；那些拒绝由沙箱最后覆盖。
    check = ceiling.check
    selected = tuple(root for root in candidates if check(root).allowed)
    if ceiling.owner_scope_root is None and ceiling.mode != "full":
        for root in selected:
            if any(denied.is_relative_to(root) and not check(denied).allowed for denied in ceiling.dangerous_roots):
                raise ValueError("授权根覆盖当前范围内不可访问的路径，请选择具体目录")
    return selected
