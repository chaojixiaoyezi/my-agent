# LLM: 只冻结 A 的返回对象，不加载路径/网络，不激活前缀或写 last-good。
# 模块用途: 给来源正文与元数据一个不可变、可校验的交接包。

import hashlib
from dataclasses import dataclass

from ._agents_contract_fields import AgentsContractError, decode_fields
from .agents_source_contract import AgentsSourceSnapshot, validate_source_snapshot


# LLM: 正文只能是不可变文本或不可用时的 None；不能把加载状态视为权限事实。
# 类用途: 描述安全加载器将交给前缀消费者的完整结果。
@dataclass(frozen=True, slots=True)
class AgentsSourceLoadResult:
    snapshot: AgentsSourceSnapshot
    content: str | None

    # LLM: 先复核摘要与完整元数据，返回独立 JSON；未绑定消费 owner，调用方仍须显式校验。
    # 函数用途: 输出正文与快照，不修改缓存或来源。
    def to_dict(self):
        if type(self.snapshot) is not AgentsSourceSnapshot:
            raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "snapshot")
        validate_source_load(self, self.snapshot.owner)
        return {"snapshot": self.snapshot.to_dict(), "content": self.content}

    # LLM: 正文不做换行规范化；完整对象和 owner 校验之后才返回。
    # 函数用途: 解析并校验来源加载交接。
    @classmethod
    def from_dict(cls, payload, expected_owner):
        data = decode_fields(cls, payload)
        data["snapshot"] = AgentsSourceSnapshot.from_dict(data["snapshot"], expected_owner)
        return validate_source_load(cls(**data), expected_owner)


# LLM: 只校验返回值，不读取文件、不作权限判断；A 必须先完成安全读取与扫描。
# 函数用途: 绑定消费 owner/CAS，核对 UTF-8 原文本摘要或不可用的无正文结果。
def validate_source_load(result, expected_owner, expected_cas=None):
    if type(result) is not AgentsSourceLoadResult:
        raise AgentsContractError("AGENTS_CONTRACT_OBJECT_REQUIRED", "result")
    snapshot = validate_source_snapshot(result.snapshot, expected_owner, expected_cas)
    if snapshot.status == "unavailable":
        if result.content is not None:
            raise AgentsContractError("AGENTS_SOURCE_RESULT_INVALID", "content")
        return result
    if type(result.content) is not str:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "content")
    try:
        digest = hashlib.sha256(result.content.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise AgentsContractError("AGENTS_CONTRACT_FIELD_INVALID", "content") from None
    if digest != snapshot.content_sha256:
        raise AgentsContractError("AGENTS_SOURCE_CONTENT_MISMATCH", "content_sha256")
    return result
