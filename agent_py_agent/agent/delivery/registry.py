from __future__ import annotations

"""LLM: 适配器注册表是 provider 到 adapter 的唯一解析入口；禁止在发送服务里硬编码 channel if/else。

模块用途: 注册已启动的 IM adapter 或按需构建的出站 adapter，并声明其稳定投递能力。
"""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from ..conversation.channels import ChannelTargetDecision, DeliveryContext

_LOGGER = logging.getLogger(__name__)
AdapterFactory = Callable[[], Any | None]
TargetValidator = Callable[[str, object], ChannelTargetDecision]
RuntimeHealthProvider = Callable[[], Mapping[str, object]]


# LLM: 能力来自 adapter 注册事实，不让模型根据自然语言或方法名猜通道是否支持主动推送和附件。
# 类用途: 描述一个通道 adapter 可由统一投递服务调用的原生能力。
@dataclass(frozen=True)
class ChannelCapabilities:
    text: bool = True
    reply: bool = True
    proactive: bool = False
    files: bool = False
    images: bool = False
    # LLM: 该标志只表示 provider 会按同一投递键折叠重复请求，不能从 adapter 名称或模型正文推断。
    # 字段用途: 允许统一副作用账本在未知终态后安全重放同一逻辑消息。
    provider_idempotency: bool = False


# LLM: health 是 adapter 生命周期/探针写入的结构化事实；configured、binding 不能隐式升级它。
# 类用途: 保存一个通道最近一次健康判断及其时间和稳定错误码。
@dataclass(frozen=True)
class ChannelHealth:
    state: str = "not_probed"
    checked_at: str = ""
    error_code: str = ""


# LLM: 该快照是能力自述与 doctor 的统一只读投影，不含凭据或真实收件人 ID。
# 类用途: 描述一个已注册通道的安装、配置、健康、当前绑定和原生投递能力。
@dataclass(frozen=True)
class ChannelRuntimeSnapshot:
    name: str
    support: str
    installed: bool
    configured: bool | None
    health: ChannelHealth
    current_bound: bool
    binding_target_kind: str
    binding_error_code: str
    capabilities: ChannelCapabilities
    state: str
    ready: bool

    # LLM: 序列化必须保留四层事实并只展开不可变能力值，禁止输出 target 或 secret。
    # 函数用途: 生成可安全提供给模型和状态接口的字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "support": self.support,
            "installed": self.installed,
            "configured": self.configured,
            "health": asdict(self.health),
            "current_bound": self.current_bound,
            "binding_target_kind": self.binding_target_kind,
            "binding_error_code": self.binding_error_code,
            "capabilities": asdict(self.capabilities),
            "state": self.state,
            "ready": self.ready,
            "setup": f"my-agent adapter start --channel {self.name}",
        }


# LLM: 注册表支持真实 adapter 实例和懒工厂；解析结果含失败 None 都缓存，避免缺凭据时反复构建。
# 类用途: 维护 channel 名、构建方式和能力声明之间的一一映射。
class ChannelAdapterRegistry:
    # LLM: 每个运行边界持有自己的 registry，adapter 和凭据状态不得跨 owner 隐式共享。
    # 函数用途: 创建空的线程安全通道注册表。
    def __init__(self, runtime_health_provider: RuntimeHealthProvider | None = None) -> None:
        self._factories: dict[str, AdapterFactory] = {}
        self._resolved: dict[str, Any | None] = {}
        self._capabilities: dict[str, ChannelCapabilities] = {}
        self._target_validators: dict[str, TargetValidator] = {}
        self._configured: dict[str, bool | None] = {}
        self._support: dict[str, str] = {}
        self._health: dict[str, ChannelHealth] = {}
        self._runtime_health_provider = runtime_health_provider
        self._lock = threading.RLock()

    # LLM: 已启动入站 adapter 注册后可直接复用作出站；同名注册以最新实例和能力为权威。
    # 函数用途: 注册一个现成 adapter 实例。
    def register_adapter(
        self,
        channel: str,
        adapter: Any,
        *,
        capabilities: ChannelCapabilities | None = None,
        target_validator: TargetValidator | None = None,
        configured: bool | None = True,
        support: str = "runtime_adapter",
    ) -> None:
        key = _channel_key(channel)
        inferred = capabilities or _infer_capabilities(adapter)
        with self._lock:
            self._factories.pop(key, None)
            self._resolved[key] = adapter
            self._capabilities[key] = inferred
            self._target_validators[key] = target_validator or _builtin_target_validator(key)
            self._configured[key] = configured
            self._support[key] = str(support or "runtime_adapter")
            self._health.setdefault(key, ChannelHealth(state="registered"))

    # LLM: 工厂只负责构造一个 provider adapter，provider 私有凭据和依赖不得进入 DeliveryService。
    # 函数用途: 注册一个按第一次发送时才执行的 adapter 工厂。
    def register_factory(
        self,
        channel: str,
        factory: AdapterFactory,
        *,
        capabilities: ChannelCapabilities,
        target_validator: TargetValidator | None = None,
        configured: bool | None = None,
        support: str = "builtin_adapter",
    ) -> None:
        key = _channel_key(channel)
        with self._lock:
            self._factories[key] = factory
            self._resolved.pop(key, None)
            self._capabilities[key] = capabilities
            self._target_validators[key] = target_validator or _builtin_target_validator(key)
            self._configured[key] = configured
            self._support[key] = str(support or "builtin_adapter")
            self._health[key] = ChannelHealth()

    # LLM: adapter 解析只走注册事实，未知 channel 返回 None，绝不能回落到某个默认 provider。
    # 函数用途: 取得已注册 adapter，并在需要时至多调用一次懒工厂。
    def adapter_for(self, channel: str) -> Any | None:
        key = _channel_key(channel)
        with self._lock:
            if key in self._resolved:
                return self._resolved[key]
            factory = self._factories.get(key)
            if factory is None:
                return None
            try:
                adapter = factory()
            except Exception as exc:
                _LOGGER.error(
                    "channel adapter factory failed channel=%s error=%s: %s",
                    key,
                    type(exc).__name__,
                    exc,
                )
                adapter = None
            self._resolved[key] = adapter
            return adapter

    # LLM: 未注册通道没有任何隐含能力，调用方必须据此 fail-closed 或返回不可用。
    # 函数用途: 返回通道的已声明能力。
    def capabilities_for(self, channel: str) -> ChannelCapabilities:
        with self._lock:
            return self._capabilities.get(
                _channel_key(channel), ChannelCapabilities(text=False, reply=False)
            )

    # LLM: "声明过这个通道"与"此刻能不能发"必须分开：adapter 掉线、凭据缺失、工厂构造失败只影响后者。
    # 后台路由的外发义务归属只能读这个声明事实，否则一次暂时不可用就会把义务抹掉、吞掉通知。
    # 函数用途: 判断当前部署是否声明过该通道（注册过 adapter 实例或懒工厂）。
    def declares_channel(self, channel: str) -> bool:
        with self._lock:
            return _channel_key(channel) in self._capabilities

    # LLM: 生命周期调用方只能写当前协议 health state；正文异常不得参与健康状态推断。
    # 函数用途: 记录 adapter 已启动、停止或失败的结构化健康事实。
    def mark_health(
        self,
        channel: str,
        state: str,
        *,
        checked_at: str = "",
        error_code: str = "",
    ) -> None:
        key = _channel_key(channel)
        if not key:
            return
        with self._lock:
            if key not in self._capabilities:
                return
            self._health[key] = ChannelHealth(
                state=str(state or "not_probed"),
                checked_at=str(checked_at or ""),
                error_code=str(error_code or ""),
            )

    # LLM: snapshot 是 installed/configured/health/current-bound 的唯一合并点；读取失败保留 not_probed。
    # 函数用途: 返回不含目标 ID 和凭据的通道运行快照，供能力清单与诊断使用。
    def runtime_snapshot(
        self,
        binding: DeliveryContext | None = None,
    ) -> tuple[ChannelRuntimeSnapshot, ...]:
        external_health = self._external_health()
        with self._lock:
            snapshots = [
                self._snapshot_for(key, binding, external_health)
                for key in sorted(self._capabilities)
            ]
        return tuple(snapshots)

    # LLM: 单通道投影只消费 registry 内结构化字段和可信 DeliveryContext，不读取用户正文。
    # 函数用途: 组装一个通道的完整运行快照。
    def _snapshot_for(
        self,
        key: str,
        binding: DeliveryContext | None,
        external_health: Mapping[str, ChannelHealth],
    ) -> ChannelRuntimeSnapshot:
        external = external_health.get(key) or external_health.get("*")
        health = external or self._local_health(key)
        configured = self._configured.get(key)
        current_bound, target_kind, binding_error = self._binding_state(key, binding)
        ready = configured is True and health.state == "healthy" and current_bound
        return ChannelRuntimeSnapshot(
            name=key,
            support=self._support.get(key, "runtime_adapter"),
            installed=True,
            configured=configured,
            health=health,
            current_bound=current_bound,
            binding_target_kind=target_kind,
            binding_error_code=binding_error,
            capabilities=self._capabilities[key],
            state=_channel_availability_state(configured, health.state, current_bound, ready),
            ready=ready,
        )

    # LLM: 现成 runtime adapter 的 running 属性是进程内健康事实；懒出站工厂不能用它推断连接健康。
    # 函数用途: 在没有跨进程覆盖时把已注册 adapter 的实时 running 状态投影出来。
    def _local_health(self, key: str) -> ChannelHealth:
        health = self._health.get(key, ChannelHealth())
        if self._support.get(key) != "runtime_adapter":
            return health
        adapter = self._resolved.get(key)
        running = getattr(adapter, "running", None)
        if running is True:
            return ChannelHealth(state="healthy", checked_at=health.checked_at)
        if running is False and health.state == "healthy":
            return ChannelHealth(
                state="unhealthy",
                checked_at=health.checked_at,
                error_code="CHANNEL_ADAPTER_NOT_RUNNING",
            )
        return health

    # LLM: current-bound 只认可信 context 与 provider validator；模型不能传任意目标获得绑定状态。
    # 函数用途: 校验本轮是否真正绑定到该通道，并只返回目标类型和错误码。
    def _binding_state(
        self,
        key: str,
        binding: DeliveryContext | None,
    ) -> tuple[bool, str, str]:
        if binding is None or _channel_key(binding.channel) != key:
            return False, "", ""
        validator = self._target_validators.get(key) or _builtin_target_validator(key)
        decision = validator(key, binding.target)
        return decision.allowed, decision.target_kind, decision.error_code

    # LLM: 外部 provider 只提供健康覆盖；异常必须 fail-soft 为 registry 自身 not_probed，不得伪造 healthy。
    # 函数用途: 安全读取 adapter 进程发布的跨进程健康快照。
    def _external_health(self) -> Mapping[str, ChannelHealth]:
        if self._runtime_health_provider is None:
            return {}
        try:
            raw = self._runtime_health_provider()
        except Exception:
            return {}
        if not isinstance(raw, Mapping):
            return {}
        result: dict[str, ChannelHealth] = {}
        for channel, value in raw.items():
            key = _channel_key(channel)
            if not key:
                continue
            if isinstance(value, ChannelHealth):
                result[key] = value
            elif isinstance(value, Mapping):
                result[key] = ChannelHealth(
                    state=str(value.get("state") or "not_probed"),
                    checked_at=str(value.get("checked_at") or ""),
                    error_code=str(value.get("error_code") or ""),
                )
        return result

    # LLM: target 类型校验属于 provider 注册合同；发送服务只调用本方法，不维护平台分支。
    # 函数用途: 按通道已注册 validator 校验可信路由地址。
    def validate_target(self, channel: str, target: object) -> ChannelTargetDecision:
        key = _channel_key(channel)
        with self._lock:
            validator = self._target_validators.get(key) or _builtin_target_validator(key)
        return validator(key, target)


# LLM: 默认注册表只登记官方实现的 Feishu adapter；新增 IM 通过新工厂注册，不修改投递主流程。
# 函数用途: 根据当前配置创建内置通道的懒加载注册表。
def build_default_channel_registry(
    config: Any,
    *,
    runtime_health_provider: RuntimeHealthProvider | None = None,
) -> ChannelAdapterRegistry:
    registry = ChannelAdapterRegistry(runtime_health_provider)
    registry.register_factory(
        "feishu",
        lambda: _build_feishu_adapter(config),
        capabilities=ChannelCapabilities(
            text=True,
            reply=True,
            proactive=True,
            files=True,
            images=True,
            provider_idempotency=True,
        ),
        configured=_configured_secret_fields(config, ("feishu_app_id", "feishu_app_secret")),
    )
    registry.register_factory(
        "qq",
        lambda: _build_qq_adapter(config),
        capabilities=ChannelCapabilities(text=True, reply=True, proactive=True),
        configured=_configured_secret_fields(config, ("qq_app_id", "qq_app_secret")),
    )
    return registry


# LLM: 飞书凭据仅在第一次真实出站时解析；这里只构造 REST adapter，不启动入站长连接。
# 函数用途: 从配置安全构建一个可出站的 FeishuAdapter，缺凭据返回 None。
def _build_feishu_adapter(config: Any) -> Any | None:
    from ..adapter.feishu import FeishuAdapter
    from ..settings.secret_ref import resolve_secret_ref

    app_id = resolve_secret_ref(str(getattr(config, "feishu_app_id", "") or ""))
    app_secret = resolve_secret_ref(str(getattr(config, "feishu_app_secret", "") or ""))
    if not app_id or not app_secret:
        return None
    return FeishuAdapter({"feishu_app_id": app_id, "feishu_app_secret": app_secret})


# LLM: QQ 与飞书共用 registry 工厂合同，缺凭据只返回 None，不尝试访问网络。
# 函数用途: 从配置安全构建可出站 QQAdapter，缺凭据时保持不可用。
def _build_qq_adapter(config: Any) -> Any | None:
    from ..adapter.qq import QQAdapter
    from ..settings.secret_ref import resolve_secret_ref

    app_id = resolve_secret_ref(str(getattr(config, "qq_app_id", "") or ""))
    app_secret = resolve_secret_ref(str(getattr(config, "qq_app_secret", "") or ""))
    if not app_id or not app_secret:
        return None
    return QQAdapter({"qq_app_id": app_id, "qq_app_secret": app_secret})


# LLM: configured 只表示 secret ref 可解析且非空；网络健康必须由独立 runtime health 提供。
# 函数用途: 判断内置 adapter 的必需凭据是否齐全。
def _configured_secret_fields(config: Any, fields: tuple[str, ...]) -> bool:
    from ..settings.secret_ref import resolve_secret_ref

    return all(
        bool(resolve_secret_ref(str(getattr(config, field, "") or "")).strip())
        for field in fields
    )


# LLM: 现成 adapter 的附件能力可由真实 callable 方法判定，主动发送仍需注册方显式声明。
# 函数用途: 为运行中的入站 adapter 推导不会越权的默认能力。
def _infer_capabilities(adapter: Any) -> ChannelCapabilities:
    return ChannelCapabilities(
        text=callable(getattr(adapter, "send_message", None)),
        reply=callable(getattr(adapter, "finalize_response", None)),
        proactive=False,
        files=callable(getattr(adapter, "send_file", None)),
        images=callable(getattr(adapter, "send_image", None)),
        provider_idempotency=bool(
            getattr(adapter, "provider_idempotent_delivery", False)
        ),
    )


# LLM: 内置 provider 的地址合同集中在注册层；第三方 IM 用 register_* 的 target_validator 扩展。
# 函数用途: 返回内置通道 validator，未知通道使用不含空白的 opaque ID 合同。
def _builtin_target_validator(channel: str) -> TargetValidator:
    return _validate_feishu_target if channel == "feishu" else _validate_opaque_target


# LLM: Feishu 当前发送 API 固定 receive_id_type=open_id，因此只接受 ou_ 前缀的结构化 owner ID。
# 函数用途: 校验 Feishu 出站目标并标注 open_id 类型。
def _validate_feishu_target(channel: str, target: object) -> ChannelTargetDecision:
    base = _validate_opaque_target(channel, target)
    if not base.allowed:
        return base
    value = str(target or "").strip()
    allowed = value.startswith("ou_") and len(value) > 3
    return ChannelTargetDecision(
        allowed,
        channel,
        "open_id",
        "" if allowed else "CHANNEL_TARGET_INVALID",
    )


# LLM: 未声明专用格式的平台仍要拒绝空值和空白字符，不能把任意正文片段当目标地址。
# 函数用途: 校验开放世界 adapter 的 opaque 用户 ID。
def _validate_opaque_target(channel: str, target: object) -> ChannelTargetDecision:
    value = str(target or "").strip()
    if not value:
        return ChannelTargetDecision(False, channel, "unknown", "CHANNEL_TARGET_MISSING")
    if any(char.isspace() for char in value):
        return ChannelTargetDecision(False, channel, "unknown", "CHANNEL_TARGET_INVALID")
    return ChannelTargetDecision(True, channel, "opaque")


# LLM: 通道名统一小写且不能为空；空值保留为空以便调用方明确判不可用。
# 函数用途: 规范化 registry 的 channel 键。
def _channel_key(channel: object) -> str:
    return str(channel or "").strip().lower()


# LLM: 展示 state 只合并当前协议字段，不从错误正文或通道名猜测可用性。
# 函数用途: 把四层事实折叠成一条简洁但不夸大的可用状态。
def _channel_availability_state(
    configured: bool | None,
    health_state: str,
    current_bound: bool,
    ready: bool,
) -> str:
    if configured is None:
        return "configuration_unknown"
    if configured is False:
        return "setup_required"
    if ready:
        return "ready"
    if health_state == "healthy":
        return "healthy_not_bound" if not current_bound else "healthy"
    if health_state in {"unhealthy", "stopped"}:
        return "unavailable"
    if health_state == "starting":
        return "starting"
    return "configured_not_health_checked"


__all__ = [
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "ChannelHealth",
    "ChannelRuntimeSnapshot",
    "RuntimeHealthProvider",
    "TargetValidator",
    "build_default_channel_registry",
]
