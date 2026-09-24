# LLM: 调用引用只复用 canonical ToolCall 的 run/attempt/call 三元身份；不解释 scoped 字符串，不从请求或正文补身份。
# 模块用途: 为工具归档和 Compact 提供可校验、不可变的单次调用引用；缺信息时明确返回未知。

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

TOOL_CALL_REF_SCHEMA = "tool_call_ref.v1"
_IDENTITY_FIELDS = ("run_id", "attempt_id", "call_id")


# LLM: 身份等价严格依赖三字段，不包含 checkpoint 提交者 request/attempt 或展示别名。
# 类用途: 表示一次已有工具调用的精确来源，允许不同调用复用相同的 provider call id。
@dataclass(frozen=True)
class ToolCallRef:
    run_id: str
    attempt_id: str
    call_id: str

    # LLM: 禁止把数字、空值或空白自动转成有效身份；不修改 canonical 字符串。
    # 函数用途: 构造时校验三项来源字段，避免带缺口的引用获得过滤权限。
    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (
            self.run_id, self.attempt_id, self.call_id,
        )):
            raise ValueError("tool call ref requires non-empty run_id, attempt_id and call_id")

    # LLM: 持久引用有独立小 schema；新增身份字段须显式升级此格式，不能改变旧三元组含义。
    # 函数用途: 输出可随现有 checkpoint 保存的最小结构化调用来源。
    def to_dict(self) -> dict[str, str]:
        return {
            "schema": TOOL_CALL_REF_SCHEMA,
            "run_id": self.run_id,
            "attempt_id": self.attempt_id,
            "call_id": self.call_id,
        }

    # LLM: 持久 reader 只接受明确 schema 和完整字段；未知版本不得降级成裸 call id。
    # 函数用途: 校验并读取一条持久引用，错误留给调用方作不确定性裁决。
    @classmethod
    def from_dict(cls, value: object) -> ToolCallRef:
        if not isinstance(value, Mapping) or set(value) != {"schema", *_IDENTITY_FIELDS}:
            raise ValueError("tool call ref fields are invalid")
        if value.get("schema") != TOOL_CALL_REF_SCHEMA:
            raise ValueError("tool call ref schema is invalid")
        return cls(*(value[key] for key in _IDENTITY_FIELDS))


# LLM: 调用方必须提供归档本身的字段；这里不接收当前运行默认值，也不解析 scoped_call_id。
# 函数用途: 从一条工具记录提取精确来源；旧记录缺字段时保留未知状态。
def tool_call_ref_from_record(record: Mapping[str, object]) -> ToolCallRef | None:
    try:
        return ToolCallRef(*(record.get(key) for key in _IDENTITY_FIELDS))
    except ValueError:
        return None
