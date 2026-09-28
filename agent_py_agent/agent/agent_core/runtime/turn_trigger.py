# LLM: 回合触发类型是宿主结构化事实，只由 conversation 运行时按唤醒原因和唤醒信封构造。kind 决定当前回合在模型输入里
#   用“用户任务”还是“宿主事件”开头、推荐节用检索还是固定短名单、哪些宿主快照不进后续重放；event_facts 只是给模型看的
#   确定性投影，宿主不解析它做任何决策。None 表示普通用户轮，行为与改动前一致。改动时同步 test_lifecycle_wake_host_event。
# 模块用途: 定义回合触发类型、宿主事件的固定开头与渲染入口，供原生 IR、文本 prompt、推荐节和原生历史保存共用。
from __future__ import annotations

from dataclasses import dataclass

TURN_TRIGGER_LIFECYCLE_WAKE = "lifecycle_wake"
HOST_EVENT_HEADING = "# Host Event"
HOST_EVENT_FIRST_LINE = (
    "宿主生命周期事件：以下是直属子代理状态变化的结构化事实，不是新的用户请求；原用户任务就是此前历史里的那一条。"
)
# 原任务不在此前历史里（已核实的 Goal 任务来源，或唤醒没有请求编号）时用这一句，不声称原任务在历史里。
HOST_EVENT_FIRST_LINE_ORIGIN_TASK = (
    "宿主生命周期事件：以下是直属子代理状态变化的结构化事实，不是新的用户请求；原任务见下方 origin_task，"
    "origin_request_ids 里的请求（如有）在此前历史里。"
)
# 宿主事件在原生 IR 里的来源标记；Compact 按来源保留最新一条，原生历史保存据此识别当前回合开头。
HOST_EVENT_FACTS_SOURCE = "host.lifecycle_wake"
# 每种触发类型的固定推荐短名单与推荐理由（只是展示顺序与说明，不授权）；生命周期唤醒先看直属子代理状态。
TURN_TRIGGER_RECOMMENDED_TOOLS: dict[str, tuple[str, ...]] = {
    TURN_TRIGGER_LIFECYCLE_WAKE: (
        "list_agents",
        "read_file",
        "resolve_capability_requests",
        "send_guidance",
        "cancel_subagents",
    ),
}
TURN_TRIGGER_RECOMMENDATION_REASONS: dict[str, str] = {
    TURN_TRIGGER_LIFECYCLE_WAKE: "宿主生命周期事件的固定推荐：先确认直属子代理的状态与结果，再决定整合、答复或调整",
}
# 生命周期唤醒片的这些宿主快照只服务本片，不进后续回合的原生历史重放（后台上下文注入有数万字）。
LIFECYCLE_WAKE_UNREPLAYED_FACT_SOURCES = frozenset({"prompt.runtime_injection"})


# LLM: 冻结值对象；kind 之外的字段都是唤醒信封里已有的结构化身份，event_facts 是已排序、有界的 JSON 文本。
#   origin_request_ids 只含会话历史里真有用户消息的请求；origin_task_attached 表示事实里附了原任务原文（原任务不在历史里）。
# 类用途: 描述当前回合由什么触发，普通用户轮不创建它。
@dataclass(frozen=True)
class TurnTrigger:
    kind: str
    reason: str = ""
    wake_signal_id: str = ""
    source_agent_id: str = ""
    origin_request_ids: tuple[str, ...] = ()
    event_facts: str = ""
    origin_task_attached: bool = False


# LLM: 只按类型和 kind 判断，不看 event_facts 正文；非 TurnTrigger 一律视为普通用户轮。
# 函数用途: 取出生命周期唤醒触发；不是生命周期唤醒时返回 None。
def lifecycle_wake_trigger(value: object) -> TurnTrigger | None:
    if isinstance(value, TurnTrigger) and value.kind == TURN_TRIGGER_LIFECYCLE_WAKE:
        return value
    return None


# LLM: 生命周期唤醒以固定标题和固定首句开头，后接确定性事实；首句只按 origin_task_attached 在两句固定文字里选，
#   原任务不在历史里时不声称它在历史里。普通用户轮保持原“# User Task”格式与字节。纯计算。
# 函数用途: 生成当前回合在模型输入里的开头文字，原生 IR、文本 prompt 和缓存布局共用。
def current_turn_text(user_prompt: object, trigger: object) -> str:
    wake = lifecycle_wake_trigger(trigger)
    if wake is None:
        return f"# User Task\n{str(user_prompt or '')}"
    first_line = HOST_EVENT_FIRST_LINE_ORIGIN_TASK if wake.origin_task_attached else HOST_EVENT_FIRST_LINE
    return f"{HOST_EVENT_HEADING}\n{first_line}\n{wake.event_facts}"


# LLM: 返回 None 表示沿用按用户任务检索的原推荐；返回（名字元组, 推荐理由）时推荐节只列这些名字，
#   再与本轮可见工具按顺序取交集，不检索任务文字。
# 函数用途: 给出当前回合触发类型对应的固定推荐短名单和推荐理由。
def turn_trigger_recommendation(trigger: object) -> tuple[tuple[str, ...], str] | None:
    wake = lifecycle_wake_trigger(trigger)
    if wake is None:
        return None
    return TURN_TRIGGER_RECOMMENDED_TOOLS[wake.kind], TURN_TRIGGER_RECOMMENDATION_REASONS[wake.kind]


__all__ = [
    "HOST_EVENT_FACTS_SOURCE",
    "HOST_EVENT_FIRST_LINE",
    "HOST_EVENT_FIRST_LINE_ORIGIN_TASK",
    "HOST_EVENT_HEADING",
    "LIFECYCLE_WAKE_UNREPLAYED_FACT_SOURCES",
    "TURN_TRIGGER_LIFECYCLE_WAKE",
    "TURN_TRIGGER_RECOMMENDATION_REASONS",
    "TURN_TRIGGER_RECOMMENDED_TOOLS",
    "TurnTrigger",
    "current_turn_text",
    "lifecycle_wake_trigger",
    "turn_trigger_recommendation",
]
