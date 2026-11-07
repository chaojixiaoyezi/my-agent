# LLM: 跨模块共用的默认数值与派生公式（输出上限、上下文窗口兜底、决策请求字数）只在这里；读取方导入，不复制数值。
# 模块用途: 提供配置默认值读取、输出上限与上下文窗口的换算函数。
from __future__ import annotations

import threading
from functools import lru_cache
from typing import Any

DEFAULT_COMMAND_ACCESS_MODE = "workspace-write"
# 模型单次回答的输出上限（含推理），所有模型统一 64K（用户 2026-09-27 要求）；随包 YAML 的 max_tokens 必须同值。
DEFAULT_MODEL_MAX_TOKENS = 65_536
# 输出上限最多占已知上下文窗口的几分之一：给输入留出至少四分之三窗口，避免输入加输出超窗被供应商拒绝。
MODEL_OUTPUT_WINDOW_DIVISOR = 4
# 上下文窗口没填（留空）、服务商也没报告时的兜底容量；随包 YAML 里 model_context_window_tokens 的说明必须同值。
DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS = 128_000
# 规划/交付质量/动作候选三个决策点位发给决策模型的当前请求字数预算默认值：更长时自动取首尾节选并标注，不整点跳过。
# 用户一般不用改（2026-09-27 用户：参数要默认就合理，99% 的人不会手动调）。
DEFAULT_DECISION_REQUEST_MAX_CHARS = 2_000


# LLM: 三个决策点位的统一读取入口，非整数/负数/字段缺失一律回落默认值；0 表示不截取（仍受决策协议 256 KB 输入上限约束）。
#   超出预算时由 backends/decision_protocol.decision_request_excerpt 取首尾节选，不再整点跳过。
# 函数用途: 从配置里取当前请求字数预算，供规划、交付质量、动作候选共用，避免各写一份读取逻辑。
def decision_request_max_chars(config: Any) -> int:
    value = getattr(config, "decision_request_max_chars", None)
    if type(value) is not int or value < 0:
        return DEFAULT_DECISION_REQUEST_MAX_CHARS
    return value


# LLM: 输出上限的唯一公式：已知窗口（>0，来自模型档案或配置里的 model_context_window_tokens，是否显式都算）时取
#   min(配置值, 窗口 // MODEL_OUTPUT_WINDOW_DIVISOR)，窗口未知时按配置值。后端工厂构造时用它算出 backend.max_tokens，
#   它就是实际发送值，请求体与输出预留直接读它，不在后端里再夹一次；没有 max_tokens 的后端由 call_runtime.max_output_tokens
#   按同一公式估算。原来只夹显式窗口，64K 默认值让“窗口×0.8−输出上限”的 compact 预算在 8 万以下窗口变成 1。
# 函数用途: 按给定窗口算出实际使用的输出 token 上限（配置值为 0 时按默认值）。
def output_cap_for_window(configured: object, window: object) -> int:
    cap = int(configured or 0) or DEFAULT_MODEL_MAX_TOKENS
    size = int(window or 0)
    return max(1, min(cap, size // MODEL_OUTPUT_WINDOW_DIVISOR)) if size > 0 else cap


# LLM: 构造期还拿不到服务商元数据，窗口按 context_window_or_default：填了用填的，留空按 128000，与合并前默认一致。
# 函数用途: 按配置里的窗口算出输出上限（不改配置本身）。
def effective_max_output_tokens(config: Any) -> int:
    return output_cap_for_window(getattr(config, "max_tokens", 0), context_window_or_default(config))


# LLM: 只归一档案原始上限；不探测模型，不改旧的四分之一窗口兜底。
# 函数用途: 为已有完整输入计量的请求保留可配置输出天花板。
def configured_max_output_tokens(config: object) -> int:
    raw = getattr(config, "max_tokens", 0)
    try:
        value = int(raw) if not isinstance(raw, bool) else 0
    except (TypeError, ValueError, OverflowError):
        value = 0
    return value if value > 0 else DEFAULT_MODEL_MAX_TOKENS


# LLM: model_context_window_tokens 是唯一窗口旋钮（原 model_context_window_explicit 已并入）：正整数 = 用户显式容量，
#   空、0、布尔、非整数 = 没填，返回 0。窗口解析、输出预留、模型切换容量比对都用它判断“是否显式”，不能再另设开关。
# 函数用途: 读出用户显式填写的上下文窗口，没填返回 0。
def configured_context_window_tokens(config: Any) -> int:
    value = getattr(config, "model_context_window_tokens", None)
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


# LLM: 只看配置、不看后端：显式值优先，否则兜底 DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS。给构造期的输出上限、Skill 索引预算、
#   决策输入里的“当前模型窗口”用；运行时真正的窗口以 agent_core/model/context_window 的解析结果为准。
# 函数用途: 返回配置层面的上下文窗口，留空按 128000。
def context_window_or_default(config: Any) -> int:
    return configured_context_window_tokens(config) or DEFAULT_MODEL_CONTEXT_WINDOW_TOKENS


@lru_cache(maxsize=1)
def default_agent_config() -> Any:
    from .config import AgentConfig

    return AgentConfig()


def default_config_value(key: str) -> Any:
    return getattr(default_agent_config(), key)


def default_config_int(key: str, *, minimum: int | None = None) -> int:
    value = int(default_config_value(key))
    return max(minimum, value) if minimum is not None else value


def default_config_float(key: str, *, minimum: float | None = None) -> float:
    value = float(default_config_value(key))
    return max(minimum, value) if minimum is not None else value


def default_config_bool(key: str) -> bool:
    return bool(default_config_value(key))


# 本进程各 owner 池登记过的常驻 owner 容量（只保留最大值）；未登记时为空。
_RESIDENT_OWNER_CAPACITIES: list[int] = []
_RESIDENT_OWNER_CAPACITY_LOCK = threading.Lock()


# LLM: owner 池构造时登记已解析的 owner_agent_pool_max_agents；按 owner 分条目的进程级派生缓存据此定界，
#   配置内常驻的 owner 互不挤掉。只升不降：多个池并存时取最大，非正数忽略。不新增配置键。
# 函数用途: 登记本进程 owner 池的常驻容量。
def register_resident_owner_capacity(count: object) -> None:
    if type(count) is not int or count < 1:
        return
    with _RESIDENT_OWNER_CAPACITY_LOCK:
        _RESIDENT_OWNER_CAPACITIES[:] = [max([count, *_RESIDENT_OWNER_CAPACITIES])]


# LLM: 未登记（CLI、单测、轻量宿主）回落同一配置键的默认值，不另设数字。
# 函数用途: 返回按常驻 owner 数定界的进程级缓存条目数。
def resident_owner_capacity() -> int:
    registered = _RESIDENT_OWNER_CAPACITIES
    return registered[0] if registered else default_config_int("owner_agent_pool_max_agents", minimum=1)


# LLM: 单个 owner 同时在跑的代理上限：前台在途请求 + 后台主代理线程（普通/管理员取大），每个根会话树再带满
#   max_subagents 个子代理；按 owner、按工作区保存的派生缓存以此为上限，配置内的并发工作区互不挤掉。
# 函数用途: 由现有并发配置推出单个 owner 的并发代理槽位数，非法值回落各键默认值。
def owner_agent_slot_count(config: Any) -> int:
    def value(key: str, minimum: int) -> int:
        raw = getattr(config, key, None)
        return raw if type(raw) is int and raw >= minimum else default_config_int(key, minimum=minimum)

    background = max(value("background_threads_per_owner", 1), value("background_admin_threads_per_owner", 1))
    return (value("gateway_user_inflight_limit", 1) + background) * (1 + value("max_subagents", 0))


__all__ = [
    "DEFAULT_COMMAND_ACCESS_MODE",
    "DEFAULT_MODEL_MAX_TOKENS",
    "MODEL_OUTPUT_WINDOW_DIVISOR",
    "effective_max_output_tokens",
    "configured_max_output_tokens",
    "output_cap_for_window",
    "default_agent_config",
    "default_config_bool",
    "default_config_float",
    "default_config_int",
    "default_config_value",
    "owner_agent_slot_count",
    "register_resident_owner_capacity",
    "resident_owner_capacity",
]
