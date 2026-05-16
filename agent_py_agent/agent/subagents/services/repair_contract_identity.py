# LLM: Repair contract identity lets create/schedule reuse the same repair owner by machine facts.
# 模块用途: 从 context_packs 中提取 repair_contract 身份键，避免固定中文名字误合并不同修复任务。

from __future__ import annotations

from typing import Any

_REPAIR_CONTRACT_SCHEMA = "subagent_repair_contract.v1"


# LLM: repair_contract_identity_from_context_packs returns an empty tuple when no repair contract exists.
# 函数用途: 读取 context_packs[*].contract，生成可比较的 kind/failed_run/target/required refs 身份键。
def repair_contract_identity_from_context_packs(value: object) -> tuple[object, ...]:
    for pack in _iter_packs(value):
        contract = pack.get("contract")
        if _text(contract.get("schema") if isinstance(contract, dict) else "") == _REPAIR_CONTRACT_SCHEMA:
            return _contract_identity(contract)
    return ()


# LLM: _contract_identity keeps repair scope comparisons independent from wording and field order.
# 函数用途: 只比较修复合同的机器字段；不同失败 run 或目标产物不会复用同一个 repair owner。
def _contract_identity(contract: dict[str, Any]) -> tuple[object, ...]:
    targets = _string_tuple(contract.get("target_artifact_refs"), normalize_path=True)
    required = _string_tuple(contract.get("required_read_paths"), normalize_path=True)
    return (
        _REPAIR_CONTRACT_SCHEMA,
        _text(contract.get("kind")),
        _string_tuple(contract.get("failed_run_ids")),
        targets,
        required if not targets else (),
    )


# LLM: _iter_packs accepts only mapping context packs so bad model payloads cannot invent identities.
# 函数用途: 遍历 list/dict 形态的 context_packs，忽略字符串或损坏项。
def _iter_packs(value: object):
    if isinstance(value, dict):
        yield value
        return
    if not isinstance(value, list):
        return
    for item in value:
        if isinstance(item, dict):
            yield item


# LLM: _string_tuple normalizes contract lists into stable sorted tuples.
# 函数用途: 去空、去重、排序，避免同一个合同因字段顺序不同被看成两个任务。
def _string_tuple(value: object, *, normalize_path: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        values = [value]
    else:
        values = value
    normalized = []
    for item in values:
        text = _normalized_path(item) if normalize_path else _text(item)
        if text:
            normalized.append(text)
    return tuple(sorted(dict.fromkeys(normalized)))


# LLM: _normalized_path trims cosmetic path differences without accessing the filesystem.
# 函数用途: 修复合同里的路径比较只去尾斜杠，不做真实路径解析。
def _normalized_path(value: object) -> str:
    text = _text(value)
    if text == "/":
        return text
    return text.rstrip("/")


# LLM: _text safely stringifies optional contract fields.
# 函数用途: 把空值和非字符串字段统一转成去空格字符串。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["repair_contract_identity_from_context_packs"]
