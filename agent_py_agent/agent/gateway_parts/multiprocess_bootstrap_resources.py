# LLM: P4 共享初始化只读合同；原 no-follow/dir_fd 能力缺失必须拒绝，不降级为普通路径打开。
# 模块用途: 在冻结根内检查声明资源；不调用 HomePaths 初始化、种子、锁创建或 SQLite 连接。
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ..common.nofollow_fs import (
    NoFollowPathError,
    open_directory_beneath,
    open_readonly_file_beneath,
)
from .multiprocess_bootstrap import WorkerBootstrapError, require_worker_bootstrap


# LLM: 沿原结构化错误回执，不回显文件内容；field 是受信资源名。
# 类用途: 说明哪个共享资源无法安全使用。
class WorkerSharedResourceError(WorkerBootstrapError):
    error_code = "WORKER_SHARED_RESOURCE_INVALID"


# LLM: 资源清单由组合根沿现有路径目录生成，不从用户请求或目录扫描推导。
# 类用途: 声明一个现存目录或普通文件的预期权限；锁和数据库都按文件只读检查。
@dataclass(frozen=True)
class SharedResource:
    name: str
    path: Path
    kind: str
    mode: int


# LLM: Agent/owner 构造前调用；校验先于 I/O。清单只声明必需资源，不证明未接线初始化已只读。
# 函数用途: 核对冻结启动值和资源，返回已核对的资源名。
def require_shared_resources(frozen, launch, resources: tuple[SharedResource, ...]) -> tuple[str, ...]:
    require_worker_bootstrap(frozen, launch)
    _validate_resources(resources)
    for resource in resources:
        _check_resource(launch, resource)
    return tuple(resource.name for resource in resources)


# LLM: 明确、非空、无重复的受信清单才能通过，缺声明不能当作“没有依赖”。
# 函数用途: 在接触文件系统前拒绝错误资源合同。
def _validate_resources(resources) -> None:
    if type(resources) is not tuple or not resources:
        raise WorkerSharedResourceError("resource_contract_invalid")
    for resource in resources:
        _validate_resource(resource)
    if len({resource.name for resource in resources}) != len(resources):
        raise WorkerSharedResourceError("resource_contract_duplicate")


# LLM: 权限是显式精确预期，且禁止声明组/其它用户可写或特殊位；数据库不连接、不迁移。
# 函数用途: 校验一个路径、资源类型和权限声明。
def _validate_resource(resource) -> None:
    if type(resource) is not SharedResource or type(resource.name) is not str or not resource.name:
        raise WorkerSharedResourceError("resource_contract_invalid")
    valid = (isinstance(resource.path, Path) and resource.path.is_absolute() and ".." not in resource.path.parts
             and resource.kind in {"file", "directory"} and type(resource.mode) is int
             and 0 <= resource.mode <= 0o777 and not resource.mode & 0o022)
    if not valid:
        raise WorkerSharedResourceError("resource_contract_invalid", resource.name)


# LLM: 根只来自冻结 launch，不 resolve 链接、不按当前配置找替代根；未知错误不能自动补种子。
# 函数用途: 在两种固定根内只读检查资源，把 OS 失败转为结构化原因。
def _check_resource(launch, resource: SharedResource) -> None:
    roots = (launch.owner_root, launch.gateway_root)
    candidates = [root for root in roots if resource.path.is_relative_to(root)]
    if not candidates:
        raise WorkerSharedResourceError("resource_outside_frozen_root", resource.name)
    root = max(candidates, key=lambda path: len(path.parts))
    try:
        _check_beneath(root, resource.path.relative_to(root).parts, resource)
    except FileNotFoundError as error:
        raise WorkerSharedResourceError("resource_missing", resource.name) from error
    except NoFollowPathError as error:
        raise WorkerSharedResourceError("resource_unsafe_path", resource.name) from error
    except OSError as error:
        raise WorkerSharedResourceError("resource_unreadable", resource.name) from error


# LLM: 复用现有 no-follow 只读打开；各父级也检查属主和写位，但不改权限、不创建锁/目录。
# 函数用途: 核对目录链及叶子的元数据，所有描述符当次关闭。
def _check_beneath(root: Path, parts: tuple, resource: SharedResource) -> None:
    parents = parts if resource.kind == "directory" else parts[:-1]
    for depth in range(len(parents) + 1):
        descriptor = open_directory_beneath(root, parents[:depth], require_dir_fd=True)
        _check_descriptor(descriptor, resource, leaf=False)
    if resource.kind == "directory":
        descriptor = open_directory_beneath(root, parts, require_dir_fd=True)
    else:
        descriptor = open_readonly_file_beneath(root, parts)
    _check_descriptor(descriptor, resource, leaf=True)


# LLM: fstat 检查已打开对象；与 no-follow 原语联合拒绝链接和身份竞态，不缓存后续安全保证。
# 函数用途: 检查文件属主与权限，成功失败都关闭 fd。
def _check_descriptor(descriptor: int, resource: SharedResource, *, leaf: bool) -> None:
    try:
        info = os.fstat(descriptor)
        if not hasattr(os, "geteuid") or info.st_uid != os.geteuid():
            raise WorkerSharedResourceError("resource_owner", resource.name)
        permission = stat.S_IMODE(info.st_mode)
        if (leaf and permission != resource.mode) or (not leaf and (permission & 0o022 or permission & 0o500 != 0o500)):
            raise WorkerSharedResourceError("resource_permissions", resource.name)
    finally:
        os.close(descriptor)
