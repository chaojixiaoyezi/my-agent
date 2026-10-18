# LLM: 只组合原冻结启动合同与原owner池；不建立模式、认领请求或安装执行登记作用域。
# 模块用途: 给真实worker入口提供惰性SimpleAgent池及构造前检查点。
from copy import deepcopy
from pathlib import Path

from ..owner_scoped_pool import OwnerScopedAgentPool, _resolved_max_agents
from ..user_space.owner_resolver import OwnerIdentity
from .multiprocess_agent_contract import AgentConstruction, require_agent_constructor
from .multiprocess_bootstrap import WorkerBootstrapError, require_worker_bootstrap
from .multiprocess_mode import current_worker_partition


# LLM: 显式启动根必须与原冻结根完全一致；不resolve链接或用环境/热config补根。
# 函数用途: 池和实际构造消费配置前验证原home，默认入口无新I/O。
def require_worker_config(frozen, config):
    require_worker_bootstrap(frozen, getattr(frozen, "launch", None))
    raw = getattr(config, "my_agent_home", None)
    if not isinstance(raw, (str, Path)) or Path(raw) != frozen.launch.owner_root:
        raise WorkerBootstrapError("constructor_home_mismatch", "home_root")
    return frozen.launch.owner_root


# LLM: 只消费同次C原constructor tuple，不resolve环境/路径或读热配置；实际构造必须完整非空，旧纯launch无roots不可据此物化Agent。
# 函数用途: 在任何构造副作用之前核对主根和全部工作根，禁止把数据home当工作区。
def require_worker_workspace(frozen, root, workspace_roots):
    require_worker_bootstrap(frozen, getattr(frozen, "launch", None))
    roots = frozen.launch.workspace_roots
    if not roots:
        raise WorkerBootstrapError("worker_workspace_required", "workspace_roots")
    if (not isinstance(root, (str, Path)) or Path(root) != roots[0]
            or type(workspace_roots) not in (list, tuple)
            or any(not isinstance(path, (str, Path)) for path in workspace_roots)
            or tuple(Path(path) for path in workspace_roots) != roots):
        raise WorkerBootstrapError("constructor_workspace_mismatch", "workspace_roots")
    return roots


# LLM: 真实SimpleAgent最早调用；缺冻结事实不能退回普通初始化，更不能先建local/main。
# 函数用途: 在root.mkdir/任何owner写入前核对即将消费的原始构造身份。
def require_worker_construction(frozen, config):
    if frozen is None:
        if current_worker_partition() is not None:
            raise WorkerBootstrapError("worker_bootstrap_required", "mode")
        return
    home = require_worker_config(frozen, config)
    owner = OwnerIdentity(config.my_agent_owner_provider, config.my_agent_owner_kind, config.my_agent_owner_id)
    require_agent_constructor(frozen, owner, AgentConstruction(home, owner))


# LLM: frozen由早期入口提供；真实C roots在共享读取前核且原样交池，不用home补根；首次ready前只读核依赖，owner仍惰性且消费时重核。
# 函数用途: 在资源完整时返回原OwnerScopedAgentPool，不物化owner或补写共享资源。
def create_worker_pool(frozen, config, root):
    from ..user_space.worker_home import frozen_home_paths, require_worker_shared_home

    require_worker_config(frozen, config)
    roots = require_worker_workspace(frozen, root, frozen.launch.workspace_roots)
    require_worker_shared_home(frozen, frozen_home_paths(frozen))
    snapshot = deepcopy(config)
    count = frozen.launch.partition.count
    capacity = _resolved_max_agents(getattr(snapshot, "owner_agent_pool_max_agents", None))
    return OwnerScopedAgentPool(
        snapshot, root, workspace_roots=roots,
        max_agents=(capacity + count - 1) // count, worker_bootstrap=frozen,
    )
