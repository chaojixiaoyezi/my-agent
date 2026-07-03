"""per-源判据 spec:模型从源样本【学】出的结构化判据,引擎只按它【机械执行】。

learn → configure → monitor 的数据契约:模型看样本(watch_stream action=sample)判断
哪个字段是结果端、什么取值是目标/常态、哪些字段是高基数噪声,产出本 spec(纯结构:
字段路径 + 字面取值集合/子串集合),经 action=configure 灌进引擎。引擎侧全部匹配都是
字面集合成员/子串包含的机械比对——理解在模型,代码零自然语言判断(铁律)。

四种互补的匹配模式(至少配一种;可并存,按 target→常态 顺序判):
- target_values:结果端取值 ∈ 集合 → 目标(目标取值已知,如样本/任务里明确)。
- target_value_contains:结果端取值(字符串化后)含任一子串 → 目标(结果端是
  混了高基数尾巴的文本消息时,精确集合失效,靠结论记号子串)。
- normal_values:结果端取值 ∉ 常态集合 → 候选(样本里往往没有目标事件,只能
  学出"常态长什么样"、盯不在常态里的异态)。
- normal_value_contains:结果端取值不含任何常态记号子串 → 候选(文本消息型
  结果端 + 目标未见过:每条消息尾巴都不同,精确常态集合失效,学常态【记号】)。
  normal_values 与 normal_value_contains 并存时满足其一即算常态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_MAX_FIELD_PATH_LEN = 200
_MAX_VALUE_LEN = 400
_MAX_TARGET_VALUES = 64
_MAX_CONTAINS = 32
_MAX_NORMAL_VALUES = 64
_MAX_IGNORE_FIELDS = 128
_MATCH_VALUE_DISPLAY_CAP = 160


@dataclass(frozen=True)
class SpecMatch:
    """一次命中判据的结构化依据(进候选 triage,模型据此复核)。"""

    path: str
    value: str
    mode: str  # target_value / target_contains / outside_normal


@dataclass(frozen=True)
class SourceSpec:
    result_field: str = ""
    target_values: frozenset[str] = frozenset()
    target_value_contains: tuple[str, ...] = ()
    normal_values: frozenset[str] = frozenset()
    normal_value_contains: tuple[str, ...] = ()
    ignore_fields: frozenset[str] = field(default_factory=frozenset)
    # 本 spec 车道每批候选上限覆盖(0=用 tuning.spec_max_candidates_per_pull)。
    max_per_pull: int = 0

    def match(self, flat: list[tuple[str, object]]) -> SpecMatch | None:
        """按 spec 对压平事件做机械匹配;未配 result_field 时恒不命中(纯忽略型 spec)。"""
        if not self.result_field:
            return None
        for path, value in flat:
            if path != self.result_field:
                continue
            hit = self._match_value(canon_value(value))
            if hit is not None:
                return hit
        return None

    def _match_value(self, canon: str) -> SpecMatch | None:
        shown = canon[:_MATCH_VALUE_DISPLAY_CAP]
        if canon in self.target_values:
            return SpecMatch(self.result_field, shown, "target_value")
        if any(token in canon for token in self.target_value_contains):
            return SpecMatch(self.result_field, shown, "target_contains")
        if self._outside_normal(canon):
            return SpecMatch(self.result_field, shown, "outside_normal")
        return None

    def _outside_normal(self, canon: str) -> bool:
        """配了常态判据且两种常态判定(精确集合/记号子串)都不满足 → 常态之外。"""
        if not self.normal_values and not self.normal_value_contains:
            return False
        if canon in self.normal_values:
            return False
        return not any(token in canon for token in self.normal_value_contains)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"result_field": self.result_field}
        if self.target_values:
            payload["target_values"] = sorted(self.target_values)
        if self.target_value_contains:
            payload["target_value_contains"] = list(self.target_value_contains)
        if self.normal_values:
            payload["normal_values"] = sorted(self.normal_values)
        if self.normal_value_contains:
            payload["normal_value_contains"] = list(self.normal_value_contains)
        if self.ignore_fields:
            payload["ignore_fields"] = sorted(self.ignore_fields)
        if self.max_per_pull:
            payload["max_per_pull"] = self.max_per_pull
        if not self.result_field:
            payload.pop("result_field")
        return payload


def canon_value(value: object) -> str:
    """标量 → 匹配用的规范字符串(与 spec 里模型给的取值同一套规范,两边可比)。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def parse_source_spec(raw: object) -> SourceSpec:
    """校验并解析模型提交的 spec(dict);不合法抛 ValueError(带可修正的原因)。"""
    if not isinstance(raw, dict):
        raise ValueError("spec 须是 JSON 对象")
    result_field = str(raw.get("result_field") or "").strip()
    if len(result_field) > _MAX_FIELD_PATH_LEN:
        raise ValueError(f"result_field 过长(>{_MAX_FIELD_PATH_LEN})")
    spec = SourceSpec(
        result_field=result_field,
        target_values=frozenset(_str_list(raw, "target_values", _MAX_TARGET_VALUES)),
        target_value_contains=tuple(_str_list(raw, "target_value_contains", _MAX_CONTAINS)),
        normal_values=frozenset(_str_list(raw, "normal_values", _MAX_NORMAL_VALUES)),
        normal_value_contains=tuple(_str_list(raw, "normal_value_contains", _MAX_CONTAINS)),
        ignore_fields=frozenset(_str_list(raw, "ignore_fields", _MAX_IGNORE_FIELDS)),
        max_per_pull=_max_per_pull(raw),
    )
    _validate_shape(spec)
    return spec


def _validate_shape(spec: SourceSpec) -> None:
    has_rule = bool(
        spec.target_values or spec.target_value_contains or spec.normal_values or spec.normal_value_contains
    )
    if spec.result_field and not has_rule:
        raise ValueError(
            "给了 result_field 就要配 target_values / target_value_contains / "
            "normal_values / normal_value_contains 至少一种"
        )
    if has_rule and not spec.result_field:
        raise ValueError("配了取值判据但缺 result_field(字段路径,如 a.b.c)")
    if not spec.result_field and not spec.ignore_fields:
        raise ValueError("空 spec:至少给 result_field+取值判据,或 ignore_fields")
    if spec.result_field in spec.ignore_fields:
        raise ValueError("result_field 不能同时列进 ignore_fields")


def _str_list(raw: dict, key: str, cap: int) -> list[str]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} 须是数组")
    if len(value) > cap:
        raise ValueError(f"{key} 过多(>{cap})")
    out: list[str] = []
    for item in value:
        if isinstance(item, (dict, list, tuple)):
            raise ValueError(f"{key} 里只能放标量")
        text = canon_value(item) if not isinstance(item, str) else item
        if not text:
            raise ValueError(f"{key} 里有空值")
        if len(text) > _MAX_VALUE_LEN:
            raise ValueError(f"{key} 里有超长取值(>{_MAX_VALUE_LEN})")
        out.append(text)
    return out


def _max_per_pull(raw: dict) -> int:
    try:
        parsed = int(str(raw.get("max_per_pull") or 0).strip() or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, min(50, parsed))


__all__ = ["SourceSpec", "SpecMatch", "canon_value", "parse_source_spec"]
