# LLM: 显式配置到后端实例只有此工厂；缺配置判据供调度共用，不读取凭据、不探测网络、不选择替代模型。
# 模块用途: 构造用户指定的本地、HTTP 或 OAuth 后端；修改时核对会话模型选择与认证测试。
from __future__ import annotations

from typing import Any

from ..settings.defaults import configured_context_window_tokens, effective_max_output_tokens
from .anthropic import AnthropicCompatibleBackend
from .base import BackendOptions, BaseBackend, EchoBackend, UnconfiguredBackend
from .openai_chat import OpenAICompatibleBackend
from .reasoning_control import resolved_reasoning_control
from .structured_output_mode import resolved_structured_output


# LLM: 后端构造与后台调度共用本地缺配置判据；不探测网络、密钥权益或模型能力，未知协议仍由工厂拒绝。
# 函数用途: 检查是否缺少明确的模型连接信息，避免把尚未设置模型当成临时网络故障。
def model_configuration_missing(name: str, config: Any | None = None) -> bool:
    if not str(name or "").strip():
        return True
    return config is not None and name in {
        "openai_compatible", "openai_responses", "anthropic_compatible",
    } and not (str(config.model_name or "").strip() and str(config.api_base or "").strip())


# LLM: temperature 是唯一采样温度旋钮（原 model_temperature_explicit 已并入）：空 = 不带温度字段，后端沿用供应商默认；
#   填了数字 = 发送。后端内部的 temperature_explicit 只在这里按“是否填写”推导，不再有第二个配置来源。
# 函数用途: 把配置里的温度换成后端构造参数，没填就返回空字典。
def _temperature_options(config: Any) -> dict[str, Any]:
    raw = getattr(config, "temperature", None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    return {"temperature": float(raw), "temperature_explicit": True}


# LLM: 只按显式协议构造后端；缺配置判据与后台准入共用，OAuth 引用附加认证层，不因失败换接口。
# 函数用途: 创建用户指定的适配器并透传媒体预算；尚未配置时真实调用明确拒绝。
def get_backend(name: str, config: Any | None = None) -> BaseBackend:
    """Resolve a configured backend name to a backend adapter instance."""

    if name == "echo":
        return EchoBackend()
    if model_configuration_missing(name, config):
        return UnconfiguredBackend()
    if config is None:
        raise ValueError("真实模型后端需要传入 config。")

    common = BackendOptions(
        api_base=config.api_base,
        api_key=config.api_key,
        model_name=config.model_name,
        input_media_max_bytes=getattr(config, "input_media_max_bytes", 16 * 1024 * 1024),
        request_timeout=config.request_timeout,
        # 输出上限按已知窗口统一夹取（唯一权威见 effective_max_output_tokens），默认模型与模型档案走同一规则。
        max_tokens=effective_max_output_tokens(config),
        context_window_tokens=configured_context_window_tokens(config),
        **_temperature_options(config),
        top_p=getattr(config, "top_p", None),
        stream_enabled=getattr(config, "stream_enabled", True),
        prompt_cache_enabled=getattr(config, "anthropic_prompt_cache_enabled", True),
        custom_headers=getattr(config, "model_custom_headers", {}),
        session_header=getattr(config, "model_session_header", ""),
        reasoning_control=resolved_reasoning_control(
            getattr(config, "model_reasoning_control", "auto"), config.api_base, name,
        ),
        structured_output=resolved_structured_output(
            getattr(config, "model_structured_output", "auto"), config.api_base, name,
        ),
    )

    auth_ref = getattr(config, "model_auth_ref", {})
    if auth_ref:
        from .oauth import OAuthChatBackend, OAuthMessagesBackend, OAuthResponsesBackend

        adapters = {"openai_compatible": OAuthChatBackend, "openai_responses": OAuthResponsesBackend,
                    "anthropic_compatible": OAuthMessagesBackend}
        if name not in adapters or (auth_ref.get("mode") == "chatgpt" and name != "openai_responses"):
            raise ValueError("登录类型与模型接口不匹配。")
        return adapters[name](common, auth_ref=auth_ref)

    if name == "openai_compatible":
        return OpenAICompatibleBackend(common)
    if name == "openai_responses":
        from .responses import OpenAIResponsesBackend

        return OpenAIResponsesBackend(common)
    if name == "anthropic_compatible":
        return AnthropicCompatibleBackend(common)

    raise ValueError(
        "未知模型后端: %s。当前内置 echo / openai_compatible / openai_responses / anthropic_compatible。" % name
    )
