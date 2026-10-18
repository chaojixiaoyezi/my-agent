# LLM: W只能消费C已初始化的共享home；此处不调用ensure_my_agent_home或共享种子/索引同步。
# 模块用途: 在实际home消费点验证共享依赖，再沿原owner初始化构造已核归属的路径。
import os
import stat
from pathlib import Path

from ..gateway_parts.multiprocess_agent_contract import require_agent_home
from ..gateway_parts.multiprocess_bootstrap_resources import (
    SharedResource,
    WorkerSharedResourceError,
    require_shared_resources,
)
from ..gateway_parts.multiprocess_worker_agent import require_worker_construction
from .home_layout import MyAgentHomePaths, _root_home_path_fields
from .home_layout_v2 import v2_home_path_fields
from .owner_resolver import (
    OwnerIdentity,
    _owner_directories,
    _owner_home_dir,
    _owner_home_result,
    _owner_seed_files,
    _owner_seed_jsons,
    home_paths_with_owner,
)


# LLM: 布局纯投影直接用冻结绝对根，不调用会resolve路径/读取环境的普通home解析器。
# 函数用途: 得到原MyAgentHomePaths结构，未物化任何目录或管理员。
def frozen_home_paths(frozen):
    root = frozen.launch.owner_root
    return MyAgentHomePaths(root=root, **_root_home_path_fields(root), **v2_home_path_fields(root))


# LLM: 模式只从有限安全集合接受；随后原P4 nofollow/fstat再次核对精确值，不信任stat证明安全。
# 函数用途: 将canonical资源声明为明确权限，缺失/危险权限不补建不chmod。
def existing_resource(name, path, kind):
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError as error:
        raise WorkerSharedResourceError("resource_missing", name) from error
    except OSError as error:
        raise WorkerSharedResourceError("resource_unreadable", name) from error
    permitted = {0o700, 0o750, 0o755} if kind == "directory" else {0o600, 0o640, 0o644}
    if mode not in permitted:
        raise WorkerSharedResourceError("resource_permissions", name)
    return SharedResource(name, path, kind, mode)


# LLM: 清单来自实际home/共享能力及scheduler消费点，含C创建的两类共享索引；不枚举其它owner。
# 函数用途: 池首次就绪与owner物化均只读核依赖，缺资源不补写。
def require_worker_shared_home(frozen, paths):
    directories = (
        "config_dir", "templates_dir", "shared_builtin_dir", "shared_skills_dir",
        "shared_role_templates_dir", "shared_policy_templates_dir", "shared_scripts_dir",
        "shared_indexes_dir", "global_index_dir", "identity_dir", "system_config_dir",
    )
    resources = tuple(existing_resource(name, getattr(paths, name), "directory") for name in directories)
    files = ("soul_md", "user_md", "agents_md", "memory_md", "memory_hot_md", "system_schema_version_json",
             "shared_indexes_skills_jsonl", "shared_indexes_role_templates_jsonl")
    resources += tuple(existing_resource(name, getattr(paths, name), "file") for name in files)
    database = existing_resource("scheduler_due", paths.global_index_dir / "scheduler_due.sqlite3", "file")
    require_shared_resources(frozen, frozen.launch, (*resources, database))


# LLM: 只检查原布局投影的初始化路径，不遍历用户资料树；新目录仍由原初始化负责。
# 函数用途: 在任何owner写入前阻断根、子目录和种子的静态跨owner链接。
def _require_owner_path(frozen, owner):
    root = frozen.launch.owner_root
    result = _owner_home_result(root, owner, _owner_home_dir(root, owner))
    targets = {path for path in vars(result).values() if isinstance(path, Path)}
    targets.update(_owner_directories(result))
    for target in targets:
        _require_existing_owner_path(root, target)


# LLM: lstat不追随链接；父级必须是当前UID的安全目录，文件单链接，检查不赋予后续竞态安全保证。
# 函数用途: 检查一个初始化路径的已有前缀，遇缺失停止但不补建。
def _require_existing_owner_path(root, target):
    path = root
    for part in target.relative_to(root).parts:
        path = path / part
        try:
            info = path.lstat()
        except FileNotFoundError:
            break
        safe_type = stat.S_ISDIR(info.st_mode) or (
            path == target and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
        )
        if not safe_type or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise WorkerSharedResourceError("resource_unsafe_path", "owner_home")


# LLM: 原P4构造及共享依赖检查在消费参数上执行；只读消费原C已物化的owner目录/种子，缺失不得W补写。
# 函数用途: 返回已存在的当前用户home投影，不执行ensure/upgrade/seed。
def initialize_worker_home(frozen, config):
    require_worker_construction(frozen, config)
    owner = OwnerIdentity(config.my_agent_owner_provider, config.my_agent_owner_kind, config.my_agent_owner_id)
    paths = frozen_home_paths(frozen)
    require_worker_shared_home(frozen, paths)
    _require_owner_path(frozen, owner)
    projected = _owner_home_result(paths.root, owner, _owner_home_dir(paths.root, owner))
    resources = tuple(existing_resource(f'owner_directory_{index}', path, 'directory')
                      for index, path in enumerate(_owner_directories(projected)))
    seeds = (*_owner_seed_files(projected), *_owner_seed_jsons(projected))
    resources += tuple(existing_resource(f'owner_seed_{index}', path, 'file')
                       for index, (path, _) in enumerate(seeds))
    require_shared_resources(frozen, frozen.launch, resources)
    actual = home_paths_with_owner(paths, projected)
    require_agent_home(frozen, owner, actual)
    return actual
