from __future__ import annotations

"""LLM: 适配器注册表是 provider 到 adapter 的唯一解析入口；禁止在发送服务里硬编码 channel if/else。

模块用途: 注册已启动的 IM adapter 或按需构建的出站 adapter，并声明其稳定投递能力。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..conversation.channels import ChannelTargetDecision

_LOGGER = logging.getLogger(__name__)
AdapterFactory = Callable[[], Any | None]
TargetValidator = Callable[[str, object], ChannelTargetDecision]


# LLM: 能力来自 adapter 注册事实，不让模型根据自然语言或方法名猜通道是否支持主动推送和附件。
# 类用途: 描述一个通道 adapter 可由统一投递服务调用的原生能力。
@dataclass(frozen=True)
class ChannelCapabilities:
    text: bool = True
    reply: bool = True
    proactive: bool = False
    files: bool = False
    images: bool = False


# LLM: 注册表支持真实 adapter 实例和懒工厂；解析结果含失败 None 都缓存，避免缺凭据时反复构建。
# 类用途: 维护 channel 名、构建方式和能力声明之间的一一映射。
class ChannelAdapterRegistry:
    # LLM: 每个运行边界持有自己的 registry，adapter 和凭据状态不得跨 owner 隐式共享。
    # 函数用途: 创建空的线程安全通道注册表。
    def __init__(self) -> None:
        self._factories: dict[str, AdapterFactory] = {}
        self._resolved: dict[str, Any | None] = {}
        self._capabilities: dict[str, ChannelCapabilities] = {}
        self._target_validators: dict[str, TargetValidator] = {}
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
    ) -> None:
        key = _channel_key(channel)
        inferred = capabilities or _infer_capabilities(adapter)
        with self._lock:
            self._factories.pop(key, None)
            self._resolved[key] = adapter
            self._capabilities[key] = inferred
            self._target_validators[key] = target_validator or _builtin_target_validator(key)

    # LLM: 工厂只负责构造一个 provider adapter，provider 私有凭据和依赖不得进入 DeliveryService。
    # 函数用途: 注册一个按第一次发送时才执行的 adapter 工厂。
    def register_factory(
        self,
        channel: str,
        factory: AdapterFactory,
        *,
        capabilities: ChannelCapabilities,
        target_validator: TargetValidator | None = None,
    ) -> None:
        key = _channel_key(channel)
        with self._lock:
            self._factories[key] = factory
            self._resolved.pop(key, None)
            self._capabilities[key] = capabilities
            self._target_validators[key] = target_validator or _builtin_target_validator(key)

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

    # LLM: target 类型校验属于 provider 注册合同；发送服务只调用本方法，不维护平台分支。
    # 函数用途: 按通道已注册 validator 校验可信路由地址。
    def validate_target(self, channel: str, target: object) -> ChannelTargetDecision:
        key = _channel_key(channel)
        with self._lock:
            validator = self._target_validators.get(key) or _builtin_target_validator(key)
        return validator(key, target)


# LLM: 默认注册表只登记官方实现的 Feishu adapter；新增 IM 通过新工厂注册，不修改投递主流程。
# 函数用途: 根据当前配置创建内置通道的懒加载注册表。
def build_default_channel_registry(config: Any) -> ChannelAdapterRegistry:
    registry = ChannelAdapterRegistry()
    registry.register_factory(
        "feishu",
        lambda: _build_feishu_adapter(config),
        capabilities=ChannelCapabilities(
            text=True, reply=True, proactive=True, files=True, images=True
        ),
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


# LLM: 现成 adapter 的附件能力可由真实 callable 方法判定，主动发送仍需注册方显式声明。
# 函数用途: 为运行中的入站 adapter 推导不会越权的默认能力。
def _infer_capabilities(adapter: Any) -> ChannelCapabilities:
    return ChannelCapabilities(
        text=callable(getattr(adapter, "send_message", None)),
        reply=callable(getattr(adapter, "finalize_response", None)),
        proactive=False,
        files=callable(getattr(adapter, "send_file", None)),
        images=callable(getattr(adapter, "send_image", None)),
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


__all__ = [
    "ChannelAdapterRegistry",
    "ChannelCapabilities",
    "TargetValidator",
    "build_default_channel_registry",
]
