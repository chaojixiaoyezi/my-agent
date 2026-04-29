from __future__ import annotations

"""LLM: defines agent runtime DTOs shared across the core mixins.

给人看的解释：
这个文件只放主代理运行结果这类小结构。
它不碰模型、不碰工具、不碰子代理，只给其他模块一个稳定的数据返回格式。
"""

from dataclasses import dataclass


@dataclass
class AgentRunResult:
    """一次 `run()` 调用的结果。"""

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
    memory_resume_context_injected: bool = False
    memory_resume_context_query: str = ""
    memory_resume_context_matches: int = 0
    memory_resume_context_token_estimate: int = 0
    memory_resume_context_error: str = ""
