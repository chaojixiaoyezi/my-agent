# LLM: “智能程度”（推理强度）的唯一换算点。用户八档与模型控制方式
#   effort/budget/none 在这里变成供应商请求字段；控制方式来自 profile 显式声明（reasoning_control），
#   auto 时只对实测确认过的供应商给默认值，其余一律 none（不发任何字段、如实告知不支持），
#   绝不按模型自述或回复正文判断能力。off 复用原 thinking_disabled 路径（采样与 DeepSeek 兼容规则一致），
#   强制工具选择时原关闭思考优先。Responses 协议写 reasoning.effort，档位按模型档案的 reasoning_levels
#   （服务商目录声明的可用档位）对应；未声明时只发通用的 low/medium/high。Chat/Messages 沿原通用四档，
#   两个新档映射为 high/max；有声明时过滤，不发声明之外的值。档位、中文名和三种换算只定义在一张表里。
#   Anthropic 预算值与未发送原因共用结构化裁决；空区间不发 thinking，不能抬高输出上限或冒称思考已关闭。
#   改动须同步 test_anthropic_reasoning_budget.py、test_reasoning_effort*.py、test_responses_reasoning.py 与选模投影。
# 模块用途: 定义智能程度档位、解析模型的思考控制方式，并把档位换算成各协议的请求字段。
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


# LLM: 表项只声明用户标签、两个 effort 方言的候选顺序及原始预算；不持有模型配置、不发请求。
# 类用途: 把一个用户档位的所有换算放在同一行，避免菜单、回执与发送各自写一套规则。
@dataclass(frozen=True)
class _ReasoningLevelRule:
    label: str
    effort: tuple[str, ...]
    responses: tuple[str, ...]
    budget: int


_REASONING_LEVEL_RULES = {
    "auto": _ReasoningLevelRule("自动（服务商默认）", (), (), 0),
    "off": _ReasoningLevelRule("关闭思考", (), ("none", "minimal"), 0),
    "low": _ReasoningLevelRule("低", ("low",), ("low",), 2048),
    "medium": _ReasoningLevelRule("中", ("medium",), ("medium",), 6144),
    "high": _ReasoningLevelRule("高", ("high",), ("high",), 12288),
    "xhigh": _ReasoningLevelRule("超高", ("high",), ("xhigh", "high"), 24576),
    "max": _ReasoningLevelRule("最高", ("max", "high"), ("max", "xhigh", "high"), 1 << 20),
    "ultra": _ReasoningLevelRule("极限", ("max", "high"), ("ultra", "max", "xhigh", "high"), 1 << 20),
}
REASONING_LEVELS = tuple(_REASONING_LEVEL_RULES)
REASONING_CONTROLS = ("auto", "effort", "budget", "none")
# 模型能否用 configuration_update 项改档位的三种声明：auto 按已核对的模型名前缀判断，on/off 由档案显式声明。
REASONING_UPDATE_ITEM_MODES = ("auto", "on", "off")
LEVEL_LABELS = {level: rule.label for level, rule in _REASONING_LEVEL_RULES.items()}
CONTROL_LABELS = {"effort": "按推理强度档位发送", "budget": "按思考预算发送（部分服务商只按开/关生效）", "none": "不支持调节"}
# 思考预算按档位取值，最终夹到 [1024, max_tokens-1024]；空区间不发送，不能以抬高下界伪装成合法夹紧。
_MIN_BUDGET_TOKENS = 1024
# 已知供应商的默认控制方式（只是优化，profile 可显式覆盖）：DeepSeek 官方 OpenAI 兼容接口的
# reasoning_effort 与 thinking 开关实测生效；其 Anthropic 兼容接口只有思考开关生效；ChatGPT 订阅的 Responses
# 接口按 reasoning.effort 生效（服务商模型目录逐模型声明 supported_reasoning_levels，09-30 核对）。
_KNOWN_CONTROLS = {
    ("api.deepseek.com", "openai"): "effort",
    ("api.deepseek.com", "anthropic"): "budget",
    ("chatgpt.com", "responses"): "effort",
}
_PROTOCOLS = {"anthropic_compatible": "anthropic", "anthropic": "anthropic",
              "openai_compatible": "openai", "openai": "openai", "openai_responses": "responses"}
# 空声明沿用各协议原通用档位，不把用户新档当作服务商已声明的新能力。
_RESPONSES_GENERIC_LEVELS = ("low", "medium", "high")
_EFFORT_GENERIC_LEVELS = ("low", "medium", "high", "max")


# LLM: 只携带同次载荷的输出上限与冻结后端的声明；调用方已按原规则计算 max_tokens，不拥有第二份配置。
# 类用途: 将预算上限与档位声明收在一起，给协议组包传参时不增加长参数列表。
@dataclass(frozen=True)
class ReasoningPayloadLimits:
    max_tokens: int
    levels: tuple[str, ...] = ()


# LLM: 只保存本次上限下的预算或未发送原因；出站与回执消费同一裁决，不持有配置、不发请求、不写状态。
# 类用途: 区分合法预算与空预算区间，防止回执和出站各算一次而发生偏差。
@dataclass(frozen=True)
class _AnthropicBudgetDecision:
    budget_tokens: int
    reason_code: str = ""


# LLM: 调用方已确认 level 有预算；max_tokens 是本次载荷或工厂派生的常规请求上限。至少保留最小预算及等量正文空间，
#   不能修改输出上限；reasoning_payload_fields 和预算回执必须共用此无副作用裁决，联测边界与正常大上限。
# 函数用途: 算出可以发送的思考预算，区间为空时返回明确原因而不是强行抬到最小值。
def _anthropic_budget_decision(level: str, max_tokens: int) -> _AnthropicBudgetDecision:
    ceiling = int(max_tokens) - _MIN_BUDGET_TOKENS
    if ceiling < _MIN_BUDGET_TOKENS:
        return _AnthropicBudgetDecision(0, "reasoning_budget_interval_empty")
    budget = max(_MIN_BUDGET_TOKENS, min(_REASONING_LEVEL_RULES[level].budget, ceiling))
    return _AnthropicBudgetDecision(budget)


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


# LLM: 显式声明优先；auto 按 (接口域名, 协议) 查已知表，查不到一律 none。model_backend 只映射到协议族
#   （openai / anthropic / responses），未登记的协议保持 none。
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
#   强制工具选择（forced=True）时原关闭思考优先、不再发档位。返回用户档位，协议组包再按同一表和声明过滤。
# 函数用途: 按档位和模型控制方式决定一次请求的思考开关与档位。
def reasoning_request_values(level: str, control: str, *, forced: bool) -> tuple[bool, str]:
    level = normalize_reasoning_level(level)
    if control not in {"effort", "budget"} or level in {"", "auto"}:
        return forced, ""
    if level == "off":
        return True, ""
    return forced, "" if forced else level


# LLM: 发送和回执共用此候选筛选；有声明时绝不外加通用档，缺声明才沿原协议通用范围。只读、不发网络。
# 函数用途: 从唯一档位表中挑出当前协议与模型允许发送的第一个 effort 值。
def _provider_effort(level: str, protocol: str, levels: tuple[str, ...] | list[str]) -> str:
    rule = _REASONING_LEVEL_RULES.get(level)
    if rule is None:
        return ""
    responses = protocol == "responses"
    available = tuple(levels) or (_RESPONSES_GENERIC_LEVELS if responses else _EFFORT_GENERIC_LEVELS)
    candidates = rule.responses if responses else rule.effort
    return next((item for item in candidates if item in available), "")


# LLM: 只用于 Chat/Messages；effort 从唯一表按协议降档后与模型声明取交集，找不到就不发；budget 不发档位字符串，
#   Anthropic 读取与回执同源的预算裁决，空区间不发 thinking；其余预算协议仍只发开关。关闭思考由外层优先处理，
#   不静默抬高输出上限，联合 test_anthropic_reasoning_budget 与两协议组包、投影回归。
# 函数用途: 将用户档位和当前载荷限制换成实际供应商字段，不请求网络、不修改后端配置。
def reasoning_payload_fields(control: str, level: str, protocol: str, limits: ReasoningPayloadLimits) -> dict[str, object]:
    rule = _REASONING_LEVEL_RULES.get(level)
    if rule is None or not rule.budget or control not in {"effort", "budget"}:
        return {}
    if control == "effort":
        sent = _provider_effort(level, protocol, limits.levels)
        if not sent:
            return {}
        return {"reasoning_effort": sent} if protocol == "openai" else {"output_config": {"effort": sent}}
    if protocol != "anthropic":
        return {"thinking": {"type": "enabled"}}
    decision = _anthropic_budget_decision(level, limits.max_tokens)
    if decision.reason_code:
        return {}
    return {"thinking": {"type": "enabled", "budget_tokens": decision.budget_tokens}}


# LLM: 只用于 Responses 协议：control 必须是 effort；levels 是模型档案声明的服务商档位（可空）。disabled=True 表示本次
#   请求要关闭思考（/effort off 或强制工具选择），只有模型声明了 none/minimal 才发送，否则不发字段、交服务商默认。
#   返回要合入载荷的字段或空字典；共用唯一表的候选筛选，xhigh/ultra 不按模型名或回执猜能力。
# 函数用途: 把智能程度档位换算成 Responses 请求的 reasoning 字段。
def responses_reasoning_field(control: str, level: str, levels: tuple[str, ...] | list[str], *, disabled: bool) -> dict:
    if control != "effort":
        return {}
    wanted = "off" if disabled else normalize_reasoning_level(level)
    choice = _provider_effort(wanted, "responses", levels)
    return {"reasoning": {"effort": choice}} if choice else {}


# LLM: 压缩降档的 configuration_update 项（OpenAI GPT-6 系列的 Responses 接口）：档位按同一张表换算成服务商值，
#   和请求级已发的档位相同就不插（白费一项）；control 不是 effort 或换算不出也不插。只读、不发网络。
# 函数用途: 生成要追加到 input 末尾的档位更新项；不需要时返回 None。
def responses_reasoning_update_item(control: str, level: str, current: object, levels: tuple[str, ...] | list[str]) -> dict | None:
    if control != "effort" or not level:
        return None
    choice = _provider_effort(normalize_reasoning_level(level), "responses", levels)
    current_effort = current.get("effort") if isinstance(current, dict) else None
    if not choice or choice == current_effort:
        return None
    return {"type": "configuration_update", "reasoning": {"effort": choice}}


# LLM: 给 /effort 回执与状态展示用的人读说明；只由结构化的档位、控制方式、协议与模型声明的服务商档位生成。
#   Responses 协议必须与发送走同一个换算（responses_reasoning_field）：没声明 none/minimal 时 off 实际不发字段，
#   没声明更高档位时 xhigh/max/ultra 实际发 high——回执要如实说出来。Chat/Messages 也读取发送的同一换算。
#   （2026-09-30 真实核对：ChatGPT 订阅目录没有任何模型声明
#   none/minimal，旧档案没有 reasoning_levels 时 max 发的是 high）。
# 函数用途: 描述某档位在某模型上会怎样生效。
def describe_reasoning_effect(level: str, control: str, *, protocol: str = "", levels: tuple[str, ...] | list[str] = ()) -> str:
    level = normalize_reasoning_level(level) or "auto"
    if level == "auto":
        return "不额外发送推理参数，由服务商按默认方式思考。"
    if control == "none":
        return "当前模型不支持调节智能程度（没有已确认可用的参数），本设置暂不改变请求；换到支持的模型后生效。"
    if protocol == "responses":
        return _describe_responses_effect(level, control, levels)
    if level == "off":
        return "请求时关闭思考。"
    if control == "budget":
        return _describe_budget_effect(level, protocol)
    return _describe_effort_effect(level, protocol, levels)


# LLM: 回执只读实际发送也使用的候选筛选；缺映射就明确不改变请求，不能把用户档位冒充服务商已接受值。
# 函数用途: 说明 Chat/Messages effort 真正发出的档位，以及为什么与用户选择不同。
def _describe_effort_effect(level: str, protocol: str, levels: tuple[str, ...] | list[str]) -> str:
    sent = _provider_effort(level, protocol, levels)
    prefix = f"{CONTROL_LABELS['effort']}：{LEVEL_LABELS[level]}"
    if not sent:
        return "当前协议与模型声明没有对应的服务商档位，本设置不改变请求。"
    if sent == level:
        return f"{prefix}（发送 {sent}）。"
    return f"{prefix}；按当前协议与模型声明降档，实际发送 {sent}。"


# LLM: budget 不发送 effort 字符串；有上限时只渲染出站同源裁决，未发送原因不可由文案或模型正文反推。
#   无上限的纯档位说明只能写条件规则，不能声称已发；配置回执上限与工厂同源，不证明强制工具请求或供应商接受。
# 函数用途: 按结构化预算事实说明实际字段或未发送原因，没有请求上限时只解释规则。
def _describe_budget_effect(level: str, protocol: str, max_tokens: int | None = None) -> str:
    prefix = f"{CONTROL_LABELS['budget']}：{LEVEL_LABELS[level]}"
    if protocol != "anthropic":
        return f"{prefix}；发送 thinking.enabled，不发送档位字符串或预算数值。"
    if max_tokens is None:
        return f"{prefix}；预算沿本次输出上限夹紧，预算区间为空时不发送 thinking，不发送档位字符串。"
    decision = _anthropic_budget_decision(level, max_tokens)
    if decision.reason_code:
        return (f"{prefix}；未发送 thinking（未发送原因：{decision.reason_code}）：常规请求输出上限 max_tokens={max_tokens}"
                f" 无法同时容纳最小思考预算 {_MIN_BUDGET_TOKENS} 和正文预留 {_MIN_BUDGET_TOKENS}；不改变输出上限。")
    return (f"{prefix}；发送 thinking.enabled，budget_tokens={decision.budget_tokens}"
            f"（常规请求输出上限 max_tokens={max_tokens}），不发送档位字符串。")


# LLM: 只读 responses_reasoning_field 的换算结果，不另写档位对应规则；实际发送的服务商档位与用户档位不同时写明。
# 函数用途: 说明 Responses 模型上某个档位实际发出的 reasoning.effort，或为什么不发。
def _describe_responses_effect(level: str, control: str, levels: tuple[str, ...] | list[str]) -> str:
    field = responses_reasoning_field(control, level, levels, disabled=level == "off")
    sent = field.get("reasoning", {}).get("effort", "") if field else ""
    if not sent and level == "off":
        return "这个模型没有声明可关闭思考的档位（none / minimal），本设置不改变请求，由服务商按默认方式思考。"
    if not sent:
        return f"这个模型声明的服务商档位里没有对应「{LEVEL_LABELS[level]}」的取值，本设置不改变请求。"
    if level == "off" or sent == level:
        return f"{CONTROL_LABELS[control]}：{LEVEL_LABELS[level]}（发送 {sent}）。"
    return f"{CONTROL_LABELS[control]}：{LEVEL_LABELS[level]}；这个模型没有声明更高的服务商档位，实际发送 {sent}。"


# LLM: 只由 REASONING_LEVELS 与 LEVEL_LABELS 生成，档位增减时自动同步；不涉及任何模型能力判断（能否生效由效果行说明）。
#   /effort 查看回执（IM 没有选择菜单，靠它知道能怎么改）使用；改文案同步 test_reasoning_effort.py。
# 函数用途: 生成“可选档位和怎么改”的一行提示。
def describe_level_choices() -> str:
    choices = "、".join(f"{level} {LEVEL_LABELS[level]}" for level in REASONING_LEVELS)
    return f"可选档位：{choices}。发送 /effort 加档位只改本会话；/effort default 回到全局默认。"


# LLM: 控制方式、协议、声明与常规输出上限均由同一运行配置解析；预算上限读 effective_max_output_tokens，与工厂同源，
#   不把配置原始 max_tokens 当出站值。auto/off/none 保持原优先级；联测真实控制服务与参数中心，纯读取、不发请求。
# 函数用途: 按模型配置说明发送字段或未发送原因，供 /effort 回执和参数中心共用。
def describe_config_reasoning_effect(level: str, config: object) -> str:
    from ..settings.defaults import effective_max_output_tokens

    level = normalize_reasoning_level(level) or "auto"
    backend = str(getattr(config, "model_backend", "") or "")
    control = resolved_reasoning_control(getattr(config, "model_reasoning_control", "auto"),
                                         getattr(config, "api_base", ""), backend)
    protocol = _PROTOCOLS.get(backend.strip().lower(), "")
    levels = tuple(getattr(config, "model_reasoning_levels", ()) or ())
    if control == "budget" and protocol == "anthropic" and level not in {"auto", "off"}:
        return _describe_budget_effect(level, protocol, effective_max_output_tokens(config))
    return describe_reasoning_effect(level, control, protocol=protocol, levels=levels)


__all__ = [
    "CONTROL_LABELS",
    "LEVEL_LABELS",
    "REASONING_CONTROLS",
    "REASONING_LEVELS",
    "ReasoningPayloadLimits",
    "describe_config_reasoning_effect",
    "describe_level_choices",
    "describe_reasoning_effect",
    "normalize_reasoning_control",
    "normalize_reasoning_level",
    "reasoning_payload_fields",
    "reasoning_request_values",
    "resolved_reasoning_control",
    "responses_reasoning_field",
]
