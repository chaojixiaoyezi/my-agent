# LLM: Delegation intent helpers read structured protocol attributes only.
# 模块用途: 集中读取 refs_only/subagent_delegation 机器字段；普通用户说法由模型规划，不由代码判断。

from __future__ import annotations


# LLM: refs_only_delegation_enabled distinguishes explicit delegation from ordinary acceptance wording.
# 函数用途: 只接受 task_attributes 里的 refs_only/delegate_only 机器字段，启用顶层 root 读正文保护。
def refs_only_delegation_enabled(attributes: dict | None) -> bool:
    return _truthy_field(attributes, "refs_only", "delegate_only")


# LLM: subagent_delegation_enabled accepts only explicit machine fields.
# 函数用途: 识别 task_attributes.subagent_delegation/delegate_only，用于派工前少读正文策略。
def subagent_delegation_enabled(attributes: dict | None) -> bool:
    return _truthy_field(attributes, "subagent_delegation", "delegate_only")


# LLM: parent_body_read_allowed recognizes explicit current-run machine override fields.
# 函数用途: 只接受 task_attributes.parent_body_read=allow 或 parent_body_read_allowed=true，临时允许父级读正文。
def parent_body_read_allowed(attributes: dict | None) -> bool:
    return _field_mode_is(attributes, "parent_body_read", "allow") or _truthy_field(
        attributes,
        "parent_body_read_allowed",
    )


# LLM: parent_product_write_allowed recognizes explicit current-run machine override fields.
# 函数用途: 只接受 task_attributes.parent_product_write=allow 或 parent_product_write_allowed=true，临时允许父级写产物。
def parent_product_write_allowed(attributes: dict | None) -> bool:
    return _field_mode_is(attributes, "parent_product_write", "allow") or _truthy_field(
        attributes,
        "parent_product_write_allowed",
    )


# LLM: _truthy_field reads exact bool-like protocol values without scanning prose.
# 函数用途: 从结构化 attributes 读取布尔字段；不解析 prompt、goal、summary 或普通句子。
def _truthy_field(attributes: dict | None, *keys: str) -> bool:
    attrs = attributes if isinstance(attributes, dict) else {}
    return any(_boolish(attrs.get(key)) for key in keys)


# LLM: _field_mode_is compares explicit enum-style protocol fields.
# 函数用途: 读取 allow/deny 这类短枚举字段，保持大小写和连接符容错。
def _field_mode_is(attributes: dict | None, key: str, expected: str) -> bool:
    attrs = attributes if isinstance(attributes, dict) else {}
    actual = str(attrs.get(key) or "").strip().casefold().replace("-", "_")
    return actual == expected.casefold().replace("-", "_")


# LLM: _boolish is deliberately tiny and closed for protocol fields.
# 函数用途: 把 true/1/yes/on/allow 归一成 True；其它值不当作自然语言判断。
def _boolish(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    return str(value or "").strip().casefold() in {"1", "true", "yes", "y", "on", "allow", "allowed"}
