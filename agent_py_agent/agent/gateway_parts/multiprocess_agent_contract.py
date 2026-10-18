# LLM: P4/P5 的真实构造与已构造 Home 检查点；只核原冻结事实，不建 Agent、池或第二身份源。
# 模块用途: 防止上游 launch 校验与实际构造参数、返回对象的归属脱节；生产接线另做。
from dataclasses import asdict, dataclass
from pathlib import Path

from ..runtime_errors import DataCorruptionError
from ..user_space.home_layout import MyAgentHomePaths
from ..user_space.owner_resolver import (
    OwnerIdentity,
    _owner_home_dir,
    _owner_home_result,
    home_paths_with_owner,
)
from .multiprocess_bootstrap import WorkerBootstrapError, require_worker_bootstrap
from .multiprocess_route import owner_worker
from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner


# LLM: 只运输当前构造调用的原实参，不保存 config 引用，不是新的 owner 登记或授权源。
# 类用途: 把末端已经解析的根和身份打包，供同一次检查与构造消费。
@dataclass(frozen=True)
class AgentConstruction:
    home_root: Path
    owner: OwnerIdentity


# LLM: actual 必须来自即将消费的实际参数；调用方保持检查至消费不漂移。
# 函数用途: 在任何 Home 初始化前核对真实构造参数，不读取配置或环境、不执行 I/O。
def require_agent_constructor(frozen, owner, actual):
    _require_worker_owner(frozen, owner)
    if type(actual) is not AgentConstruction:
        raise WorkerBootstrapError("constructor_arguments_invalid", "actual")
    if not isinstance(actual.home_root, Path) or actual.home_root != frozen.launch.owner_root:
        raise WorkerBootstrapError("constructor_home_mismatch", "home_root")
    if type(actual.owner) is not OwnerIdentity or actual.owner != owner:
        raise WorkerBootstrapError("constructor_owner_mismatch", "owner")


# LLM: 复用原P4模式及main唯一host owner解码器核规范三元组；只检查投影，不从热配置、route或文本重建权限。
# 函数用途: 确认期望 owner 是当前 worker 的规范归属，不允许先构造 main 或跨分区对象。
def _require_worker_owner(frozen, owner):
    require_worker_bootstrap(frozen, getattr(frozen, "launch", None))
    try:
        canonical = frozen_request_owner({REQUEST_OWNER_KEY: asdict(owner)}) if type(owner) is OwnerIdentity else None
    except DataCorruptionError:
        canonical = None
    if (type(owner) is not OwnerIdentity
            or not all(type(value) is str and value for value in (owner.provider, owner.owner_kind, owner.owner_id))
            or owner != canonical
            or owner_worker(owner, frozen.launch.partition.count) != frozen.launch.partition.index):
        raise WorkerBootstrapError("constructor_owner_invalid", "owner")


# LLM: paths 必须是返回对象原 home_paths，不得从热 config 重造；只核原规范投影，无 I/O。
# 函数用途: 校验已构造 Agent 的真实根和 owner 路径，不能把热配置身份当成构造事实。
def require_agent_home(frozen, owner, paths):
    _require_worker_owner(frozen, owner)
    if type(paths) is not MyAgentHomePaths:
        raise WorkerBootstrapError("agent_home_invalid", "home_paths")
    root = frozen.launch.owner_root
    if not isinstance(paths.root, Path) or paths.root != root:
        raise WorkerBootstrapError("agent_home_root_mismatch", "root")
    expected = _owner_home_result(root, owner, _owner_home_dir(root, owner))
    if paths != home_paths_with_owner(paths, expected):
        raise WorkerBootstrapError("agent_home_owner_mismatch", "home_paths")
