# LLM: “智能程度”（推理强度）的唯一换算点。用户档位 auto/off/low/medium/high/max 与模型控制方式
#   effort/budget/none 在这里变成供应商请求字段；控制方式来自 profile 显式声明（reasoning_control），
#   auto 时只对 2026-09-26 实测确认过的供应商给默认值，其余一律 none（不发任何字段、如实告知不支持），
#   绝不按模型自述或回复正文判断能力。off 复用原 thinking_disabled 路径（采样与 DeepSeek 兼容规则一致），
#   强制工具选择时原关闭思考优先。改动须同步 test_reasoning_effort*.py、gateway_model_adoption 与子代理首轮选模投影。
# 模块用途: 定义智能程度档位、解析模型的思考控制方式，并把档位换算成各协议的请求字段。
from __future__ import annotations

from urllib.parse import urlsplit

REASONING_LEVELS = ("auto", "off", "low", "medium", "high", "max")
REASONING_CONTROLS = ("auto", "effort", "budget", "none")
LEVEL_LABELS = {"auto": "自动（服务商默认）", "off": "关闭思考", "low": "低", "medium": "中", "high": "高", "max": "最高"}
CONTROL_LABELS = {"effort": "按推理强度档位发送", "budget": "按思考预算发送（部分服务商只按开/关生效）", "none": "不支持调节"}
# 思考预算按档位取值，最终再夹到 [1024, max_tokens-1024]；Anthropic 协议要求预算小于 max_tokens。
_BUDGET_TOKENS = {"low": 2048, "medium": 6144, "high": 12288, "max": 1 << 20}
_MIN_BUDGET_TOKENS = 1024
# 已知供应商的默认控制方式（只是优化，profile 可显式覆盖）：DeepSeek 官方 OpenAI 兼容接口的
# reasoning_effort 与 thinking 开关实测生效；其 Anthropic 兼容接口只有思考开关生效。
_KNOWN_CONTROLS = {
    ("api.deepseek.com", "openai"): "effort",
    ("api.deepseek.com", "anthropic"): "budget",
}
_PROTOCOLS = {"anthropic_compatible": "anthropic", "anthropic": "anthropic",
              "openai_compatible": "openai", "openai": "openai"}


# LLM: 大小写与首尾空白不敏感；不认识的值返回空串，由调用方决定回退默认或报错，绝不猜。
# 函数用途: 把一个档位写法规范成 REASONING_LEVELS 之一。
def normalize_reasoning_level(value: object) -> str:
    level = str(value or "").strip().lower()
    return level if level in REASONING_LEVELS else ""


# LLM: 与档位同样的规范化规则；空值视为 auto。
# 函数用途: 把一个控制方式写法规范成 REASONING_CONTROLS 之一。
def normalize_reasoning_control(value: object) -> str:
    control = str(value or "auto").strip().lower()
    return control if control in REASONING_CONTROLS else ""


# LLM: 显式声明优先；auto 按 (接口域名, 协议) 查已知表，查不到一律 none。model_backend 只映射到协议族，
#   Responses 等尚未接入字段换算的协议保持 none。
# 函数用途: 得到一个模型实际采用的思考控制方式。
def resolved_reasoning_control(declared: object, api_base: object, model_backend: object) -> str:
    control = normalize_reasoning_control(declared) or "auto"
    protocol = _PROTOCOLS.get(str(model_backend or "").strip().lower(), "")
    if not protocol:
        return "none"
    if control != "auto":
        return control
    host = (urlsplit(str(api_base or "")).hostname or "").lower()
    return _KNOWN_CONTROLS.get((host, protocol), "none")


# LLM: 返回 (thinking_disabled, reasoning_effort)：none 或 auto 档位不改变原请求；off 走原关闭思考；
#   强制工具选择（forced=True）时原关闭思考优先、不再发档位。reasoning_effort 只会是 low/medium/high/max 或空串。
# 函数用途: 按档位和模型控制方式决定一次请求的思考开关与档位。
def reasoning_request_values(level: str, control: str, *, forced: bool) -> tuple[bool, str]:
    level = normalize_reasoning_level(level)
    if control not in {"effort", "budget"} or level in {"", "auto"}:
        return forced, ""
    if level == "off":
        return True, ""
    return forced, "" if forced else level


# LLM: 只在 reasoning_effort 非空时调用；effort 按协议写 reasoning_effort（OpenAI Chat）或 output_config.effort
#   （Anthropic），budget 写 thinking.enabled（Anthropic 带夹紧后的 budget_tokens）。返回要合入载荷的字段。
# 函数用途: 把档位换算成一次请求要加的供应商字段。
def reasoning_payload_fields(control: str, level: str, protocol: str, max_tokens: int) -> dict[str, object]:
    if level not in _BUDGET_TOKENS or control not in {"effort", "budget"}:
        return {}
    if control == "effort":
        return {"reasoning_effort": level} if protocol == "openai" else {"output_config": {"effort": level}}
    if protocol != "anthropic":
        return {"thinking": {"type": "enabled"}}
    ceiling = max(_MIN_BUDGET_TOKENS, int(max_tokens) - _MIN_BUDGET_TOKENS)
    return {"thinking": {"type": "enabled", "budget_tokens": max(_MIN_BUDGET_TOKENS, min(_BUDGET_TOKENS[level], ceiling))}}


# LLM: 给 /effort 回执与状态展示用的人读说明；只由结构化的档位与控制方式生成。
# 函数用途: 描述某档位在某模型上会怎样生效。
def describe_reasoning_effect(level: str, control: str) -> str:
    level = normalize_reasoning_level(level) or "auto"
    if level == "auto":
        return "不额外发送推理参数，由服务商按默认方式思考。"
    if control == "none":
        return "当前模型不支持调节智能程度（没有已确认可用的参数），本设置暂不改变请求；换到支持的模型后生效。"
    if level == "off":
        return "请求时关闭思考。"
    return f"{CONTROL_LABELS[control]}：{LEVEL_LABELS[level]}。"


__all__ = [
    "CONTROL_LABELS",
    "LEVEL_LABELS",
    "REASONING_CONTROLS",
    "REASONING_LEVELS",
    "describe_reasoning_effect",
    "normalize_reasoning_control",
    "normalize_reasoning_level",
    "reasoning_payload_fields",
    "reasoning_request_values",
    "resolved_reasoning_control",
]
