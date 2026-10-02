# LLM: 决策请求只有建议权；身份与版本来自宿主，不能从模型正文获得权限，消费者仍须复查当前事实。
# 模块用途: 冻结短决策的输入、来源与逐题结果，和聊天正文、工具调用、持久业务状态分开。
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field

# 决策请求输入上限 256 KiB：防超大请求体撑爆内存。
MAX_DECISION_INPUT_BYTES = 262_144
# 决策响应上限 1 MiB：防超大响应撑爆解析。
MAX_DECISION_RESPONSE_BYTES = 1_048_576
# 决策请求 JSON 嵌套最多 16 层：防超深结构把解析撑爆，无物理单位。
MAX_DECISION_JSON_DEPTH = 16
# 决策请求 JSON 最多 16384 个节点：防超深超宽结构把解析撑爆。
MAX_DECISION_JSON_NODE_COUNT = 16_384


# LLM: 决策输入窗口判断需要与后端发送前同一套 token 估算口径；估算实现留在 memory_archive.tokens
#   （那层的既有职责），这里只做转发，让 memory_store 等下游层不必直接依赖 memory_archive。
# 函数用途: 用与后端一致的口径估算一段决策载荷的输入 token 上界。
def decision_input_tokens(payload: object) -> int:
    from ..memory_archive.tokens import estimate_tokens

    return estimate_tokens(payload)


# LLM: 错误只带固定文案，不能泄漏原 state、候选材料或私有配置；运行失败不等于 not_needed。
# 类用途: 表示本地决策输入不符合合同，请调用方保留原业务方案。
class DecisionInputError(ValueError):
    pass


# LLM: 决策材料含不应外发的内容（如带查询串的 URL）时抛出；reason 是宿主定义的结构化跳过原因码，调用方据此记一条
#   skipped 结果。它仍是 DecisionInputError：未专门处理的调用方照旧放弃本次可选决策、保留原结果。
# 类用途: 可选决策因隐私保护被跳过时的输入错误，携带跳过原因码。
class DecisionPrivacySkip(DecisionInputError):
    # LLM: message 只供日志阅读，判定只看 reason 字段。
    # 函数用途: 记录错误说明与结构化跳过原因码（默认 privacy_url）。
    def __init__(self, message: str, reason: str = "privacy_url") -> None:
        super().__init__(message)
        self.reason = reason


# LLM: 只遍历已确认的原生 dict/list，字段名计入编码前预算，不接受可执行的自定义映射对象。
# 函数用途: 取得 JSON 容器成员和字段名长度，供同一个迭代预算检查使用。
def _json_members(value: dict | list):
    if isinstance(value, list):
        return value, 0
    if any(type(key) is not str for key in value):
        raise DecisionInputError("决策材料字段名须为字符串。")
    return value.values(), sum(len(key) for key in value)


# LLM: 标量校验不能先展开巨整数；估算只用于拒绝超量，真实 UTF-8 字节仍在最终编码后核对。
# 函数用途: 检查 JSON 标量并累计字符串和整数的长度，拒绝非有限数及自定义对象。
def _scalar_characters(value: object) -> int:
    if type(value) is str:
        return len(value)
    if type(value) is int:
        return value.bit_length() * 30103 // 100000 + 1 + (value < 0)
    if type(value) is float:
        if not math.isfinite(value):
            raise DecisionInputError("决策材料数值须有限。")
        return 0
    if value is None or type(value) is bool:
        return 0
    raise DecisionInputError("决策材料须为 JSON 数据。")


# LLM: 当前请求超出字数预算时取首尾节选（开头约 2/3、结尾约 1/3，中间写明省略字数），并返回结构化完整性标注
#   {status: complete|truncated, chars, kept_chars}；budget<=0 表示不截取（仍受 MAX_DECISION_INPUT_BYTES 约束）。
#   只删不改，调用方必须把标注和节选一起放进 state，让决策模型知道这是节选、信息不足时选 need_data；不能把节选冒充完整原话。
# 函数用途: 把用户当前请求压进决策输入预算，长指令也能得到决策，而不是整个点位被跳过。
def decision_request_excerpt(text: str, budget: int) -> tuple[str, dict[str, object]]:
    if budget <= 0 or len(text) <= budget:
        return text, {"status": "complete", "chars": len(text)}
    head = budget * 2 // 3
    tail = budget - head
    omitted = len(text) - head - tail
    excerpt = f"{text[:head]}\n…（中间省略 {omitted} 字）…\n{text[len(text) - tail:]}"
    return excerpt, {"status": "truncated", "chars": len(text), "kept_chars": head + tail}

# LLM: 输入上限属于单次内存/解析防护，不代替目标模型 token 窗口；不得截断后冒充完整输入。
# 函数用途: 检查 JSON 深度、节点和字节并生成独立快照，拒绝循环、非有限数与非 JSON 类型。
def decision_json(value: object, *, limit: int = MAX_DECISION_INPUT_BYTES) -> bytes:
    pending = [(value, 0)]
    nodes = 0
    characters = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_DECISION_JSON_DEPTH or nodes > MAX_DECISION_JSON_NODE_COUNT:
            raise DecisionInputError("决策材料层数或条目过多。")
        if type(item) in (dict, list):
            if len(item) + len(pending) + nodes > MAX_DECISION_JSON_NODE_COUNT:
                raise DecisionInputError("决策材料条目过多。")
            members, key_characters = _json_members(item)
            characters += key_characters
            pending.extend((child, depth + 1) for child in members)
        else:
            characters += _scalar_characters(item)
        if characters > limit:
            raise DecisionInputError("决策材料超过字节上限。")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise DecisionInputError("决策材料无法编码为 JSON。") from exc
    if len(encoded) > limit:
        raise DecisionInputError("决策材料超过字节上限。")
    return encoded


# LLM: 此对象绑定宿主输入快照，不是身份认证；来源 refs 不自动发给外部服务，运行前后由消费点复核。
# 类用途: 标记一次建议属于哪个接入点、owner、操作及候选版本，防止把旧结果应用到新任务。
@dataclass(frozen=True)
class DecisionBinding:
    point: str
    owner_ref: str
    operation_id: str
    policy_revision: str
    candidates_revision: str
    thread_id: str = ""
    run_id: str = ""
    task_id: str = ""
    source_refs: tuple[str, ...] = ()

    # LLM: 固定引用只接受有限字符串；冻结来源集合，不解析自然语言身份或控制事件。
    # 函数用途: 创建请求时检查宿主绑定，防止空版本或可变引用进入快照。
    def __post_init__(self) -> None:
        values = (self.point, self.owner_ref, self.operation_id, self.policy_revision, self.candidates_revision)
        if any(type(value) is not str or not value or len(value) > 1024 for value in values):
            raise DecisionInputError("决策绑定缺少接入点、归属、操作或版本。")
        if type(self.source_refs) is not tuple or len(self.source_refs) > 256:
            raise DecisionInputError("决策来源引用须为有界的不可变集合。")
        if any(type(value) is not str or len(value) > 1024 for value in
               (self.thread_id, self.run_id, self.task_id, *self.source_refs)):
            raise DecisionInputError("决策来源引用格式无效。")


# LLM: 请求构造即深快照；摘要包含宿主版本和来源，payload 不含本地身份，不能被外部可变 dict 改写。
# 类用途: 在输入准备阶段固定本次判断材料，后端只发送 state/questions；没有写文件或网络副作用。
@dataclass(frozen=True, init=False)
class DecisionRequest:
    binding: DecisionBinding
    input_digest: str
    _body: bytes = field(repr=False)

    # LLM: 调用方须在准备前建立阶段期限；本构造器只冻结输入，不延长或重置期限。
    # 函数用途: 校验并复制 state/questions，后续编辑原对象不会影响实际请求或摘要。
    def __init__(self, binding: DecisionBinding, state: object, questions: dict) -> None:
        if not isinstance(binding, DecisionBinding) or type(questions) is not dict or not questions:
            raise DecisionInputError("决策请求须含宿主绑定和题目集合。")
        body = decision_json({"state": state, "questions": questions})
        digest = hashlib.sha256(decision_json(asdict(binding) | {"source_refs": list(binding.source_refs)}) + b"\n" + body).hexdigest()
        object.__setattr__(self, "binding", binding)
        object.__setattr__(self, "input_digest", digest)
        object.__setattr__(self, "_body", body)

    # LLM: 返回独立副本，模型名称来自已解析配置；绑定身份和 refs 不被隐式上传。
    # 函数用途: 生成一次发送的请求体，调用方修改返回值不影响冻结快照。
    def payload(self, model: str) -> dict:
        if type(model) is not str or not model.strip() or len(model) > 4096:
            raise DecisionInputError("决策模型名称无效。")
        return {**json.loads(self._body), "model": model}


# LLM: value 与 probability 保留协议原义，confidence 不是授权；单题错误不改称 abstain/not_needed。
# 类用途: 保存一个题目的选择、评分或概率，以及该题独立的校验失败。
@dataclass(frozen=True)
class DecisionAnswer:
    question_id: str
    kind: str
    value: str | float | None = None
    confidence: float | None = None
    probabilities: tuple[tuple[str, float], ...] = ()
    legend: tuple[tuple[str, str], ...] = ()
    error_code: str = ""


# LLM: 不含正文/工具执行权；usage 属性沿原账本的 dict 接口，缺用量保持缺失，不能推导供应商零计费。
# 类用途: 统一决策响应的绑定、实际模型版本、逐题结果和用量，供原调用账和业务消费点读取。
@dataclass(frozen=True)
class DecisionResponse:
    binding: DecisionBinding
    input_digest: str
    requested_model: str
    model: str
    answers: tuple[DecisionAnswer, ...]
    _usage_json: bytes = field(repr=False)
    usage_reported: bool = False
    backend: str = "typesafe_decision"

    # LLM: 每次读回用量均为副本，未知字段留在原用量信封；迟到补记须由原账本决定，响应没有提交权。
    # 函数用途: 让统一费用记录读取供应商用量，未报告时返回空对象而非补零。
    @property
    def usage(self) -> dict:
        return json.loads(self._usage_json)
