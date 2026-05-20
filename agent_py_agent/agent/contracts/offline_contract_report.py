# LLM: Shared report helpers keep offline contract modules small and consistent.
# 模块用途: 提供离线合同校验的统一 dataclass、finding、字符串规整和列表规整工具。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# LLM: OfflineContractValidation is a generic machine-readable validation result.
# 类用途: 返回合同是否通过、错误码、逐项 finding 和可选忽略事件 ID。
@dataclass(frozen=True)
class OfflineContractValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    ignored_event_ids: tuple[str, ...] = ()


# LLM: validation_report builds a stable validation object from findings.
# 函数用途: 从 findings[].code 去重生成 error_codes。
def validation_report(
    findings: list[dict[str, object]],
    *,
    ignored_event_ids: tuple[str, ...] = (),
) -> OfflineContractValidation:
    return OfflineContractValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        ignored_event_ids=ignored_event_ids,
    )


# LLM: finding creates compact machine findings.
# 函数用途: 统一生成 code 和可选结构字段。
def finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: dict_items returns dict entries from explicit arrays only.
# 函数用途: 过滤非 dict 项，避免把自然语言文本当机器事实。
def dict_items(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: string_tuple normalizes explicit string collections.
# 函数用途: 把结构化数组规整成去空字符串元组。
def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(item_text for item in value for item_text in (text(item),) if item_text)


# LLM: positive_int reads numeric facts without inventing missing values.
# 函数用途: 将显式数字转为非负 int，缺失或非法时返回 0。
def positive_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


# LLM: text normalizes scalar values for exact comparisons only.
# 函数用途: 将 None 或标量转成去空白字符串，不解析自然语言含义。
def text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineContractValidation", "dict_items", "finding", "positive_int", "string_tuple", "text", "validation_report"]
