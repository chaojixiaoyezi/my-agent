
from __future__ import annotations

"""原生 tool_use 协议的结构化中间表示（IR）。

这套 IR 是「provider 无关」的内部表达，用来无损描述一段工具循环历史：

    第 N 轮：assistant 文本 + 若干 ToolCall
    随后：与这些调用一一对应的 ToolResult
    期间：用户在同一运行 turn 里追加的 UserTurn

它替代现有文本协议里把「调用 + 结果」拼成 ``[tool-record]/[tool-output-record]``
字符串塞进 ``tool_context: list[str]`` 的做法（见 ``_tool_loop_service.py`` 的
``_record_tool_call``）。Step 2 接入时，每记一次工具调用就追加一个 ``ToolCall``，每
记一次结果就追加一个 ``ToolResult``，再由 ``MessageAdapter`` 翻译成厂商原生 messages。

字段刻意贴合现有运行时已有的数据，保证 Step 2 是「换装」而非「补数据」：
- ``ToolCall.id`` ← ``payload["call_id"]`` / ``ToolExecutionResult.call_id``；
- ``ToolCall.name`` ← ``payload["tool"]`` / ``ToolExecutionResult.tool``；
- ``ToolCall.input`` ← 去掉 ``tool``/``call_id`` 控制键后的结构化参数 dict；
- ``ToolResult.content`` ← ``ToolExecutionResult.output``；
- ``ToolResult.is_error`` ← ``not ToolExecutionResult.ok``。

本模块纯加法、零依赖，不 import 运行时模块，避免反向耦合。
"""

from dataclasses import dataclass, field
from typing import Any

# payload / input dict 里属于「控制键」而非工具真实参数的键。构造 ToolCall.input
# 时要剔除它们，否则会把 tool 名、call_id 当成参数回传给模型。
_CONTROL_KEYS = ("tool", "tool_name", "call_id")


@dataclass(frozen=True)
class ToolCall:
    """一次模型发起的工具调用。

    ``input`` 是「结构化 dict」而不是 JSON 字符串——这正是原生 tool_use 相对自研
    文本协议的核心差异：参数不再经过一次 ``json.dumps`` 再被模型重新转义解析。
    """

    id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_payload(payload: object, *, fallback_id: str = "") -> ToolCall:
        """从工具循环的调用 ``payload`` dict 构造 ToolCall。

        ``payload`` 形如 ``{"tool": name, "call_id"?: cid, **input}``（见
        ``response_decision._flatten_tool_use_block``）。这里把控制键剥掉，剩下的就
        是结构化 input。``fallback_id`` 在 payload 没带 call_id 时兜底（例如可由
        结果侧的 ``ToolExecutionResult.call_id`` 提供）。
        """
        data = payload if isinstance(payload, dict) else {}
        name = str(data.get("tool") or data.get("tool_name") or "")
        call_id = str(data.get("call_id") or fallback_id or "")
        tool_input = {
            key: value for key, value in data.items() if key not in _CONTROL_KEYS
        }
        return ToolCall(id=call_id, name=name, input=tool_input)


@dataclass(frozen=True)
class ToolResult:
    """一次工具执行回执，通过 ``tool_call_id`` 与发起它的 ``ToolCall`` 配对。"""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class UserTurn:
    """同一运行 turn 期间追加的一条真实用户输入。

    这不是运行时策略或系统提示。它用于保存 会话运行时 steer：用户在模型或工具
    正在工作时补充的输入必须留在原有工具往返历史中的准确时间位置，并在后续采样
    继续作为 ``role=user`` 可见，不能只在下一次请求临时出现一次。
    """

    text: str


# LLM: AssistantTurn 是原生工具历史的单一 assistant 事实；content_blocks 只保存后端白名单清洗后的有序块，不能直接作为用户正文。
# 类用途: 保存一轮模型的可见文字、工具调用，以及下一轮厂商协议要求回放的内部内容块。
@dataclass(frozen=True)
class AssistantTurn:
    """一轮 assistant 输出：可见文本 + 本轮发起的全部工具调用。

    一轮里文本与 tool_calls 至少有一个非空。Anthropic 协议下，文本与 tool_use 同处
    一条 assistant 消息，所以这里把它们绑在同一个容器里，方便适配器一次产出。
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    content_blocks: list[dict[str, Any]] = field(default_factory=list)


__all__ = [
    "AssistantTurn",
    "ToolCall",
    "ToolResult",
    "UserTurn",
]
