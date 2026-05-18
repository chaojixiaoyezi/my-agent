# LLM: Idempotency contract identity lets child create/schedule reuse only explicit machine facts.
# 模块用途: 从 context_packs 中提取 subagent_idempotency_contract.v1，避免用普通 goal 文本做复用依据。

from __future__ import annotations

from typing import Any

_IDEMPOTENCY_CONTRACT_SCHEMA = "subagent_idempotency_contract.v1"


# LLM: idempotency_contract_identity_from_context_packs returns an empty tuple when no contract exists.
# 函数用途: 读取 context_packs[*].contract 的 schema/key/scope 字段，生成稳定可比较身份。
def idempotency_contract_identity_from_context_packs(value: object) -> tuple[object, ...]:
    for pack in _iter_packs(value):
        contract = pack.get("contract")
        if _text(contract.get("schema") if isinstance(contract, dict) else "") == _IDEMPOTENCY_CONTRACT_SCHEMA:
            return _contract_identity(contract)
    return ()


# LLM: _contract_identity keeps reuse independent from display wording.
# 函数用途: 用 schema、kind、idempotency_key 和 scope_refs 区分同父级重复创建请求。
def _contract_identity(contract: dict[str, Any]) -> tuple[object, ...]:
    return (
        _IDEMPOTENCY_CONTRACT_SCHEMA,
        _text(contract.get("kind")),
        _text(contract.get("idempotency_key") or contract.get("key")),
        _string_tuple(contract.get("scope_refs"), normalize_path=True),
    )


# LLM: _iter_packs accepts only mapping context packs so arbitrary strings cannot invent identities.
# 函数用途: 遍历 list/dict 形态的 context_packs，忽略损坏项。
def _iter_packs(value: object):
    if isinstance(value, dict):
        yield value
        return
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


# LLM: _string_tuple normalizes contract list fields into stable sorted tuples.
# 函数用途: 去空、去重、排序；路径类字段只做字符串级尾斜杠规整。
def _string_tuple(value: object, *, normalize_path: bool = False) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    normalized = []
    for item in values:
        text = _normalized_path(item) if normalize_path else _text(item)
        if text:
            normalized.append(text)
    return tuple(sorted(dict.fromkeys(normalized)))


# LLM: _normalized_path trims cosmetic path differences without filesystem access.
# 函数用途: 对合同路径只去尾斜杠，避免不存在路径触发 I/O。
def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


# LLM: _text safely stringifies optional contract fields.
# 函数用途: 将空值和非字符串字段转换为去空格字符串。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["idempotency_contract_identity_from_context_packs"]
