# LLM: Agent core orchestration module; keep planning, dispatch, tool-loop, and finalization contracts stable.
# 模块用途: 支撑主代理运行循环、计划、工具调用、子代理调度和收尾。

from __future__ import annotations

"""defines agent runtime DTOs shared across the core mixins.

这个文件只放主代理运行结果这类小结构。
它不碰模型、不碰工具、不碰子代理，只给其他模块一个稳定的数据返回格式。
"""

from dataclasses import dataclass


# LLM: AgentRunResult 属于 SimpleAgent 核心运行的类边界；调整时先确认运行循环、工具调用、调度记录和最终响应仍按原契约工作。
# 类用途: 集中保存agentrun结果字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass
class AgentRunResult:

    prompt: str
    response: str
    backend: str
    used_memories: int
    tool_rounds: int = 0
    executed_tools: list[str] | None = None
    memory_route_matches: int = 0
    memory_route_paths: list[str] | None = None
    archive_events: int = 0
    archive_token_estimate: int = 0
    prompt_token_estimate: int = 0
    runtime_injection_token_estimate: int = 0
    recovery_snapshot_id: str = ""
    recovery_snapshot_path: str = ""
    recovery_snapshot_error: str = ""
    recovery_snapshot_token_estimate: int = 0
    compression_snapshot_id: str = ""
    compression_snapshot_path: str = ""
    compression_applied: bool = False
    memory_resume_context_injected: bool = False
    memory_resume_context_query: str = ""
    memory_resume_context_matches: int = 0
    memory_resume_context_token_estimate: int = 0
    memory_resume_context_error: str = ""
    turn_token_estimate: int = 0
    cumulative_token_estimate: int = 0
