# LLM: 采样参数仅由显式配置及已核对的精确服务端协议决定；不读取正文，不改变会话身份或重试策略。
# 模块用途: 统一 top_p 校验与已知 DeepSeek V4 Flash 采样默认值，避免任意兼容端点被强加参数。
from __future__ import annotations

import math
from urllib.parse import urlsplit


# LLM: 配置/schema/backend 共用有限数值校验；None 或空串表示未覆盖，布尔值不是数值。
# 函数用途: 把用户填写的概率转换为 0 至 1 的数值；非法值仅返回固定错误，不输出原始输入。
def validate_top_p(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("top_p 须留空或填写 0 至 1 的有限数值。")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("top_p 须留空或填写 0 至 1 的有限数值。") from exc
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError("top_p 须留空或填写 0 至 1 的有限数值。")
    return number


# LLM: 这是已核对的提供商方言，不是开放世界模型清单；未知模型/代理保持通用显式配置路径。
# 函数用途: 判断是否为官方或 OpenCode Go/Zen 的 V4 Flash，防止域名后缀、端口和私有路径误匹配。
def uses_deepseek_flash_sampling(api_base: str, model_name: str) -> bool:
    try:
        endpoint = urlsplit(api_base)
        if endpoint.scheme != "https" or endpoint.port not in (None, 443):
            return False
    except ValueError:
        return False
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        return False
    path = endpoint.path.rstrip("/")
    if endpoint.hostname == "api.deepseek.com" and path in ("", "/v1"):
        return model_name in {"deepseek-v4-flash", "deepseek-v4-flash-0731", "deepseek-v4-flash:0731"}
    if endpoint.hostname == "opencode.ai" and path in ("/zen/v1", "/zen/go/v1"):
        return model_name in {"deepseek-v4-flash", "deepseek-v4-flash-0731", "deepseek-v4-flash:0731"}
    return False


# LLM: 仅 Chat 方言使用供应商默认；显式值保留到出站，已知 thinking 下限与官方协议一致但不改保存值。
# 函数用途: 生成本次 top_p 字段；普通模型未填写时省略，Flash 思考默认 0.95、非思考固定 1。
def chat_top_p(api_base: str, model_name: str, value: object, *, thinking_disabled: bool = False) -> float | None:
    explicit = validate_top_p(value)
    if not uses_deepseek_flash_sampling(api_base, model_name):
        return explicit
    if thinking_disabled:
        return 1.0
    return max(0.95, explicit) if explicit is not None else 0.95


# LLM: 返回本次 Chat 的采样副本；保留显式温度，不读写工作片或 canonical 历史。
# 函数用途: 把默认/显式采样集中到一处，普通端点继续发送原温度，已知 Flash 省略未显式温度。
def chat_sampling_fields(api_base: str, model_name: str, *, top_p: float | None, temperature: float,
                         temperature_explicit: bool, thinking_disabled: bool = False) -> dict[str, float]:
    result = {}
    if temperature_explicit or not uses_deepseek_flash_sampling(api_base, model_name):
        result["temperature"] = temperature
    probability = chat_top_p(api_base, model_name, top_p, thinking_disabled=thinking_disabled)
    if probability is not None:
        result["top_p"] = probability
    return result
