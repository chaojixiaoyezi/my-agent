# LLM: 仅串行本进程的来源提交/资源启动；配置CAS仍只来自唯一repository，资源仍由原注册表持有。
# 模块用途: 用结构化来源版本给宿主进程绑定边界并在提交前请求原注册表退役，不检查stdin文本。
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from time import monotonic
from weakref import WeakSet

from ..path_access_policy import (
    PathAccessPolicy,
    _persona_owner_homes,
    agent_home_root_for_owner,
    owner_home_containing,
)
from ._agents_contract_fields import AgentsContractError

_boundary_lock = RLock()
_registries = WeakSet()


# LLM: 原注册表的弱引用仅用于通知，不复制资源账或配置；无模型输入，登记和通知共用锁。
# 函数用途: 让PTY/插件自己的资源管理器接收切源通知。
def register_source_registry(registry):
    with _boundary_lock:
        _registries.add(registry)


# LLM: 锁只覆盖本进程；先拿本锁再拿来源配置/资源锁。有deadline的调用不得在等锁时重置预算。
# 函数用途: 串行来源变更与进程登记，可在原调用期限内拒绝等待。
@contextmanager
def source_process_boundary(*, deadline=None):
    remaining = -1 if deadline is None else max(0.0, deadline - monotonic())
    if not _boundary_lock.acquire(timeout=remaining):
        raise TimeoutError("AGENTS_SOURCE_BOUNDARY_TIMEOUT")
    try:
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError("AGENTS_SOURCE_BOUNDARY_TIMEOUT")
        yield
    finally:
        _boundary_lock.release()


# LLM: 读取唯一repository的完整五字段CAS；FullAccess按同一严格owner集合冻结依赖，配置坏即拒绝启动。
# 函数用途: 为新进程保存当前来源版本，不拉正文或新增配置文件。
def process_source_configurations(owner_home, *, full_access=False):
    if not owner_home:
        return ()
    home = Path(str(owner_home)).expanduser().absolute()
    root = agent_home_root_for_owner(home)
    # 非规范嵌入式PTY没有owner来源配置；不能根据cwd推断成某个真实owner。
    if root is None or owner_home_containing(home, root) != home:
        return ()
    homes = _persona_owner_homes(root) if full_access else (home,)
    return tuple((str(candidate), _current_configuration(candidate, root)) for candidate in homes)


# LLM: 不缓存配置；FullAccess还鲜活比较严格owner集合，不能漏新增owner；结构/权限失败拒绝复用。
# 函数用途: 比较启动依赖集合和唯一仓库完整CAS，不使用正文或时间戳作版本。
def process_source_configurations_current(configurations, *, owner_home=None, full_access=False):
    try:
        current = (process_source_configurations(owner_home, full_access=True) if full_access else
                   tuple((home, _fresh_owner_configuration(home)) for home, _ in configurations))
        return (tuple(home for home, _ in current) == tuple(home for home, _ in configurations)
                and all(_same_source_configuration(now, expected)
                        for (_, now), (_, expected) in zip(current, configurations)))
    except (AgentsContractError, OSError, ValueError):
        return False


# LLM: 通知只比较结构化依赖；FullAccess的同一规范root新增owner也命中，不能靠旧数组缺项跳过退役。
# 函数用途: 判断本资源是否受一次来源换代影响，不授予跨owner停止权限。
def process_source_change_affects(configurations, owner_home, proposed_cas, *, bound_scope=(None, False)):
    bound_owner_home, full_access = bound_scope
    if any(home == owner_home and cas != proposed_cas for home, cas in configurations):
        return True
    if not full_access or not bound_owner_home or any(home == owner_home for home, _ in configurations):
        return False
    bound_root = agent_home_root_for_owner(Path(bound_owner_home))
    changed_root = agent_home_root_for_owner(Path(owner_home))
    return bound_root is not None and bound_root == changed_root


# LLM: 冻结依赖必须仍能对应规范owner；根失效抛来源错误，由外层鲜活比较失败关闭。
# 函数用途: 重读已有owner的唯一完整CAS，不回退到空集合或默认许可。
def _fresh_owner_configuration(home):
    path = Path(home)
    root = agent_home_root_for_owner(path)
    if root is None:
        raise AgentsContractError("AGENTS_SOURCE_RESOURCE_UNCONFIRMED", "owner_root")
    return _current_configuration(path, root)


# LLM: 完整冻结CAS包括revision；不能只比较定位或正文摘要，否则同来源版本变更被误接受。
# 函数用途: 判定一个来源版本是否仍与启动快照完全一致。
def _same_source_configuration(current, expected):
    return current == expected


# LLM: 延迟导入避免repository→通知模块→repository环；只读配置，不加载内容或改变权限。
# 函数用途: 从唯一owner来源服务取得结构化版本。
def _current_configuration(home, root):
    from .agents_source_repository import AgentsSourceRepository
    repo = AgentsSourceRepository(owner=str(home.relative_to(root / "owners")), owner_home=home,
                                  path_policy=PathAccessPolicy.from_values(mode="full", agent_home_root=root))
    return repo.configuration().configuration_cas


# LLM: 调用方已核原授权/旧CAS/权限且持来源事务锁；原注册表不能确认退役时抛错，不能提交或消费授权。
# 函数用途: 在切源配置落盘前要求本进程已登记资源退出。
def retire_source_resources(owner_home, proposed_cas):
    for registry in tuple(_registries):
        registry.retire_agents_source(str(owner_home), proposed_cas)
