# LLM: A0提供冻结DTO；审计selector与普通local UID分责校验，DTO不授权，后续范围及写端仍重核真实issuer。
# 模块用途: 定义插件宿主v2调用与独立调用者，将闭合参数解码和身份范围授权分开。
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .action_specs import action_spec
from .action_specs import required_capabilities as required_capabilities
from .errors import HostAPIError as HostAPIError
from .schemas import (
    closed,
    freeze,
    reject_identity_injection,
    strict_json,
    text,
    validate_local_user_id,
    validate_object,
)


# LLM: payload深冻结，客户端不能从对象别名覆盖已校验参数。
# 类用途: 保存一份与公开JSON一一对应的调用。
@dataclass(frozen=True)
class Call:
    schema: str
    action: str
    payload: Mapping
    idempotency_key: str | None = None
    as_user_id: str | None = None

    # LLM: 构造DTO不授权限；后续issuer再次校验来源与内容。
    # 函数用途: 防止原字典变更影响调用。
    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze(self.payload))


# LLM: lease和caller只由宿主发行器核验登记，复制DTO不成来源；会话逐次核验且不进入展示。
# 类用途: 固定真实安装来源与独立的认证调用者，不将安装者当普通使用者。
@dataclass(frozen=True)
class PluginIngressSource:
    _issuer: object = field(repr=False)
    _lease: object = field(repr=False)
    caller_owner: object = None
    user_session_token: str | None = field(default=None, repr=False)


# LLM: grant是当前安装权威的固定快照，来源和有效性始终由issuer末核。
# 类用途: 区分插件管理actor和实际数据/执行owner。
@dataclass(frozen=True)
class VerifiedPluginBinding:
    grant: Mapping
    actor: object
    effective_owner: object
    source: PluginIngressSource = field(repr=False)
    capabilities: frozenset[str] = frozenset()
    user_revision: int = 0
    policy_digest: str = ""

    # LLM: 结构不可变不等于权限不失效，存活调用仍须revalidate。
    # 函数用途: 冻结嵌套授权字段。
    def __post_init__(self) -> None:
        object.__setattr__(self, "grant", freeze(self.grant))
        object.__setattr__(self, "capabilities", frozenset(self.capabilities))

    # LLM: 只读固定安装引用，不推断文件位置。
    # 函数用途: 暴露授权所绑定的安装标识。
    @property
    def installation_ref(self) -> str:
        return self.grant["installation_ref"]

    # LLM: 包摘要属于当前激活，不追随同名新版本。
    # 函数用途: 暴露固定包内容摘要。
    @property
    def package_sha256(self) -> str:
        return self.grant["package_sha256"]

    # LLM: 代次只能由真实原激活产生。
    # 函数用途: 暴露固定激活标识。
    @property
    def activation_id(self) -> str:
        return self.grant["activation_id"]

    # LLM: 修订用于游标和调用fence，不能代替实时授权读取。
    # 函数用途: 暴露固定授权修订。
    @property
    def grant_revision(self) -> int:
        return self.grant["grant_revision"]


# LLM: 只是原授权过程的投影，伪造或replace副本在issuer中拒绝。
# 类用途: 向领域适配器传递已核验调用及最小范围。
@dataclass(frozen=True)
class AuthorizedCall:
    call: Call
    binding: VerifiedPluginBinding
    effective_owner: object
    resource_scope: Mapping
    execution_policy: object

    # LLM: 防止领域消费者热改授权范围。
    # 函数用途: 冻结资源范围。
    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_scope", freeze(self.resource_scope))


# LLM: 字段是不可提升的固定上限，不能从父权限继承admin。
# 类用途: 表示明确的普通用户执行权限。
@dataclass(frozen=True)
class RestrictedPermission:
    role: str = "USER"
    is_admin: bool = False
    can_access_all_users: bool = False
    access_mode: str = "workspace_only"

    # LLM: 显式构造不允许伪装为普通权限的高权限字段。
    # 函数用途: 拒绝任何权限提升。
    def __post_init__(self) -> None:
        if (self.role != "USER" or self.is_admin is not False or self.can_access_all_users is not False
                or self.access_mode != "workspace_only"):
            raise HostAPIError("CAPABILITY_DENIED", 403)


# LLM: 根只能由C的宿主resolver产生；网络第一版固定禁止，预算仍受目标策略收紧。
# 类用途: 冻结当前调用允许的工具、路径及预算。
@dataclass(frozen=True)
class RestrictedExecutionPolicy:
    read_roots: tuple = ()
    write_roots: tuple = ()
    tool_names: frozenset[str] = frozenset()
    network: bool = False
    budgets: Mapping = field(default_factory=dict)
    policy_digest: str = ""

    # LLM: 该值不能使未实现factory就绪，禁止网络并固定容器。
    # 函数用途: 保持执行上限不可变。
    def __post_init__(self) -> None:
        if self.network is not False:
            raise HostAPIError("CAPABILITY_DENIED", 403)
        object.__setattr__(self, "read_roots", tuple(self.read_roots))
        object.__setattr__(self, "write_roots", tuple(self.write_roots))
        object.__setattr__(self, "tool_names", frozenset(self.tool_names))
        object.__setattr__(self, "budgets", freeze(self.budgets))


# LLM: 只读上下文禁止ensure或拉起agent，身份与home由宿主resolver核对。
# 类用途: 向历史、记忆和制品适配器提供只读owner。
@dataclass(frozen=True)
class OwnerReadContext:
    identity: object
    owner_home: object
    source_binding: VerifiedPluginBinding
    user_revision: int = 0


# LLM: 独立受限factory由C实现，不能拿main agent/invoker填入此DTO冒充隔离。
# 类用途: 冻结普通用户执行所需的来源和权限。
@dataclass(frozen=True)
class OwnerResolutionResult:
    identity: object
    owner_home: object
    permission: RestrictedPermission
    execution_policy: RestrictedExecutionPolicy
    user_revision: int
    source_binding: VerifiedPluginBinding


# LLM: 成功写结果须在原回执提交后构造；202仅证明提交受理不等于业务完成。错误outcome描述写副作用，不是读取是否运行。
# 类用途: 统一领域到传输的成功结果。
@dataclass(frozen=True)
class DomainResult:
    http_status: int
    data: object
    provenance: str = "canonical"
    warnings: tuple[str, ...] = ()

    # LLM: 限定合法成功形状，未知异常必须走HostAPIError而非伪成功。
    # 函数用途: 固定领域输出并校验状态。
    def __post_init__(self) -> None:
        if self.http_status not in {200, 201, 202} or self.provenance not in {"canonical", "projection"}:
            raise ValueError("领域结果合同无效")
        object.__setattr__(self, "data", freeze(self.data))
        object.__setattr__(self, "warnings", tuple(self.warnings))


# LLM: 只能在宿主组合根安装领域对象，None表示未接入而不是成功空结果。
# 类用途: 固定四条互相独立的业务端口。
@dataclass(frozen=True)
class HostDomainPorts:
    conversation: object = None
    local_users: object = None
    execution: object = None
    observations: object = None


# LLM: 治理和审计组拒as_user；审计组selector交原managed范围门统一403，其余local动作保留严格UID校验，DTO不授权。
# 函数用途: 校验冻结调用并区分受管选择器、普通本地账号和资产投递的身份边界。
def decode_call(raw: bytes) -> Call:
    row = validate_object(strict_json(raw), closed({
        "schema": {"const": "plugin_host_call.v2"}, "action": text(96, 1), "payload": {"type": "object"}}, {
        "idempotency_key": text(128, 16, r"^[A-Za-z0-9_-]{16,128}$"),
        "as_user": closed({"user_id": text(64, 3, r"^[a-z][a-z0-9_-]{2,63}$")})}))
    spec = action_spec(row["action"])
    if spec.requires_idempotency != ("idempotency_key" in row):
        raise HostAPIError("INVALID_SCHEMA")
    users = row["action"].startswith(("users.", "governance.reviews.", "assets.delivery."))
    if row["action"].startswith(("users.", "governance.reviews.")) and "as_user" in row:
        raise HostAPIError("INVALID_SCHEMA")
    reject_identity_injection(row["payload"], users)
    payload = spec.validate_payload(row["payload"])
    if (row["action"].startswith("users.local.") and row["action"] != "users.local.audit_group.set"
            and "user_id" in payload):
        validate_local_user_id(payload["user_id"])
    target = validate_local_user_id(row["as_user"]["user_id"]) if "as_user" in row else None
    return Call(row["schema"], row["action"], payload, row.get("idempotency_key"), target)


# LLM: 固定普通用户权限不接受父权限或全局auth开关作为默认值。
# 函数用途: 创建不可提权的权限对象。
def restricted_permission() -> RestrictedPermission:
    return RestrictedPermission()


# LLM: 仅使用真实规范终态和已知阶段，不从空日志或取消受理猜成功。
# 函数用途: 将原请求状态转换为公开状态。
def project_request_state(*, receipt_state: str, claim_state: str, cancellation_requested: bool,
                          canonical_terminal: dict | None, execution_unknown: bool) -> str:
    if canonical_terminal and canonical_terminal.get("status") in {"succeeded", "failed", "cancelled"}:
        return canonical_terminal["status"]
    if execution_unknown:
        return "unknown"
    if cancellation_requested:
        return "cancelling"
    if claim_state in {"running", "waiting_approval", "waiting_input"}:
        return claim_state
    if claim_state == "claimed":
        return "running"
    return receipt_state if receipt_state in {"queued", "admission_wait"} else "unknown"


# LLM: 路径、哈希与owner的真实复验仍由制品领域完成。
# 函数用途: 提供稳定的制品参数校验入口。
def validate_artifact_read(payload: dict) -> dict:
    reject_identity_injection(payload)
    return action_spec("workspace.artifacts.read").validate_payload(payload)


# LLM: 单一授权实现惰性导入以免与DTO循环依赖。
# 函数用途: 经真实来源核验调用。
def authorize_call(ingress: PluginIngressSource, call: Call) -> AuthorizedCall:
    from .authorization import authorize_call as authorize
    return authorize(ingress, call)


# LLM: 路径不得从插件正文生成，resolver由宿主组合根提供。
# 函数用途: 取得不初始化Agent的只读上下文。
def resolve_read_context(authorized: AuthorizedCall) -> OwnerReadContext:
    from .authorization import revalidate_call
    revalidate_call(authorized)
    return authorized.binding.source._issuer.resolve_read(authorized)


# LLM: 没有C的受限factory时失败关闭，绝不使用main invoker回退。
# 函数用途: 取得当前调用的独立普通用户执行上下文。
def resolve_execution_context(authorized: AuthorizedCall) -> OwnerResolutionResult:
    from .authorization import revalidate_call
    revalidate_call(authorized)
    return authorized.binding.source._issuer.resolve_execution(authorized)


# LLM: 组合根只选择静态端口，不动态导入插件指定方法。
# 函数用途: 将已验证调用分发到对应领域。
def dispatch_call(authorized: AuthorizedCall, ports: HostDomainPorts) -> DomainResult:
    from .composition import dispatch_call as dispatch
    return dispatch(authorized, ports)
