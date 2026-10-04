# LLM: 工具事件由唯一执行器在真实 handler 边界发出；只消费宿主请求身份和 canonical 结果，不参与授权或写账。
# 模块用途: 保存一次工具调用是否真正开始的观察状态，重试只发一对事件，重放和前置拒绝不发。
from __future__ import annotations

from dataclasses import dataclass

from ..plugin_events.points import EventPointContext, emit_event
from .runtime_contracts import ToolCall, ToolResult


# LLM: 本实例只属于单次执行器调用，不放共享注册表；actor 只读宿主 context，绝不从 arguments 恢复。
# 类用途: 在 handler 开始时发最小事实，执行器收口时补结果，不携带工具参数或输出。
@dataclass
class ToolEventObservation:
    context: EventPointContext | None
    call: ToolCall
    effect: str
    started: bool = False

    # LLM: 此回调由 registry 在所有前置拒绝之后调用；观察口故障由 emit_event 隔离，不更改业务状态。
    # 函数用途: 标记真实执行并发布开始事实，同一次调用的自动重试不重复计数。
    def start(self) -> None:
        if self.started:
            return
        self.started = True
        emit_event(self.context, "tool_call_started", {
            "call_id": self.call.call_id, "tool": self.call.tool_name,
            "effect": self.effect, "args_hash": self.call.args_hash,
        })

    # LLM: 只有本次真的进入过 handler 才收口，缓存结果中的 handler_executed 不能伪造本次执行；结果只复制标量。
    # 函数用途: 发布工具结果的最小字段，拒绝或幂等重放直接跳过。
    def finish(self, result: ToolResult) -> None:
        if not self.started:
            return
        emit_event(self.context, "tool_call_finished", {
            "call_id": self.call.call_id, "tool": self.call.tool_name, "ok": result.ok,
            "error_code": result.error_code or "", "failure_stage": result.failure_stage or "",
            "duration_ms": result.duration_ms, "handler_executed": result.handler_executed,
        })
