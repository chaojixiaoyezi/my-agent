# LLM: B5 的纯收紧合同，输入只来自已规范化 ToolCall 和 B1 不可变声明；本段不读安装表、不连接插件、不授权或写账。
# 模块用途: 为后续执行器接线提供精确匹配、参数投影、回复校验与只能更严的合并结果，不能代替宿主审批。
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass

from ..common.log_redaction import redact_sensitive_value
from ..tooling.runtime_contracts import ToolCall
from .declarations import PluginToolGateDeclaration

# 完整参数最多四千个字符，遵循一期公开投影预算；先脱敏再截，避免在截断边缘露出半个秘密。
MAX_PLUGIN_GATE_ARGUMENT_CHARS = 4000
# 插件消息最多八十个字符，为审批前缀保留可见空间，不让插件消息淹没宿主要求。
MAX_PLUGIN_GATE_MESSAGE_CHARS = 80
_PLUGIN_GATE_REASON_CODE = re.compile(r"[A-Z0-9_]{1,40}\Z")
_PLUGIN_GATE_VERDICT_RANK = {"allow_as_is": 0, "ask": 1, "deny": 2}


# LLM: 原ToolCall与效果/身份只取宿主；approved_gate_refs仅为原write_boundary已批准行的复制，不能来自参数或插件回复。
# 类用途: 保存一次收紧征询需要的调用事实，不另建工具调用身份。
@dataclass(frozen=True)
class GateCall:
    call: ToolCall
    effect: str
    actor: str
    interactive: bool
    approved_gate_refs: tuple[str, ...] = ()


# LLM: 只读激活投影不是新状态源，后续征询服务须鲜活复核安装表，不能凭此对象延长失效激活。
# 类用途: 将一个插件激活与其中一个已验证的收紧门关联。
@dataclass(frozen=True)
class GateTarget:
    plugin_id: str
    version: str
    activation_id: str
    declaration: PluginToolGateDeclaration


# LLM: 插件回复只允许三个结构化决定和严格原因码；直接构造与解码共用消息清洗，message 永不作为机器裁决。
# 类用途: 保存收紧回复并统一清洗展示消息，不携带参数修改或权限授予。
@dataclass(frozen=True)
class GateReply:
    verdict: str
    reason_code: str
    message: str = ""

    # LLM: 构造与 dataclasses.replace 都不能绕过清洗；协议解码和审批前缀须同源，消息不产生授权。
    # 函数用途: 将所有来源的插件消息规范成最多八十字的单行安全文字。
    def __post_init__(self) -> None:
        object.__setattr__(self, "message", clean_gate_message(self.message))


# LLM: outcome/latency投影真实征询；approval_applied只表示当前安装鲜活复核后的宿主精确批准、没有协议请求，不应当作征询记账。
# 类用途: 把单门事实与激活身份绑定，避免故障或直接构造意外变成放行。
@dataclass(frozen=True)
class GateReview:
    target: GateTarget
    reply: GateReply
    outcome: str = "ok"
    latency_ms: int = 0
    approval_applied: bool = False

    # LLM: merge 不复核故障与 verdict，一切非 ok 构造必须在此同源转 ask；replace 也走相同不变量。
    # 函数用途: 为故障与撤销覆盖不可信回复，未知故障按 error 收紧。
    def __post_init__(self) -> None:
        if self.outcome != "ok":
            outcome = self.outcome if self.outcome in ("timeout", "error", "malformed", "unavailable", "revoked") else "error"
            object.__setattr__(self, "outcome", outcome)
            object.__setattr__(self, "reply", _failure_reply(outcome))
            object.__setattr__(self, "approval_applied", False)


# LLM: 此结果只能收紧已有宿主决定，primary/requirements 是只读展示与审批输入，不是批准凭据。
# 类用途: 保存合并后的严格程度、稳定主原因及所有要求确认的门。
@dataclass(frozen=True)
class GateDecision:
    status: str
    primary: GateReview | None = None
    requirements: tuple[GateReview, ...] = ()


# LLM: 纯逻辑先独立验证；后续 B5 征询仍须使用 B2 唯一共用池，不能在此自行启动或维护另一条连接。
# 类用途: 精确投影和合并插件收紧协议，不执行工具也不放宽宿主限制。
class PluginToolGate:
    # LLM: 仅解码宿主附带引用；四调用字段和当前插件/version/activation/gate全等才跳门，坏JSON/类型/缺字段失败关闭。
    # 函数用途: 核对本次调用是否已由用户批准当前这一个收紧门，不缓存或授予新批准。
    @staticmethod
    def approved(target: GateTarget, call: GateCall) -> bool:
        for raw in call.approved_gate_refs:
            try:
                ref = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(ref, dict) or not _approved_call_matches(ref, call.call):
                continue
            gates = ref.get("gates")
            if isinstance(gates, list) and any(_approved_target_matches(item, target) for item in gates):
                return True
        return False

    # LLM: 仅消费清单声明和宿主效果，不能用工具描述或参数文字推断命中。
    # 函数用途: 返回精确工具名或效果命中的门。
    @staticmethod
    def matching(declarations: tuple[PluginToolGateDeclaration, ...], call: GateCall) -> tuple[PluginToolGateDeclaration, ...]:
        return tuple(gate for gate in declarations if call.call.tool_name in gate.tools or call.effect in gate.effects)

    # LLM: 只产出发送副本，不改变原 ToolCall；full 同源脱敏后限长并如实标截断，none 不暴露标记。
    # 函数用途: 构造 my-agent/tool-gate.review 的单门请求。
    @staticmethod
    def request_payload(target: GateTarget, call: GateCall) -> dict:
        facts = {"call_id": call.call.call_id, "tool": call.call.tool_name, "effect": call.effect,
                 "actor": call.actor, "interactive": call.interactive, "args_hash": call.call.args_hash}
        if target.declaration.arguments == "full":
            sanitized = redact_sensitive_value(_without_internal_keys(call.call.arguments))
            facts["arguments"] = _bounded_value(sanitized, MAX_PLUGIN_GATE_ARGUMENT_CHARS)
            facts["arguments_truncated"] = len(_json_text(sanitized)) > MAX_PLUGIN_GATE_ARGUMENT_CHARS
        return {"gate_id": target.declaration.id, "call": facts}

    # LLM: 非法回复共用故障构造，直接回复共用 GateReply 清洗；多余字段无控制权，不能反写参数。
    # 函数用途: 恢复可参与合并的回复，消息清洗和故障不变量不在解码处另写一份。
    @staticmethod
    def decode_reply(target: GateTarget, value: object) -> GateReview:
        if not _valid_reply(value):
            return failure_review(target, "malformed")
        return GateReview(target, GateReply(value["verdict"], value["reason_code"], value.get("message", "")))

    # LLM: 宿主 deny 不被插件影响；同严主因用 plugin_id/gate_id 稳定排序，不读消息或返回先后。
    # 函数用途: 按设计七种组合得到最多同严、绝不更松的结果。
    @staticmethod
    def merge(host_status: str, reviews: tuple[GateReview, ...]) -> GateDecision:
        if host_status not in ("allow", "ask", "deny"):
            raise ValueError("宿主收紧输入必须是已验证的结构化决定")
        if host_status == "deny":
            return GateDecision("deny")
        active = sorted((item for item in reviews if item.outcome != "revoked"), key=_review_order)
        restrictive = tuple(item for item in active if item.reply.verdict != "allow_as_is")
        requirements = tuple(item for item in active if item.reply.verdict == "ask")
        primary = restrictive[0] if restrictive else None
        status = primary.reply.verdict if primary else host_status
        return GateDecision("ask" if host_status == "ask" and status == "allow" else status, primary,
                            tuple(sorted(requirements, key=lambda item: (item.target.plugin_id, item.target.declaration.id))))


# LLM: 不读取 message 做排序，稳定顺序是严格度、插件编号、门编号；联测多插件所有返回排列。
# 函数用途: 给合并提供与并发完成顺序无关的主原因顺序。
def _review_order(review: GateReview) -> tuple:
    return (-_PLUGIN_GATE_VERDICT_RANK[review.reply.verdict], review.target.plugin_id, review.target.declaration.id)


# LLM: 内层插件引用只比operation/call/key/hash四字段；外层plugin_gate_policy先核原批准行tool/run/operation/key/hash五字段。
#   tool/run绑定宿主批准归属，call_id绑定插件本次征询；两层有意分工，不可互相替代或只比call_id。
# 函数用途: 判断原批准引用是否绑定当前这一次不可变工具调用。
def _approved_call_matches(ref: dict, call: ToolCall) -> bool:
    return all(isinstance(ref.get(key), str) and bool(ref[key]) and ref[key] == getattr(call, key)
               for key in ("operation_id", "call_id", "idempotency_key", "args_hash"))


# LLM: 目标来自当前安装表，消息/原因不授权；插件、包版本、激活、门均不可借用其它门批准。
# 函数用途: 核对批准列表中的一个门是否正是当前有效激活的门。
def _approved_target_matches(item: object, target: GateTarget) -> bool:
    if not isinstance(item, dict):
        return False
    identity = {"plugin_id": target.plugin_id, "version": target.version,
                "activation_id": target.activation_id, "gate_id": target.declaration.id}
    return all(isinstance(item.get(key), str) and bool(item[key]) and item[key] == value
               for key, value in identity.items())


# LLM: 解码、直接 GateReply 与审批前缀均复用此清洗；删除所有控制/格式符含双向控制和 Unicode 换行，不读正文决策。
# 函数用途: 生成最多八十字的单行插件消息，不写文件或修改权威参数。
def clean_gate_message(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value if not unicodedata.category(char).startswith("C")
                   and char not in ("\u2028", "\u2029"))[:MAX_PLUGIN_GATE_MESSAGE_CHARS]


# LLM: 唯一故障回复构造只会 ask；revoked 仍 ask 但由合并剔除，固定原因不采用异常正文或原插件消息。
# 函数用途: 给超时、错误、非法、不可用和撤销选出同源安全回复。
def _failure_reply(outcome: str) -> GateReply:
    code = "PLUGIN_GATE_UNAVAILABLE" if outcome == "revoked" else "PLUGIN_GATE_" + outcome.upper()
    return GateReply("ask", code)


# LLM: 征询与非法解码统一走此入口，GateReview 构造再次守不变量；不授予批准、不记录消息正文。
# 函数用途: 构造单门故障事实，保留激活身份与本次延迟。
def failure_review(target: GateTarget, outcome: str, latency_ms: int = 0) -> GateReview:
    return GateReview(target, _failure_reply(outcome), outcome, latency_ms)


# LLM: 只接受协议中的三个 verdict；reason/message 类型和边界严格，多余字段完全不读取。
# 函数用途: 判断回复能否使用，非法时由调用方统一宁严要求确认。
def _valid_reply(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (value.get("verdict") in ("allow_as_is", "ask", "deny")
            and isinstance(value.get("reason_code"), str)
            and _PLUGIN_GATE_REASON_CODE.fullmatch(value["reason_code"]) is not None
            and isinstance(value.get("message", ""), str))


# LLM: 任意深度内部键都不能公开；递归新建容器，不修改权威 ToolCall 或只去顶层键。
# 函数用途: 为统一脱敏生成不含宿主内部字段的参数副本。
def _without_internal_keys(value: object) -> object:
    if isinstance(value, dict):
        return {key: _without_internal_keys(item) for key, item in value.items() if not str(key).startswith("__")}
    if isinstance(value, (list, tuple)):
        return [_without_internal_keys(item) for item in value]
    return value


# LLM: 限长以真实发送的紧凑 JSON 字符数为准，不用 Python repr 估计，也不把对象截成非法 JSON。
# 函数用途: 给投影预算计算一份确定的 JSON 文本。
def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# LLM: full 的 arguments 仍是对象；依原插入序保留能容纳的成员和字符串前缀，不新增伪参数或修改原参数。
# 函数用途: 在脱敏之后缩小 JSON 副本，同时保持合法数据类型和总预算。
def _bounded_value(value: object, budget: int) -> object:
    if len(_json_text(value)) <= budget:
        return value
    if isinstance(value, dict):
        return _bounded_mapping(value, budget)
    if isinstance(value, list):
        return _bounded_list(value, budget)
    if isinstance(value, str):
        return _bounded_string(value, budget)
    return value


# LLM: JSON 键、引号、冒号、逗号和括号均计费；预算不足的尾成员不公开，不加入协议外的截断标记。
# 函数用途: 按序生成不超过指定字符数的对象投影。
def _bounded_mapping(value: dict, budget: int) -> dict:
    result, used = {}, 2
    for key, item in value.items():
        cost = len(_json_text(str(key))) + 1 + int(bool(result))
        remaining = budget - used - cost
        if remaining < 2:
            break
        projected = _bounded_value(item, remaining)
        size = len(_json_text(projected))
        if size > remaining:
            break
        result[key] = projected
        used += cost + size
    return result


# LLM: 数组也计入逗号与括号，递归只生成投影；不能逐元素给满预算导致整个请求越界。
# 函数用途: 保留总字符预算内的数组前缀。
def _bounded_list(value: list, budget: int) -> list:
    result, used = [], 2
    for item in value:
        remaining = budget - used - int(bool(result))
        if remaining < 2:
            break
        projected = _bounded_value(item, remaining)
        size = len(_json_text(projected))
        if size > remaining:
            break
        used += size + int(bool(result))
        result.append(projected)
    return result


# LLM: 先脱敏后限长；二分按 JSON 转义后的字符数找前缀，反斜杠或引号不能让计数失真。
# 函数用途: 返回字符串的可发送前缀，不拆 JSON 转义结构。
def _bounded_string(value: str, budget: int) -> str:
    low, high = 0, min(len(value), max(0, budget - 2))
    while low < high:
        middle = (low + high + 1) // 2
        if len(_json_text(value[:middle])) <= budget:
            low = middle
        else:
            high = middle - 1
    return value[:low]
