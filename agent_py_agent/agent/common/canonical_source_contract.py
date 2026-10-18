# LLM: 本模块只运输实际组合根注入的canonical业务metadata规则；没有状态枚举/路径推断/执行许可，缺producer拒绝，不把空结果当成功。
# 模块用途: 让平台读取业务原来源合同，由业务自身维护schema、状态和存活语义。
from __future__ import annotations

from dataclasses import dataclass


# LLM: callback由真实业务模块提供，identity是原结构化字段投影；平台继续原source_digest，不新增指纹账或权限。
# 类用途: 描述canonical child来源的只读校验接口。
@dataclass(frozen=True)
class CanonicalChildSourceContract:
    validate: object
    identity: object
    has_work: object


_contract = None


# LLM: 实际core装配同一业务规则；重复构造只能使用同一组三函数，禁止重绑另一schema或从请求选择callback。
# 函数用途: 接入唯一canonical child元数据合同，不访问任何业务文件。
def bind_canonical_child_source(contract):
    global _contract
    if type(contract) is not CanonicalChildSourceContract or not all(callable(item) for item in
            (contract.validate, contract.identity, contract.has_work)):
        raise ValueError("canonical_child_source_contract_required")
    if _contract is not None and contract != _contract:
        raise ValueError("canonical_child_source_contract_changed")
    _contract = contract


# LLM: 未经真实root装配不能补默认状态或授消费权；contract仅读元数据，执行仍原平台P3/P5门。
# 函数用途: 返回唯一已装配的canonical规则，缺失明确拒绝。
def canonical_child_source_contract():
    if type(_contract) is not CanonicalChildSourceContract:
        raise ValueError("canonical_child_source_contract_required")
    return _contract
