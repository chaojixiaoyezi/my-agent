# LLM: 只检查管理员明确选择的路径；不猜依赖、不授 OS 权限，不能由读 HOME 扩成宿主控制面授权。
# 模块用途: 冻结有限路径身份和额外程序内容，供确认与启动前复核漂移。
from __future__ import annotations

import hashlib
from pathlib import Path


# LLM: 阻止受保护根及其祖先被宽授权覆盖；系统根不是“程序前缀”的可用别名。
# 函数用途: 检查有限授权根，避免以整个目录绕开控制面保护。
def validate_permission_root(path: Path, forbidden_roots) -> None:
    broad = {"/", "/Users", "/home", "/private", "/tmp", "/var", "/usr", "/opt", "/System"}
    if str(path) in broad:
        raise ValueError("授权根过宽，请选择具体目录或程序")
    blocked = tuple(Path(root).resolve() for root in forbidden_roots)
    if any(path == root or path in root.parents or root in path.parents for root in blocked):
        raise ValueError("不能授权宿主控制目录或其祖先")


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
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return PermissionPath(str(path), stat.st_dev, stat.st_ino, digest)


# LLM: 排序去重只作用于同一明确根集合，不合并父子目录扩大权限。
# 函数用途: 冻结一组有限根，未知形状及过大输入先拒绝。
def permission_paths(values: object, forbidden_roots, *, program: bool = False) -> tuple:
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("授权路径必须是最多 32 项的列表")
    paths = [permission_path(value, forbidden_roots, program=program) for value in values]
    unique = {item.path: item for item in paths}
    return tuple(unique[key] for key in sorted(unique))
