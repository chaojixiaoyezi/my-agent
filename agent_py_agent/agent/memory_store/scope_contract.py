from __future__ import annotations

"""共享 scope-key canonical/validator 合同：写入与召回共用同一把尺。

一个概念一个权威位置：
- 正式写入键：project 一律 project:<id>（task:<id> 只作旧账本/运行时 task_id 的
  召回兼容 read alias；写入入口自动归一，不允许以 task:<id> 新持久化）。
  其余 typed scope 一律 <type>:<id>；global/personal 是固定键。
- raw ID（如 acme）→ canonical（company:acme）；已 canonical 原样；
  嵌套 typed prefix（company:company:acme、project:task:alpha）与空 remainder
  （company:、project:、task:）一律拒绝。
"""

# LLM: 每种 typed scope 只允许自己类型的前缀；project 的 task: 是旧账本别名前缀，
# 归一到 project:<id>（单一权威）；global/personal 是 owner 内固定键。
# 函数用途: 声明各 scope 的合法 typed 前缀与固定键。
_TYPED_PREFIXES: dict[str, tuple[str, ...]] = {
    "company": ("company:",),
    "project": ("project:",),  # task: 由 _TASK_PREFIX 归一到 project:<id>
    "task_class": ("task_class:",),
    "session": ("session:",),
    "temporary": ("temporary:",),
}
_TASK_PREFIX = "task:"
_FIXED_KEYS: dict[str, str] = {"global": "global", "personal": "personal"}
_ALL_PREFIXES: tuple[str, ...] = tuple(
    sorted(
        {prefix for prefixes in _TYPED_PREFIXES.values() for prefix in prefixes}
        | {_TASK_PREFIX}
    )
)


# LLM: 命中 typed 前缀但 id 为空（如 company:）是坏键，写入/召回都必须 fail-closed。
# 函数用途: 拒绝空 remainder 的 typed 键。
def _require_remainder(scope_type: str, remainder: str) -> None:
    if not remainder:
        raise ValueError(f"{scope_type} scope_key 不能以空 remainder 结尾: {scope_type}:")


# LLM: id 部分再以任何 scope 前缀开头即嵌套 typed prefix，fail-closed 拒绝。
# 函数用途: 检查去前缀后的 id 不含嵌套 typed prefix。
def _reject_nested(value: str) -> None:
    for prefix in _ALL_PREFIXES:
        if value.startswith(prefix):
            raise ValueError(f"scope_key 含嵌套 typed prefix: {prefix}{value}")


# LLM: canonical 化必须幂等（canonical(canonical(x)) == canonical(x)），
# raw/typed/task 别名三种输入必须落同一键，写侧与召回侧才能对齐。
# 函数用途: 把 raw host ID、已 canonical typed key 或 task 别名归一为唯一 canonical 键。
def canonical_scope_key(scope_type: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("scope_key 不能为空")
    fixed = _FIXED_KEYS.get(scope_type)
    if fixed is not None:
        if text == fixed:
            return text
        raise ValueError(f"{scope_type} scope_key 必须等于 {fixed!r}")
    prefixes = _TYPED_PREFIXES.get(scope_type)
    if prefixes is None:
        raise ValueError(f"unsupported scope_type: {scope_type}")
    canonical_prefix = prefixes[0]
    if text.startswith(canonical_prefix):
        remainder = text[len(canonical_prefix):]
        _require_remainder(scope_type, remainder)
        _reject_nested(remainder)
        return text
    if scope_type == "project" and text.startswith(_TASK_PREFIX):
        remainder = text[len(_TASK_PREFIX):]
        _require_remainder(scope_type, remainder)
        _reject_nested(remainder)
        return f"{canonical_prefix}{remainder}"
    _reject_nested(text)
    return f"{canonical_prefix}{text}"


# LLM: 写入侧只放行 typed/canonical 输入，返回归一持久化键；project 的 task:<id>
# 是旧账本兼容输入，写入时归一到 project:<id>，避免 project/task 双正式身份。
# 函数用途: 校验新观察 scope key 已 typed 且无嵌套，返回归一持久化键。
def validate_typed_scope_key(scope_type: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("scope_key 不能为空")
    fixed = _FIXED_KEYS.get(scope_type)
    if fixed is not None:
        if text != fixed:
            raise ValueError(f"{scope_type} scope_key 必须等于 {fixed!r}")
        return text
    prefixes = _TYPED_PREFIXES.get(scope_type)
    if prefixes is None:
        raise ValueError(f"unsupported scope_type: {scope_type}")
    canonical = canonical_scope_key(scope_type, text)
    is_typed = text.startswith(prefixes[0]) or (
        scope_type == "project" and text.startswith(_TASK_PREFIX)
    )
    if not is_typed:
        raise ValueError(f"{scope_type} scope_key 必须是 typed key，收到 raw: {text!r}")
    return canonical


# LLM: 别名只用于召回投影（老账本兼容），不产生新的写侧合法键。
# 函数用途: 返回 canonical 键的兼容召回别名（project 的 task:<id> 形式）。
def scope_key_aliases(scope_type: str, canonical: str) -> tuple[str, ...]:
    if scope_type == "project" and canonical.startswith("project:"):
        return (canonical, "task:" + canonical[len("project:"):])
    return (canonical,)


__all__ = [
    "canonical_scope_key",
    "scope_key_aliases",
    "validate_typed_scope_key",
]
