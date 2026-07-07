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

from .text_tokens import head_token

_MAX_FIELD_PATH_LEN = 200
_MAX_VALUE_LEN = 400
_MAX_TARGET_VALUES = 64
_MAX_CONTAINS = 32
_MAX_NORMAL_VALUES = 64
_MAX_IGNORE_FIELDS = 128
_MATCH_VALUE_DISPLAY_CAP = 160


@dataclass(frozen=True)
class SpecMatch:
    """一次判据匹配的结构化依据。抬升模式(target_value/target_contains/outside_normal)
    进候选 triage,模型据此复核;常态模式(normal_value/normal_contains)= 内容过滤规则
    命中,value 存【命中的规则条目】(集合成员/子串记号)而非事件取值,引擎按条目记账
    (规则可审计:哪条规则、拦了多少)。"""

    path: str
    value: str
    mode: str  # target_value / target_contains / outside_normal / normal_value / normal_contains


NORMAL_RULE_MODES = ("normal_value", "normal_contains")


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
        """抬升命中(target/常态之外);常态规则命中折叠为 None。保留旧契约供只关心
        "抬不抬"的调用方;引擎走 classify(还要给常态规则记账)。"""
        hit = self.classify(flat)
        if hit is None or hit.mode in NORMAL_RULE_MODES:
            return None
        return hit

    def classify(self, flat: list[tuple[str, object]]) -> SpecMatch | None:
        """按 spec 对压平事件做机械匹配,五种模式全量报告;未配 result_field 时恒不命中
        (纯忽略型 spec);结果端字段不在事件里 → None。"""
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
        if self._in_set(canon, self.target_values):
            return SpecMatch(self.result_field, shown, "target_value")
        if any(token in canon for token in self.target_value_contains):
            return SpecMatch(self.result_field, shown, "target_contains")
        if not self.normal_values and not self.normal_value_contains:
            return None
        normal_entry = self._normal_entry(canon)
        if normal_entry is not None:
            return SpecMatch(self.result_field, normal_entry[1], normal_entry[0])
        return SpecMatch(self.result_field, shown, "outside_normal")

    def _normal_entry(self, canon: str) -> tuple[str, str] | None:
        """命中的常态规则条目 (mode, 条目):精确集合命中报集合成员(整串或首记号),
        子串命中报第一个匹配的记号(配置序,确定性)。None=常态之外。"""
        if self._in_set(canon, self.normal_values):
            member = canon if canon in self.normal_values else head_token(canon)
            return ("normal_value", member)
        for token in self.normal_value_contains:
            if token in canon:
                return ("normal_contains", token)
        return None

    @staticmethod
    def _in_set(canon: str, values: frozenset[str]) -> bool:
        """集合成员判定,对文本结果端做首记号兜底(§4):模型学的常态/目标记号清单常是
        裸词(accepted/diverted),而结果端字段是"结论词 + 高基数尾巴"的整句文本
        (accepted ref=... t=7);整串永不等于裸词 → 精确集合在文本字段上恒不命中,
        normal_values 于是把每条都判成"常态之外"→ 候选洪泛淹没真目标(真机实锤 2621 洪泛)。
        兜底=整串不命中时,再拿该值的首记号比一次;纯字面切分+集合成员,零自然语言判断。
        裸词/枚举字段:canon 本身就是裸词,首记号==canon,行为不变。"""
        if not values:
            return False
        if canon in values:
            return True
        head = head_token(canon)
        return bool(head) and head != canon and head in values

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


__all__ = ["NORMAL_RULE_MODES", "SourceSpec", "SpecMatch", "canon_value", "parse_source_spec"]
