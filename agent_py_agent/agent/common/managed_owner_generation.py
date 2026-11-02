# LLM: 代次只由受管工厂在进程内签发并以对象身份认可；序列化副本、同形对象或重建实例都不是授权，绑定只能收窄不能替换。
# 模块用途: 为受管普通用户执行绑定原账号/安全/治理代次，并在新模型、供应商、工具与原生效果前复核当前权威。
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakSet

from ..plugin_host.errors import HostAPIError

_ISSUED: WeakSet = WeakSet()
_BOUND: ContextVar[ManagedOwnerGeneration | None] = ContextVar("managed_owner_generation", default=None)


# LLM: 状态码沿原账号/存储拒绝；outcome固定not_started，复核失败时本次效果尚未开始。
# 类用途: 表示受管执行的原代次已失效或无法证明。
class ManagedOwnerGenerationError(HostAPIError):
    # LLM: phase只用于安全诊断，不含路径、凭据或请求正文。
    # 函数用途: 保存拒绝码及发生拒绝的效果关口。
    def __init__(self, code: str, status: int, phase: str):
        super().__init__(code, status, message="Managed owner authority changed before this effect.")
        self.error_code = code
        self.phase = phase


# LLM: eq=False使WeakSet按对象身份登记；字段只是复核基准，不能据字段值签发新代次。
# 类用途: 冻结一次受管执行被接纳时的账号修订、安全指纹、治理修订和受限配置摘要。
@dataclass(frozen=True, eq=False)
class ManagedOwnerGeneration:
    root: Path
    user_id: str
    account_revision: int
    security_fingerprint: str
    policy_revision: int
    config_digest: str

    # LLM: 池按完整键精确区分代次，任何一项变化都必须使用新实例。
    # 函数用途: 返回受管池缓存与比较使用的精确代次键。
    @property
    def key(self) -> tuple:
        return (self.user_id, self.account_revision, self.security_fingerprint, self.policy_revision, self.config_digest)


# LLM: 每次都冷读原账号存储、认证戳与宿主治理策略；停用、缺失或损坏均按原错误失败关闭。
# 函数用途: 读取受管用户当前账号修订、安全指纹和治理策略修订。
def _current_authority(root: Path, user_id: str) -> tuple[int, str, int]:
    from ..user_space.local_user_store import LocalUserStore
    from ..user_space.managed_user_governance import _GovernanceControl
    from ..user_space.user_auth import _credential_fingerprint

    store = LocalUserStore(root)
    user = store.require_enabled(user_id)
    return user.revision, _credential_fingerprint(store.root, user), _GovernanceControl(store.root, user_id).policy()["revision"]


# LLM: 仅受管owner工厂调用；签发前先冷读当前权威，调用方不能传入修订或指纹。
# 函数用途: 为一个已启用的受管用户签发进程内原代次。
def issue_managed_owner_generation(root, user_id: str, *, config_digest: str) -> ManagedOwnerGeneration:
    resolved = Path(root).resolve()
    revision, security, policy = _current_authority(resolved, user_id)
    generation = ManagedOwnerGeneration(resolved, user_id, revision, security, policy, config_digest)
    _ISSUED.add(generation)
    return generation


# LLM: 只返回当前权威的比较键，不签发对象；池以此精确区分缓存代次，取得键不等于获得执行许可。
# 函数用途: 冷读受管用户当前代次键。
def current_managed_owner_key(root, user_id: str, *, config_digest: str) -> tuple:
    revision, security, policy = _current_authority(Path(root).resolve(), user_id)
    return (user_id, revision, security, policy, config_digest)


# LLM: 只认本模块签发的对象；序列化后重建或复制的同字段对象一律拒绝。
# 函数用途: 判断对象是否是有效的原签发代次。
def is_issued_managed_owner_generation(generation: object) -> bool:
    return type(generation) is ManagedOwnerGeneration and generation in _ISSUED


# LLM: None与同一对象为空操作；已绑定另一代次时拒绝而不是替换，退出时恢复原绑定。
# 函数用途: 在当前执行上下文中绑定原受管代次，只能收窄不能改绑。
@contextmanager
def bind_managed_owner_generation(generation: ManagedOwnerGeneration | None):
    current = _BOUND.get()
    if generation is None or generation is current:
        yield current
        return
    if not is_issued_managed_owner_generation(generation):
        raise ManagedOwnerGenerationError("MANAGED_OWNER_GENERATION_INVALID", 403, "bind")
    if current is not None:
        raise ManagedOwnerGenerationError("MANAGED_OWNER_GENERATION_CHANGED", 409, "bind")
    token = _BOUND.set(generation)
    try:
        yield generation
    finally:
        _BOUND.reset(token)


# LLM: 只读当前上下文；返回值供同一执行内的子线程原样运输，不能作为新签发来源。
# 函数用途: 返回当前绑定的受管原代次。
def current_managed_owner_generation() -> ManagedOwnerGeneration | None:
    return _BOUND.get()


# LLM: 未绑定即非受管执行，保持原行为；已绑定则在本次新效果前逐项复核，结果与清理路径不调用本门。
# 函数用途: 在新模型/供应商/工具/原生效果前确认原受管代次仍是当前权威。
def require_managed_owner_generation(phase: str) -> ManagedOwnerGeneration | None:
    if type(phase) is not str or not phase:
        raise ValueError("phase is required")
    generation = _BOUND.get()
    if generation is None:
        return None
    if not is_issued_managed_owner_generation(generation):
        raise ManagedOwnerGenerationError("MANAGED_OWNER_GENERATION_INVALID", 403, phase)
    try:
        revision, security, policy = _current_authority(generation.root, generation.user_id)
    except HostAPIError as error:
        raise ManagedOwnerGenerationError(error.code, error.status, phase) from None
    if revision != generation.account_revision:
        raise ManagedOwnerGenerationError("USER_REVISION_CHANGED", 409, phase)
    if security != generation.security_fingerprint:
        raise ManagedOwnerGenerationError("MANAGED_OWNER_SECURITY_CHANGED", 409, phase)
    if policy != generation.policy_revision:
        raise ManagedOwnerGenerationError("MANAGED_OWNER_POLICY_CHANGED", 409, phase)
    return generation


# LLM: 带Agent的关口先以该Agent原代次做只收窄绑定，再复核；普通Agent无代次时退回上下文绑定。
# 函数用途: 在已知执行Agent的效果关口复核其原受管代次。
def require_agent_managed_owner_generation(agent: object, phase: str) -> ManagedOwnerGeneration | None:
    with bind_managed_owner_generation(getattr(agent, "_managed_owner_generation", None)):
        return require_managed_owner_generation(phase)
