# LLM: B的纯来源事实合同，宿主从正式来源/触发事实构造，不从模型语句推断。
# 模块用途: 冻结可学习来源、自动化排除和子代理标签，不接候选库或curator。
from dataclasses import asdict, dataclass

from ._agents_contract_fields import (
    AgentsContractError,
    decode_fields,
    require_counter,
    require_text,
    validate_scalars,
)


# LLM: 标签和lineage都必须来自宿主；can_learn只可保守关闭，不覆盖来源排除。
# 类用途: 不可变地携带归属、当次触发及线程/回合/请求和子代理来源引用。
@dataclass(frozen=True, slots=True)
class AgentsLearningSourceFacts:
    schema_version: int
    owner: str
    source_kind: str
    trigger_kind: str
    can_learn: bool
    subagent_tag: str
    source_run_ids: tuple[str, ...]
    source_task_ids: tuple[str, ...]
    thread_id: str
    turn_id: str
    request_id: str
    policy_version: int

    # LLM: JSON是完整投影而非可信来源证明；与原元组无共享可变对象，无I/O。
    # 函数用途: 输出v1 JSON数组，保持子代理标签与来源ID。
    def to_dict(self) -> dict[str, object]:
        _validate_learning_shape(self)
        result = asdict(self)
        result["source_run_ids"] = list(self.source_run_ids)
        result["source_task_ids"] = list(self.source_task_ids)
        return result

    # LLM: 缺失来源不默认用户；B从可信存储解码，模型候选不能替换host事实。
    # 函数用途: 严格解码完整JSON并核对当前规范owner。
    @classmethod
    def from_dict(cls, payload, expected_owner):
        values = decode_fields(cls, payload)
        for field in ("source_run_ids", "source_task_ids"):
            if type(values[field]) is not list:
                raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)
            values[field] = tuple(values[field])
        return validate_learning_source(cls(**values), expected_owner)


# LLM: expected_owner独立来自宿主；仅验形状和来源规则，不访问历史或自动采集。
# 函数用途: 拒绝跨owner及可学性矛盾；未知来源只有can_learn=false时可保留。
def validate_learning_source(facts, expected_owner):
    if type(facts) is not AgentsLearningSourceFacts:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "facts")
    _validate_learning_shape(facts)
    require_text(expected_owner, "expected_owner")
    if facts.owner != expected_owner:
        raise AgentsContractError("AGENTS_LEARNING_OWNER_MISMATCH", "owner")
    return facts


# LLM: 精确事实类型才能序列化；未知来源/触发/策略不扩张为可学，关闭许可保留false。
# 函数用途: 验证来源事实与学习许可是否相容，不根据消息role推测。
def _validate_learning_shape(facts) -> None:
    if type(facts) is not AgentsLearningSourceFacts:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "facts")
    validate_scalars(facts)
    for field in ("owner", "source_kind", "trigger_kind", "thread_id", "turn_id", "request_id"):
        require_text(getattr(facts, field), field)
    require_counter(facts.policy_version, "policy_version")
    _validate_lineage(facts)
    if facts.can_learn and not _source_eligible(facts):
        raise AgentsContractError("AGENTS_LEARNING_SOURCE_FORBIDDEN", "can_learn")


# LLM: 子代理标签固定subagent，不冒充user_explicit；run必有，未创建task可保留空tuple。
# 函数用途: 核对不可变lineage、非空去重ID及标签和来源的一致性。
def _validate_lineage(facts) -> None:
    require_text(facts.subagent_tag, "subagent_tag", allow_empty=True)
    for field in ("source_run_ids", "source_task_ids"):
        identifiers = getattr(facts, field)
        for identifier in identifiers:
            require_text(identifier, field)
        if len(set(identifiers)) != len(identifiers):
            raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", field)
    if facts.source_kind == "subagent":
        if facts.subagent_tag != "subagent" or not facts.source_run_ids:
            raise AgentsContractError("AGENTS_LEARNING_CHILD_FACTS_INVALID", "subagent_tag")
    elif facts.subagent_tag:
        raise AgentsContractError("AGENTS_LEARNING_CHILD_FACTS_INVALID", "subagent_tag")


# LLM: trigger_kind是当次根触发，不能沿用原用户goal种子；自动化子代理也不得学。
# 函数用途: 仅v1策略下用户触发的用户对话或带标签子代理具备来源资格。
def _source_eligible(facts) -> bool:
    if facts.policy_version != 1 or facts.trigger_kind != "user_dialogue":
        return False
    return facts.source_kind in {"user_dialogue", "subagent"}
