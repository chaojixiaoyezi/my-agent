# LLM: “智能程度”档位的唯一解析与持久化入口。档位是会话线程属性：主会话由 /effort 写入，子代理线程在创建时
#   写入（create_subagents 的 effort，省略则取父级本轮实际档位）；线程没有设置时回落到全局 model_reasoning_effort。
#   每次模型请求按本轮 params 的 agent_thread_id/conversation_thread_id 现读线程，真实请求与自动选模投影共用
#   request_reasoning_options，保证载荷一致。用户八档及其换算与控制方式归 backends/reasoning_control.py。
#   同步检查 tool_model_generation._do_backend_generate、gateway_model_adoption、subagent/model_selection、
#   orchestration/create_policy、conversation/agent_thread 与 test_reasoning_effort*.py。
# 模块用途: 解析一次运行的智能程度档位，保存会话档位，并在创建子代理时确定子代理档位。
from __future__ import annotations

from dataclasses import replace

from ..backends.reasoning_control import (
    REASONING_LEVELS,
    normalize_reasoning_level,
    reasoning_request_values,
)

THREAD_FIELD = "reasoning_effort"
CHILD_ATTR = "host_reasoning_effort.v1"


# LLM: 配置已由规范化层保证合法；读不到或不认识时视为 auto，不因配置损坏阻断请求。
# 函数用途: 读取全局默认档位。
def configured_reasoning_level(config: object) -> str:
    return normalize_reasoning_level(getattr(config, "model_reasoning_effort", "auto")) or "auto"


# LLM: 与 provider_runtime_scope 同一优先级：子代理线程 agent_thread_id 优先，其次普通会话 conversation_thread_id。
# 函数用途: 取出本轮运行所属的会话线程编号。
def run_thread_id(params: object) -> str:
    attrs = getattr(params, "task_attributes", None) or {}
    if not isinstance(attrs, dict):
        return ""
    return str(attrs.get("agent_thread_id") or attrs.get("conversation_thread_id") or "").strip()


# LLM: 线程读取失败或没有设置时回落全局默认，绝不因为档位读取问题阻断模型请求。
# 函数用途: 得到本轮运行的智能程度档位（线程设置优先，其次全局默认）。
def run_reasoning_level(agent: object, params: object) -> str:
    thread_id = run_thread_id(params)
    level = ""
    if thread_id:
        try:
            thread = agent.conversation_store.threads.load(thread_id)
        except Exception:  # noqa: BLE001 - 档位是可选增强，线程暂不可读时按默认发送。
            thread = None
        level = normalize_reasoning_level(getattr(thread, THREAD_FIELD, ""))
    return level or configured_reasoning_level(getattr(agent, "config", None))


# LLM: 「只思考被输出上限截断」的续跑要在紧邻那一次逻辑调用上降一档思考强度：覆盖值挂在
#   params.live_archive_state（工具循环整轮复用同一 params 实例），读取无副作用、供真实请求与各载荷
#   投影共用同一口径。覆盖带「武装时的 model turn 序号」：只有已经越过该序号的物理尝试才启用它
#   （同一次续跑的超时重试序号更大，保持降档），isolated 参数视图（自然回执轮）属于另一用途，
#   既不使用也不消费；清除由消费它的那次逻辑调用结束时执行（见
#   tool_model_generation.generate_model_response），因此它只影响一次逻辑调用、不改会话、线程或
#   用户设置。伪 params（无 dict 状态）时全部退化为无覆盖。
NEXT_CALL_LEVEL_KEY = "_next_call_reasoning_level"


# LLM: 与 tool_model_generation._model_turn_sequence 同源只读：宿主每开始一次模型生成 +1，
#   超时重试也 +1，所以「越过武装序号」能同时覆盖续跑与它的重试。拿不到时返回 0。
# 函数用途: 读取当前 model turn 序号（live_archive_state 上的宿主序号字段）。
def _live_model_turn_sequence(params: object) -> int:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return 0
    try:
        return max(0, int(state.get("_model_turn_sequence") or 0))
    except (TypeError, ValueError):
        return 0


# 函数用途: 读取「紧邻下一次调用」的档位覆盖；没有或不可用时返回空串。
def next_call_reasoning_level(params: object) -> str:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return ""
    raw = state.get(NEXT_CALL_LEVEL_KEY)
    level = raw.get("level") if isinstance(raw, dict) else raw
    return normalize_reasoning_level(level)


# 函数用途: 写入「紧邻下一次调用」的档位覆盖（带当前 model turn 序号作为武装点）；非法档位或伪 params 时返回 False。
def set_next_call_reasoning_level(params: object, level: str) -> bool:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return False
    normalized = normalize_reasoning_level(level)
    if not normalized:
        return False
    state[NEXT_CALL_LEVEL_KEY] = {
        "level": normalized,
        "armed_sequence": _live_model_turn_sequence(params),
    }
    return True


# 函数用途: 清除「紧邻下一次调用」的档位覆盖（消费它的逻辑调用结束时调用）。
def clear_next_call_reasoning_level(params: object) -> None:
    state = getattr(params, "live_archive_state", None)
    if isinstance(state, dict):
        state.pop(NEXT_CALL_LEVEL_KEY, None)


# 函数用途: 读出「紧邻下一次调用」覆盖的武装序号；没有覆盖或不可用时返回 None。
def _override_armed_sequence(params: object) -> int | None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return None
    raw = state.get(NEXT_CALL_LEVEL_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return int(raw.get("armed_sequence") or 0)
    except (TypeError, ValueError):
        return None


# LLM: 覆盖只对「目标逻辑调用」生效：isolated 参数视图（自然回执轮）是另一用途，既不使用也不消费；
#   其余视图要求已越过武装序号（紧邻之后的物理尝试，含超时重试）。判定只读结构化字段。
# 函数用途: 判断这次模型请求是否该启用「紧邻下一次调用」的档位覆盖。
def next_call_override_applies(params: object) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "isolated":
        return False
    armed = _override_armed_sequence(params)
    return armed is not None and _live_model_turn_sequence(params) > armed


# LLM: 与 next_call_override_applies 同一判定的「逻辑调用入口版」：入口时序号尚未 +1，
#   用 >= 武装序号判定本次调用是否拥有覆盖（拥有者在调用结束时负责清除）。
# 函数用途: 判断本次逻辑调用是否拥有「紧邻下一次调用」的档位覆盖。
def next_call_override_owned(params: object) -> bool:
    if str(getattr(params, "context_scope", "") or "") == "isolated":
        return False
    armed = _override_armed_sequence(params)
    return armed is not None and _live_model_turn_sequence(params) >= armed


# LLM: 真实请求与两处载荷投影都必须调用本函数；backend.reasoning_control 缺失（假后端、旧后端）按 none 处理。
#   返回 (thinking_disabled, reasoning_effort)，forced 表示原强制工具选择要求关闭思考。
#   「只思考截断续跑」的降档覆盖优先于线程/全局档位，但只对目标逻辑调用生效（见
#   next_call_override_applies）；投影与真实请求共用本函数，所以口径一致。
# 函数用途: 计算一次模型请求的思考开关与档位。
def request_reasoning_options(agent: object, params: object, backend: object, *, forced: bool) -> tuple[bool, str]:
    control = str(getattr(backend, "reasoning_control", "none") or "none")
    level = run_reasoning_level(agent, params)
    override = next_call_reasoning_level(params)
    if override and next_call_override_applies(params):
        level = override
    return reasoning_request_values(level, control, forced=forced)


# LLM: 只改线程自己的档位字段（原子更新）；空串表示清除会话设置、回落全局默认。八档取唯一表，非法档位抛 ValueError。
# 函数用途: 保存或清除某个会话的智能程度档位。
def set_thread_reasoning_level(store: object, thread_id: str, level: str) -> object:
    normalized = normalize_reasoning_level(level) if level else ""
    if level and not normalized:
        raise ValueError(f"智能程度只接受：{'、'.join(REASONING_LEVELS)}")
    return store.threads.update_atomic(
        thread_id, lambda latest: replace(latest, **{THREAD_FIELD: normalized}),
    )


# LLM: 宿主属性先移除伪造值；显式 effort 必须是合法档位（否则在整批创建前报错），省略则冻结父级本轮实际档位，
#   子代理之后不随父会话的 /effort 变化，xhigh/ultra 不在这里提前降档。副作用：写 attrs[CHILD_ATTR]。
# 函数用途: 为即将创建的子代理确定并记录智能程度档位。
def inherit_reasoning_effort(attrs: dict, agent: object, effort: object = None) -> None:
    attrs.pop(CHILD_ATTR, None)
    if effort is not None and str(effort).strip():
        level = normalize_reasoning_level(effort)
        if not level:
            raise ValueError(f"子代理 effort 只接受 {'、'.join(REASONING_LEVELS)}；省略则继承当前会话的智能程度。")
    else:
        level = run_reasoning_level(agent, getattr(agent, "_current_run_params", None))
    attrs[CHILD_ATTR] = {"level": level}


# LLM: 读取创建时冻结的子代理档位；缺失或损坏返回空串（线程创建时不写档位，运行时回落全局默认）。
# 函数用途: 从子代理任务属性里取出档位，用于初始化子代理线程。
def child_reasoning_level(attrs: object) -> str:
    ref = attrs.get(CHILD_ATTR) if isinstance(attrs, dict) else None
    return normalize_reasoning_level(ref.get("level")) if isinstance(ref, dict) else ""


__all__ = [
    "CHILD_ATTR",
    "THREAD_FIELD",
    "child_reasoning_level",
    "clear_next_call_reasoning_level",
    "configured_reasoning_level",
    "inherit_reasoning_effort",
    "next_call_override_applies",
    "next_call_override_owned",
    "next_call_reasoning_level",
    "request_reasoning_options",
    "run_reasoning_level",
    "run_thread_id",
    "set_next_call_reasoning_level",
    "set_thread_reasoning_level",
]
