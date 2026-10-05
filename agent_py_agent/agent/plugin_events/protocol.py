# LLM: 插件事件投递的纯协议层：公开字段、方法名、能力常量与握手校验；不读安装表、不碰连接、不记状态。
#   事件事实由调用方（B4 事件点）投影后交给 hub；这里只做形状校验与公共字段组装，不解释 facts 内容。
# 模块用途: 固定 my-agent/events.observe 的线上格式与能力门，供事件中心、Gateway 组装和合同测试共用。
from __future__ import annotations

from dataclasses import dataclass, field

from .declarations import MAX_PLUGIN_EVENT_COUNT, PLUGIN_EVENT_TYPES

# 握手扩展名与版本：清单声明 + 握手声明二者都满足才投递（设计第 5 节）。
EVENTS_EXTENSION = "my-agent/events"
EVENTS_VERSION = "1"
# 观察方法名（设计第 5 节）。
EVENTS_OBSERVE_METHOD = "my-agent/events.observe"
# 一批最多带几条事件：直接引用清单的订阅类型上限（每个订阅类型最多一条、类型不许重复），
# 批量永远装得下收集到的待发，不会出现先消耗水位、后截断丢事件。
MAX_BATCH_EVENTS = MAX_PLUGIN_EVENT_COUNT
# actor 取值（设计第 6 节）：main / subagent / decision。
EVENT_ACTOR_KINDS = ("main", "subagent", "decision")
# 自由文本引用字段（渠道名、会话哈希）的长度上界，防止调用方把大段正文塞进公共字段。
MAX_REF_CHARS = 120
# 提示正文截断长度（设计 D4：4000 字）；名字带 PROMPT 是为了和别的模块的同名概念区分开。
MAX_PROMPT_CONTENT_CHARS = 4000


# LLM: 调用方给 hub 的一条观察事实；hub 只把它当数据，不改写 facts。
#   类型校验放在发布入口（normalize_event_fact）而不是构造器：构造永不抛，坏数据在门外被丢弃。
#   content 只有提示提交事件可能带（B4 投影后传入），hub 再按插件声明决定是否真的发给该插件。
# 类用途: 打包一条待投递事件的结构化事实与公开字段。
@dataclass(frozen=True)
class EventFact:
    type: str
    facts: dict = field(default_factory=dict)
    channel: str = ""
    thread_ref: str = ""
    actor: str = "main"
    content: str = ""


# LLM: 事件能力缺失不是连接故障：调用方按"不可用"计数并丢弃该批，不退避整条连接（与请求级错误同级）。
#   独立异常类是为了让上层只按类型判断，不读错误文字。
# 类用途: 标记插件握手没有声明 my-agent/events 能力。
class PluginEventUnavailable(ValueError):
    code = "PLUGIN_EVENT_UNAVAILABLE"


# LLM: 发送期字段（编号、序号、丢弃计数、时间、正文授权）与事件事实分开打包，
#   收集阶段一次算好、发送阶段只做序列化；字段收在一个不可变对象里，避免组装函数参数随需求增长。
# 类用途: 打包一条待发送事件的事实与发送期字段。
@dataclass(frozen=True)
class EventEnvelope:
    fact: EventFact
    event_id: str
    seq: int
    dropped_before: int
    occurred_at: float
    include_content: bool = False


# LLM: 只读 client.capabilities 的 experimental 声明（与 plugin_runtime 的 _negotiated 同一读法）；
#   声明不授予权限，只决定投不投事件。client 由池在启动完成后交给 before_send，此时握手已结束。
# 函数用途: 校验插件握手声明了事件能力，未声明时抛 PluginEventUnavailable。
def require_events_capability(client) -> None:
    capabilities = getattr(client, "capabilities", None)
    experimental = capabilities.get("experimental") if isinstance(capabilities, dict) else None
    declared = experimental.get(EVENTS_EXTENSION) if isinstance(experimental, dict) else None
    versions = declared.get("versions") if isinstance(declared, dict) else None
    if not (isinstance(versions, list) and EVENTS_VERSION in versions):
        raise PluginEventUnavailable("插件未声明事件能力")


# LLM: 归一化在发布入口做（坏输入丢弃而不是抛给调用方）；字符串字段截断、actor 只认白名单、
#   facts 必须是字典否则置空；content 只在提示事件上保留，纵深防御不依赖 B4 投影先过滤。
# 函数用途: 纯计算归一不可变事件并剥离非提示正文，发布入口调用；不读写磁盘或改变正文许可。
def normalize_event_fact(value) -> EventFact | None:
    if not isinstance(value, EventFact) or value.type not in PLUGIN_EVENT_TYPES:
        return None
    actor = value.actor if value.actor in EVENT_ACTOR_KINDS else "main"
    return EventFact(
        type=value.type,
        facts=dict(value.facts) if isinstance(value.facts, dict) else {},
        channel=_ref(value.channel),
        thread_ref=_ref(value.thread_ref),
        actor=actor,
        content=_ref(value.content, MAX_PROMPT_CONTENT_CHARS) if value.type == "prompt_submitted" else "",
    )


# LLM: 公共字段按设计第 6 节；dropped_before 与 seq 由 hub 计算后传入；content 只在该插件声明
#   text 且确实有内容时出现，其余情况连键都不给，避免插件靠键的存在与否猜宿主实现。
# 函数用途: 组装一条发给某插件的观察事件。
def build_event_payload(envelope: EventEnvelope) -> dict:
    fact = envelope.fact
    payload = {
        "event_id": envelope.event_id, "type": fact.type, "seq": envelope.seq,
        "occurred_at": envelope.occurred_at, "dropped_before": envelope.dropped_before,
        "channel": fact.channel, "thread_ref": fact.thread_ref,
        "actor": fact.actor, "facts": dict(fact.facts),
    }
    if envelope.include_content and fact.content:
        payload["content"] = fact.content
    return payload


# LLM: 只接受普通字符串；非字符串给空串，超长截断，不解释内容。
# 函数用途: 归一化一个自由文本字段。
def _ref(value, limit: int = MAX_REF_CHARS) -> str:
    return value[:limit] if isinstance(value, str) else ""
